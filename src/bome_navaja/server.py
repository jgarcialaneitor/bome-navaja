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
slower sync pace.

Settings: every site-facing tunable (sync pace, jitter and cap, interactive
pace, guard budget, window and cooldown, pause after an error, timeout) comes
from :mod:`bome_navaja.ajustes`, read from the environment once per process
(:func:`_ajustes`, when a client, guard or sync is first needed; its warnings
go to stderr then). Changing a variable needs a restart; ``estado_servidor``
re-reads them only to report them.

Two syncs, one per origin, each created lazily once per process with its own
lease owner: :class:`~bome_navaja.sync.SincronizadorIndice` (bomemelilla.es)
and :class:`~bome_navaja.sync_antiguo.SincronizadorPortalAntiguo` (the old
portal, built with the old-portal guard and a portal client from
``_portal_factory`` at the same sync pace and cap). Both write the same index,
so they share its single lease: only one of them runs at a time.
``sincronizar_indice(origen=...)`` picks one; ``cancelar_sincronizacion`` and
the ``sincronizacion`` field of the state tools follow whichever is running
(else the last one started).

Site guard: one :class:`~bome_navaja.guard.GuardiaSitio` per process,
created lazily and persisted in the data folder (``estado_sitio.json``, so
restarts and other server processes share its error budget and cooldown; a
memory-only guard when the data folder is unavailable), is wired into both
the shared client and the sync client. While it refuses, tools answer
``sitio_bloqueando`` or ``pausa_preventiva`` with ``reintentar_tras_segundos``
without touching the network.

Old portal (melilla.es, bulletins 1985-2021-03-12): one lazy
:class:`~bome_navaja.antiguo.PortalAntiguo` per process, used under its own
``_portal_use_lock`` (the two sites are independent), with its own guard
persisted as ``estado_sitio_melilla.json`` (memory-only without a data
folder). Its errors never count against bomemelilla.es's budget, and the
model-facing messages name the site that failed. robots.txt of melilla.es
disallows the ficha and ``mandar.php``. PDFs are requested only on demand, one
tool call at a time. Fichas are also crawled in bulk, but only by the
old-portal sync, started by hand, slow and capped (user decision 2026-09-24).

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
from .ajustes import Ajustes, ajustes_desde_entorno
from .antiguo import (
    FICHERO_CATALOGO,
    PORTAL_URL,
    BoletinAmbiguoError,
    BoletinAntiguo,
    PortalAntiguo,
    UrlPdfInvalidaError,
    estado_catalogo,
    texto_busqueda_valido,
)
from .antiguo import DEFAULT_JITTER as PORTAL_JITTER
from .antiguo import DEFAULT_POLITE_DELAY as PORTAL_POLITE_DELAY
from .antiguo import SITIO as SITIO_ANTIGUO
from .client import BomeClient
from .cve import BASE_URL, Cve, CveKind, InvalidCveError, parse_cve, pdf_url
from .documents import LecturaInvalidaError
from .documents import localizar_articulo as _localizar_articulo
from .documents import descargar_pdf as _descargar_pdf
from .documents import descargar_pdf_antiguo as _descargar_pdf_antiguo
from .documents import leer_articulo as _leer_articulo
from .documents import leer_boletin as _leer_boletin
from .documents import leer_pdf as _leer_pdf
from .documents import leer_pdf_antiguo as _leer_pdf_antiguo
from .guard import FICHERO_ESTADO, FICHERO_ESTADO_MELILLA, SITIO_POR_DEFECTO, GuardiaSitio
from .index import ORIGEN_ANTIGUO, ORIGEN_BOME, ORIGENES, SumarioIndex
from .models import (
    BomeBlockedError,
    BomeDocumentTooLargeError,
    BomeError,
    BomeHTTPError,
    BomeIndexUnavailableError,
    BomeIndexVersionError,
    BomeNotFoundError,
    BomeParseError,
    BomePausaPreventivaError,
    BomeStorageError,
)
from .paths import data_dir, index_path, pdf_dir
from .search import BusquedaInvalidaError, articulos_del_boletin
from .search import buscar_articulos as _buscar_articulos
from .search import buscar_bomes as _buscar_bomes
from .sync import SincronizadorBase, SincronizadorIndice, sync_settings_from_env
from .sync_antiguo import SincronizadorPortalAntiguo

MAX_LISTADO = 500
"""``listar_bomes`` returns at most this many bulletins (newest first)."""

DEFAULT_LISTADO_DIAS = 30
SYNC_SHUTDOWN_WAIT = 10.0

INICIO_BOMEMELILLA = date(2014, 1, 1)
"""bomemelilla.es has nothing before this date: earlier ranges come from the old portal."""

FIN_PORTAL_ANTIGUO = date(2021, 3, 12)
"""Last bulletin of the old portal's frozen catalog."""

MAX_LIMITE_ANTIGUO = 500
"""Most articles ``buscar_bome_antiguo`` returns in one call."""

