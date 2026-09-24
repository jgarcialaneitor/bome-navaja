"""PDF download with a safe local cache, and paginated text reading.

Public entry points, designed to be exposed 1:1 as MCP tools (task 6):

* :func:`descargar_pdf` — download (or reuse) the PDF of any CVE.
* :func:`leer_pdf` — text of any PDF, page by page, with a cursor.
* :func:`leer_articulo` — article text from its web page (``#pagina-N``
  blocks), falling back to its PDF for old articles without HTML text.
* :func:`localizar_articulo` — the verified article page of an article CVE
  (``BOME-AX`` resolved without the site's buggy CVE resolver, see below).
* :func:`leer_boletin` — the whole bulletin PDF text plus bulletin metadata.
* :func:`descargar_pdf_antiguo` / :func:`leer_pdf_antiguo` — the same for a
  PDF of the old portal on melilla.es, given by its whitelisted ``mandar.php``
  URL (see :func:`bome_navaja.antiguo.pdf_url_valida`) and fetched through the
  portal client (its own site guard).

Cursor contract, shared by the three readers (:func:`paginar`):

* A call returns whole pages until adding the next page would exceed
  ``max_caracteres`` (clamped to 1000..100000), and always at least one page.
* If the page at the cursor alone does not fit, only ``max_caracteres``
  characters of it are returned (``cortada: true``) and the cursor points
  inside that page (``desde_caracter``).
* ``siguiente`` is the ``{desde_pagina, desde_caracter}`` to pass on the next
  call, or ``None`` when the end was reached. Nothing is ever dropped:
  concatenating the chunks rebuilds every page exactly.
* ``completo`` is true only when one call returned the whole document.
* ``numero`` is the 1-based position in the document (the cursor unit);
  ``pagina_bome`` is the printed bulletin page when known (HTML pages).

Site facts (verified live 2026-09-23): an ordinary bulletin PDF is ~4 MB /
37 pages (BOME-B-2026-6416), its sumario ~250 KB / 2 pages, an article
~66-265 KB / 1-4 pages, a page PDF ~66 KB / 1 page, all with a text layer.
PDFs of 2014-2016 bulletins and articles answer 404
(BOME-B-2014-5092, BOME-B-2015-5272, BOME-B-2016-5397, BOME-A-2014-2).

Resolver bug (verified live 2026-09-24): ``/buscar-cve`` drops the X of
extraordinary article and page CVEs (``BOME-AX-2019-103`` →
``/bome/BOME-B-2019-5625/articulo/103``, an ordinary article). So an ``AX`` is
located through the local index when it knows it, else the resolver only when it
answers an extraordinary bulletin, else a binary search over the year's
extraordinary bulletins; and every article fetched for an article CVE must show
that very CVE (:class:`ArticuloDistintoError` otherwise).
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import pypdf

from .client import BomeClient
from .cve import Cve, CveKind, InvalidCveError, parse_cve, pdf_url
from .models import (
    Article,
    BomeError,
    BomeNotFoundError,
    BomeParseError,
    BomeStorageError,
    JsonModel,
)
from .paths import pdf_dir

MAX_PDF_BYTES = 100 * 1024 * 1024
"""Downloads above this abort with :class:`BomeDocumentTooLargeError`."""

DEFAULT_MAX_CARACTERES = 20_000
MIN_MAX_CARACTERES = 1_000
MAX_MAX_CARACTERES = 100_000

_ARTICLE_PATH = re.compile(r"/bome/(BOME-BX?-\d{4}-\d+)/articulo/(\d+)/?$")
_OLD_PDF_PATH = re.compile(r"https://www\.melilla\.es/mandar\.php/n/(\d+)/(\d+)/([A-Za-z0-9_]+)\.pdf")

ORIGEN_BOMEMELILLA = "bomemelilla.es"
ORIGEN_ANTIGUO = "melilla.es"

Fuente = Literal["pdf", "html", "ninguna"]


class LecturaInvalidaError(BomeError, ValueError):
    """Invalid reading arguments (CVE kind, number, cursor or budget)."""


class ArticuloDistintoError(BomeNotFoundError):
    """The page fetched for an article CVE shows another article: never returned."""


class ArticuloNoLocalizadoError(BomeNotFoundError):
    """An extraordinary article (``BOME-AX``) was not found in its year's bulletins."""


BoletinDeArticulo = Callable[[Cve], Cve | None]
"""Optional lookup article CVE → bulletin CVE (e.g. the local index), ``None`` if unknown."""

BUSQUEDA_EXTRA_MARGEN = 2
"""Bulletin pages the AX search may fetch beyond ``ceil(log2(#BX))``."""

BUSQUEDA_EXTRA_HUECO = 4
"""Bulletin pages the AX search may fetch, after its binary search, between the
two listed neighbours of the number (a 200 page costs the site guard nothing)."""

