"""Old BOME portal on melilla.es: catalog 1985-2021, text search, bulletin ficha, page PDFs.

Reconnaissance (2026-09-24). bomemelilla.es is an incomplete migration for
2014-2017 (141 bulletins missing); the city's old portal keeps the frozen
catalog from 1985-01-03 to 2021-03-12. Facts the code relies on:

* Base ``https://www.melilla.es/melillaPortal/``, HTTPS only (port 80 is
  closed) although its pages link PDFs as ``http://www.melilla.es/mandar.php/...``:
  every ``mandar.php`` URL is rewritten to https. Pages are ISO-8859-1, the
  ``Content-Type`` does not always say so, so bodies are decoded as latin-1.
* Catalog: one GET of ``contenedor.jsp?seccion=bome.jsp`` (~970 KB) lists
  every bulletin in an accordion (``div.set`` per year > month blocks >
  ``ul.menu li a``). Link texts: ``nº 5842 / 12-03-2021``, ``nº Extra16 /
  09-03-2021``, ``nº Extraordinario 17,17(II) / 20-11-2010``,
  ``nº EXTRAORDINARIO 3 BIS / 05-04-2003`` and typos (``ESTRAORDINARIO``).
  The "ÚLTIMO BOLETÍN" block repeats the newest one (deduplicated by dboid).
  The 1985-01-03 bulletin sits under a set whose header image says 1927: the
  link's own date wins and a warning ("aviso") records it.
* Numbering is the bomemelilla.es CVE numbering (verified 2014-2021): ``nº N``
  of year Y is ``BOME-B-Y-N``, ``Extra N`` / ``Extraordinario N`` is
  ``BOME-BX-Y-N``. Before 2014 the identifiers follow the same shape but are
  not real bomemelilla.es CVEs (``cve_oficial`` is False) and are not unique
  (e.g. two "Extra1" in 1986): lookups by CVE return lists.
* Search: POST ``contenedor.jsp?seccion=busqueda_bome.jsp`` with the latin-1
  form field ``textobome``; all matches in one page, no pagination (so the
  response is size-capped). Results are articles with per-page PDF links.
* Ficha: GET ``contenedor.jsp?seccion=ficha_bome.jsp&dboidboletin=<id>``:
  header, whole-bulletin PDF, and a flat sequence of headings and articles.
* robots.txt disallows ``ficha_bome.jsp`` and ``/mandar.php``. User decision
  2026-09-24: allowed for on-demand requests only; nothing here crawls them.

The site is not bomemelilla.es: :class:`PortalAntiguo` has its own
:class:`~bome_navaja.guard.GuardiaSitio` (state file
:data:`~bome_navaja.guard.FICHERO_ESTADO_MELILLA`) and a polite delay of 1 s.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from .client import DEFAULT_TIMEOUT, BomeClient
from .cve import InvalidCveError
from .documents import MAX_PDF_BYTES
from .guard import FICHERO_ESTADO_MELILLA, GuardiaSitio
from .models import (
    BomeDocumentTooLargeError,
    BomeError,
    BomeNotFoundError,
    BomeParseError,
    JsonModel,
)
from .paths import data_dir
from .search import BusquedaInvalidaError

PORTAL_URL: Final = "https://www.melilla.es/melillaPortal"
"""Base of the old portal (https only)."""

SITIO: Final = "melilla.es"
"""Host named in the guard messages."""

ORIGEN: Final = "melilla.es"
"""``origen`` of every record built from the old portal."""

FICHERO_CATALOGO: Final = "catalogo_portal_antiguo.json"
"""Catalog cache inside the data folder (the portal is frozen: reused forever)."""

MAX_RESPUESTA_BYTES = 15 * 1024 * 1024
"""Largest HTML page accepted (the search never paginates)."""

DEFAULT_POLITE_DELAY: Final = 1.0
DEFAULT_JITTER: Final = 0.5

MIN_TEXTO_BUSQUEDA: Final = 3
MAX_TEXTO_BUSQUEDA: Final = 200

PRIMER_ANIO_CVE_OFICIAL: Final = 2014
"""First year whose numbering matches real bomemelilla.es CVEs."""

_CATALOG_VERSION: Final = 1
_COMMON_PARAMS: Final = (("codResi", "1"), ("language", "es"), ("codAdirecto", "15"))

_PDF_URL = re.compile(r"https?://www\.melilla\.es/mandar\.php/n/\d+/\d+/[A-Za-z0-9_]+\.pdf")
_DBOID = re.compile(r"dboidboletin=(\d+)")
_NUMERACION = re.compile(
    r"^\s*(?:n\s*[º°]\s*\.?\s*)?"  # optional "nº"
    r"(?P<extra>e[xs]tra[^\W\d_]*)?"  # Extra / Extraordinario / EXTRAORINARIO / ESTRAORDINARIO
    r"[\s\-]*(?P<etiqueta>(?:n\s*[º°]\s*)?(?P<numero>\d+).*?)"
    r"\s*/\s*(?P<dia>\d{1,2})-(?P<mes>\d{1,2})-(?P<anio>\d{4})\s*$",
    re.IGNORECASE | re.DOTALL,
)
_NUMERO_FICHA = re.compile(
    r"(?:n\s*[º°]|n[úu]mero)\s*(?P<extra>e[xs]tra[^\W\d_]*)?[\s\-]*(?:n\s*[º°]\s*)?(?P<numero>\d+)",
    re.IGNORECASE,
)
_FECHA_LARGA = re.compile(r"(\d{1,2})\s+de\s+([^\W\d_]+)\s+de\s+(\d{4})", re.IGNORECASE)
_FECHA_CORTA = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})")
_YEAR_IMAGE = re.compile(r"/(\d{4})\.jpg$")
_PAGE_FILE = re.compile(r"_(\d+)\.pdf$")
_MESES: Final = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


# --------------------------------------------------------------------------- errors


class UrlPdfInvalidaError(BomeError, ValueError):
    """A PDF URL outside the old portal's ``mandar.php`` whitelist."""

    error_code = "url_pdf_invalida"