INSTRUCTIONS = """\
Servidor del Boletín Oficial de la Ciudad Autónoma de Melilla (BOME). Dos orígenes:
- bomemelilla.es (el sitio actual): boletines desde 2014 (ordinarios BOME-B y extraordinarios
  BOME-BX). Los sumarios de artículos existen solo desde finales de 2016; los boletines de
  2014-2016 no tienen texto de artículos ni PDF descargable, y antes de 2018 le faltan
  boletines.
- melilla.es (el portal antiguo, congelado): boletines de 1985 al 12-03-2021, con sumarios de
  artículos desde ~1991 y PDF por página. Herramientas: buscar_bome_antiguo (búsqueda de
  artículos), ver_bome_antiguo (un boletín con sus artículos) y leer_pdf / descargar_pdf con
  'url' (los PDF que dan esas dos). Cada resultado lleva 'origen'.

Qué portal usar: para boletines anteriores a 2018, o cuando bomemelilla.es no tiene un
boletín (ver_bome responde no_encontrado), usa las herramientas del portal antiguo;
listar_bomes ya junta los dos catálogos antes de 2021-03-13. Los identificadores anteriores a
2014 tienen forma de CVE pero no son CVE de bomemelilla.es y algunos se repiten: usa el dboid.

Qué herramienta usar:
- Buscar por sumario de artículo (lo habitual): buscar_en_indice si el índice local está
  sincronizado (mira estado_indice); si no, buscar_articulos (en vivo, más lento).
- Antes de 2018: si el índice ya tiene el portal antiguo (estado_indice, por_origen
  "melilla.es"; cubre 1991-2017 tras sincronizar_indice(origen="melilla.es")), usa
  buscar_en_indice para búsquedas con Y / O / no contiene o que abarquen varios años; si no,
  buscar_bome_antiguo en vivo (literal, una sola frase).
- Texto completo: leer_articulo (un anuncio), leer_boletin (boletín entero), leer_pdf; todas
  paginan con el cursor 'siguiente'. Descargar el PDF: descargar_pdf.
- Buscar dentro del contenido de las páginas: buscar_bomes con ambito="contenido" (devuelve
  boletines, no artículos).
- Explorar: listar_bomes (calendario), ver_bome (árbol de artículos), resolver_cve.
- Índice local: sincronizar_indice (solo cuando haga falta; cada ejecución indexa como mucho
  250 boletines por defecto en ~15-20 min; el histórico completo necesita varias ejecuciones
  espaciadas). Por defecto cubre 2018-01-01..hoy: antes de 2018 bomemelilla.es está incompleto
  e inestable (faltan boletines y hay páginas rotas), y los boletines anteriores se consultan
  mejor en el portal antiguo de melilla.es. Un 'desde' anterior es posible pero no
  recomendable. Los boletines "rotos" (su página da error interno del sitio) se saltan;
  reintentar_rotos solo para comprobar si el sitio los arregló. Con origen="melilla.es"
  indexa los sumarios del portal antiguo (por defecto 1991-01-01..2017-12-31, también 250
  boletines por ejecución; ~2.000-2.500 boletines, varias ejecuciones). Solo corre una
  sincronización a la vez por índice, sea del origen que sea.

Cortafuegos del sitio: bomemelilla.es bloquea la IP tras unas 5 respuestas de error, y
bome-navaja se protege sola (por defecto, como mucho 3 errores cada 10 minutos entre todas las
herramientas y la sincronización). Si una herramienta responde pausa_preventiva (pausa propia,
no un bloqueo) o sitio_bloqueando (el sitio nos bloqueó: no se le pide nada durante ~75 min),
espera reintentar_tras_segundos antes de reintentar; no repitas la llamada en bucle ni cambies
de herramienta para esquivarlo. estado_servidor muestra la guardia en guardia_sitio. El
portal antiguo (melilla.es) tiene su propia guardia, igual pero aparte (guardia_portal_antiguo).

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


_ajustes_lock = threading.Lock()
_ajustes_proceso: Ajustes | None = None


def _ajustes() -> Ajustes:
    """The site-facing settings of this process (:mod:`bome_navaja.ajustes`).

    Read from the environment on first use and kept until
    :func:`close_shared_state`, so changing a variable needs a restart. The
    first read prints its invalid-value and risk warnings to stderr. It takes
    only its own lock, so it may run while ``_state_lock`` is held.
    """
    global _ajustes_proceso
    with _ajustes_lock:
        if _ajustes_proceso is None:
            _ajustes_proceso = ajustes_desde_entorno(os.environ)
            for mensaje in _ajustes_proceso.mensajes:
                print(f"bome-navaja: {mensaje}", file=sys.stderr)
        return _ajustes_proceso


def _default_client_factory(
    *,
    polite_delay: float | None = None,
    jitter: float = 0.0,
    guard: GuardiaSitio | None = None,
) -> BomeClient:
    """A bomemelilla.es client with the configured timeout; ``polite_delay``
    defaults to the configured interactive pace (``BOME_NAVAJA_QUERY_DELAY``)."""
    ajustes = _ajustes()
    return BomeClient(
        polite_delay=ajustes.pausa_consultas_segundos if polite_delay is None else polite_delay,
        jitter=jitter,
        guard=guard,
        timeout=ajustes.tiempo_espera_segundos,
    )


_client_factory: Callable[..., BomeClient] = _default_client_factory
"""Builds every client, always with the process's site guard as ``guard=``.
The shared (interactive) client gets no other argument, the sync client the
sync pace as ``polite_delay``/``jitter`` keywords. Tests replace it with a
MockTransport one."""

_state_lock = threading.Lock()
_client_use_lock = threading.Lock()
_client: BomeClient | None = None
_index: SumarioIndex | None = None
_sync: SincronizadorIndice | None = None
_sync_antiguo: SincronizadorPortalAntiguo | None = None
_sync_reciente: SincronizadorBase | None = None
"""The sync of this process started last (either origin)."""
_guard: GuardiaSitio | None = None


def _default_portal_factory(
    *,
    guard: GuardiaSitio,
    polite_delay: float = PORTAL_POLITE_DELAY,
    jitter: float = PORTAL_JITTER,
) -> PortalAntiguo:
    """An old-portal client with the configured timeout; the interactive pace
    stays the portal's own (1 s + up to 0.5 s)."""
    return PortalAntiguo(
        guard=guard, polite_delay=polite_delay, jitter=jitter, timeout=_ajustes().tiempo_espera_segundos
    )


_portal_factory: Callable[..., PortalAntiguo] = _default_portal_factory
"""Builds every old-portal client with the process's old-portal guard as
``guard=``. The shared (interactive) portal gets no other argument, the
old-portal sync's portal the sync pace as ``polite_delay``/``jitter``
keywords. Tests replace it with a MockTransport one."""

_portal_use_lock = threading.Lock()
_portal: PortalAntiguo | None = None
_portal_guard: GuardiaSitio | None = None


def _guard_limits(ajustes: Ajustes) -> dict[str, Any]:
    """The configured error budget, window and cooldown, as guard keywords."""
    return {
        "max_errores": ajustes.guardia_max_errores,
        "ventana_segundos": ajustes.guardia_ventana_segundos,
        "enfriamiento_segundos": ajustes.guardia_enfriamiento_segundos,
    }


def _get_guard() -> GuardiaSitio:
    """The process's site guard, persisted in the data folder when there is one."""
    global _guard
    ajustes = _ajustes()
    with _state_lock:
        if _guard is None:
            try:
                path = data_dir()[0] / FICHERO_ESTADO
            except BomeError as exc:
                print(
                    f"bome-navaja: no data folder for the site guard ({exc}); "
                    "its state is kept in memory for this process",
                    file=sys.stderr,
                )
                path = None
            _guard = GuardiaSitio(path, **_guard_limits(ajustes))
        return _guard


