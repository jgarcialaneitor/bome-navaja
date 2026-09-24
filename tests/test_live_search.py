"""Live search checks against bomemelilla.es (opt-in: BOME_NAVAJA_LIVE=1).

Target question: "todos los BOMEs donde hay nombramientos y ceses de personal
eventual".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from bome_navaja.client import BomeClient
from bome_navaja.search import RECOMMENDED_POLITE_DELAY, buscar_articulos, buscar_bomes
from bome_navaja.text import normalize

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def bome() -> Iterator[BomeClient]:
    with BomeClient(polite_delay=RECOMMENDED_POLITE_DELAY) as client:
        yield client


def test_live_bomes_personal_eventual_and_cese(bome: BomeClient) -> None:
    result = buscar_bomes(bome, texto="personal eventual", terminos=[{"texto": "cese"}])
    # 12 at recon (2026-09-23); the count can only grow.
    assert result.resultados.total_results >= 12
    assert len(result.resultados.results) == 10
    assert result.resultados.has_next


def test_live_articulos_personal_eventual_and_cese(bome: BomeClient) -> None:
    result = buscar_articulos(
        bome, texto="personal eventual", terminos=[{"texto": "cese"}], max_bomes=20
    )
    assert result.total_bomes >= 12
    assert result.bomes_revisados >= 12
    assert len(result.articulos) >= 12
    for article in result.articulos:
        folded = normalize(article.sumario)
        assert "personal eventual" in folded and "cese" in folded
    # The site ANDs terms inside one article sumario, exactly like the local
    # matcher, so every bulletin the site returns must contain a match.
    assert result.bomes_sin_coincidencia == ()
    assert result.errores == ()
    dates = [a.bome_fecha for a in result.articulos]
    assert dates == sorted(dates, reverse=True)


def test_live_hidden_article_is_recovered(bome: BomeClient) -> None:
    # BOME-B-2025-6294's page lists 744 and 746-750; article 745 (the cese of
    # a Personal Eventual de Confianza) only exists at /articulo/745.
    result = buscar_articulos(
        bome, texto="personal eventual", terminos=[{"texto": "cese"}], numero_bome=6294
    )
    assert [a.cve for a in result.articulos] == ["BOME-A-2025-745"]
    assert result.articulos[0].listado_en_bome is False
    assert result.articulos[0].consejeria == "PRESIDENCIA"


def test_live_articulos_nombramientos_or_ceses(bome: BomeClient) -> None:
    # (personal eventual AND cese) OR (personal eventual AND nombramiento)
    result = buscar_articulos(
        bome,
        texto="personal eventual",
        terminos=[
            {"texto": "cese"},
            {"texto": "personal eventual", "operador": "o"},
            {"texto": "nombramiento"},
        ],
        max_bomes=8,
    )
    assert len(result.parametros_sitio) == 2
    assert result.bomes_revisados == 8
    assert result.truncado and result.motivo_truncado == "max_bomes"
    for article in result.articulos:
        folded = normalize(article.sumario)
        assert "personal eventual" in folded
        assert "cese" in folded or "nombramiento" in folded


def test_live_bomes_page_text(bome: BomeClient) -> None:
    result = buscar_bomes(
        bome, texto="personal eventual", ambito="contenido", desde="2026-01-01"
    )
    assert result.resultados.total_results >= 1
    assert all(ref.date and ref.date.year == 2026 for ref in result.resultados.results)
