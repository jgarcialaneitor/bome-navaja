"""Domain models and errors for the Boletín Oficial de Melilla (BOME).

Every model is a frozen dataclass whose ``to_dict()`` returns JSON-safe
values (dates as ISO strings, tuples as lists) for the MCP layer.

The bulletin tree mirrors the page ``/bome/{CVE}``:

    Bulletin → Section (the site's "departamento") → Consejeria → Organismo
    → ArticleRef

CVEs are stored as canonical strings (``"BOME-B-2026-6416"``); use
:func:`bome_navaja.cve.parse_cve` to get a structured value.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from enum import Enum
from typing import Any

# The site's search always pages by 10 results.
SEARCH_PAGE_SIZE = 10


class BomeError(Exception):
    """Base class for every error raised by ``bome_navaja``."""


class BomeHTTPError(BomeError):
    """The site answered with an unexpected HTTP status, or was unreachable."""

    def __init__(self, message: str, *, status: int | None, url: str) -> None:
        super().__init__(message)
        self.status = status
        """HTTP status code, or ``None`` for transport failures."""
        self.url = url


class BomeNotFoundError(BomeHTTPError):
    """The requested document does not exist (404 or an empty page)."""


class BomeBlockedError(BomeHTTPError):
    """The site is refusing our requests: rate-limited or firewalled (403/429/503).

    Callers doing bulk work must stop instead of recording it as one more
    per-document failure, and wait before asking again.
    """

    def __init__(
        self, message: str, *, status: int | None, url: str, retry_after: float | None = None
    ) -> None:
        super().__init__(message, status=status, url=url)
        self.retry_after = retry_after
        """Seconds to wait before asking again: the site's ``Retry-After`` or, with a
        :class:`~bome_navaja.guard.GuardiaSitio`, what is left of its cooldown;
        ``None`` if unknown."""


class BomePausaPreventivaError(BomeBlockedError):
    """Our own error budget is full: a local safety pause, not a block by the site.

    Raised by :class:`~bome_navaja.guard.GuardiaSitio` before any request when
    the site answered too many errors recently (its firewall bans the IP from
    the fifth). ``status`` is ``None``; ``retry_after`` is the seconds until the
    budget has room again.
    """

    error_code = "pausa_preventiva"


class BomeParseError(BomeError):
    """A page or payload did not have the expected shape."""


class BomeDocumentTooLargeError(BomeError):
    """A download exceeded the size cap (100 MB for PDFs); nothing was saved."""

    error_code = "documento_demasiado_grande"

    def __init__(self, message: str, *, size: int, limit: int) -> None:
        super().__init__(message)
        self.size = size
        """Bytes announced or received when the download was stopped."""
        self.limit = limit


class BomeStorageError(BomeError):
    """A local file operation failed (disk full, permissions, locked file)."""

    error_code = "error_almacenamiento"

    def __init__(self, message: str, *, path: str) -> None:
        super().__init__(message)
        self.path = path


class BomeIndexUnavailableError(BomeError):
    """The local sumario index cannot be used (e.g. SQLite built without FTS5)."""

    error_code = "indice_no_disponible"


class BomeIndexVersionError(BomeIndexUnavailableError):
    """The index file has a newer or unknown schema version."""

    error_code = "indice_version_incompatible"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


class _JsonModel:
    """Mixin giving dataclasses a recursive, JSON-safe ``to_dict()``."""

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping, dates rendered as ISO strings."""
        return _jsonable(self)


# Public name so other layers (search, index) can reuse the same JSON contract.
JsonModel = _JsonModel


@dataclass(frozen=True, slots=True)
class BulletinRef(_JsonModel):
    """A bulletin as listed by the calendar API, the year slider or a search."""

    cve: str
    number: int
    date: date | None
    extraordinary: bool
    url: str
    title: str | None = None
    """Label shown by the site, e.g. ``"Nº 6410"`` (calendar) or
    ``"BOME EXTRA Nº 44 del viernes, 18 de julio de 2025"`` (search)."""