def _get_portal_guard() -> GuardiaSitio:
    """The old portal's guard (melilla.es), persisted apart from bomemelilla.es's."""
    global _portal_guard
    ajustes = _ajustes()
    with _state_lock:
        if _portal_guard is None:
            try:
                path = data_dir()[0] / FICHERO_ESTADO_MELILLA
            except BomeError as exc:
                print(
                    f"bome-navaja: no data folder for the old portal's site guard ({exc}); "
                    "its state is kept in memory for this process",
                    file=sys.stderr,
                )
                path = None
            _portal_guard = GuardiaSitio(path, sitio=SITIO_ANTIGUO, **_guard_limits(ajustes))
        return _portal_guard


def _get_portal() -> PortalAntiguo:
    global _portal
    guard = _get_portal_guard()
    with _state_lock:
        if _portal is None:
            _portal = _portal_factory(guard=guard)
        return _portal


class _LockedPortal:
    """Proxy over the shared old-portal client: one request at a time, errors tagged.

    Every :class:`BomeError` it lets through carries ``sitio = "melilla.es"``
    so :func:`error_result` names the right site.
    """

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(_get_portal(), name)
        if not callable(attribute):
            return attribute

        @functools.wraps(attribute)
        def locked(*args: Any, **kwargs: Any) -> Any:
            try:
                with _portal_use_lock:
                    return getattr(_get_portal(), name)(*args, **kwargs)
            except BomeError as exc:
                exc.sitio = SITIO_ANTIGUO  # type: ignore[attr-defined]
                raise

        return locked


def _portal_antiguo() -> PortalAntiguo:
    """The shared old-portal client behind its per-call lock."""
    return _LockedPortal()  # type: ignore[return-value]


def _get_client() -> BomeClient:
    global _client
    guard = _get_guard()
    with _state_lock:
        if _client is None:
            _client = _client_factory(guard=guard)
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


def _boletin_indexado(cve: Cve) -> Cve | None:
    """Bulletin of an article CVE according to the local index, if present and it knows it.

    The shortcut the readers use for ``BOME-AX`` (the site's resolver sends those to
    ordinary bulletins). Any index problem just means "unknown": it is optional.
    """
    try:
        index = _open_index_if_present()
        found = index.boletin_de_articulo(str(cve)) if index is not None else None
        return parse_cve(found) if found is not None else None
    except (BomeError, sqlite3.Error, OSError):
        return None


def _get_sync() -> SincronizadorIndice:
    global _sync
    index = _get_index()
    guard = _get_guard()
    settings = _ajustes()
    with _state_lock:
        if _sync is None:
            delay = settings.pausa_sincronizacion_segundos
            jitter = settings.variacion_sincronizacion_segundos

            def sync_client(*, guard: GuardiaSitio | None = None) -> BomeClient:
                return _client_factory(polite_delay=delay, jitter=jitter, guard=guard)

            _sync = SincronizadorIndice(
                index,
                sync_client,
                polite_delay=delay,
                jitter=jitter,
                max_boletines=settings.max_boletines_por_ejecucion,
                guard=guard,
                pausa_tras_error=settings.pausa_tras_error_segundos,
            )
        return _sync


def _get_sync_antiguo() -> SincronizadorPortalAntiguo:
    """The old-portal sync: the old-portal guard, the sync pace, cap and pause
    after an error, its own lease owner."""
    global _sync_antiguo
    index = _get_index()
    guard = _get_portal_guard()
    settings = _ajustes()
    with _state_lock:
        if _sync_antiguo is None:
            delay = settings.pausa_sincronizacion_segundos
            jitter = settings.variacion_sincronizacion_segundos

            def sync_portal(*, guard: GuardiaSitio) -> PortalAntiguo:
                return _portal_factory(guard=guard, polite_delay=delay, jitter=jitter)

            _sync_antiguo = SincronizadorPortalAntiguo(
                index,
                sync_portal,
                polite_delay=delay,
                jitter=jitter,
                max_boletines=settings.max_boletines_por_ejecucion,
                guard=guard,
                pausa_tras_error=settings.pausa_tras_error_segundos,
            )
        return _sync_antiguo


def _sync_actual() -> SincronizadorBase | None:
    """The sync of this process that is running, else the one started last, else any."""
    syncs = [job for job in (_sync, _sync_antiguo) if job is not None]
    for job in syncs:
        if job.estado().estado == "en_curso":
            return job
    if _sync_reciente is not None and _sync_reciente in syncs:
        return _sync_reciente
    return syncs[0] if syncs else None


def close_shared_state() -> None:
    """Stop both syncs, close the shared clients and the index (shutdown and tests).

    The settings are forgotten too: the next use reads the environment again.
    """
    global _client, _index, _sync, _sync_antiguo, _sync_reciente, _guard, _portal, _portal_guard
    global _ajustes_proceso
    with _state_lock:
        syncs = (_sync, _sync_antiguo)
        index, client, portal = _index, _client, _portal
        _sync = _sync_antiguo = _sync_reciente = None
        _index = _client = _portal = None
        _guard = _portal_guard = None
    with _ajustes_lock:
        _ajustes_proceso = None
    for sync in syncs:
        if sync is not None:
            sync.cancelar()
    for sync in syncs:
        if sync is not None and not sync.esperar(SYNC_SHUTDOWN_WAIT):
            print(f"bome-navaja: the {sync.origen} index sync did not stop in time", file=sys.stderr)
    if client is not None:
        client.close()
    if portal is not None:
        portal.close()
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
    (UrlPdfInvalidaError, "url_pdf_invalida", "URL de PDF no válida"),
    (BoletinAmbiguoError, "boletin_ambiguo", "Identificador ambiguo en el portal antiguo (melilla.es)"),
    (BomeIndexVersionError, "indice_version_incompatible", "El índice local tiene una versión incompatible"),
    (BomeIndexUnavailableError, "indice_no_disponible", "El índice local no está disponible"),
    (BomeDocumentTooLargeError, "documento_demasiado_grande", "El documento supera el límite de tamaño"),
    (BomeStorageError, "error_almacenamiento", "Error de almacenamiento local"),
    (
        BomePausaPreventivaError,
        "pausa_preventiva",
        "Pausa de seguridad propia de bome-navaja para no activar el cortafuegos de "
        "{sitio} (no es un bloqueo del sitio); espera los segundos de "
        "reintentar_tras_segundos antes de reintentar y no repitas la llamada en bucle",
    ),
    (
        BomeBlockedError,
        "sitio_bloqueando",
        "{sitio} está rechazando nuestras peticiones (límite de peticiones o "
        "cortafuegos) y bome-navaja no le pedirá nada durante reintentar_tras_segundos; "
        "espera ese tiempo antes de reintentar y no repitas la llamada en bucle",
    ),
    (BomeNotFoundError, "no_encontrado", "No existe en {sitio}"),
    (BomeHTTPError, "error_http", "{sitio} no respondió correctamente"),
    (BomeParseError, "error_formato", "La respuesta del sitio no tiene el formato esperado"),
    (BomeError, "error", "Error del cliente BOME"),
)