class BoletinAmbiguoError(BomeError, ValueError):
    """A pre-2014 identifier matches several bulletins of the old portal."""

    error_code = "boletin_ambiguo"

    def __init__(self, message: str, *, candidatos: tuple[BoletinAntiguo, ...]) -> None:
        super().__init__(message)
        self.candidatos = candidatos


# --------------------------------------------------------------------------- models


@dataclass(frozen=True, slots=True)
class BoletinAntiguo(JsonModel):
    """A bulletin of the old portal's catalog."""

    cve: str
    """``BOME-B-Y-N`` or ``BOME-BX-Y-N`` (see :attr:`cve_oficial`)."""
    numero: int
    extraordinario: bool
    sufijo: str | None
    """Numbering as printed when it is more than the number (``"17,17(II)"``, ``"3 BIS"``)."""
    fecha: date
    dboid: int
    """The portal's bulletin id (``dboidboletin``)."""
    url_ficha: str
    cve_oficial: bool
    """True from 2014 on, when bomemelilla.es uses the same CVE; before, the
    identifier only follows the same shape (and may repeat)."""
    origen: str = ORIGEN


@dataclass(frozen=True, slots=True)
class PaginaAntigua(JsonModel):
    """One printed page of an article, with its PDF on ``mandar.php``."""

    numero: int | None
    url_pdf: str


@dataclass(frozen=True, slots=True)
class ArticuloAntiguo(JsonModel):
    """An article as listed by the search or a bulletin ficha."""

    cve_boletin: str | None
    fecha: date | None
    numero: int | None
    """Article number within the bulletin (``Articulo:``)."""
    tipo: str | None
    """Label printed before the sumario (``"Notificación"``, ...)."""
    sumario: str | None
    ruta: tuple[str, ...]
    """Headings above the article: top block, consejería, dirección, sección."""
    paginas: tuple[PaginaAntigua, ...]
    dboid_boletin: int | None = None
    url_ficha: str | None = None
    origen: str = ORIGEN


@dataclass(frozen=True, slots=True)
class FichaAntigua(JsonModel):
    """A bulletin ficha: the bulletin, its whole PDF and its articles."""

    cve: str
    numero: int
    extraordinario: bool
    sufijo: str | None
    fecha: date
    dboid: int
    url_ficha: str
    cve_oficial: bool
    url_pdf: str | None
    articulos: tuple[ArticuloAntiguo, ...]
    origen: str = ORIGEN


