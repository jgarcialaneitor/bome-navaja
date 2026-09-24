"""Live checks against the real bomemelilla.es (opt-in: BOME_NAVAJA_LIVE=1).

They guard the parsers against silent site changes; CI never runs them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import pytest

from bome_navaja.client import BomeClient
from bome_navaja.models import BomeNotFoundError

pytestmark = pytest.mark.live


@pytest.fixture
def bome() -> Iterator[BomeClient]:
    with BomeClient(polite_delay=0.5) as client:
        yield client


def test_live_calendar_recent_month(bome: BomeClient) -> None:
    end = date.today()
    refs = bome.calendar(end - timedelta(days=31), end)
    assert refs, "no bulletin in the last month"
    assert all(ref.cve.startswith(("BOME-B-", "BOME-BX-")) for ref in refs)
    assert all(ref.date is not None for ref in refs)


def test_live_bulletin(bome: BomeClient) -> None:
    bulletin = bome.bulletin("BOME-B-2026-6416")
    assert bulletin.number == 6416
    assert bulletin.date == date(2026, 9, 22)
    assert len(bulletin.articles) == 13
    assert bulletin.articles[0].cve == "BOME-A-2026-1050"


def test_live_resolve_cve(bome: BomeClient) -> None:
    assert bome.resolve_cve("BOME-B-2026-6416").endswith("/bome/BOME-B-2026-6416")
    # The resolver redirects unknown CVEs too; the existence check must catch it.
    with pytest.raises(BomeNotFoundError):
        bome.resolve_cve("BOME-B-2099-1")


def test_live_search_page(bome: BomeClient) -> None:
    page = bome.search_page(
        "/buscar",
        [("from", "1990-01-01"), ("tipo", "1"), ("contenido", "personal eventual")],
    )
    assert page.total_results >= 30
    assert page.total_pages >= 3
    assert len(page.results) == 10