def error_result(exc: BaseException) -> dict[str, Any]:
    """The ``ok: false`` payload for a :class:`BomeError`.

    The prefix names the site that failed: ``exc.sitio`` when the old-portal
    proxy tagged it, else bomemelilla.es.
    """
    sitio = getattr(exc, "sitio", SITIO_POR_DEFECTO)
    for kind, code, prefix in _ERRORS:
        if isinstance(exc, kind):
            text = prefix.format(sitio=sitio)
            result: dict[str, Any] = {"ok": False, "error": f"{text}: {exc}", "error_code": code}
            if isinstance(exc, BomeHTTPError):
                result["estado_http"] = exc.status
                result["url"] = exc.url
            if isinstance(exc, BomeBlockedError):
                result["reintentar_tras_segundos"] = exc.retry_after
            if isinstance(exc, BoletinAmbiguoError):
                result["candidatos"] = [_candidato(b) for b in exc.candidatos]
            return result
    raise TypeError(f"not a BomeError: {exc!r}")


def _candidato(boletin: BoletinAntiguo) -> dict[str, Any]:
    return {
        "cve": boletin.cve,
        "dboid": boletin.dboid,
        "fecha": boletin.fecha.isoformat(),
        "sufijo": boletin.sufijo,
        "extraordinario": boletin.extraordinario,
    }


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


def _entrada_antigua(boletin: BoletinAntiguo) -> dict[str, Any]:
    """A ``listar_bomes`` entry for a bulletin only the old portal lists."""
    etiqueta = boletin.sufijo or str(boletin.numero)
    return {
        "cve": boletin.cve,
        "number": boletin.numero,
        "date": boletin.fecha.isoformat(),
        "extraordinary": boletin.extraordinario,
        "url": boletin.url_ficha,
        "title": f"Nº {'Extra ' if boletin.extraordinario else ''}{etiqueta}",
        "origen": SITIO_ANTIGUO,
        "dboid": boletin.dboid,
        "cve_oficial": boletin.cve_oficial,
        "sufijo": boletin.sufijo,
        "ver_con": f"ver_bome_antiguo(dboid={boletin.dboid})",
    }


@server.tool()
@_herramienta
def listar_bomes(desde: str | None = None, hasta: str | None = None) -> dict:
    """Lista los boletines publicados entre dos fechas (calendario de bomemelilla.es y, antes
    de 2021-03-13, también el catálogo del portal antiguo de melilla.es).

    Fechas en AAAA-MM-DD o DD/MM/AAAA, ambas incluidas. Por defecto, los últimos 30 días
    hasta hoy. Devuelve como máximo 500 boletines, del más reciente al más antiguo; si hay
    más, 'truncado' es true y 'total' dice cuántos hay: acota el rango. Cada boletín trae
    cve, number, date, extraordinary (BOME-BX), title, url y origen ("bomemelilla.es" o
    "melilla.es"). Si el rango llega antes del 2021-03-13 se añaden los boletines que solo
    tiene el portal antiguo (a bomemelilla.es le faltan muchos antes de 2018 y no tiene nada
    antes de 2014); traen dboid y ver_con (ábrelos con ver_bome_antiguo) y
    'solo_portal_antiguo' los cuenta. Si un boletín está en los dos, gana bomemelilla.es
    (ábrelo con ver_bome). Si el portal antiguo no responde, devuelve lo de bomemelilla.es y
    un 'aviso'. Antes de 2014 los identificadores no son CVE de bomemelilla.es
    (cve_oficial=false) y pueden repetirse: usa el dboid.
    """
    end = _fecha(hasta, "hasta", ArgumentoInvalidoError) or date.today()
    start = _fecha(desde, "desde", ArgumentoInvalidoError) or end - timedelta(days=DEFAULT_LISTADO_DIAS)
    if end < start:
        raise ArgumentoInvalidoError(f"'hasta' ({end}) es anterior a 'desde' ({start})")
    entries: list[dict[str, Any]] = []
    if end >= INICIO_BOMEMELILLA:
        refs = _cliente().calendar(max(start, INICIO_BOMEMELILLA), end)
        entries = [{**ref.to_dict(), "origen": SITIO_POR_DEFECTO} for ref in refs]
    extra: dict[str, Any] = {}
    if start <= FIN_PORTAL_ANTIGUO:
        try:
            catalogo = _portal_antiguo().catalogo()
        except BomeError as exc:
            if end < INICIO_BOMEMELILLA:
                raise  # the old portal is the only source for this range
            failure = error_result(exc)
            extra["aviso"] = (
                "No se pudo consultar el catálogo del portal antiguo (melilla.es): "
                f"{failure['error']} La lista solo trae los boletines de bomemelilla.es, al que le "
                "faltan boletines antes de 2018; reinténtalo más tarde (respeta "
                "portal_antiguo_reintentar_tras_segundos si viene) o busca con buscar_bome_antiguo."
            )
            extra["portal_antiguo_error_code"] = failure["error_code"]
            if "reintentar_tras_segundos" in failure:
                extra["portal_antiguo_reintentar_tras_segundos"] = failure["reintentar_tras_segundos"]
        else:
            known = {entry["cve"] for entry in entries}
            old_only = [
                _entrada_antigua(b)
                for b in catalogo.entre(start, min(end, FIN_PORTAL_ANTIGUO))
                if b.cve not in known
            ]
            entries.extend(old_only)
            extra["solo_portal_antiguo"] = len(old_only)
    entries.sort(key=lambda e: (e["date"] or "", e["number"]), reverse=True)
    return {
        "desde": start.isoformat(),
        "hasta": end.isoformat(),
        "total": len(entries),
        "truncado": len(entries) > MAX_LISTADO,
        "bomes": entries[:MAX_LISTADO],
        **extra,
    }