@dataclass(frozen=True, slots=True)
class CatalogoAntiguo(JsonModel):
    """The whole parsed catalog with its lookups."""

    boletines: tuple[BoletinAntiguo, ...]
    avisos: tuple[str, ...]
    fetched_at: str
    """When the catalog page was downloaded (ISO 8601, UTC)."""
    desde_cache: bool = False

    def por_cve(self, cve: str) -> list[BoletinAntiguo]:
        """Every bulletin with that identifier (usually one; pre-2014 may repeat)."""
        wanted = normalizar_cve(cve)
        return [b for b in self.boletines if b.cve == wanted]

    def por_dboid(self, dboid: int) -> BoletinAntiguo | None:
        """The bulletin with that portal id, if listed."""
        return next((b for b in self.boletines if b.dboid == dboid), None)

    def entre(self, desde: date | None, hasta: date | None) -> list[BoletinAntiguo]:
        """Bulletins dated from ``desde`` to ``hasta`` (both inclusive, either open), catalog order."""
        return [
            b
            for b in self.boletines
            if (desde is None or b.fecha >= desde) and (hasta is None or b.fecha <= hasta)
        ]


# --------------------------------------------------------------------------- helpers


def url_ficha(dboid: int) -> str:
    """Absolute URL of a bulletin ficha."""
    return (
        f"{PORTAL_URL}/contenedor.jsp?seccion=ficha_bome.jsp&dboidboletin={int(dboid)}"
        "&codResi=1&language=es&codAdirecto=15"
    )


def pdf_url_valida(url: str) -> str:
    """The https form of a ``mandar.php`` PDF URL of the old portal.

    Only ``http(s)://www.melilla.es/mandar.php/n/<digits>/<digits>/<name>.pdf``
    is accepted (``name`` of letters, digits and ``_``), so a model can never
    make us fetch an arbitrary URL. Raises :class:`UrlPdfInvalidaError`.
    """
    text = url.strip() if isinstance(url, str) else ""
    if not _PDF_URL.fullmatch(text):
        raise UrlPdfInvalidaError(
            f"URL de PDF no válida: {url!r}. Solo se aceptan PDF del portal antiguo con la "
            "forma https://www.melilla.es/mandar.php/n/<número>/<número>/<nombre>.pdf, tal "
            "como aparecen en sus búsquedas y fichas."
        )
    return "https://" + text.split("://", 1)[1]


def normalizar_cve(cve: str) -> str:
    """Canonical ``BOME-B-Y-N`` / ``BOME-BX-Y-N``; raises :class:`InvalidCveError`."""
    compact = re.sub(r"\s+", "", cve).upper() if isinstance(cve, str) else ""
    match = re.fullmatch(r"BOME-(BX|B)-(\d{4})-(\d+)", compact)
    if match is None or int(match.group(3)) == 0:
        raise InvalidCveError(f"not a bulletin CVE (BOME-B-AAAA-N or BOME-BX-AAAA-N): {cve!r}")
    kind, year, number = match.groups()
    return f"BOME-{kind}-{year}-{int(number)}"


def texto_busqueda_valido(texto: str) -> str:
    """Search text with collapsed whitespace, or :class:`BusquedaInvalidaError`."""
    clean = " ".join(texto.split()) if isinstance(texto, str) else ""
    if len(clean) < MIN_TEXTO_BUSQUEDA:
        raise BusquedaInvalidaError(
            f"el texto a buscar en el portal antiguo debe tener al menos {MIN_TEXTO_BUSQUEDA} caracteres"
        )
    if len(clean) > MAX_TEXTO_BUSQUEDA:
        raise BusquedaInvalidaError(
            f"el texto a buscar en el portal antiguo admite como mucho {MAX_TEXTO_BUSQUEDA} caracteres"
        )
    try:
        clean.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise BusquedaInvalidaError(
            f"el buscador del portal antiguo solo admite caracteres latin-1 (español y europeo "
            f"occidental); quita {clean[exc.start:exc.end]!r}"
        ) from None
    return clean


def _decode(html: str | bytes) -> str:
    return html.decode("latin-1") if isinstance(html, bytes) else html


def _soup(html: str | bytes) -> BeautifulSoup:
    return BeautifulSoup(_decode(html), "lxml")


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ").split()) if node is not None else ""


def _https(url: str) -> str:
    """Absolute URL with the portal's ``http://www.melilla.es`` links moved to https."""
    absolute = urljoin(PORTAL_URL + "/", url.strip())
    if absolute.lower().startswith("http://www.melilla.es/"):
        return "https://" + absolute[len("http://") :]
    return absolute


def _dboid(href: str) -> int | None:
    match = _DBOID.search(href)
    return int(match.group(1)) if match else None


