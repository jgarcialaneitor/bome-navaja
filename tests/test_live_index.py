"""Live index sync of one month (opt-in: BOME_NAVAJA_LIVE=1). Never a full sync."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from bome_navaja.client import BomeClient
from bome_navaja.index import SumarioIndex
from bome_navaja.search import RECOMMENDED_POLITE_DELAY
from bome_navaja.sync import SincronizadorIndice

pytestmark = pytest.mark.live

SYNC_TIMEOUT = 600


@pytest.fixture
def index(tmp_path: Path) -> Iterator[SumarioIndex]:
    idx = SumarioIndex(tmp_path / "sumarios.sqlite3")
    yield idx
    idx.close()


def test_live_sync_one_month_and_search(index: SumarioIndex) -> None:
    sync = SincronizadorIndice(
        index, lambda: BomeClient(polite_delay=RECOMMENDED_POLITE_DELAY)
    )
    sync.iniciar(desde=date(2026, 9, 1), hasta=date(2026, 9, 30))
    assert sync.esperar(SYNC_TIMEOUT)
    state = sync.estado()
    assert state.estado == "completado", state
    assert state.total_planificado >= 8
    assert state.errores == 0
    assert state.indexados == state.total_planificado

    result = index.buscar("relacion provisional", desde="2026-09-01")
    assert result.total >= 5
    assert all("**" in (a.resaltado or "") for a in result.articulos)
    assert result.cobertura["boletines_indexados"] == state.total_planificado

    # Substring mode can only find more than word-start mode.
    fragmento = index.buscar("cese", desde="2026-09-01", limite=200)
    palabra = index.buscar("cese", desde="2026-09-01", limite=200, coincidencia="palabra")
    assert fragmento.total >= palabra.total
    assert {a.cve for a in palabra.articulos} <= {a.cve for a in fragmento.articulos}
    # "rden" only occurs inside words ("Orden nº ..."), so the modes must differ.
    inside = index.buscar("rden", desde="2026-09-01")
    assert inside.total >= 1
    assert index.buscar("rden", desde="2026-09-01", coincidencia="palabra").total == 0
    assert index.buscar("orden", desde="2026-09-01", coincidencia="palabra").total >= 1
    short = index.buscar("de", desde="2026-09-01")
    assert short.total >= 1  # under 3 characters: direct scan

    stored = index.estado()
    assert stored.boletines["indexado"] == state.total_planificado
    assert stored.articulos_con_sumario >= 13
    assert stored.pendientes == 0
    print(
        f"\n[live] bulletins={state.total_planificado} s/bulletin={state.segundos_por_boletin} "
        f"articles={stored.articulos} db_bytes={stored.tamano_bytes} "
        f"cese fragmento={fragmento.total} palabra={palabra.total}"
    )


def test_live_sync_2014_bulletin_has_no_sumarios(index: SumarioIndex) -> None:
    sync = SincronizadorIndice(
        index, lambda: BomeClient(polite_delay=RECOMMENDED_POLITE_DELAY)
    )
    sync.iniciar(desde=date(2014, 1, 3), hasta=date(2014, 1, 3))
    assert sync.esperar(SYNC_TIMEOUT)
    state = sync.estado()
    assert state.estado == "completado"
    assert (state.total_planificado, state.sin_sumarios) == (1, 1)
    assert index.estado_boletin("BOME-B-2014-5092") == "sin_sumarios"
