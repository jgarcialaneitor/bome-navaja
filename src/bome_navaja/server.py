"""MCP server for the Boletín Oficial de la Ciudad Autónoma de Melilla (BOME).

Runs over stdio: stdout carries JSON-RPC only; anything meant for a human
(warnings, tracebacks) goes to stderr.

Tool contract: every tool returns a dict with ``ok``. On failure it carries
``ok: false``, ``error`` (a Spanish message for the model) and ``error_code``;
no tool ever raises. The mapping lives in :func:`_herramienta`.

Concurrency model: MCPServer runs synchronous tools in anyio worker threads,
so calls can overlap. The shared :class:`BomeClient` (one connection pool and
its polite-delay bookkeeping) is created lazily and used under
``_client_use_lock``: site requests from tools are serialised, which also
keeps the server polite. The local index uses one SQLite connection per
thread (see :mod:`bome_navaja.index`) and needs no extra lock. The background
sync owns a separate client created by the same factory, but asked for the
slower sync pace (:func:`bome_navaja.sync.sync_settings_from_env`: its
environment overrides are read when the sync is first used).

Nothing touches the network or the index file at import time; the index is
opened only by the index tools, and a sync starts only through
``sincronizar_indice`` (user decision 2026-09-23).
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import functools
import os
import sqlite3
import sys
import threading
import traceback
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .client import BomeClient
from .cve import BASE_URL, InvalidCveError, parse_cve
from .documents import LecturaInvalidaError
from .documents import descargar_pdf as _descargar_pdf
from .documents import leer_articulo as _leer_articulo
from .documents import leer_boletin as _leer_boletin
from .documents import leer_pdf as _leer_pdf
from .index import SumarioIndex
from .models import (
    BomeBlockedError,
    BomeDocumentTooLargeError,
    BomeError,
    BomeHTTPError,
    BomeIndexUnavailableError,
    BomeIndexVersionError,
    BomeNotFoundError,
    BomeParseError,
    BomeStorageError,
)
from .paths import data_dir, index_path, pdf_dir
from .search import RECOMMENDED_POLITE_DELAY, BusquedaInvalidaError, articulos_del_boletin
from .search import buscar_articulos as _buscar_articulos
from .search import buscar_bomes as _buscar_bomes
from .sync import SincronizadorIndice, SyncSettings, sync_settings_from_env

MAX_LISTADO = 500
"""``listar_bomes`` returns at most this many bulletins (newest first)."""

DEFAULT_LISTADO_DIAS = 30
SYNC_SHUTDOWN_WAIT = 10.0

INSTRUCTIONS = """\
Servidor del Boletín Oficial de la Ciudad Autónoma de Melilla (BOME, bomemelilla.es).
Cobertura: boletines desde 2014 (ordinarios BOME-B y extraordinarios BOME-BX). Los sumarios
de artículos existen solo desde finales de 2016; los boletines de 2014-2016 no tienen texto
de artículos ni PDF descargable.

Qué herramienta usar:
- Buscar por sumario de artículo (lo habitual): buscar_en_indice si el índice local está
  sincronizado (mira estado_indice); si no, buscar_articulos (en vivo, más lento).
- Texto completo: leer_articulo (un anuncio), leer_boletin (boletín entero), leer_pdf; todas
  paginan con el cursor 'siguiente'. Descargar el PDF: descargar_pdf.
- Buscar dentro del contenido de las páginas: buscar_bomes con ambito="contenido" (devuelve
  boletines, no artículos).
- Explorar: listar_bomes (calendario), ver_bome (árbol de artículos), resolver_cve.
- Índice local: sincronizar_indice (solo cuando haga falta; cada ejecución indexa como mucho
  250 boletines por defecto en ~15-20 min; el histórico completo necesita varias ejecuciones
  espaciadas).

Semántica de búsqueda del sitio: coincidencia literal por subcadena, sin distinguir tildes ni
mayúsculas, sin sinónimos (busca "cese", no "destitución"; "nombra" encuentra
"nombramiento"). Los términos unidos con Y deben aparecer en el MISMO artículo. El sitio ignora
el O: usa buscar_articulos o buscar_en_indice para combinaciones con O.

