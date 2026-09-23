"""Tests for CVE parsing, normalisation and URL builders."""

from __future__ import annotations

import pytest

from bome_navaja.cve import (
    BASE_URL,
    Cve,
    CveKind,
    InvalidCveError,
    article_url,
    bulletin_url,
    parse_cve,
    pdf_url,
    resolve_url,
    sumario_url,
)
from bome_navaja.models import BomeError


@pytest.mark.parametrize(
    ("text", "kind", "year", "number"),
    [
        ("BOME-B-2026-6416", CveKind.BULLETIN, 2026, 6416),
        ("BOME-BX-2026-41", CveKind.EXTRA_BULLETIN, 2026, 41),
        ("BOME-A-2026-1051", CveKind.ARTICLE, 2026, 1051),
        ("BOME-AX-2026-102", CveKind.EXTRA_ARTICLE, 2026, 102),
        ("BOME-S-2026-6416", CveKind.SUMARIO, 2026, 6416),
        ("BOME-SX-2026-41", CveKind.EXTRA_SUMARIO, 2026, 41),
        ("BOME-P-2026-4784", CveKind.PAGE, 2026, 4784),
        ("BOME-A-2014-1", CveKind.ARTICLE, 2014, 1),
    ],
)
def test_parse_cve_valid_kinds(text: str, kind: CveKind, year: int, number: int) -> None:
    cve = parse_cve(text)
    assert cve == Cve(kind, year, number)
    assert str(cve) == text


@pytest.mark.parametrize(
    "text",
    ["bome-b-2026-6416", "  BOME-B-2026-6416\n", "Bome - B - 2026 - 6416", "BOME-B-2026-06416"],
)
def test_parse_cve_normalises_case_and_whitespace(text: str) -> None:
    assert str(parse_cve(text)) == "BOME-B-2026-6416"


@pytest.mark.parametrize(
    "text",
    ["", "   ", "BOME-Z-2026-1", "BOME-B-26-6416", "BOME-B-2026-", "B-2026-6416", "BOME-B-2026-abc", "BOME-B-2026-0"],
)
def test_parse_cve_rejects_invalid(text: str) -> None:
    with pytest.raises(InvalidCveError):
        parse_cve(text)


def test_invalid_cve_error_is_value_error_and_bome_error() -> None:
    with pytest.raises(ValueError):
        parse_cve("nope")
    assert issubclass(InvalidCveError, BomeError)


def test_parse_cve_accepts_cve_instance() -> None:
    cve = Cve(CveKind.BULLETIN, 2026, 6416)
    assert parse_cve(cve) is cve


def test_kind_properties() -> None:
    assert CveKind.EXTRA_BULLETIN.is_extraordinary
    assert CveKind.EXTRA_ARTICLE.is_extraordinary
    assert not CveKind.BULLETIN.is_extraordinary
    assert CveKind.BULLETIN.is_bulletin and CveKind.EXTRA_BULLETIN.is_bulletin
    assert not CveKind.ARTICLE.is_bulletin


def test_derived_cves() -> None:
    bulletin = parse_cve("BOME-B-2026-6416")
    extra = parse_cve("BOME-BX-2026-41")
    assert str(bulletin.sumario_cve()) == "BOME-S-2026-6416"
    assert str(extra.sumario_cve()) == "BOME-SX-2026-41"
    assert str(bulletin.article_cve(1051)) == "BOME-A-2026-1051"
    assert str(extra.article_cve(102)) == "BOME-AX-2026-102"
    assert str(parse_cve("BOME-SX-2026-41").bulletin_cve()) == "BOME-BX-2026-41"
    assert str(parse_cve("BOME-S-2026-6416").bulletin_cve()) == "BOME-B-2026-6416"
    with pytest.raises(InvalidCveError):
        parse_cve("BOME-A-2026-1").sumario_cve()
    with pytest.raises(InvalidCveError):
        parse_cve("BOME-P-2026-1").bulletin_cve()


def test_url_builders() -> None:
    assert BASE_URL == "https://bomemelilla.es"
    assert bulletin_url("bome-b-2026-6416") == f"{BASE_URL}/bome/BOME-B-2026-6416"
    assert sumario_url("BOME-B-2026-6416") == f"{BASE_URL}/bome/BOME-B-2026-6416/sumario"
    # A sumario CVE maps to the web view of its bulletin.
    assert sumario_url("BOME-SX-2026-41") == f"{BASE_URL}/bome/BOME-BX-2026-41/sumario"
    assert (
        article_url("BOME-B-2026-6416", 1051)
        == f"{BASE_URL}/bome/BOME-B-2026-6416/articulo/1051"
    )
    assert pdf_url("BOME-P-2026-4784") == f"{BASE_URL}/bome/descargar/BOME-P-2026-4784.pdf"
    assert resolve_url("BOME-A-2026-1051") == f"{BASE_URL}/buscar-cve?cve=BOME-A-2026-1051"


def test_url_builders_honour_custom_base_url() -> None:
    assert bulletin_url("BOME-B-2026-6416", base_url="http://x.test/") == (
        "http://x.test/bome/BOME-B-2026-6416"
    )


def test_bulletin_urls_reject_non_bulletin_cves() -> None:
    with pytest.raises(InvalidCveError):
        bulletin_url("BOME-A-2026-1051")
    with pytest.raises(InvalidCveError):
        article_url("BOME-A-2026-1051", 1)
    with pytest.raises(InvalidCveError):
        sumario_url("BOME-P-2026-1")