def _date(day: str, month: int | str, year: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _Numeracion:
    extraordinario: bool
    numero: int
    sufijo: str | None
    fecha: date

    @property
    def cve(self) -> str:
        return f"BOME-{'BX' if self.extraordinario else 'B'}-{self.fecha.year}-{self.numero}"


def _numeracion(text: str) -> _Numeracion | None:
    """``nº Extra16 / 09-03-2021`` (catalog) or ``5722 / 17-01-2020`` (search)."""
    match = _NUMERACION.match(text)
    if match is None:
        return None
    fecha = _date(match["dia"], match["mes"], match["anio"])
    numero = int(match["numero"])
    if fecha is None or numero == 0:
        return None
    etiqueta = " ".join(match["etiqueta"].split())
    return _Numeracion(
        extraordinario=match["extra"] is not None,
        numero=numero,
        sufijo=etiqueta if etiqueta != match["numero"] else None,
        fecha=fecha,
    )


def _boletin(numeracion: _Numeracion, dboid: int) -> BoletinAntiguo:
    return BoletinAntiguo(
        cve=numeracion.cve,
        numero=numeracion.numero,
        extraordinario=numeracion.extraordinario,
        sufijo=numeracion.sufijo,
        fecha=numeracion.fecha,
        dboid=dboid,
        url_ficha=url_ficha(dboid),
        cve_oficial=numeracion.fecha.year >= PRIMER_ANIO_CVE_OFICIAL,
    )


# --------------------------------------------------------------------------- parsers


def _set_year(link: Tag) -> int | None:
    block = link.find_parent("div", class_="set")
    image = block.select_one("div.title img") if block is not None else None
    match = _YEAR_IMAGE.search(str(image.get("src", ""))) if image is not None else None
    return int(match.group(1)) if match else None


def parse_catalogo(html: str | bytes) -> tuple[list[BoletinAntiguo], list[str]]:
    """Every bulletin of the catalog page, newest first, and the warnings ("avisos").

    Deduplicated by dboid (the "ÚLTIMO BOLETÍN" block repeats the newest).
    A link without a recognisable number or date is skipped with one aviso;
    a bulletin filed under another year's header is kept (its own date wins)
    with one aviso per header. Raises :class:`BomeParseError` when the page
    lists no bulletin at all.
    """
    soup = _soup(html)
    links = [a for a in soup.find_all("a", href=True) if "ficha_bome.jsp" in str(a["href"])]
    boletines: dict[int, BoletinAntiguo] = {}
    avisos: list[str] = []
    textless: list[int] = []
    misplaced: dict[int, list[BoletinAntiguo]] = {}
    for link in links:
        dboid = _dboid(str(link["href"]))
        text = _text(link)
        if dboid is None:
            avisos.append(f"omitido el enlace {text!r}: no lleva dboidboletin")
            continue
        if not text:
            textless.append(dboid)  # the "ÚLTIMO BOLETÍN" image link
            continue
        numeracion = _numeracion(text)
        if numeracion is None:
            avisos.append(f"omitido el enlace {text!r} (dboid {dboid}): sin número o fecha reconocibles")
            continue
        boletin = _boletin(numeracion, dboid)
        previous = boletines.setdefault(dboid, boletin)
        if previous is not boletin:
            if previous != boletin:
                avisos.append(
                    f"dboid {dboid} aparece dos veces con datos distintos ({previous.cve} y "
                    f"{boletin.cve}); se conserva el primero"
                )
            continue
        year = _set_year(link)
        if year is not None and year != numeracion.fecha.year:
            misplaced.setdefault(year, []).append(boletin)
    if not boletines:
        raise BomeParseError("the old portal's catalog page lists no bulletin (did the page change?)")
    for year, wrong in misplaced.items():
        detail = ", ".join(f"{b.cve} del {b.fecha.isoformat()} (dboid {b.dboid})" for b in wrong)
        avisos.append(
            f"el bloque del año {year} incluye {len(wrong)} boletín(es) de otro año: {detail}; "
            "se usa la fecha del enlace"
        )
    for dboid in textless:
        if dboid not in boletines:
            avisos.append(f"omitido el enlace sin texto al dboid {dboid}: sin número ni fecha")
    return list(boletines.values()), avisos


def _label(li: Tag) -> tuple[str, Tag | None]:
    span = li.find("span", class_="txBoN")
    label = _text(span).rstrip(":").strip() if span is not None else ""
    return label, span


def _fold(label: str) -> str:
    return label.casefold().replace("í", "i").replace("á", "a")


def _paginas(li: Tag) -> tuple[PaginaAntigua, ...]:
    pages = []
    for link in li.find_all("a", href=True):
        url = _https(str(link["href"]))
        text = _text(link)
        if text.isdigit():
            number: int | None = int(text)
        else:
            match = _PAGE_FILE.search(url)
            number = int(match.group(1)) if match else None
        pages.append(PaginaAntigua(number, url))
    return tuple(pages)


def _articulo(
    ul: Tag, ruta: tuple[str, ...], boletin: BoletinAntiguo | _Numeracion | None, dboid: int | None
) -> ArticuloAntiguo:
    numero: int | None = None
    tipo: str | None = None
    sumario: str | None = None
    paginas: tuple[PaginaAntigua, ...] = ()
    for li in ul.find_all("li", recursive=False):
        label, span = _label(li)
        kind = _fold(label)
        if kind == "boletin":
            link = li.find("a", href=True)
            if link is not None:
                dboid = _dboid(str(link["href"]))
                boletin = _numeracion(_text(link))
        elif kind == "articulo":
            match = re.match(r"\s*(\d+)", _text(li)[len(_text(span)) :])
            numero = int(match.group(1)) if match else None
        elif kind == "paginas":
            paginas = _paginas(li)
        elif tipo is None:
            if span is not None:
                span.extract()
            tipo = label or None
            sumario = _text(li).lstrip(":").strip() or None
    return ArticuloAntiguo(
        cve_boletin=boletin.cve if boletin is not None else None,
        fecha=boletin.fecha if boletin is not None else None,
        numero=numero,
        tipo=tipo,
        sumario=sumario,
        ruta=ruta,
        paginas=paginas,
        dboid_boletin=dboid,
        url_ficha=url_ficha(dboid) if dboid is not None else None,
    )


def _articulos(
    band: Tag, boletin: BoletinAntiguo | _Numeracion | None = None, dboid: int | None = None
) -> list[ArticuloAntiguo]:
    """Articles of a search or ficha body with their heading chains.

    The page is a flat sequence: ``div.bandabome`` (top block) starts a new
    chain, ``div.bandanegociado`` headings accumulate, and each article
    (``div.B2 > ul``) takes the headings seen since the previous article;
    an article with no new heading keeps the previous chain.
    """
    articulos: list[ArticuloAntiguo] = []
    top = ""
    pending: list[str] = []
    chain: list[str] = []
    for div in band.find_all("div", class_=["bandabome", "bandanegociado", "B2"]):
        classes = div.get("class") or []
        if "bandabome" in classes:
            top, pending, chain = _text(div), [], []
        elif "bandanegociado" in classes:
            pending.append(_text(div))
        else:
            for ul in div.find_all("ul"):
                if ul.find("li") is None:
                    continue
                if pending:
                    chain, pending = pending, []
                ruta = tuple(heading for heading in (top, *chain) if heading)
                articulos.append(_articulo(ul, ruta, boletin, dboid))
    return articulos


def _body(soup: BeautifulSoup, what: str) -> Tag:
    band = soup.find("div", class_="bandaNo")
    if band is None:
        raise BomeParseError(f"the old portal's {what} page has no results area (did the page change?)")
    return band


def parse_busqueda(html: str | bytes) -> list[ArticuloAntiguo]:
    """Every article of a search results page (all of them: the portal never paginates)."""
    return _articulos(_body(_soup(html), "search"))


def _fecha_ficha(text: str) -> date | None:
    match = _FECHA_LARGA.search(text)
    if match and match.group(2).casefold() in _MESES:
        return _date(match.group(1), _MESES[match.group(2).casefold()], match.group(3))
    match = _FECHA_CORTA.search(text)
    return _date(*match.groups()) if match else None


def parse_ficha(html: str | bytes, dboid: int, *, boletin: BoletinAntiguo | None = None) -> FichaAntigua:
    """A bulletin ficha.

    Without ``boletin`` the numbering and date come from the ficha header
    (``BOLETÍN Nº 5302`` / ``Número 5302 - viernes, 8 de enero de 2016``);
    with the catalog entry ``boletin`` (same ``dboid``) its numbering wins,
    since it also carries the suffix and the extraordinary flag.
    """
    if boletin is not None and boletin.dboid != dboid:
        raise ValueError(f"the catalog entry has dboid {boletin.dboid}, not {dboid}")
    band = _body(_soup(html), "ficha")
    header = " ".join(_text(band.find("div", class_=name)) for name in ("banda8", "banda9"))
    if boletin is None:
        match = _NUMERO_FICHA.search(header)
        fecha = _fecha_ficha(header)
        if match is None or fecha is None or int(match["numero"]) == 0:
            raise BomeParseError(f"the old portal's ficha {dboid} has no bulletin number or date")
        boletin = _boletin(
            _Numeracion(
                extraordinario=match["extra"] is not None or re.search(r"e[xs]tra", header, re.I) is not None,
                numero=int(match["numero"]),
                sufijo=None,
                fecha=fecha,
            ),
            dboid,
        )
    pdf = next(
        (
            _https(str(a["href"]))
            for name in ("banda9", "banda8")
            if (div := band.find("div", class_=name)) is not None
            for a in div.find_all("a", href=True)
            if "mandar.php" in str(a["href"])
        ),
        None,
    )
    return FichaAntigua(
        cve=boletin.cve,
        numero=boletin.numero,
        extraordinario=boletin.extraordinario,
        sufijo=boletin.sufijo,
        fecha=boletin.fecha,
        dboid=dboid,
        url_ficha=url_ficha(dboid),
        cve_oficial=boletin.cve_oficial,
        url_pdf=pdf,
        articulos=tuple(_articulos(band, boletin, dboid)),
    )


# --------------------------------------------------------------------------- catalog cache


def _now_iso() -> str:
    """Current UTC time; a seam for tests."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _catalog_to_json(catalogo: CatalogoAntiguo) -> str:
    return json.dumps(
        {
            "version": _CATALOG_VERSION,
            "fetched_at": catalogo.fetched_at,
            "fuente": _catalog_url(),
            "boletines": [b.to_dict() for b in catalogo.boletines],
            "avisos": list(catalogo.avisos),
        },
        ensure_ascii=False,
    )


def _catalog_from_json(raw: str) -> CatalogoAntiguo:
    """Rebuild a cached catalog; any shape problem raises ``ValueError``."""
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != _CATALOG_VERSION:
        raise ValueError("unknown catalog cache version")
    fetched_at, rows, avisos = data.get("fetched_at"), data.get("boletines"), data.get("avisos")
    if not isinstance(fetched_at, str) or not isinstance(rows, list) or not rows or not isinstance(avisos, list):
        raise ValueError("incomplete catalog cache")
    boletines = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("catalog row is not an object")
        try:
            boletin = BoletinAntiguo(**{**row, "fecha": date.fromisoformat(row["fecha"])})
        except (KeyError, TypeError) as exc:
            raise ValueError(f"bad catalog row: {exc}") from exc
        if not (isinstance(boletin.dboid, int) and isinstance(boletin.numero, int) and isinstance(boletin.cve, str)):
            raise ValueError("bad catalog row types")
        boletines.append(boletin)
    return CatalogoAntiguo(
        boletines=tuple(boletines),
        avisos=tuple(str(a) for a in avisos),
        fetched_at=fetched_at,
        desde_cache=True,
    )


def _catalog_url() -> str:
    return (
        f"{PORTAL_URL}/contenedor.jsp?seccion=bome.jsp&language=es&codResi=1"
        "&layout=contenedor.jsp&codAdirecto=15"
    )


# --------------------------------------------------------------------------- client


def guardia_portal_antiguo() -> GuardiaSitio:
    """The old portal's site guard, persisted in the data folder when there is one."""
    try:
        path: Path | None = data_dir()[0] / FICHERO_ESTADO_MELILLA
    except BomeError as exc:
        print(
            f"bome-navaja: no data folder for the old portal's site guard ({exc}); "
            "its state is kept in memory for this process",
            file=sys.stderr,
        )
        path = None
    return GuardiaSitio(path, sitio=SITIO)


def _default_catalog_path() -> Path | None:
    try:
        return data_dir()[0] / FICHERO_CATALOGO
    except BomeError:
        return None


_DEFAULT: Any = object()


class PortalAntiguo:
    """Client of the old BOME portal on melilla.es.

    Every request goes through a :class:`~bome_navaja.client.BomeClient`
    configured for the portal (browser User-Agent, guard check and record,
    polite delay with jitter, HTTP error mapping), with this site's own
    ``guard`` (default :func:`guardia_portal_antiguo`). ``cache_path`` is the
    catalog cache (default ``data_dir()/catalogo_portal_antiguo.json``;
    ``None`` keeps it in memory only).
    """

    def __init__(
        self,
        *,
        guard: GuardiaSitio | None = None,
        cache_path: str | os.PathLike[str] | None = _DEFAULT,
        transport: httpx.BaseTransport | None = None,
        polite_delay: float = DEFAULT_POLITE_DELAY,
        jitter: float = DEFAULT_JITTER,
        rng: random.Random | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.guard = guard if guard is not None else guardia_portal_antiguo()
        if cache_path is _DEFAULT:
            cache_path = _default_catalog_path()
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self._http = BomeClient(
            base_url=PORTAL_URL,
            timeout=timeout,
            transport=transport,
            polite_delay=polite_delay,
            jitter=jitter,
            rng=rng,
            guard=self.guard,
        )
        self._catalogo: CatalogoAntiguo | None = None
        self._lock = threading.Lock()
        self._warned = False

    # ------------------------------------------------------------------ lifecycle

    @property
    def polite_delay(self) -> float:
        return self._http.polite_delay

    @property
    def jitter(self) -> float:
        return self._http.jitter

    def close(self) -> None:
        self._http.close()

    @property
    def closed(self) -> bool:
        return self._http.closed

    def __enter__(self) -> PortalAntiguo:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ transport

    def _page(
        self,
        method: str,
        params: tuple[tuple[str, str], ...],
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """One portal page (``contenedor.jsp``), size-capped and decoded as latin-1."""
        body, response = self._http.fetch_bytes(
            method,  # type: ignore[arg-type]
            "/contenedor.jsp",
            params=params,
            content=content,
            headers=headers,
            max_bytes=MAX_RESPUESTA_BYTES,
        )
        if not body.strip():
            url = str(response.url)
            raise BomeNotFoundError(f"empty page: {url}", status=response.status_code, url=url)
        return body.decode("latin-1")

    # ------------------------------------------------------------------ catalog

    def catalogo(self, *, refrescar: bool = False) -> CatalogoAntiguo:
        """The whole catalog: from memory, else the cache file, else one GET.

        The portal is frozen, so the cache is reused forever unless
        ``refrescar``. An unreadable or corrupt cache is refetched and
        rewritten; an unwritable one leaves the catalog in memory only.
        """
        with self._lock:
            if not refrescar:
                if self._catalogo is None:
                    self._catalogo = self._read_cache()
                if self._catalogo is not None:
                    return self._catalogo
            text = self._page(
                "GET",
                (("seccion", "bome.jsp"), ("language", "es"), ("codResi", "1"), ("layout", "contenedor.jsp"), ("codAdirecto", "15")),
            )
            boletines, avisos = parse_catalogo(text)
            self._catalogo = CatalogoAntiguo(tuple(boletines), tuple(avisos), _now_iso())
            self._write_cache(self._catalogo)
            return self._catalogo

    def _cached_catalog(self) -> CatalogoAntiguo | None:
        """The catalog if it is already in memory or on disk; never fetches."""
        with self._lock:
            if self._catalogo is None:
                self._catalogo = self._read_cache()
            return self._catalogo

    def _read_cache(self) -> CatalogoAntiguo | None:
        if self.cache_path is None:
            return None
        try:
            return _catalog_from_json(self.cache_path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self._warn(f"cannot be used ({exc}); fetching the catalog again")
            return None

    def _write_cache(self, catalogo: CatalogoAntiguo) -> None:
        if self.cache_path is None:
            return
        temporary = self.cache_path.with_name(
            f".{self.cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(_catalog_to_json(catalogo), "utf-8")
            os.replace(temporary, self.cache_path)
        except OSError as exc:
            self._warn(f"cannot be written ({exc}); the catalog stays in memory for this process")
            with contextlib.suppress(OSError):
                temporary.unlink()

    def _warn(self, problem: str) -> None:
        if not self._warned:
            self._warned = True
            print(f"bome-navaja: the old portal's catalog cache {self.cache_path} {problem}", file=sys.stderr)

    # ------------------------------------------------------------------ search

    def buscar(self, texto: str) -> list[ArticuloAntiguo]:
        """Articles whose text matches ``texto`` (all of them, in one POST).

        Raises :class:`~bome_navaja.search.BusquedaInvalidaError` before any
        request when the text is shorter than 3 characters, longer than 200
        or not latin-1, and :class:`BomeDocumentTooLargeError` when the
        answer passes :data:`MAX_RESPUESTA_BYTES`.
        """
        clean = texto_busqueda_valido(texto)
        body = f"textobome={quote_plus(clean, encoding='latin-1')}".encode("ascii")
        try:
            text = self._page(
                "POST",
                (("seccion", "busqueda_bome.jsp"), *_COMMON_PARAMS),
                content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except BomeDocumentTooLargeError as exc:
            limit = f"{exc.limit / (1024 * 1024):g} MB" if exc.limit >= 1024 * 1024 else f"{exc.limit} bytes"
            raise BomeDocumentTooLargeError(
                f"la búsqueda de {clean!r} en el portal antiguo devuelve más de {limit} "
                "(el portal no pagina los resultados); usa un texto más concreto",
                size=exc.size,
                limit=exc.limit,
            ) from exc
        return parse_busqueda(text)

    # ------------------------------------------------------------------ ficha

    @staticmethod
    def _valid_dboid(dboid: int | str) -> int:
        if isinstance(dboid, bool):
            raise ValueError(f"dboid must be a positive integer, got {dboid!r}")
        if isinstance(dboid, str) and dboid.strip().isdigit():
            dboid = int(dboid.strip())
        if not isinstance(dboid, int) or dboid <= 0:
            raise ValueError(f"dboid must be a positive integer, got {dboid!r}")
        return dboid

    def ficha(self, dboid: int | str) -> FichaAntigua:
        """The ficha of the bulletin with portal id ``dboid``.

        When the catalog is already in memory or cached on disk its entry
        supplies the numbering (suffix, extraordinary flag); the catalog is
        never downloaded for this.
        """
        number = self._valid_dboid(dboid)
        text = self._page("GET", (("seccion", "ficha_bome.jsp"), ("dboidboletin", str(number)), *_COMMON_PARAMS))
        cached = self._cached_catalog()
        hint = cached.por_dboid(number) if cached is not None else None
        return parse_ficha(text, number, boletin=hint)

    def ficha_por_cve(self, cve: str) -> FichaAntigua:
        """The ficha of a bulletin by identifier, found through the catalog.

        Raises :class:`BomeNotFoundError` when the catalog does not list it
        and :class:`BoletinAmbiguoError` when a pre-2014 identifier matches
        several bulletins (use :meth:`ficha` with one of their dboids).
        """
        wanted = normalizar_cve(cve)
        candidatos = self.catalogo().por_cve(wanted)
        if not candidatos:
            raise BomeNotFoundError(
                f"el catálogo del portal antiguo (1985-2021) no tiene el boletín {wanted}",
                status=None,
                url=_catalog_url(),
            )
        if len(candidatos) > 1:
            detail = "; ".join(
                f"dboid {b.dboid} del {b.fecha.isoformat()}" + (f" ({b.sufijo})" if b.sufijo else "")
                for b in candidatos
            )
            raise BoletinAmbiguoError(
                f"{wanted} corresponde a {len(candidatos)} boletines del portal antiguo: {detail}; "
                "pide la ficha por su dboid",
                candidatos=tuple(candidatos),
            )
        return self.ficha(candidatos[0].dboid)

    # ------------------------------------------------------------------ PDFs

    def pdf(self, url: str, *, max_bytes: int | None = MAX_PDF_BYTES) -> bytes:
        """Bytes of a ``mandar.php`` PDF (whitelisted by :func:`pdf_url_valida`)."""
        target = pdf_url_valida(url)
        content, response = self._http.fetch_bytes("GET", target, max_bytes=max_bytes)
        if not content.lstrip()[:4] == b"%PDF":
            content_type = response.headers.get("content-type")
            raise BomeParseError(f"{target} did not return a PDF (content-type {content_type!r})")
        return content


__all__ = [
    "DEFAULT_JITTER",
    "DEFAULT_POLITE_DELAY",
    "FICHERO_CATALOGO",
    "MAX_RESPUESTA_BYTES",
    "ORIGEN",
    "PORTAL_URL",
    "ArticuloAntiguo",
    "BoletinAmbiguoError",
    "BoletinAntiguo",
    "CatalogoAntiguo",
    "FichaAntigua",
    "PaginaAntigua",
    "PortalAntiguo",
    "UrlPdfInvalidaError",
    "guardia_portal_antiguo",
    "normalizar_cve",
    "parse_busqueda",
    "parse_catalogo",
    "parse_ficha",
    "pdf_url_valida",
    "texto_busqueda_valido",
    "url_ficha",
]
