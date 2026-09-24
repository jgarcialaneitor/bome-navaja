"""CVE (Código de Verificación Electrónica) parsing and URL builders.

Every BOME document carries a CVE shaped ``BOME-{kind}-{year}-{number}``.
Kinds observed on bomemelilla.es (fixtures captured 2026-09-23):

* ``B`` ordinary bulletin, ``BX`` extraordinary bulletin. Ordinary bulletins
  are numbered continuously across years (``BOME-B-2026-6416``);
  extraordinary ones restart every year (``BOME-BX-2026-41``).
* ``A`` article of an ordinary bulletin, ``AX`` article of an extraordinary
  bulletin, both numbered per year (``BOME-A-2026-1051``, ``BOME-AX-2026-102``).
* ``S`` / ``SX`` sumario PDF of a bulletin; the number is the bulletin number
  (``BOME-S-2026-6416`` is the sumario of ``BOME-B-2026-6416``).
* ``P`` single page PDF, numbered per year (``BOME-P-2026-4784``).

The article number in the ``/bome/{CVE}/articulo/{n}`` URL is the article CVE
number (``BOME-A-2026-1051`` → ``/bome/BOME-B-2026-6416/articulo/1051``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from .models import BomeError

BASE_URL = "https://bomemelilla.es"

_CVE_RE = re.compile(r"BOME-([A-Z]{1,2})-(\d{4})-(\d+)")


class InvalidCveError(BomeError, ValueError):
    """Raised when a text is not a valid BOME CVE for the requested use."""


class CveKind(StrEnum):
    """Document kind encoded in the second CVE segment."""

    BULLETIN = "B"
    EXTRA_BULLETIN = "BX"
    ARTICLE = "A"
    EXTRA_ARTICLE = "AX"
    SUMARIO = "S"
    EXTRA_SUMARIO = "SX"
    PAGE = "P"

    @property
    def is_extraordinary(self) -> bool:
        """True for the kinds that belong to an extraordinary bulletin."""
        return self.value.endswith("X")

    @property
    def is_bulletin(self) -> bool:
        """True for whole-bulletin CVEs (``B`` and ``BX``)."""
        return self in (CveKind.BULLETIN, CveKind.EXTRA_BULLETIN)

    @property
    def is_sumario(self) -> bool:
        """True for sumario CVEs (``S`` and ``SX``)."""
        return self in (CveKind.SUMARIO, CveKind.EXTRA_SUMARIO)


@dataclass(frozen=True, slots=True)
class Cve:
    """A parsed CVE. ``str()`` returns the canonical form."""

    kind: CveKind
    year: int
    number: int

    def __str__(self) -> str:
        return f"BOME-{self.kind.value}-{self.year}-{self.number}"

    def bulletin_cve(self) -> Cve:
        """The bulletin CVE for a bulletin or sumario CVE (same number)."""
        if self.kind.is_bulletin:
            return self
        if self.kind is CveKind.SUMARIO:
            return Cve(CveKind.BULLETIN, self.year, self.number)
        if self.kind is CveKind.EXTRA_SUMARIO:
            return Cve(CveKind.EXTRA_BULLETIN, self.year, self.number)
        raise InvalidCveError(f"{self} does not identify a bulletin")

    def sumario_cve(self) -> Cve:
        """The sumario PDF CVE of a bulletin (``B``→``S``, ``BX``→``SX``)."""
        bulletin = self.bulletin_cve()
        kind = (
            CveKind.EXTRA_SUMARIO
            if bulletin.kind is CveKind.EXTRA_BULLETIN
            else CveKind.SUMARIO
        )
        return Cve(kind, bulletin.year, bulletin.number)

    def article_cve(self, number: int) -> Cve:
        """The CVE of article ``number`` of this bulletin (``B``→``A``, ``BX``→``AX``)."""
        if not self.kind.is_bulletin:
            raise InvalidCveError(f"{self} is not a bulletin CVE")
        kind = (
            CveKind.EXTRA_ARTICLE
            if self.kind is CveKind.EXTRA_BULLETIN
            else CveKind.ARTICLE
        )
        return Cve(kind, self.year, number)


def parse_cve(text: str | Cve) -> Cve:
    """Parse a CVE, tolerating case, surrounding and inner whitespace.

    ``" bome - b - 2026 - 6416 "`` parses as ``BOME-B-2026-6416``. Leading
    zeros in the number are dropped. Raises :class:`InvalidCveError`.
    """
    if isinstance(text, Cve):
        return text
    if not isinstance(text, str):
        raise InvalidCveError(f"not a CVE: {text!r}")
    compact = re.sub(r"\s+", "", text).upper()
    match = _CVE_RE.fullmatch(compact)
    if match is None:
        raise InvalidCveError(f"not a BOME CVE (BOME-L-AAAA-NNNN): {text!r}")
    kind_code, year, number = match.groups()
    try:
        kind = CveKind(kind_code)
    except ValueError:
        raise InvalidCveError(f"unknown CVE kind {kind_code!r} in {text!r}") from None
    if int(number) == 0:
        raise InvalidCveError(f"CVE number must be positive: {text!r}")
    return Cve(kind, int(year), int(number))


def _join(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _bulletin(cve: str | Cve) -> Cve:
    parsed = parse_cve(cve)
    if not parsed.kind.is_bulletin:
        raise InvalidCveError(f"{parsed} is not a bulletin CVE (BOME-B / BOME-BX)")
    return parsed


def bulletin_path(cve: str | Cve) -> str:
    """Path of a bulletin page: ``/bome/{CVE}``."""
    return f"/bome/{_bulletin(cve)}"


def sumario_path(cve: str | Cve) -> str:
    """Path of the sumario web view; accepts the bulletin or its sumario CVE."""
    return f"/bome/{parse_cve(cve).bulletin_cve()}/sumario"


def article_path(bulletin_cve: str | Cve, number: int) -> str:
    """Path of an article page: ``/bome/{bulletin CVE}/articulo/{n}``."""
    return f"{bulletin_path(bulletin_cve)}/articulo/{int(number)}"


def pdf_path(cve: str | Cve) -> str:
    """Path of the PDF for any CVE: ``/bome/descargar/{CVE}.pdf``."""
    return f"/bome/descargar/{parse_cve(cve)}.pdf"


def bulletin_url(cve: str | Cve, *, base_url: str = BASE_URL) -> str:
    """Absolute URL of a bulletin page."""
    return _join(base_url, bulletin_path(cve))


def sumario_url(cve: str | Cve, *, base_url: str = BASE_URL) -> str:
    """Absolute URL of a bulletin's sumario web view."""
    return _join(base_url, sumario_path(cve))


def article_url(bulletin_cve: str | Cve, number: int, *, base_url: str = BASE_URL) -> str:
    """Absolute URL of an article page."""
    return _join(base_url, article_path(bulletin_cve, number))


def pdf_url(cve: str | Cve, *, base_url: str = BASE_URL) -> str:
    """Absolute URL of the PDF for any CVE."""
    return _join(base_url, pdf_path(cve))


def resolve_url(cve: str | Cve, *, base_url: str = BASE_URL) -> str:
    """Absolute URL of the site's CVE resolver (answers with a 302)."""
    return _join(base_url, f"/buscar-cve?cve={quote(str(parse_cve(cve)))}")