Cita siempre el CVE (p. ej. BOME-A-2026-1051) y la URL de cada boletín o artículo que uses.
"""


class ArgumentoInvalidoError(BomeError, ValueError):
    """A tool argument outside any more specific validation (e.g. a date)."""

    error_code = "argumento_invalido"


# --------------------------------------------------------------------------- shared state


def _default_client_factory(
    *, polite_delay: float = RECOMMENDED_POLITE_DELAY, jitter: float = 0.0
) -> BomeClient:
    return BomeClient(polite_delay=polite_delay, jitter=jitter)


_client_factory: Callable[..., BomeClient] = _default_client_factory
"""Builds every client. The shared (interactive) client is built with no
arguments, the sync client with the sync pace as ``polite_delay``/``jitter``
keywords. Tests replace it with a MockTransport one."""

_state_lock = threading.Lock()
_client_use_lock = threading.Lock()
_client: BomeClient | None = None
_index: SumarioIndex | None = None
_sync: SincronizadorIndice | None = None


def _get_client() -> BomeClient:
    global _client
    with _state_lock:
        if _client is None:
            _client = _client_factory()
        return _client


class _LockedClient:
    """Proxy over the shared client that holds ``_client_use_lock`` per call.

    Every site request from any tool is serialised (polite, and safe for the
    client's delay bookkeeping), while a long drill-down still interleaves
    with other tools request by request instead of blocking them for minutes.
    """

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(_get_client(), name)
        if not callable(attribute):
            return attribute

        @functools.wraps(attribute)
        def locked(*args: Any, **kwargs: Any) -> Any:
            with _client_use_lock:
                return getattr(_get_client(), name)(*args, **kwargs)

        return locked


def _cliente() -> BomeClient:
    """The shared client behind its per-call lock."""
    return _LockedClient()  # type: ignore[return-value]


def _index_file() -> tuple[str, bool]:
    path, _reason = index_path()
    return str(path), path.exists()


def _get_index() -> SumarioIndex:
    """Open (or create) the index file on first use."""
    global _index
    with _state_lock:
        if _index is None:
            _index = SumarioIndex(index_path()[0])
        return _index


def _open_index_if_present() -> SumarioIndex | None:
    """The index when already open or when its file exists; never creates it."""
    if _index is not None:
        return _index
    _path, exists = _index_file()
    return _get_index() if exists else None


def _sync_settings() -> SyncSettings:
    return sync_settings_from_env(os.environ)


def _get_sync() -> SincronizadorIndice:
    global _sync
    index = _get_index()
    with _state_lock:
        if _sync is None:
            settings = _sync_settings()
            for warning in settings.warnings:
                print(f"bome-navaja: {warning}", file=sys.stderr)
            _sync = SincronizadorIndice(
                index,
                lambda: _client_factory(polite_delay=settings.polite_delay, jitter=settings.jitter),
                polite_delay=settings.polite_delay,
                jitter=settings.jitter,
                max_boletines=settings.max_boletines,
            )
        return _sync


def close_shared_state() -> None:
    """Stop the sync, close the shared client and the index (shutdown and tests)."""
    global _client, _index, _sync
    with _state_lock:
        sync, index, client = _sync, _index, _client
        _sync = _index = _client = None
    if sync is not None:
        sync.cancelar()
        if not sync.esperar(SYNC_SHUTDOWN_WAIT):
            print("bome-navaja: the index sync did not stop in time", file=sys.stderr)
    if client is not None:
        client.close()
    if index is not None:
        index.close()


atexit.register(close_shared_state)


@contextlib.asynccontextmanager
async def _lifespan(app: MCPServer[Any]) -> AsyncIterator[None]:
    try:
        yield
    finally:
        close_shared_state()


server = MCPServer(
    "bome-navaja",
    version=__version__,
    instructions=INSTRUCTIONS,
    lifespan=_lifespan,
    # The MCP default (INFO) makes httpx log every request to stderr.
    log_level="WARNING",
)


# --------------------------------------------------------------------------- contract


_ERRORS: tuple[tuple[type[BaseException], str, str], ...] = (
    (InvalidCveError, "cve_invalido", "CVE no válido (formato BOME-L-AAAA-N)"),
    (BusquedaInvalidaError, "busqueda_invalida", "Criterios de búsqueda no válidos"),
    (LecturaInvalidaError, "lectura_invalida", "Parámetros de lectura no válidos"),
    (ArgumentoInvalidoError, "argumento_invalido", "Argumento no válido"),
    (BomeIndexVersionError, "indice_version_incompatible", "El índice local tiene una versión incompatible"),
    (BomeIndexUnavailableError, "indice_no_disponible", "El índice local no está disponible"),
    (BomeDocumentTooLargeError, "documento_demasiado_grande", "El documento supera el límite de tamaño"),
    (BomeStorageError, "error_almacenamiento", "Error de almacenamiento local"),
    (
        BomeBlockedError,
        "sitio_bloqueando",
        "bomemelilla.es está rechazando nuestras peticiones (límite de peticiones o "
        "cortafuegos); espera varios minutos antes de reintentar y no repitas la llamada "
        "en bucle",
    ),
    (BomeNotFoundError, "no_encontrado", "No existe en bomemelilla.es"),
    (BomeHTTPError, "error_http", "bomemelilla.es no respondió correctamente"),
    (BomeParseError, "error_formato", "La respuesta del sitio no tiene el formato esperado"),
    (BomeError, "error", "Error del cliente BOME"),
)


def error_result(exc: BaseException) -> dict[str, Any]:
    """The ``ok: false`` payload for a :class:`BomeError`."""
    for kind, code, prefix in _ERRORS:
        if isinstance(exc, kind):
            result: dict[str, Any] = {"ok": False, "error": f"{prefix}: {exc}", "error_code": code}
            if isinstance(exc, BomeHTTPError):
                result["estado_http"] = exc.status
                result["url"] = exc.url
            if isinstance(exc, BomeBlockedError):
                result["reintentar_tras_segundos"] = exc.retry_after
            return result
    raise TypeError(f"not a BomeError: {exc!r}")


def _herramienta(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Wrap a tool body: add ``ok: true`` or map any failure to the error contract."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            data = fn(*args, **kwargs)
        except BomeError as exc:
            return error_result(exc)
        except Exception:  # noqa: BLE001 - a tool must never raise
            print(f"bome-navaja: unexpected error in {fn.__name__}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return {
                "ok": False,
                "error": "Error interno del servidor bome-navaja; el detalle está en su log (stderr).",
                "error_code": "error_interno",
            }
        return {"ok": True, **data}

    return wrapper


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y")


def _fecha(value: str | None, name: str, error: type[BomeError] = BusquedaInvalidaError) -> date | None:
    """``YYYY-MM-DD`` or ``DD/MM/AAAA`` → date (``None`` passes through)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    raise error(f"'{name}' {value!r} no es una fecha; usa AAAA-MM-DD o DD/MM/AAAA")


# --------------------------------------------------------------------------- navigation tools


@server.tool()
@_herramienta
def listar_bomes(desde: str | None = None, hasta: str | None = None) -> dict:
    """Lista los boletines publicados entre dos fechas (calendario del sitio).

    Fechas en AAAA-MM-DD o DD/MM/AAAA, ambas incluidas. Por defecto, los últimos 30 días
    hasta hoy. Devuelve como máximo 500 boletines, del más reciente al más antiguo; si hay
    más, 'truncado' es true y 'total' dice cuántos hay: acota el rango. Cada boletín trae
    cve, number, date, extraordinary (BOME-BX), title y url. Para ver sus artículos usa ver_bome.
    """
    end = _fecha(hasta, "hasta", ArgumentoInvalidoError) or date.today()
    start = _fecha(desde, "desde", ArgumentoInvalidoError) or end - timedelta(days=DEFAULT_LISTADO_DIAS)
    if end < start:
        raise ArgumentoInvalidoError(f"'hasta' ({end}) es anterior a 'desde' ({start})")
    client = _cliente()
    refs = client.calendar(start, end)
    refs.sort(key=lambda r: (r.date or date.min, r.number), reverse=True)
    return {
        "desde": start.isoformat(),
        "hasta": end.isoformat(),
        "total": len(refs),
        "truncado": len(refs) > MAX_LISTADO,
        "bomes": [ref.to_dict() for ref in refs[:MAX_LISTADO]],
    }


@server.tool()
@_herramienta
def ver_bome(cve: str, recuperar_ocultos: bool = False) -> dict:
    """Muestra un boletín (BOME-B-AAAA-N o BOME-BX-AAAA-N) con su árbol de artículos.

    Estructura: sections (departamento) → consejerias → organismos → articles, cada
    artículo con cve, number, sumario, url y pdf_url. Antes de finales de 2016 los
    artículos no tienen sumario. La página del sitio a veces omite artículos; con
    recuperar_ocultos=true se buscan los huecos de numeración (una petición por hueco) y se
    devuelven en 'articulos_ocultos'. Para el texto de un artículo usa leer_articulo.
    """
    client = _cliente()
    bulletin = client.bulletin(cve)
    data: dict[str, Any] = {**bulletin.to_dict(), "total_articulos": len(bulletin.articles)}
    if recuperar_ocultos:
        articles, errors = articulos_del_boletin(client, bulletin)
        data["articulos_ocultos"] = [a.to_dict() for a in articles if not a.listado_en_bome]
        data["errores_ocultos"] = [e.to_dict() for e in errors]
    return data


@server.tool()
@_herramienta
def ver_sumario(cve: str) -> dict:
    """Vista web del sumario de un boletín: lista plana de artículos con su primera página.

    Acepta el CVE del boletín (BOME-B/BX) o del sumario (BOME-S/SX). Aviso: en algunos
    boletines el sumario web es texto libre y se analiza como 0 entradas; en ese caso usa
    ver_bome, que es la fuente fiable de artículos y sumarios.
    """
    client = _cliente()
    sumario = client.sumario(cve)
    data = sumario.to_dict()
    if not sumario.entries:
        data["aviso"] = (
            "Este sumario web no tiene formato estructurado (0 entradas analizadas); "
            "usa ver_bome para obtener los artículos."
        )
    return data


@server.tool()
@_herramienta
def resolver_cve(cve: str) -> dict:
    """Devuelve la URL canónica de cualquier CVE (boletín, artículo, sumario o página).

    Útil para citar o para saber a qué boletín y artículo pertenece un CVE de artículo
    (BOME-A-...) o de página (BOME-P-...). Comprueba que la página exista.
    """
    canonical = str(parse_cve(cve))
    client = _cliente()
    url = client.resolve_cve(canonical)
    return {"cve": canonical, "url": url}


@server.tool()
@_herramienta
def listar_consejerias(departamento: int) -> dict:
    """Consejerías de un departamento, con su id (para filtrar buscar_bomes/buscar_articulos).

    El departamento 1 es CIUDAD AUTÓNOMA DE MELILLA, el habitual.
    """
    client = _cliente()
    items = client.consejerias(departamento)
    return {"departamento": departamento, "total": len(items), "consejerias": [e.to_dict() for e in items]}


@server.tool()
@_herramienta
def listar_organismos(consejeria: int) -> dict:
    """Organismos de una consejería, con su id (filtro 'organismo' de las búsquedas en vivo)."""
    client = _cliente()
    items = client.organismos(consejeria)
    return {"consejeria": consejeria, "total": len(items), "organismos": [e.to_dict() for e in items]}


# --------------------------------------------------------------------------- reading tools

@server.tool()
@_herramienta
def leer_articulo(
    cve: str,
    numero: int | None = None,
    desde_pagina: int = 1,
    desde_caracter: int = 0,
    max_caracteres: int = 20000,
) -> dict:
    """Texto completo de un artículo (anuncio), página a página.

    cve: el CVE del artículo (BOME-A-2026-1051) o el del boletín (BOME-B-...) junto con
    numero (el número del artículo). Los artículos de 2014-2016 no tienen texto en el sitio:
    fuente="ninguna" y un aviso. Paginación: devuelve páginas enteras hasta max_caracteres
    (1000-100000, por defecto 20000), siempre al menos una; una página
    más larga que max_caracteres se corta ahí (cortada=true). Si 'siguiente' no es null, vuelve
    a llamar con desde_pagina y desde_caracter de 'siguiente' para continuar; null significa
    que no queda más.
    """
    client = _cliente()
    return _leer_articulo(
        client,
        cve,
        numero,
        desde_pagina=desde_pagina,
        desde_caracter=desde_caracter,
        max_caracteres=max_caracteres,
    ).to_dict()


@server.tool()
@_herramienta
def leer_boletin(
    cve: str,
    desde_pagina: int = 1,
    desde_caracter: int = 0,
    max_caracteres: int = 20000,
) -> dict:
    """Texto completo de un boletín entero (su PDF) con los metadatos del boletín.

    Un boletín ordinario tiene ~35 páginas; lee por tramos. El PDF se descarga una vez y se
    guarda en caché. Los boletines de 2014-2016 no tienen PDF (fuente="ninguna"): lee sus
    artículos con leer_articulo. Paginación: devuelve páginas enteras hasta max_caracteres
    (1000-100000, por defecto 20000), siempre al menos una; una página
    más larga que max_caracteres se corta ahí (cortada=true). Si 'siguiente' no es null, vuelve
    a llamar con desde_pagina y desde_caracter de 'siguiente'; null significa que no queda más.
    """
    client = _cliente()
    return _leer_boletin(
        client, cve, desde_pagina=desde_pagina, desde_caracter=desde_caracter,
        max_caracteres=max_caracteres,
    ).to_dict()


@server.tool()
@_herramienta
def leer_pdf(
    cve: str,
    desde_pagina: int = 1,
    desde_caracter: int = 0,
    max_caracteres: int = 20000,
) -> dict:
    """Texto del PDF de cualquier CVE: boletín, sumario (BOME-S), artículo (BOME-A) o página
    (BOME-P).

    Las páginas sin texto extraíble (escaneadas) salen con sin_texto=true y un aviso.
    Paginación: devuelve páginas enteras hasta max_caracteres (1000-100000, por defecto
    20000), siempre al menos una; una página más larga que max_caracteres se corta ahí
    (cortada=true). Si 'siguiente' no es null, vuelve a llamar con desde_pagina y
    desde_caracter de 'siguiente'; null significa que no queda más.
    """
    client = _cliente()
    return _leer_pdf(
        client, cve, desde_pagina=desde_pagina, desde_caracter=desde_caracter,
        max_caracteres=max_caracteres,
    ).to_dict()


@server.tool()
@_herramienta
def descargar_pdf(cve: str, refrescar: bool = False) -> dict:
    """Descarga el PDF de cualquier CVE a la caché local y devuelve su ruta, tamaño, sha256 y
    número de páginas.

    El nombre del fichero es siempre el CVE canónico; el directorio lo fija
    BOME_NAVAJA_PDF_DIR (ver estado_servidor). Reutiliza la copia en caché salvo
    refrescar=true. Límite: 100 MB. Los PDF de 2014-2016 no existen en el sitio (404).
    """
    client = _cliente()
    return _descargar_pdf(client, cve, refrescar=refrescar).to_dict()


# --------------------------------------------------------------------------- live search tools


@server.tool()
@_herramienta
def buscar_bomes(
    texto: str | None = None,
    ambito: str = "sumario",
    terminos: list[dict[str, Any]] | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    departamento: int | None = None,
    consejeria: int | None = None,
    organismo: int | None = None,
    numero_bome: int | None = None,
    numero_articulo: int | None = None,
    numero_pagina: int | None = None,
    anio: int | None = None,
    pagina: int = 1,
) -> dict:
    """Búsqueda en vivo en el buscador avanzado del sitio: devuelve BOLETINES (10 por página).

    texto: frase literal (subcadena, sin tildes ni mayúsculas). ambito: "sumario" (sumarios
    de artículos, desde finales de 2016) o "contenido" (texto de las páginas, única forma de
    buscar en 2014-2016). terminos: más frases [{texto, modo: "contiene"|"no_contiene",
    ambito?}], todas con Y y en el MISMO artículo; el sitio ignora el O, así que se rechaza
    (usa buscar_articulos). Fechas AAAA-MM-DD o DD/MM/AAAA (por defecto 2014-01-01..hoy).
    Filtros por id (listar_consejerias/listar_organismos) y números exactos. Para ver qué
    artículos coinciden usa buscar_articulos o buscar_en_indice.
    """
    return _buscar_bomes(
        _cliente(),
        texto=texto,
        ambito=ambito,  # type: ignore[arg-type]
        terminos=terminos,
        desde=_fecha(desde, "desde"),
        hasta=_fecha(hasta, "hasta"),
        departamento=departamento,
        consejeria=consejeria,
        organismo=organismo,
        numero_bome=numero_bome,
        numero_articulo=numero_articulo,
        numero_pagina=numero_pagina,
        anio=anio,
        pagina=pagina,
    ).to_dict()


@server.tool()
@_herramienta
def buscar_articulos(
    texto: str | None = None,
    terminos: list[dict[str, Any]] | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    departamento: int | None = None,
    consejeria: int | None = None,
    organismo: int | None = None,
    numero_bome: int | None = None,
    numero_articulo: int | None = None,
    anio: int | None = None,
    max_bomes: int = 20,
    max_articulos: int = 100,
) -> dict:
    """Búsqueda en vivo de ARTÍCULOS por su sumario: busca boletines en el sitio, abre cada uno
    y devuelve los artículos cuyo sumario coincide.

    Misma semántica que el sitio (subcadena sin tildes ni mayúsculas; Y dentro del mismo
    artículo) y además admite O: terminos=[{texto, operador: "y"|"o", modo:
    "contiene"|"no_contiene"}]. Lenta (una petición por boletín, ~0,5 s): acotada por max_bomes
    (por defecto 20, máx. 100) y max_articulos (por defecto 100, máx. 500); mira 'truncado' y
    'total_bomes'. Si el índice local está sincronizado, buscar_en_indice es instantánea.
    Recupera artículos que la página del boletín omite. Los boletines sin coincidencia local
    salen en 'bomes_sin_coincidencia'.
    """
    return _buscar_articulos(
        _cliente(),
        texto=texto,
        terminos=terminos,
        desde=_fecha(desde, "desde"),
        hasta=_fecha(hasta, "hasta"),
        departamento=departamento,
        consejeria=consejeria,
        organismo=organismo,
        numero_bome=numero_bome,
        numero_articulo=numero_articulo,
        anio=anio,
        max_bomes=max_bomes,
        max_articulos=max_articulos,
    ).to_dict()


# --------------------------------------------------------------------------- local index tools


def _index_aviso(cobertura: dict[str, Any], sync_state: str | None) -> str | None:
    notes: list[str] = []
    if not cobertura.get("boletines_indexados"):
        notes.append(
            "El índice local está vacío: llama a sincronizar_indice (tarda ~20-25 min la primera "
            "vez, en segundo plano) y, mientras tanto, usa buscar_articulos (en vivo)."
        )
    if cobertura.get("sincronizacion_en_curso") or sync_state == "en_curso":
        notes.append("Hay una sincronización en curso: los resultados son parciales.")
    elif cobertura.get("pendientes"):
        notes.append(
            f"{cobertura['pendientes']} boletines del calendario están pendientes o con error; "
            "sincronizar_indice los reintenta."
        )
    return " ".join(notes) or None


def _sync_state() -> dict[str, Any] | None:
    return _sync.estado().to_dict() if _sync is not None else None


@server.tool()
@_herramienta
def buscar_en_indice(
    texto: str | None = None,
    terminos: list[dict[str, Any]] | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    extraordinario: bool | None = None,
    consejeria: str | None = None,
    orden: str = "fecha",
    limite: int = 20,
    desplazamiento: int = 0,
    coincidencia: str = "fragmento",
) -> dict:
    """Búsqueda instantánea de artículos en el índice local de sumarios (sin tocar el sitio).

    Requiere haber llamado antes a sincronizar_indice; si el índice está vacío devuelve 0
    resultados y un aviso (usa buscar_articulos mientras tanto). Mira 'cobertura' (rango
    indexado, pendientes, sincronización en curso) antes de afirmar que algo no existe.
    coincidencia: "fragmento" (subcadena, como el sitio: "cese" encuentra "ceses" y
    "procese") o "palabra" (cada frase debe empezar una palabra: "cese" → cese, ceses, no
    procese). terminos=[{texto, operador: "y"|"o", modo: "contiene"|"no_contiene"}]; Y dentro
    del mismo artículo. consejeria: parte del nombre (sin tildes). orden: "fecha" o
    "relevancia". Pagina con limite (máx. 200) y desplazamiento ('siguiente' da el próximo).
    Solo cubre sumarios (desde finales de 2016).
    """
    index = _open_index_if_present()
    if index is None:
        cobertura = {
            "boletines_indexados": 0,
            "fecha_min": None,
            "fecha_max": None,
            "pendientes": None,
            "ultima_sincronizacion": None,
            "sincronizacion_en_curso": False,
        }
        return {
            "articulos": [],
            "total": 0,
            "limite": limite,
            "desplazamiento": desplazamiento,
            "siguiente": None,
            "cobertura": cobertura,
            "aviso": _index_aviso(cobertura, None),
        }
    result = index.buscar(
        texto,
        terminos,
        desde=_fecha(desde, "desde"),
        hasta=_fecha(hasta, "hasta"),
        extraordinario=extraordinario,
        consejeria=consejeria,
        orden=orden,  # type: ignore[arg-type]
        limite=limite,
        desplazamiento=desplazamiento,
        coincidencia=coincidencia,  # type: ignore[arg-type]
    )
    data = result.to_dict()
    state = _sync_state()
    aviso = _index_aviso(result.cobertura, state["estado"] if state else None)
    if aviso:
        data["aviso"] = aviso
    return data


@server.tool()
@_herramienta
def estado_indice() -> dict:
    """Estado del índice local de sumarios y de su sincronización (no toca el sitio).

    Devuelve boletines indexados / sin sumarios / con error, artículos, rango de fechas,
    pendientes frente al calendario, última sincronización y el progreso de la actual
    (hechos, total_planificado, eta_segundos). Úsalo para seguir una sincronización lanzada con sincronizar_indice.
    Estados de la sincronización: en_curso, completado, cancelado, fallido y bloqueado (el sitio
    rechaza las peticiones por límite de ritmo o cortafuegos: lo indexado se conserva; espera,
    horas si es un bloqueo del cortafuegos, antes de volver a sincronizar; ver 'mensaje').
    """
    path, exists = _index_file()
    index = _open_index_if_present()
    if index is None:
        return {
            "existe": False,
            "ruta": path,
            "indice": None,
            "sincronizacion": None,
            "aviso": "El índice aún no existe; créalo con sincronizar_indice.",
        }
    return {"existe": True, "ruta": path, "indice": index.estado().to_dict(), "sincronizacion": _sync_state()}


@server.tool()
@_herramienta
def sincronizar_indice(
    desde: str | None = None,
    hasta: str | None = None,
    reindexar_recientes_dias: int = 7,
    reintentar_errores: bool = True,
    max_boletines: int | None = None,
) -> dict:
    """Arranca en segundo plano la sincronización del índice local de sumarios y vuelve al
    instante.

    Recorre el calendario (por defecto 2014-01-01..hoy) del más reciente al más antiguo:
    indexa los boletines que falten, re-indexa los de los últimos reindexar_recientes_dias
    días y, si reintentar_errores, los que fallaron. Para no saturar el sitio va despacio
    (~2-3 s entre peticiones) y cada ejecución indexa como mucho max_boletines boletines
    (por defecto 250, los más recientes; ~15-20 minutos). El histórico completo desde 2014
    (~1900 boletines) necesita varias ejecuciones: si el estado final trae
    pendientes_tras_limite > 0, vuelve a llamarla más tarde (espaciar las ejecuciones es más
    amable con el sitio). Es reanudable: si se corta, la siguiente llamada continúa donde
    quedó. Sigue el progreso con estado_indice; mientras tanto
    buscar_en_indice da resultados parciales. Si ya hay una en curso (en este u otro proceso)
    devuelve su estado sin arrancar otra. Solo sincroniza cuando se le pide. Si el sitio rechaza
    las peticiones (403/429/503 o conexiones cortadas) espera y reintenta; si sigue rechazándolas
    termina en estado "bloqueado": no la relances enseguida, espera (horas si es el cortafuegos).
    """
    sync = _get_sync()
    return sync.iniciar(
        desde=_fecha(desde, "desde", ArgumentoInvalidoError),
        hasta=_fecha(hasta, "hasta", ArgumentoInvalidoError),
        reindexar_recientes_dias=reindexar_recientes_dias,
        reintentar_errores=reintentar_errores,
        max_boletines=max_boletines,
    ).to_dict()


@server.tool()
@_herramienta
def cancelar_sincronizacion() -> dict:
    """Pide parar la sincronización en curso; termina tras el boletín que esté procesando (o al
    instante si está esperando porque el sitio la había bloqueado).

    Lo ya indexado se conserva y una nueva sincronizar_indice continúa desde ahí.
    """
    if _sync is None:
        return {"estado": "inactivo", "mensaje": "No hay ninguna sincronización en este proceso."}
    return _sync.cancelar().to_dict()


# --------------------------------------------------------------------------- server state


def _sqlite_capabilities() -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    try:
        capabilities: dict[str, Any] = {"version": sqlite3.sqlite_version}
        for name, ddl in (
            ("fts5", "CREATE VIRTUAL TABLE t1 USING fts5(x)"),
            ("trigram", "CREATE VIRTUAL TABLE t2 USING fts5(x, tokenize='trigram')"),
        ):
            try:
                conn.execute(ddl)
                capabilities[name] = True
            except sqlite3.Error:
                capabilities[name] = False
        return capabilities
    finally:
        conn.close()


def _ruta(resolver: Callable[[], tuple[Any, str]]) -> dict[str, Any]:
    try:
        path, reason = resolver()
    except BomeError as exc:
        return {"ruta": None, "motivo": None, "error": str(exc)}
    return {"ruta": str(path), "motivo": reason}


@server.tool()
@_herramienta
def estado_servidor() -> dict:
    """Configuración y estado del servidor, sin tocar la red ni crear el índice.

    Versión, pid, rutas de datos, PDFs e índice (con el motivo de cada una: variable de
    entorno, XDG, LOCALAPPDATA...; las rutas relativas en BOME_NAVAJA_* se resuelven contra
    el directorio de trabajo), si existe el fichero del índice y su estado si ya está
    abierto, versión de SQLite con FTS5/trigram, pausa de cortesía de las herramientas y de
    la sincronización (pausa, variación aleatoria y máximo de boletines por ejecución, con
    BOME_NAVAJA_SYNC_DELAY y BOME_NAVAJA_SYNC_MAX_BOLETINES aplicadas) y URL base.
    """
    try:
        exists = index_path()[0].exists()
    except BomeError:
        exists = False
    index = _index
    settings = _sync_settings()
    sync_pace: dict[str, Any] = {
        "pausa_segundos": settings.polite_delay,
        "variacion_segundos": settings.jitter,
        "max_boletines_por_ejecucion": settings.max_boletines,
    }
    if settings.warnings:
        sync_pace["avisos"] = list(settings.warnings)
    return {
        "version": __version__,
        "pid": os.getpid(),
        "rutas": {"datos": _ruta(data_dir), "pdfs": _ruta(pdf_dir), "indice": _ruta(index_path)},
        "indice_existe": exists,
        "indice": index.estado().to_dict() if index is not None else None,
        "sincronizacion": _sync_state(),
        "sqlite": _sqlite_capabilities(),
        "cortesia_segundos": RECOMMENDED_POLITE_DELAY,
        "cortesia_sincronizacion": sync_pace,
        "url_base": BASE_URL,
    }


def main(argv: list[str] | None = None) -> None:
    """Run the bome-navaja MCP server over stdio."""
    parser = argparse.ArgumentParser(
        prog="bome-navaja-mcp",
        description="MCP server for the Boletín Oficial de la Ciudad Autónoma de Melilla (BOME)",
    )
    parser.parse_args(argv)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
