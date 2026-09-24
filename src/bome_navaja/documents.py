"""PDF download with a safe local cache, and paginated text reading.

Public entry points, designed to be exposed 1:1 as MCP tools (task 6):

* :func:`descargar_pdf` — download (or reuse) the PDF of any CVE.
* :func:`leer_pdf` — text of any PDF, page by page, with a cursor.
* :func:`leer_articulo` — article text from its web page (``#pagina-N``
  blocks), falling back to its PDF for old articles without HTML text.
* :func:`leer_boletin` — the whole bulletin PDF text plus bulletin metadata.

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
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import pypdf

from .client import BomeClient
from .cve import Cve, CveKind, parse_cve, pdf_url
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

Fuente = Literal["pdf", "html", "ninguna"]


class LecturaInvalidaError(BomeError, ValueError):
    """Invalid reading arguments (CVE kind, number, cursor or budget)."""


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

    cve: str
    fuente: Fuente
    url: str
    total_paginas: int
    max_caracteres: int
    paginas: tuple[PaginaTexto, ...]
    siguiente: Cursor | None
    completo: bool
    aviso: str | None = None
    metadatos: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DescargaPdf(JsonModel):
    """A PDF stored in the local cache."""

    cve: str
    ruta: str
    tamano_bytes: int
    sha256: str
    total_paginas: int
    cache_hit: bool
    url: str
    motivo_directorio: str
    """Why this directory was chosen (``BOME_NAVAJA_PDF_DIR``, XDG, ...)."""


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
    cve: str,
    fuente: Fuente,
    url: str,
    textos: Sequence[str],
    desde_pagina: int,
    desde_caracter: int,
    budget: int,
    metadatos: dict[str, Any],
    paginas_bome: Sequence[int | None] | None = None,
    aviso: str | None = None,
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
    """Download the PDF of any CVE (B, BX, A, AX, S, SX, P) into the local cache.

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
    if not refrescar:
        cached = _cached(target)
        if cached is not None:
            data, pages = cached
            return _descarga(canonical, target, data, pages, True, source_url, reason)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BomeStorageError(f"cannot create {directory}: {exc}", path=str(directory)) from exc
    data = client.download(canonical, max_bytes=MAX_PDF_BYTES)
    pages = _pdf_info(data)
    _atomic_write(target, data)
    return _descarga(canonical, target, data, pages, False, source_url, reason)


def _descarga(
    cve: Cve, target: Path, data: bytes, pages: int, hit: bool, url: str, reason: str
) -> DescargaPdf:
    return DescargaPdf(
        cve=str(cve),
        ruta=str(target),
        tamano_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        total_paginas=pages,
        cache_hit=hit,
        url=url,
        motivo_directorio=reason,
    )


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


def _article_target(client: BomeClient, cve: Cve, numero: int | None) -> tuple[Cve, int]:
    if cve.kind.is_bulletin:
        if numero is None:
            raise LecturaInvalidaError("give 'numero' (the article number) with a bulletin CVE")
        number = _int_arg(numero, "numero")
        if number < 1:
            raise LecturaInvalidaError(f"'numero' must be positive, got {number}")
        return cve, number
    if cve.kind in (CveKind.ARTICLE, CveKind.EXTRA_ARTICLE):
        if numero is not None:
            raise LecturaInvalidaError("'numero' is implied by an article CVE; leave it empty")
        # Article numbers are independent of bulletin numbers, so the bulletin
        # can only be learnt from the site's resolver (302 to the article page).
        location = client.resolve_cve(cve, confirm=False)
        match = _ARTICLE_PATH.search(urlsplit(location).path)
        if match is None:
            raise BomeParseError(f"{cve} resolved to {location}, not an article page")
        return parse_cve(match.group(1)), int(match.group(2))
    raise LecturaInvalidaError(
        f"{cve} is not an article: give an article CVE (BOME-A/BOME-AX) or a bulletin "
        "CVE (BOME-B/BOME-BX) plus 'numero'; use leer_pdf for sumario and page CVEs"
    )


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
) -> TextoPaginado:
    """Full text of an article from its web page, one printed page per unit.

    Accepts an article CVE (``BOME-A-2026-1051``, resolved through the site's
    CVE resolver) or a bulletin CVE plus ``numero``. Old articles without
    HTML text (2014-2016) fall back to the article PDF when the page links
    one; otherwise the result has ``fuente: "ninguna"``, no pages and an
    ``aviso`` pointing to the bulletin PDF.
    """
    budget = _budget(max_caracteres)
    bulletin_cve, number = _article_target(client, parse_cve(cve_boletin_o_articulo), numero)
    article = client.article(bulletin_cve, number)
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
    "DEFAULT_MAX_CARACTERES",
    "MAX_MAX_CARACTERES",
    "MAX_PDF_BYTES",
    "MIN_MAX_CARACTERES",
    "Cursor",
    "DescargaPdf",
    "LecturaInvalidaError",
    "PaginaTexto",
    "TextoPaginado",
    "descargar_pdf",
    "leer_articulo",
    "leer_boletin",
    "leer_pdf",
    "paginar",
]