def _pista_portal_antiguo(cve: str) -> str | None:
    """A hint towards ver_bome_antiguo for a bulletin CVE the old portal may have."""
    try:
        parsed = parse_cve(cve)
    except InvalidCveError:
        return None
    if not parsed.kind.is_bulletin or parsed.year > FIN_PORTAL_ANTIGUO.year:
        return None
    return (
        f"bomemelilla.es no tiene todos los boletines anteriores a 2018: prueba "
        f'ver_bome_antiguo(cve="{parsed}") (portal antiguo de melilla.es, 1985-2021).'
    )


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
    try:
        bulletin = client.bulletin(cve)
    except BomeNotFoundError as exc:
        hint = _pista_portal_antiguo(cve)
        if hint is None:
            raise
        raise BomeNotFoundError(f"{exc}. {hint}", status=exc.status, url=exc.url) from exc
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
    (BOME-A-...) o de página (BOME-P-...). Comprueba que la página exista. El resolutor del
    sitio confunde los extraordinarios con los ordinarios: un BOME-AX se localiza sin él
    (índice local o calendario del año y páginas de sus boletines extraordinarios; puede
    costar unas peticiones más) y se comprueba el artículo; para un BOME-PX se devuelve la
    URL de su PDF, con un aviso. Nunca se da por buena una redirección a un boletín del otro
    tipo (extraordinario frente a ordinario).
    """
    parsed = parse_cve(cve)
    canonical = str(parsed)
    client = _cliente()
    if parsed.kind is CveKind.EXTRA_ARTICLE:
        article = _localizar_articulo(client, parsed, boletin_de_articulo=_boletin_indexado)
        return {"cve": canonical, "url": article.url}
    if parsed.kind is CveKind.EXTRA_PAGE:
        return {
            "cve": canonical,
            "url": pdf_url(parsed, base_url=client.base_url),
            "aviso": (
                "El resolutor de CVE de bomemelilla.es confunde las páginas de boletines "
                "extraordinarios (BOME-PX) con las de boletines ordinarios y lleva a un "
                "artículo equivocado, así que no se usa: esta es la URL del PDF de la página, "
                "que no pasa por el resolutor (no se ha comprobado que exista). Para leerla usa "
                "leer_pdf."
            ),
        }
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
    numero (el número del artículo). Un artículo extraordinario (BOME-AX-...) se localiza sin
    el resolutor del sitio, que lo confunde con el artículo ordinario del mismo número: se usa
    el índice local si lo tiene y, si no, el calendario del año y las páginas de sus boletines
    extraordinarios (puede costar unas peticiones más). Nunca se devuelve un artículo distinto
    del pedido. Los artículos de 2014-2016 no tienen texto en el sitio:
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
        boletin_de_articulo=_boletin_indexado,
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


def _cve_o_url(cve: str | None, url: str | None) -> None:
    if (cve is None) == (url is None):
        raise LecturaInvalidaError(
            "da exactamente uno de 'cve' (PDF de bomemelilla.es) o 'url' (PDF del portal antiguo "
            "de melilla.es, tal como lo dan buscar_bome_antiguo y ver_bome_antiguo)"
        )


@server.tool()
@_herramienta
def leer_pdf(
    cve: str | None = None,
    desde_pagina: int = 1,
    desde_caracter: int = 0,
    max_caracteres: int = 20000,
    url: str | None = None,
) -> dict:
    """Texto del PDF de cualquier CVE: boletín, sumario (BOME-S), artículo (BOME-A) o página
    (BOME-P / BOME-PX); o, con url en vez de cve, de un PDF del portal antiguo de melilla.es.

    Da exactamente uno: cve, o url (solo las URL https://www.melilla.es/mandar.php/... que
    devuelven buscar_bome_antiguo y ver_bome_antiguo, por página o del boletín entero; se
    rechaza cualquier otra). Las páginas sin texto extraíble (escaneadas, frecuentes en los
    boletines antiguos) salen con sin_texto=true y un aviso.
    Paginación: devuelve páginas enteras hasta max_caracteres (1000-100000, por defecto
    20000), siempre al menos una; una página más larga que max_caracteres se corta ahí
    (cortada=true). Si 'siguiente' no es null, vuelve a llamar con desde_pagina y
    desde_caracter de 'siguiente'; null significa que no queda más.
    """
    _cve_o_url(cve, url)
    if url is not None:
        return _leer_pdf_antiguo(
            _portal_antiguo(), url, desde_pagina=desde_pagina, desde_caracter=desde_caracter,
            max_caracteres=max_caracteres,
        ).to_dict()
    client = _cliente()
    return _leer_pdf(
        client, cve, desde_pagina=desde_pagina, desde_caracter=desde_caracter,  # type: ignore[arg-type]
        max_caracteres=max_caracteres,
    ).to_dict()


@server.tool()
@_herramienta
def descargar_pdf(cve: str | None = None, refrescar: bool = False, url: str | None = None) -> dict:
    """Descarga el PDF de cualquier CVE (o, con url, uno del portal antiguo de melilla.es) a la
    caché local y devuelve su ruta, tamaño, sha256 y número de páginas.

    Da exactamente uno: cve, o url (solo las URL https://www.melilla.es/mandar.php/... que
    devuelven buscar_bome_antiguo y ver_bome_antiguo). El nombre del fichero sale solo del CVE
    canónico o de la ruta de esa URL (melilla-9-4914-5302_73.pdf); el directorio lo fija
    BOME_NAVAJA_PDF_DIR (ver estado_servidor). Reutiliza la copia en caché salvo
    refrescar=true. Límite: 100 MB. Los PDF de 2014-2016 no existen en bomemelilla.es (404):
    búscalos en el portal antiguo.
    """
    _cve_o_url(cve, url)
    if url is not None:
        return _descargar_pdf_antiguo(_portal_antiguo(), url, refrescar=refrescar).to_dict()
    client = _cliente()
    return _descargar_pdf(client, cve, refrescar=refrescar).to_dict()  # type: ignore[arg-type]


# --------------------------------------------------------------------------- old portal tools


@server.tool()
@_herramienta
def buscar_bome_antiguo(
    texto: str,
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 100,
) -> dict:
    """Busca artículos en el portal antiguo del BOME (melilla.es): boletines de 1985 al
    12-03-2021, con sumarios de artículos desde ~1991.

    Úsalo para cualquier cosa anterior a 2018 (bomemelilla.es está incompleto ahí y no tiene
    nada antes de 2014). Búsqueda literal de texto en los artículos (3-200 caracteres,
    sin distinguir mayúsculas; no busca por número de boletín: para eso usa ver_bome_antiguo
    o listar_bomes). El portal devuelve TODO en una sola página, así que usa términos
    concretos. Cada artículo trae cve_boletin, fecha, numero, tipo, sumario, ruta (consejería,
    dirección, sección), paginas (número y url_pdf de cada página: léelas con
    leer_pdf(url=...)), dboid_boletin y url_ficha. desde/hasta (AAAA-MM-DD o DD/MM/AAAA)
    filtran por la fecha del boletín después de buscar. Devuelve como mucho limite
    artículos (por defecto 100, máx. 500) en el orden del portal; 'total' cuenta los que
    pasan el filtro y 'truncado' dice si hay más: acota el texto o las fechas.
    """
    if isinstance(limite, bool) or not isinstance(limite, int) or not 1 <= limite <= MAX_LIMITE_ANTIGUO:
        raise BusquedaInvalidaError(f"'limite' debe estar entre 1 y {MAX_LIMITE_ANTIGUO}, no {limite!r}")
    start, end = _fecha(desde, "desde"), _fecha(hasta, "hasta")
    if start is not None and end is not None and end < start:
        raise BusquedaInvalidaError(f"'hasta' ({end}) es anterior a 'desde' ({start})")
    texto_busqueda_valido(texto)  # before touching the portal
    articulos = _portal_antiguo().buscar(texto)
    matching = [
        a
        for a in articulos
        if (start is None and end is None)
        or (a.fecha is not None and (start is None or a.fecha >= start) and (end is None or a.fecha <= end))
    ]
    data: dict[str, Any] = {
        "texto": " ".join(texto.split()),
        "desde": start.isoformat() if start else None,
        "hasta": end.isoformat() if end else None,
        "total": len(matching),
        "total_sin_filtrar": len(articulos),
        "limite": limite,
        "truncado": len(matching) > limite,
        "articulos": [a.to_dict() for a in matching[:limite]],
    }
    if not matching:
        data["aviso"] = (
            "Sin resultados en el portal antiguo. Su búsqueda es literal (sin sinónimos): prueba "
            "otra palabra o una forma más corta, o quita el filtro de fechas. Los sumarios existen "
            "desde ~1991."
        )
    return data


@server.tool()
@_herramienta
def ver_bome_antiguo(cve: str | None = None, dboid: int | None = None) -> dict:
    """Ficha de un boletín del portal antiguo (melilla.es, 1985 al 12-03-2021): PDF del
    boletín entero y sus artículos.

    Da exactamente uno: cve (BOME-B-AAAA-N o BOME-BX-AAAA-N; se busca en el catálogo del
    portal) o dboid (el id del portal, como lo dan listar_bomes, buscar_bome_antiguo y los
    errores de ambigüedad). Antes de 2014 los identificadores tienen forma de CVE pero no son
    CVE de bomemelilla.es y algunos se repiten: entonces responde boletin_ambiguo con los
    candidatos (dboid, fecha, sufijo) y hay que repetir con su dboid. Devuelve url_pdf (el
    boletín entero), y articulos con numero, tipo, sumario, ruta (consejería, dirección,
    sección) y paginas (url_pdf de cada página). Lee cualquiera de esos PDF con
    leer_pdf(url=...).
    """
    if (cve is None) == (dboid is None):
        raise ArgumentoInvalidoError("da exactamente uno de 'cve' o 'dboid'")
    portal = _portal_antiguo()
    if dboid is not None:
        if isinstance(dboid, bool) or not isinstance(dboid, int) or dboid <= 0:
            raise ArgumentoInvalidoError(f"'dboid' debe ser un entero positivo, no {dboid!r}")
        ficha = portal.ficha(dboid)
    else:
        ficha = portal.ficha_por_cve(cve)  # type: ignore[arg-type]
    data: dict[str, Any] = {**ficha.to_dict(), "total_articulos": len(ficha.articulos)}
    if not ficha.cve_oficial:
        data["aviso"] = (
            f"{ficha.cve} es el número del portal antiguo con forma de CVE, pero no es un CVE de "
            "bomemelilla.es (que empieza en 2014) y puede repetirse: cita también la fecha y el "
            "dboid."
        )
    return data


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
    if cobertura.get("rotos"):
        notes.append(
            f"{cobertura['rotos']} boletines están rotos (su página respondió con error interno "
            "del sitio dos veces): no se indexan ni se reintentan salvo con "
            "sincronizar_indice(reintentar_rotos=True)."
        )
    return " ".join(notes) or None


def _sync_state() -> dict[str, Any] | None:
    """State of this process's running sync (either origin), else of the last one started."""
    job = _sync_actual()
    return job.estado().to_dict() if job is not None else None


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
    La sincronización por defecto cubre desde 2018-01-01: 'pendientes' cuenta solo boletines
    desde esa fecha y pendientes_anteriores_2018 los anteriores del calendario sin indexar
    (bomemelilla.es está incompleto antes de 2018; esos boletines se consultan mejor en el
    portal antiguo de melilla.es).
    Incluye también los boletines del portal antiguo (melilla.es, 1991-2017) una vez
    sincronizados con sincronizar_indice(origen="melilla.es"): cada artículo trae 'origen'
    ("bomemelilla.es" o "melilla.es"); en los de melilla.es, url es la ficha del boletín en el
    portal antiguo, pdf_url el PDF de la página del artículo (léelo con leer_pdf(url=...)),
    bome_cve el identificador del portal (antes de 2014 no es un CVE de bomemelilla.es) y cve
    una clave interna MEL-<dboid>-<numero>. cobertura.por_origen da lo indexado de cada origen.
    Un boletín que está en los dos sale una sola vez, del origen con mejor resultado (con
    sumarios gana; a igualdad, bomemelilla.es).
    coincidencia: "fragmento" (subcadena, como el sitio: "cese" encuentra "ceses" y
    "procese") o "palabra" (cada frase debe empezar una palabra: "cese" → cese, ceses, no
    procese). terminos=[{texto, operador: "y"|"o", modo: "contiene"|"no_contiene"}]; Y dentro
    del mismo artículo. consejeria: parte del nombre (sin tildes). orden: "fecha" o
    "relevancia". Pagina con limite (máx. 200) y desplazamiento ('siguiente' da el próximo).
    Solo cubre sumarios: de bomemelilla.es desde finales de 2016 y del portal antiguo desde
    ~1991.
    """
    index = _open_index_if_present()
    if index is None:
        cobertura = {
            "boletines_indexados": 0,
            "fecha_min": None,
            "fecha_max": None,
            "pendientes": None,
            "pendientes_anteriores_2018": None,
            "rotos": None,
            "ultima_sincronizacion": None,
            "sincronizacion_en_curso": False,
            "por_origen": None,
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

    Devuelve boletines indexados / sin sumarios / con error / rotos (páginas que el sitio
    respondió con error interno dos veces; no cuentan como pendientes), artículos, rango de
    fechas, pendientes frente al calendario, última sincronización y el progreso de la actual
    (hechos, total_planificado, eta_segundos). 'pendientes' cuenta solo boletines desde
    2018-01-01 (el inicio por defecto de sincronizar_indice); los anteriores del calendario sin
    indexar (de sincronizaciones antiguas o con un 'desde' anterior) salen aparte en
    pendientes_anteriores_2018 y no son trabajo pendiente. Úsalo para seguir una
    sincronización lanzada con sincronizar_indice.
    Por origen: indice.por_origen cuenta boletines, artículos y fechas de cada origen
    ("bomemelilla.es" y "melilla.es", el portal antiguo) e indice.ultimas_sincronizaciones
    guarda el resumen de la última sincronización de cada uno (ultima_sincronizacion es la de
    bomemelilla.es). 'sincronizacion' es la de este proceso que está en curso (o la última
    lanzada), de cualquiera de los dos orígenes, con su 'origen'; indice.sincronizacion_en_curso
    dice qué origen sincroniza ahora cualquier proceso.
    Estados de la sincronización: en_curso, completado, cancelado, fallido y bloqueado (el sitio
    nos bloqueó: 403/429/503 o dos peticiones seguidas sin respuesta; lo indexado se conserva y
    bome-navaja no le pide nada durante reintentar_tras_segundos, ~75 min por defecto: no vuelvas
    a sincronizar antes; ver 'mensaje'). En 'mensaje' también aparecen las pausas preventivas
    (por defecto, como mucho 3 respuestas de error del sitio cada 10 minutos) y la sincronización cuenta
    los boletines que quedaron rotos en 'rotos'.
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
    reintentar_rotos: bool = False,
    origen: str = ORIGEN_BOME,
) -> dict:
    """Arranca en segundo plano la sincronización del índice local de sumarios y vuelve al
    instante.

    origen: "bomemelilla.es" (por defecto, el sitio actual) o "melilla.es" (el portal
    antiguo). Solo corre una a la vez por índice, sea del origen que sea: si ya hay una
    sincronización en curso (de cualquiera de los dos, en este u otro proceso) devuelve su
    estado o en_curso_en_otro_proceso con el origen que la ocupa, sin arrancar otra.

    origen="bomemelilla.es": recorre el calendario (por defecto 2018-01-01..hoy) del más
    reciente al más antiguo: indexa los boletines que falten, re-indexa los de los últimos
    reindexar_recientes_dias días y, si reintentar_errores, los que fallaron. Empieza en 2018
    porque antes bomemelilla.es está incompleto e inestable (faltan boletines y sus páginas
    rotas responden HTTP 500, que el cortafuegos castiga) y apenas tiene texto buscable; los boletines
    anteriores se consultan mejor en el portal antiguo de melilla.es. Un 'desde' anterior es
    posible pero no recomendable. Para no saturar el sitio va despacio
    (~2-3 s entre peticiones) y cada ejecución indexa como mucho max_boletines boletines
    (por defecto 250, los más recientes; ~15-20 minutos). El rango por defecto
    (~1100 boletines) necesita varias ejecuciones: si el estado final trae
    pendientes_tras_limite > 0, vuelve a llamarla más tarde (espaciar las ejecuciones es más
    amable con el sitio). Es reanudable: si se corta, la siguiente llamada continúa donde
    quedó. Sigue el progreso con estado_indice; mientras tanto
    buscar_en_indice da resultados parciales. Si ya hay una en curso (en este u otro proceso)
    devuelve su estado sin arrancar otra. Solo sincroniza cuando se le pide. El cortafuegos del
    sitio bloquea la IP tras unas 5 respuestas de error, así que la sincronización admite como
    mucho 3 cada 10 minutos (si llega al límite hace una pausa preventiva y va más lenta) y
    espera 30-60 s tras una página rota (valores por defecto; los que están en uso, en
    estado_servidor, 'ajustes'). Si el sitio la bloquea (403/429/503 o dos peticiones
    seguidas sin respuesta) termina en estado "bloqueado" y bome-navaja no le pide nada durante
    reintentar_tras_segundos (~75 min): no la relances antes (terminaría "bloqueado" al
    instante).
    Los boletines "rotos" (su página respondió con error interno, HTTP 500, dos veces) se
    saltan; reintentar_rotos=True los vuelve a pedir: úsalo solo para comprobar si el sitio
    los arregló, porque cada uno cuesta un HTTP 500 que el cortafuegos del sitio cuenta.

    origen="melilla.es" indexa los sumarios de artículos de las fichas de boletín del portal
    antiguo: por defecto los boletines de 1991-01-01 a 2017-12-31 (antes de 1991 las fichas no
    traen artículos; desde 2018 manda bomemelilla.es), del más reciente al más antiguo, y se
    salta los que ya están indexados con sumarios desde cualquiera de los dos orígenes. Una
    petición por boletín (la ficha, nunca los PDF), igual de despacio (~2-3 s entre
    peticiones) y con el mismo límite max_boletines (por defecto 250, ~15-20 minutos por
    ejecución): los ~2.000-2.500 boletines del rango necesitan varias ejecuciones espaciadas
    (mira pendientes_tras_limite). Es reanudable, reintenta los fallidos si
    reintentar_errores y los rotos solo con reintentar_rotos. reindexar_recientes_dias no se
    aplica (el portal está congelado). El portal antiguo tiene su propia guardia
    (guardia_portal_antiguo): sus errores no cuentan para bomemelilla.es. Rellena también los
    boletines de 2014-2016 que bomemelilla.es tiene sin sumarios.
    """
    global _sync_reciente
    if origen not in ORIGENES:
        raise ArgumentoInvalidoError(
            f"'origen' debe ser \"{ORIGEN_BOME}\" (el sitio actual) o \"{ORIGEN_ANTIGUO}\" "
            f"(el portal antiguo), no {origen!r}"
        )
    start = _fecha(desde, "desde", ArgumentoInvalidoError)
    end = _fecha(hasta, "hasta", ArgumentoInvalidoError)
    job: SincronizadorBase
    if origen == ORIGEN_ANTIGUO:
        old_portal = _get_sync_antiguo()
        job = old_portal
        state = old_portal.iniciar(
            desde=start,
            hasta=end,
            reintentar_errores=reintentar_errores,
            max_boletines=max_boletines,
            reintentar_rotos=reintentar_rotos,
        )
    else:
        current = _get_sync()
        job = current
        state = current.iniciar(
            desde=start,
            hasta=end,
            reindexar_recientes_dias=reindexar_recientes_dias,
            reintentar_errores=reintentar_errores,
            max_boletines=max_boletines,
            reintentar_rotos=reintentar_rotos,
        )
    if state.estado == "en_curso":
        _sync_reciente = job
    return state.to_dict()


@server.tool()
@_herramienta
def cancelar_sincronizacion() -> dict:
    """Pide parar la sincronización en curso de este proceso, sea cual sea su origen
    (bomemelilla.es o el portal antiguo melilla.es: cualquiera de las dos, solo corre una a la
    vez); termina tras el boletín que esté procesando (o al instante si está en una pausa:
    preventiva o tras una página rota del sitio).

    Devuelve el estado de esa sincronización, con su 'origen'. Lo ya indexado se conserva y
    una nueva sincronizar_indice (con el mismo origen) continúa desde ahí.
    """
    job = _sync_actual()
    if job is None:
        return {"estado": "inactivo", "mensaje": "No hay ninguna sincronización en este proceso."}
    return job.cancelar().to_dict()


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
    abierto, versión de SQLite con FTS5/trigram, URL base y:
    - ajustes: todos los ajustes que marcan cuánto se le pide a los sitios, con las variables
      de entorno aplicadas (un único juego para bomemelilla.es y el portal antiguo):
      pausa_sincronizacion_segundos y variacion_sincronizacion_segundos (ritmo de la
      sincronización), max_boletines_por_ejecucion, pausa_consultas_segundos (herramientas de
      bomemelilla.es), guardia_max_errores, guardia_ventana_minutos y
      guardia_enfriamiento_minutos (guardia del sitio), pausa_tras_error_segundos (la pausa
      tras una página rota va de ese valor al doble, pausa_tras_error_max_segundos),
      tiempo_espera_segundos; 'variables' dice qué variable BOME_NAVAJA_* fija cada uno,
      'avisos' los valores no válidos (se usa el valor por defecto) y 'riesgos' los valores
      más arriesgados que lo recomendado (se usan igualmente; explícale al usuario el riesgo
      de bloqueo). Se leen al arrancar: cambiarlos exige reiniciar el servidor.
    - cortesia_segundos (pausa de las herramientas) y cortesia_sincronizacion (pausa, variación
      aleatoria y máximo de boletines por ejecución, con BOME_NAVAJA_SYNC_DELAY,
      BOME_NAVAJA_SYNC_JITTER y BOME_NAVAJA_SYNC_MAX_BOLETINES aplicadas, y sus avisos).
    - guardia_sitio: enfriamiento_hasta, segundos_restantes y motivo si el sitio nos bloqueó,
      durante el cual las herramientas responden sitio_bloqueando; errores HTTP en la ventana
      (ventana_segundos, 10 minutos por defecto) frente al máximo permitido (max_errores, 3 por
      defecto): con el cupo lleno las herramientas responden pausa_preventiva; y su fichero
      estado_sitio.json, compartido por todos los procesos de bome-navaja.
    - Del portal antiguo (melilla.es): url_portal_antiguo, su propia guardia
      (guardia_portal_antiguo, con su fichero estado_sitio_melilla.json) y la caché de su
      catálogo (catalogo_portal_antiguo: ruta, existe, fetched_at y número de boletines). La
      sincronización del portal antiguo (sincronizar_indice con origen="melilla.es") va al
      mismo ritmo y con el mismo máximo por ejecución que la de bomemelilla.es, con las mismas
      variables de entorno aplicadas: cortesia_sincronizacion_portal_antiguo (el portal
      interactivo va a ~1-1,5 s entre peticiones).
    """
    try:
        exists = index_path()[0].exists()
    except BomeError:
        exists = False
    index = _index
    ajustes = ajustes_desde_entorno(os.environ)
    settings = sync_settings_from_env(os.environ)
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
        "ajustes": ajustes.to_dict(),
        "cortesia_segundos": ajustes.pausa_consultas_segundos,
        "cortesia_sincronizacion": sync_pace,
        "cortesia_sincronizacion_portal_antiguo": dict(sync_pace),
        "url_base": BASE_URL,
        "guardia_sitio": _get_guard().estado(),
        "url_portal_antiguo": PORTAL_URL,
        "guardia_portal_antiguo": _get_portal_guard().estado(),
        "catalogo_portal_antiguo": _estado_catalogo(),
    }


def _estado_catalogo() -> dict[str, Any]:
    try:
        path = data_dir()[0] / FICHERO_CATALOGO
    except BomeError as exc:
        return {**estado_catalogo(None), "error": str(exc)}
    return estado_catalogo(path)


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