@dataclass(frozen=True, slots=True)
class ArticleRef(_JsonModel):
    """An article as listed in a bulletin page or a sumario."""

    cve: str
    number: int
    """Article number, which is the number of its CVE (``BOME-A-2026-1051`` → 1051)."""
    sumario: str | None
    """One-line summary. ``None`` for old bulletins (before late 2016)."""
    url: str
    """Web view of the full article."""
    pdf_url: str | None
    """Article PDF; ``None`` when the site does not offer one (2014 bulletins)."""
    first_page: int | None = None
    """First bulletin page, only known from the sumario view."""


@dataclass(frozen=True, slots=True)
class Organismo(_JsonModel):
    """Issuing body inside a consejería (``<h4>`` in the bulletin page)."""

    name: str
    articles: tuple[ArticleRef, ...] = ()


@dataclass(frozen=True, slots=True)
class Consejeria(_JsonModel):
    """Government department block (an accordion in the bulletin page)."""

    name: str
    id: int | None = None
    """Site id, usable with ``/api/section/organismos/{id}``."""
    article_count: int | None = None
    page_count: int | None = None
    organismos: tuple[Organismo, ...] = ()


@dataclass(frozen=True, slots=True)
class Section(_JsonModel):
    """Top-level block of a bulletin, the site's "departamento"
    (e.g. ``"CIUDAD AUTÓNOMA DE MELILLA"``)."""

    name: str
    page_count: int | None = None
    article_count: int | None = None
    consejerias: tuple[Consejeria, ...] = ()


@dataclass(frozen=True, slots=True)
class Bulletin(_JsonModel):
    """A whole bulletin page: metadata plus its article tree."""

    cve: str
    number: int
    date: date | None
    extraordinary: bool
    url: str
    pdf_url: str
    sumario_pdf_url: str | None = None
    """Sumario PDF (``BOME-S``/``BOME-SX``); absent for old bulletins."""
    sumario_url: str | None = None
    """Sumario web view; absent for old bulletins."""
    sections: tuple[Section, ...] = ()

    @property
    def articles(self) -> tuple[ArticleRef, ...]:
        """Every article of the bulletin in page order."""
        return tuple(
            article
            for section in self.sections
            for consejeria in section.consejerias
            for organismo in consejeria.organismos
            for article in organismo.articles
        )


@dataclass(frozen=True, slots=True)
class Page(_JsonModel):
    """One printed bulletin page as rendered in an article or sumario view."""

    number: int
    cve: str | None
    """Page CVE (``BOME-P-YYYY-N``) when the page links its PDF."""
    pdf_url: str | None
    text: str


@dataclass(frozen=True, slots=True)
class Article(_JsonModel):
    """A single article page ``/bome/{CVE}/articulo/{n}`` in full."""

    cve: str
    number: int
    bulletin_cve: str
    bulletin_number: int | None
    bulletin_date: date | None
    heading: str | None
    """``"DEPARTAMENTO - CONSEJERÍA - ORGANISMO"`` as printed by the site."""
    sumario: str | None
    text: str | None
    """Full plain text (pages joined by blank lines); ``None`` when the site
    has no HTML text for the article (old bulletins: use the PDF)."""
    pages: tuple[Page, ...]
    url: str
    pdf_url: str | None


@dataclass(frozen=True, slots=True)
class SumarioEntry(_JsonModel):
    """One sumario line with the headings it sits under."""

    section: str | None
    consejeria: str | None
    article: ArticleRef


@dataclass(frozen=True, slots=True)
class Sumario(_JsonModel):
    """The sumario web view ``/bome/{CVE}/sumario``.

    The sumario has no organismo level: it lists departamento → consejería →
    articles, and adds each article's first page number.
    """

    bulletin_cve: str
    cve: str
    bulletin_number: int | None
    date: date | None
    url: str
    pdf_url: str | None
    pages: tuple[int, ...]
    """Bulletin page numbers that the sumario itself occupies."""
    entries: tuple[SumarioEntry, ...]


@dataclass(frozen=True, slots=True)
class Entity(_JsonModel):
    """An ``{id, nombre}`` item of the section APIs (consejería, organismo)."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class SearchPage(_JsonModel):
    """One page of results of ``/buscar`` or ``/buscador-avanzado``.

    The site returns bulletins only, without snippets.
    """

    results: tuple[BulletinRef, ...]
    page: int
    total_pages: int
    total_results: int
    per_page: int = SEARCH_PAGE_SIZE

    @property
    def has_next(self) -> bool:
        """True when a later page exists."""
        return self.page < self.total_pages