MAX_INTENTOS_ARTICULO_EXTRA = 2
"""Article pages one AX lookup may fetch: each wrong guess is a 404 that the
site guard counts. Its budget is 3 errors per 10 minutes, so a failed lookup
leaves room for the explicit ``cve='BOME-BX-…', numero=`` call its error
suggests."""

_MAX_BOLETINES_EN_ERROR = 10


# --------------------------------------------------------------------------- models


@dataclass(frozen=True, slots=True)
class Cursor(JsonModel):
    """Where the next reading call starts."""

    desde_pagina: int
    desde_caracter: int = 0


@dataclass(frozen=True, slots=True)
class PaginaTexto(JsonModel):
    """Text of one document page (or of a slice of it, see ``cortada``)."""

    numero: int
    """1-based position of the page in the document."""
    texto: str
    sin_texto: bool
    """True when the page has no extractable text (e.g. a scanned page)."""
    pagina_bome: int | None = None
    """Printed bulletin page number, when known."""
    desde_caracter: int = 0
    """Offset of ``texto`` inside the page (non-zero for a continued page)."""
    cortada: bool = False
    """True when the page continues in the next call."""


@dataclass(frozen=True, slots=True)
class TextoPaginado(JsonModel):
    """One chunk of a document's text, with the cursor to continue."""

    cve: str | None
    """``None`` for a PDF of the old portal (it is identified by ``url``)."""
    fuente: Fuente
    url: str
    total_paginas: int
    max_caracteres: int
    paginas: tuple[PaginaTexto, ...]
    siguiente: Cursor | None
    completo: bool
    aviso: str | None = None
    metadatos: dict[str, Any] | None = None
    origen: str = ORIGEN_BOMEMELILLA
    """Site the document comes from: ``bomemelilla.es`` or ``melilla.es`` (old portal)."""


@dataclass(frozen=True, slots=True)
class DescargaPdf(JsonModel):
    """A PDF stored in the local cache."""

    cve: str | None
    """Canonical CVE; ``None`` for a PDF of the old portal (identified by ``url``)."""
    ruta: str
    tamano_bytes: int
    sha256: str
    total_paginas: int
    cache_hit: bool
    url: str
    motivo_directorio: str
    """Why this directory was chosen (``BOME_NAVAJA_PDF_DIR``, XDG, ...)."""
    origen: str = ORIGEN_BOMEMELILLA


# --------------------------------------------------------------------------- pagination


