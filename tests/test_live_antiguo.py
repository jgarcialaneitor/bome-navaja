"""Live checks against the old BOME portal on melilla.es (skipped unless BOME_NAVAJA_LIVE=1).

At most two requests: the catalog page and one search. No ficha or PDF
(robots.txt disallows them; they are only fetched on demand by a user).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest

from bome_navaja.antiguo import PortalAntiguo
from bome_navaja.guard import GuardiaSitio

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def portal() -> Iterator[PortalAntiguo]:
    client = PortalAntiguo(guard=GuardiaSitio(None, sitio="melilla.es"), cache_path=None)
    yield client
    client.close()


def test_live_catalog_is_the_frozen_1985_2021_list(portal: PortalAntiguo) -> None:
    catalogo = portal.catalogo()
    assert len(catalogo.boletines) >= 3200
    fechas = [b.fecha for b in catalogo.boletines]
    assert min(fechas) <= date(1985, 1, 3) and max(fechas) >= date(2021, 3, 12)
    assert sum(b.cve_oficial for b in catalogo.boletines) >= 1000
    assert [b.dboid for b in catalogo.por_cve("BOME-B-2016-5302")] == [216808]


def test_live_search_returns_articles_with_https_pdfs(portal: PortalAntiguo) -> None:
    resultados = portal.buscar("personal eventual")
    assert len(resultados) >= 70
    assert all(p.url_pdf.startswith("https://www.melilla.es/mandar.php/") for r in resultados for p in r.paginas)