def _int_arg(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LecturaInvalidaError(f"'{name}' must be an integer, got {value!r}")
    return value


def _budget(max_caracteres: Any) -> int:
    value = _int_arg(max_caracteres, "max_caracteres")
    return max(MIN_MAX_CARACTERES, min(value, MAX_MAX_CARACTERES))


def paginar(
    textos: Sequence[str],
    *,
    desde_pagina: int,
    desde_caracter: int,
    max_caracteres: int,
    paginas_bome: Sequence[int | None] | None = None,
) -> tuple[tuple[PaginaTexto, ...], Cursor | None]:
    """Cut one chunk out of per-page texts following the module's cursor contract.

    ``max_caracteres`` is used as given (callers clamp it) but must be
    positive, otherwise the cursor could never advance. Returns the pages of
    the chunk and the cursor of the next one (``None`` at the end).
    """
    if _int_arg(max_caracteres, "max_caracteres") < 1:
        raise LecturaInvalidaError(f"'max_caracteres' must be positive, got {max_caracteres}")
    start_page = _int_arg(desde_pagina, "desde_pagina")
    offset = _int_arg(desde_caracter, "desde_caracter")
    total = len(textos)
    if total == 0:
        if (start_page, offset) != (1, 0):
            raise LecturaInvalidaError("the document has no pages; start at desde_pagina=1")
        return (), None
    if not 1 <= start_page <= total:
        raise LecturaInvalidaError(f"'desde_pagina' must be between 1 and {total}, got {start_page}")
    first = textos[start_page - 1]
    if offset < 0 or (offset > 0 and offset >= len(first)):
        raise LecturaInvalidaError(
            f"'desde_caracter' must be between 0 and {max(len(first) - 1, 0)} "
            f"for page {start_page}, got {offset}"
        )

    def printed(index: int) -> int | None:
        return paginas_bome[index] if paginas_bome is not None else None

    rest = first[offset:]
    if len(rest) > max_caracteres:
        piece = PaginaTexto(
            numero=start_page,
            texto=rest[:max_caracteres],
            sin_texto=False,
            pagina_bome=printed(start_page - 1),
            desde_caracter=offset,
            cortada=True,
        )
        return (piece,), Cursor(start_page, offset + max_caracteres)
    pages = [
        PaginaTexto(
            numero=start_page,
            texto=rest,
            sin_texto=not first.strip(),
            pagina_bome=printed(start_page - 1),
            desde_caracter=offset,
        )
    ]
    used = len(rest)
    for index in range(start_page, total):
        text = textos[index]
        if used + len(text) > max_caracteres:
            return tuple(pages), Cursor(index + 1, 0)
        pages.append(
            PaginaTexto(
                numero=index + 1,
                texto=text,
                sin_texto=not text.strip(),
                pagina_bome=printed(index),
            )
        )
        used += len(text)
    return tuple(pages), None


def _chunk(
    *,
    cve: str | None,
    fuente: Fuente,
    url: str,
    textos: Sequence[str],
    desde_pagina: int,
    desde_caracter: int,
    budget: int,
    metadatos: dict[str, Any],
    paginas_bome: Sequence[int | None] | None = None,
    aviso: str | None = None,
    origen: str = ORIGEN_BOMEMELILLA,
) -> TextoPaginado:
    pages, cursor = paginar(
        textos,
        desde_pagina=desde_pagina,
        desde_caracter=desde_caracter,
        max_caracteres=budget,
        paginas_bome=paginas_bome,
    )
    empty = [page.numero for page in pages if page.sin_texto]
    notes = [aviso] if aviso else []
    if empty:
        notes.append(
            f"pages {', '.join(map(str, empty))} have no extractable text "
            "(probably scanned images); check the PDF itself"
        )
    return TextoPaginado(
        cve=cve,
        fuente=fuente,
        url=url,
        total_paginas=len(textos),
        max_caracteres=budget,
        paginas=pages,
        siguiente=cursor,
        completo=desde_pagina == 1 and desde_caracter == 0 and cursor is None and bool(textos),
        aviso=" ".join(notes) or None,
        metadatos=metadatos,
        origen=origen,
    )


def _no_text(
    *,
    cve: str,
    url: str,
    budget: int,
    aviso: str,
    metadatos: dict[str, Any],
    desde_pagina: int,
    desde_caracter: int,
) -> TextoPaginado:
    paginar([], desde_pagina=desde_pagina, desde_caracter=desde_caracter, max_caracteres=budget)
    return TextoPaginado(
        cve=cve,
        fuente="ninguna",
        url=url,
        total_paginas=0,
        max_caracteres=budget,
        paginas=(),
        siguiente=None,
        completo=False,
        aviso=aviso,
        metadatos=metadatos,
    )


# --------------------------------------------------------------------------- PDF cache


def _pdf_info(data: bytes | Path) -> int:
    """Page count, validating that pypdf can open the document."""
    try:
        source = data if isinstance(data, Path) else io.BytesIO(data)
        return len(pypdf.PdfReader(source).pages)
    except Exception as exc:  # pypdf raises many unrelated types on bad input
        raise BomeParseError(f"unreadable PDF: {exc}") from exc


def _cached(target: Path) -> tuple[bytes, int] | None:
    """Bytes and page count of a valid cached PDF, or ``None``."""
    try:
        if not target.is_file():
            return None
        data = target.read_bytes()
    except OSError:
        return None
    if not data.lstrip()[:4] == b"%PDF":
        return None
    try:
        return data, _pdf_info(data)
    except BomeParseError:
        return None


def _atomic_write(target: Path, data: bytes) -> None:
    """Write through a temp file in the same directory, then ``os.replace``."""
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException as exc:
        # Any failure, including KeyboardInterrupt, must not leave a .part file.
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise BomeStorageError(f"cannot write {target}: {exc}", path=str(target)) from exc
        raise


def descargar_pdf(client: BomeClient, cve: str | Cve, *, refrescar: bool = False) -> DescargaPdf:
    """Download the PDF of any CVE (B, BX, A, AX, S, SX, P, PX) into the local cache.

    The file is ``pdf_dir()/{canonical CVE}.pdf``: its name comes only from
    the parsed CVE, so no caller or server text reaches the path, and the
    only way to move it is ``BOME_NAVAJA_PDF_DIR``. A valid cached copy is
    reused unless ``refrescar``. New bytes are validated (``%PDF`` magic,
    readable by pypdf, at most :data:`MAX_PDF_BYTES`, else
    :class:`BomeDocumentTooLargeError`) before atomically replacing the file;
    a failed refresh keeps the old copy. Local file errors raise
    :class:`BomeStorageError` (``error_code`` ``"error_almacenamiento"``).
    """
    canonical = parse_cve(cve)
    directory, reason = pdf_dir()
    target = directory / f"{canonical}.pdf"
    source_url = pdf_url(canonical, base_url=client.base_url)
    data, pages, hit = _fetch_into_cache(
        target, lambda: client.download(canonical, max_bytes=MAX_PDF_BYTES), refrescar=refrescar
    )
    return _descarga(str(canonical), target, data, pages, hit, source_url, reason)


def _fetch_into_cache(target: Path, fetch: Callable[[], bytes], *, refrescar: bool) -> tuple[bytes, int, bool]:
    """Bytes, page count and cache hit of ``target``: reused when valid, else fetched and stored."""
    if not refrescar:
        cached = _cached(target)
        if cached is not None:
            return (*cached, True)
    directory = target.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BomeStorageError(f"cannot create {directory}: {exc}", path=str(directory)) from exc
    data = fetch()
    pages = _pdf_info(data)
    _atomic_write(target, data)
    return data, pages, False


def _descarga(
    cve: str | None,
    target: Path,
    data: bytes,
    pages: int,
    hit: bool,
    url: str,
    reason: str,
    origen: str = ORIGEN_BOMEMELILLA,
) -> DescargaPdf:
    return DescargaPdf(
        cve=cve,
        ruta=str(target),
        tamano_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        total_paginas=pages,
        cache_hit=hit,
        url=url,
        motivo_directorio=reason,
        origen=origen,
    )


# --------------------------------------------------------------------------- old portal PDFs


class PortalPdf(Protocol):
    """What the old-portal readers need: :meth:`bome_navaja.antiguo.PortalAntiguo.pdf`."""

    def pdf(self, url: str, *, max_bytes: int | None = ...) -> bytes: ...


def _old_pdf_url(url: str) -> tuple[str, str]:
    """The validated https URL of an old-portal PDF and its cache file name.

    Raises :class:`~bome_navaja.antiguo.UrlPdfInvalidaError` for anything
    outside the ``mandar.php`` whitelist.
    """
    from .antiguo import pdf_url_valida  # antiguo imports this module

    valid = pdf_url_valida(url)
    match = _OLD_PDF_PATH.fullmatch(valid)
    if match is None:  # pdf_url_valida only returns this shape
        raise BomeParseError(f"unexpected old-portal PDF URL {valid!r}")
    first, second, name = match.groups()
    return valid, f"melilla-{first}-{second}-{name}.pdf"


def nombre_pdf_antiguo(url: str) -> str:
    """Cache file name of an old-portal PDF, derived only from its validated URL path.

    ``https://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf`` →
    ``melilla-9-4914-5302_73.pdf``: digits, letters and ``_`` only, so no
    caller text can reach the path, and it never collides with a CVE name.
    """
    return _old_pdf_url(url)[1]


def descargar_pdf_antiguo(portal: PortalPdf, url: str, *, refrescar: bool = False) -> DescargaPdf:
    """Download (or reuse) a PDF of the old portal into the PDF cache.

    Same cache contract as :func:`descargar_pdf`; the file is
    ``pdf_dir()/``:func:`nombre_pdf_antiguo`, the request goes through
    ``portal`` (its guard), and the result has ``cve=None`` and
    ``origen="melilla.es"``.
    """
    valid, name = _old_pdf_url(url)
    directory, reason = pdf_dir()
    target = directory / name
    data, pages, hit = _fetch_into_cache(
        target, lambda: portal.pdf(valid, max_bytes=MAX_PDF_BYTES), refrescar=refrescar
    )
    return _descarga(None, target, data, pages, hit, valid, reason, ORIGEN_ANTIGUO)


@lru_cache(maxsize=8)
def _page_texts_cached(path: str, _mtime_ns: int, _size: int) -> tuple[str, ...]:
    try:
        reader = pypdf.PdfReader(path)
        return tuple(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf raises many unrelated types on bad input
        raise BomeParseError(f"cannot extract text from {path}: {exc}") from exc


def _page_texts(path: Path) -> tuple[str, ...]:
    """Per-page text of a cached PDF; memoised on (path, mtime, size)."""
    try:
        stat = path.stat()
    except OSError as exc:
        raise BomeStorageError(f"cannot read {path}: {exc}", path=str(path)) from exc
    return _page_texts_cached(str(path), stat.st_mtime_ns, stat.st_size)


# --------------------------------------------------------------------------- readers


def leer_pdf(
    client: BomeClient,
    cve: str | Cve,
    *,
    desde_pagina: int = 1,
    desde_caracter: int = 0,
    max_caracteres: int = DEFAULT_MAX_CARACTERES,
) -> TextoPaginado:
    """Text of the PDF of any CVE, page by page (downloaded or from cache).

    Follows the module's cursor contract. Pages without extractable text are
    flagged ``sin_texto`` and reported in ``aviso``.
    """
    budget = _budget(max_caracteres)
    download = descargar_pdf(client, cve)
    texts = _page_texts(Path(download.ruta))
    return _chunk(
        cve=download.cve,
        fuente="pdf",
        url=download.url,
        textos=texts,
        desde_pagina=desde_pagina,
        desde_caracter=desde_caracter,
        budget=budget,
        metadatos={
            "ruta": download.ruta,
            "tamano_bytes": download.tamano_bytes,
            "sha256": download.sha256,
            "cache_hit": download.cache_hit,
        },
    )


def leer_pdf_antiguo(
    portal: PortalPdf,
    url: str,
    *,
    desde_pagina: int = 1,
    desde_caracter: int = 0,
    max_caracteres: int = DEFAULT_MAX_CARACTERES,
) -> TextoPaginado:
    """Text of an old-portal PDF, page by page (see :func:`leer_pdf`)."""
    budget = _budget(max_caracteres)
    download = descargar_pdf_antiguo(portal, url)
    return _chunk(
        cve=None,
        fuente="pdf",
        url=download.url,
        textos=_page_texts(Path(download.ruta)),
        desde_pagina=desde_pagina,
        desde_caracter=desde_caracter,
        budget=budget,
        metadatos={
            "ruta": download.ruta,
            "tamano_bytes": download.tamano_bytes,
            "sha256": download.sha256,
            "cache_hit": download.cache_hit,
        },
        origen=ORIGEN_ANTIGUO,
    )


def localizar_articulo(
    client: BomeClient,
    cve_boletin_o_articulo: str | Cve,
    numero: int | None = None,
    *,
    boletin_de_articulo: BoletinDeArticulo | None = None,
) -> Article:
    """The article page of an article CVE, or of a bulletin CVE plus ``numero``.

    ``BOME-A`` goes through the site's CVE resolver. ``BOME-AX`` never trusts
    it (it sends those to ordinary bulletins, see the module docstring): it
    asks ``boletin_de_articulo`` first (optional, e.g. the local index), then
    the resolver only if it answers an extraordinary bulletin of the same
    year, then the candidates of :func:`_buscar_boletin_extra`, fetching at
    most ``MAX_INTENTOS_ARTICULO_EXTRA`` article pages in all (shortcuts
    included: each wrong guess is a 404 the site guard counts). For an
    article CVE the fetched page must show that very CVE, else
    :class:`ArticuloDistintoError`; an ``AX`` not found raises
    :class:`ArticuloNoLocalizadoError`, naming any candidate left untried.
    """
    cve = parse_cve(cve_boletin_o_articulo)
    if cve.kind.is_bulletin:
        if numero is None:
            raise LecturaInvalidaError("give 'numero' (the article number) with a bulletin CVE")
        number = _int_arg(numero, "numero")
        if number < 1:
            raise LecturaInvalidaError(f"'numero' must be positive, got {number}")
        return client.article(cve, number)
    if cve.kind not in (CveKind.ARTICLE, CveKind.EXTRA_ARTICLE):
        raise LecturaInvalidaError(
            f"{cve} is not an article: give an article CVE (BOME-A/BOME-AX) or a bulletin "
            "CVE (BOME-B/BOME-BX) plus 'numero'; use leer_pdf for sumario and page CVEs"
        )
    if numero is not None:
        raise LecturaInvalidaError("'numero' is implied by an article CVE; leave it empty")
    if cve.kind is CveKind.EXTRA_ARTICLE:
        return _extra_article(client, cve, boletin_de_articulo)
    # Article numbers are independent of bulletin numbers, so the bulletin of
    # an ordinary article is learnt from the site's resolver (302 to its page).
    location = client.resolve_cve(cve, confirm=False)
    match = _ARTICLE_PATH.search(urlsplit(location).path)
    if match is None:
        raise BomeParseError(f"{cve} resolved to {location}, not an article page")
    return _verified_article(client, parse_cve(match.group(1)), int(match.group(2)), cve)


def _verified_article(client: BomeClient, bulletin: Cve, number: int, expected: Cve) -> Article:
    """Article ``number`` of ``bulletin``, which must be ``expected`` itself."""
    article = client.article(bulletin, number)
    if article.cve != str(expected):
        url = f"{client.base_url}/bome/{bulletin}/articulo/{number}"
        raise ArticuloDistintoError(
            f"the page {url} shows {article.cve}, not {expected}; that other article is not "
            "returned",
            status=None,
            url=url,
        )
    return article


def _same_year_extra_bulletin(bulletin: Cve | None, cve: Cve) -> bool:
    return (
        bulletin is not None
        and bulletin.kind is CveKind.EXTRA_BULLETIN
        and bulletin.year == cve.year
    )


def _indexed_bulletin(cve: Cve, lookup: BoletinDeArticulo | None) -> Cve | None:
    if lookup is None:
        return None
    try:
        bulletin = lookup(cve)
    except BomeError:  # the lookup is an optional shortcut, never a reason to fail
        return None
    return bulletin if _same_year_extra_bulletin(bulletin, cve) else None


def _resolver_bulletin(client: BomeClient, cve: Cve) -> Cve | None:
    """The resolver's bulletin for an ``AX``, only when it is extraordinary (the site's bug)."""
    try:
        location = client.resolve_cve(cve, confirm=False)
    except BomeNotFoundError:  # includes ResolucionIncoherenteError: the known bug
        return None
    match = _ARTICLE_PATH.search(urlsplit(location).path)
    if match is None or int(match.group(2)) != cve.number:
        return None
    bulletin = parse_cve(match.group(1))
    return bulletin if _same_year_extra_bulletin(bulletin, cve) else None


def _extra_article(client: BomeClient, cve: Cve, lookup: BoletinDeArticulo | None) -> Article:
    """Locate an ``AX`` article with at most ``MAX_INTENTOS_ARTICULO_EXTRA`` article pages.

    The shortcuts (index, resolver) that fetch an article page count toward
    the cap; the search's candidates follow in its order. Candidates left
    untried by the cap are named in the error so the caller can go on.
    """
    tried: list[Cve] = []

    def attempt(bulletin: Cve | None) -> Article | None:
        if bulletin is None or bulletin in tried or len(tried) >= MAX_INTENTOS_ARTICULO_EXTRA:
            return None
        tried.append(bulletin)
        try:
            return _verified_article(client, bulletin, cve.number, cve)
        except BomeNotFoundError:  # includes ArticuloDistintoError: not in this bulletin
            return None

    for shortcut in (lambda: _indexed_bulletin(cve, lookup), lambda: _resolver_bulletin(client, cve)):
        if (article := attempt(shortcut())) is not None:
            return article
    search = _buscar_boletin_extra(client, cve)
    for candidate in search.candidatos:
        if (article := attempt(candidate)) is not None:
            return article
    raise search.error(cve, [c for c in search.candidatos if c not in tried])


def _cve_list(bulletins: Sequence[Cve]) -> str:
    shown = ", ".join(str(bulletin) for bulletin in bulletins[:_MAX_BOLETINES_EN_ERROR])
    rest = len(bulletins) - _MAX_BOLETINES_EN_ERROR
    return f"{shown} and {rest} more" if rest > 0 else shown


@dataclass(frozen=True, slots=True)
class _BusquedaExtra:
    candidatos: tuple[Cve, ...]
    boletines: int
    paginas: int
    agotada: bool
    url: str
    hueco: tuple[Cve, ...] = ()
    """The bulletins between the listed neighbours when the gap budget ran out."""
    sin_revisar: int = 0
    """How many of ``hueco`` were never fetched."""

    def error(self, cve: Cve, untried: Sequence[Cve] = ()) -> ArticuloNoLocalizadoError:
        checked = (
            f"{self.paginas} bulletin pages checked"
            f"{', search budget exhausted' if self.agotada else ''}"
        )
        example = f"cve='BOME-BX-{cve.year}-N', numero={cve.number}"
        if self.boletines == 0:
            where = f"the site calendar lists no extraordinary bulletin in {cve.year}"
        elif untried:
            where = (
                f"the lookup stopped after {MAX_INTENTOS_ARTICULO_EXTRA} article pages (each "
                "wrong guess is an HTTP error the site guard counts), with candidate bulletins "
                f"left untried: {_cve_list(untried)} ({checked})"
            )
            example = f"cve='{untried[0]}', numero={cve.number}" + (
                ", then the other untried bulletins" if len(untried) > 1 else ""
            )
        elif self.sin_revisar:
            where = (
                f"it can only be in the {len(self.hueco)} extraordinary bulletins from "
                f"{self.hueco[0]} to {self.hueco[-1]}, but the gap page budget ran out with "
                f"{self.sin_revisar} bulletins unchecked, so no article page was guessed "
                f"({checked})"
            )
        else:
            where = f"it is not in the {self.boletines} extraordinary bulletins of {cve.year} ({checked})"
        return ArticuloNoLocalizadoError(
            f"{cve} could not be located: {where}. The site's CVE resolver cannot be used for "
            f"it (it sends extraordinary articles to ordinary bulletins). Give the bulletin CVE "
            f"plus 'numero' instead, calling leer_articulo with {example} "
            "(find the bulletin with listar_bomes, ver_bome or buscar_bomes)",
            status=None,
            url=self.url,
        )


class _BudgetExhausted(Exception):
    pass


def _buscar_boletin_extra(client: BomeClient, cve: Cve) -> _BusquedaExtra:
    """Candidate extraordinary bulletins for an ``AX`` CVE, in the order to try them.

    Extraordinary article numbers are consecutive within a year and
    extraordinary bulletins are numbered in publication order, so the
    bulletins of the year (from the calendar, sorted by number) cover
    increasing article-number ranges. Each bulletin page fetched gives its
    listed range min..max; a number inside it belongs to that bulletin (pages
    may hide articles).

    1. Binary search: a page listing nothing is skipped rightwards; at most
       ``ceil(log2(#BX)) + BUSQUEDA_EXTRA_MARGEN`` pages are fetched.
    2. Without a hit, the gap is the bulletins strictly between the last
       listed range below the number and the first listed range above it
       (the start/end of the year when absent). Gap pages never fetched are
       fetched now, nearest to the lower neighbour first, at most
       ``BUSQUEDA_EXTRA_HUECO`` of them (a 200 page costs the site guard
       nothing, a wrong article guess costs a 404): a range containing the
       number is the answer; any other listed range becomes the new, closer
       neighbour, so the gap shrinks and the pages it leaves are dropped.
       If pages remain unfetched when that budget runs out, no candidate is
       returned (guessing blind would only spend the guard's error budget).
    3. Candidates: the gap bulletins whose page lists nothing (nearest to
       the lower neighbour first), then the lower neighbour, then the upper
       one (a hidden article at a bulletin edge).
    """
    year, target = cve.year, cve.number
    start, end = date(year, 1, 1), date(year, 12, 31)
    url = f"{client.base_url}/api/bomes/calendar?start={start.isoformat()}&end={end.isoformat()}"
    found: set[Cve] = set()
    for ref in client.calendar(start, end):
        try:
            bulletin = parse_cve(ref.cve)
        except InvalidCveError:
            continue
        if _same_year_extra_bulletin(bulletin, cve):
            found.add(bulletin)
    bulletins = sorted(found, key=lambda bulletin: bulletin.number)
    if not bulletins:
        return _BusquedaExtra((), 0, 0, False, url)
    budget = math.ceil(math.log2(len(bulletins))) + BUSQUEDA_EXTRA_MARGEN
    spans: dict[int, tuple[int, int] | None] = {}
    exhausted = False

    def fetch(index: int) -> tuple[int, int] | None:
        bulletin = bulletins[index]
        numbers = [
            ref.number
            for ref in client.bulletin(bulletin).articles
            if ref.cve == str(bulletin.article_cve(ref.number))
        ]
        spans[index] = (min(numbers), max(numbers)) if numbers else None
        return spans[index]

    def span(index: int) -> tuple[int, int] | None:
        if index not in spans:
            if len(spans) >= budget:
                raise _BudgetExhausted
            fetch(index)
        return spans[index]

    def result(candidates: Sequence[int], gap: Sequence[int] = (), unchecked: int = 0) -> _BusquedaExtra:
        return _BusquedaExtra(
            tuple(bulletins[i] for i in candidates),
            len(bulletins),
            len(spans),
            exhausted,
            url,
            tuple(bulletins[i] for i in gap),
            unchecked,
        )

    low, high = 0, len(bulletins) - 1
    try:
        while low <= high:
            middle = (low + high) // 2
            probe = middle
            while probe <= high and span(probe) is None:
                probe += 1
            if probe > high:  # nothing listed from middle to high
                high = middle - 1
                continue
            first, last = spans[probe]  # type: ignore[misc]
            if first <= target <= last:
                return result([probe])
            if target < first:
                high = probe - 1
            else:
                low = probe + 1
    except _BudgetExhausted:
        exhausted = True
    gap_pages = 0
    while True:
        listed = {i: s for i, s in spans.items() if s is not None}
        below = max((i for i, s in listed.items() if s[1] < target), default=None)
        above = min((i for i, s in listed.items() if s[0] > target), default=None)
        gap = range(0 if below is None else below + 1, len(bulletins) if above is None else above)
        unfetched = [i for i in gap if i not in spans]
        if not unfetched:
            break
        if gap_pages >= BUSQUEDA_EXTRA_HUECO:
            return result([], gap, len(unfetched))
        gap_pages += 1
        listed_span = fetch(unfetched[0])
        if listed_span is not None and listed_span[0] <= target <= listed_span[1]:
            return result([unfetched[0]])
    empty = [i for i in gap if spans[i] is None]
    return result([*empty, *(i for i in (below, above) if i is not None)])


def _article_metadata(article: Article, base_url: str) -> dict[str, Any]:
    return {
        "articulo_cve": article.cve,
        "numero": article.number,
        "bome_cve": article.bulletin_cve,
        "bome_numero": article.bulletin_number,
        "bome_fecha": article.bulletin_date.isoformat() if article.bulletin_date else None,
        "encabezado": article.heading,
        "sumario": article.sumario,
        "url": article.url,
        "pdf_url": article.pdf_url,
        "pdf_boletin_url": pdf_url(article.bulletin_cve, base_url=base_url),
    }


def leer_articulo(
    client: BomeClient,
    cve_boletin_o_articulo: str | Cve,
    numero: int | None = None,
    *,
    desde_pagina: int = 1,
    desde_caracter: int = 0,
    max_caracteres: int = DEFAULT_MAX_CARACTERES,
    boletin_de_articulo: BoletinDeArticulo | None = None,
) -> TextoPaginado:
    """Full text of an article from its web page, one printed page per unit.

    Accepts an article CVE (``BOME-A-2026-1051`` through the site's CVE
    resolver; ``BOME-AX-2019-103`` located without trusting it, see
    :func:`localizar_articulo`, optionally helped by ``boletin_de_articulo``)
    or a bulletin CVE plus ``numero``. Old articles without HTML text
    (2014-2016) fall back to the article PDF when the page links one;
    otherwise the result has ``fuente: "ninguna"``, no pages and an ``aviso``
    pointing to the bulletin PDF.
    """
    budget = _budget(max_caracteres)
    article = localizar_articulo(
        client, cve_boletin_o_articulo, numero, boletin_de_articulo=boletin_de_articulo
    )
    metadata = _article_metadata(article, client.base_url)
    if article.text is not None:
        return _chunk(
            cve=article.cve,
            fuente="html",
            url=article.url,
            textos=[page.text for page in article.pages],
            paginas_bome=[page.number for page in article.pages],
            desde_pagina=desde_pagina,
            desde_caracter=desde_caracter,
            budget=budget,
            metadatos=metadata,
        )
    no_html = f"{article.cve} has no HTML text on the site"
    if article.pdf_url is not None:
        try:
            chunk = leer_pdf(
                client,
                article.cve,
                desde_pagina=desde_pagina,
                desde_caracter=desde_caracter,
                max_caracteres=budget,
            )
        except BomeNotFoundError:
            no_pdf = f"{no_html} and its PDF answered 404"
        else:
            note = f"{no_html}; text read from the article PDF."
            return replace(
                chunk,
                aviso=f"{note} {chunk.aviso}" if chunk.aviso else note,
                metadatos={**metadata, **(chunk.metadatos or {})},
            )
    else:
        no_pdf = f"{no_html} and no article PDF is offered"
    return _no_text(
        cve=article.cve,
        url=article.url,
        budget=budget,
        aviso=(
            f"{no_pdf} (usual for 2014-2016 bulletins). The whole bulletin PDF is "
            f"{metadata['pdf_boletin_url']}, but for those years it also answered 404 "
            "when checked."
        ),
        metadatos=metadata,
        desde_pagina=desde_pagina,
        desde_caracter=desde_caracter,
    )


def leer_boletin(
    client: BomeClient,
    cve: str | Cve,
    *,
    desde_pagina: int = 1,
    desde_caracter: int = 0,
    max_caracteres: int = DEFAULT_MAX_CARACTERES,
) -> TextoPaginado:
    """The whole bulletin text (its PDF) plus bulletin metadata, with a cursor.

    Costs one bulletin-page request per call for the metadata; the PDF is
    downloaded once and then read from the cache. When the site has no PDF
    (404, seen for 2014-2016) the result has ``fuente: "ninguna"``.
    """
    budget = _budget(max_caracteres)
    parsed = parse_cve(cve)
    if not parsed.kind.is_bulletin:
        raise LecturaInvalidaError(
            f"{parsed} is not a bulletin CVE (BOME-B/BOME-BX); use leer_pdf or leer_articulo"
        )
    bulletin = client.bulletin(parsed)
    metadata: dict[str, Any] = {
        "numero": bulletin.number,
        "fecha": bulletin.date.isoformat() if bulletin.date else None,
        "extraordinario": bulletin.extraordinary,
        "total_articulos": len(bulletin.articles),
        "url": bulletin.url,
        "pdf_url": bulletin.pdf_url,
        "sumario_pdf_url": bulletin.sumario_pdf_url,
    }
    try:
        chunk = leer_pdf(
            client,
            parsed,
            desde_pagina=desde_pagina,
            desde_caracter=desde_caracter,
            max_caracteres=budget,
        )
    except BomeNotFoundError:
        return _no_text(
            cve=bulletin.cve,
            url=bulletin.pdf_url,
            budget=budget,
            aviso=(
                f"the site answered 404 for {bulletin.pdf_url} (usual for 2014-2016); "
                "read the articles one by one with leer_articulo"
            ),
            metadatos=metadata,
            desde_pagina=desde_pagina,
            desde_caracter=desde_caracter,
        )
    return replace(chunk, metadatos={**metadata, **(chunk.metadatos or {})})


__all__ = [
    "BUSQUEDA_EXTRA_HUECO",
    "BUSQUEDA_EXTRA_MARGEN",
    "MAX_INTENTOS_ARTICULO_EXTRA",
    "DEFAULT_MAX_CARACTERES",
    "MAX_MAX_CARACTERES",
    "MAX_PDF_BYTES",
    "MIN_MAX_CARACTERES",
    "ArticuloDistintoError",
    "ArticuloNoLocalizadoError",
    "BoletinDeArticulo",
    "Cursor",
    "DescargaPdf",
    "LecturaInvalidaError",
    "PaginaTexto",
    "TextoPaginado",
    "descargar_pdf",
    "descargar_pdf_antiguo",
    "leer_articulo",
    "leer_boletin",
    "leer_pdf",
    "leer_pdf_antiguo",
    "localizar_articulo",
    "nombre_pdf_antiguo",
    "paginar",
]
