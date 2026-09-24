"""Background sync of the old BOME portal (melilla.es) into the local index.

Offline: a MockTransport-backed :class:`PortalAntiguo` serves the captured
catalog and ficha fixtures; guards run on a fake clock and waits never sleep.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from bome_navaja import sync_antiguo as sync_antiguo_module
from bome_navaja.antiguo import PortalAntiguo
from bome_navaja.client import BomeClient
from bome_navaja.guard import ENFRIAMIENTO_SEGUNDOS, GuardiaSitio
from bome_navaja.index import SumarioIndex
from bome_navaja.models import BomePausaPreventivaError, BulletinRef
from bome_navaja.search import ArticuloEncontrado, BusquedaInvalidaError
from bome_navaja.sync import (
    DEFAULT_MAX_BULLETINS_PER_RUN,
    PAUSA_TRAS_ERROR_MAX_SEGUNDOS,
    PAUSA_TRAS_ERROR_MIN_SEGUNDOS,
    SYNC_JITTER,
    SYNC_POLITE_DELAY,
    SincronizadorIndice,
)
from bome_navaja.sync_antiguo import (
    DESDE_POR_DEFECTO,
    HASTA_POR_DEFECTO,
    SincronizadorPortalAntiguo,
)

FIXTURES = Path(__file__).parent / "fixtures"
ANTIGUO = FIXTURES / "antiguo"
TODAY = date(2026, 9, 24)
TIMEOUT = 10

Handler = Callable[[httpx.Request], httpx.Response]


def entry(dboid: int, text: str) -> str:
    return (
        '<li><a href="contenedor.jsp?seccion=ficha_bome.jsp&amp;dboidboletin='
        f'{dboid}&amp;codResi=1&amp;language=es&amp;codAdirecto=15">{text}</a></li>'
    )


def catalog_page(years: dict[int, list[tuple[int, str]]]) -> str:
    """A catalog page shaped like the real one: one accordion set per year."""
    sets = "".join(
        f'<div class="set set{i}"><div class="title"><img src="resid/1/img/{year}.jpg"/></div>'
        '<div class="content"><div class="cBome"><div class="c45">'
        '<div class="listado1"><a href="">Mes</a></div><div class="listado2"><ul class="menu">'
        + "".join(entry(dboid, text) for dboid, text in links)
        + "</ul></div></div></div></div></div>"
        for i, (year, links) in enumerate(years.items(), start=1)
    )
    return (
        '<html><body><div class="bandaNo"><div id="accordion3" class="accordionWrapper">'
        f"{sets}</div></div></body></html>"
    )


SMALL_CATALOG = catalog_page(
    {
        2018: [(300001, "nº 5520 / 02-01-2018")],
        2016: [(216808, "nº 5302 / 08-01-2016"), (216807, "nº 5301 / 05-01-2016")],
        1999: [(276000, "nº 3660 / 30-12-1999")],
        1991: [
            (278000, "nº 3175 / 26-12-1991"),
            (278002, "nº Extra1 / 20-09-1991"),
            (278001, "nº Extra1 / 10-06-1991"),
        ],
        1986: [(279997, "nº 2899 / 25-12-1986")],
    }
)
"""Seven bulletins: 2018 and 1986 fall outside the default range; BX-1991-1 repeats."""

SMALL_FICHAS = {
    216808: "ficha_5302.html",
    216807: "ficha_5302.html",  # numbering comes from the catalog entry
    276000: "ficha_1999_3660.html",
    278000: "ficha_1991_3175.html",
    278002: "ficha_1986_2899.html",  # no articles: sin_sumarios
    278001: "ficha_1986_2899.html",
    279997: "ficha_1986_2899.html",
    300001: "ficha_5302.html",
}
DEFAULT_PLAN = [216808, 216807, 276000, 278000, 278002, 278001]
"""Newest first, 1991-01-01..2017-12-31."""


class Portal:
    """The old portal behind a MockTransport: routes by ``seccion`` and ``dboidboletin``."""

    def __init__(self, catalog: str | bytes = SMALL_CATALOG, fichas: dict[int, str] | None = None,
                 default_ficha: str | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.lock = threading.Lock()
        self.clock = [2_000_000.0]
        body = catalog.encode("latin-1") if isinstance(catalog, str) else catalog
        self.catalog: Handler = lambda request: httpx.Response(200, content=body)
        self.fichas: dict[int, Handler] = {}
        for dboid, name in (SMALL_FICHAS if fichas is None else fichas).items():
            self.fichas[dboid] = self.fixture(name)
        self.default_ficha = self.fixture(default_ficha) if default_ficha else None
        self.guards: list[GuardiaSitio | None] = []

    @staticmethod
    def fixture(name: str) -> Handler:
        body = (ANTIGUO / name).read_bytes()
        return lambda request: httpx.Response(200, content=body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
        seccion = request.url.params.get("seccion")
        if seccion == "bome.jsp":
            return self.catalog(request)
        if seccion == "ficha_bome.jsp":
            handler = self.fichas.get(int(request.url.params["dboidboletin"]), self.default_ficha)
            return handler(request) if handler else httpx.Response(404)
        return httpx.Response(404)

    def factory(self, *, guard: GuardiaSitio | None = None, cache_path: Path | None = None) -> PortalAntiguo:
        self.guards.append(guard)
        return PortalAntiguo(
            transport=httpx.MockTransport(self), guard=guard, cache_path=cache_path, polite_delay=0.0, jitter=0.0
        )

    def guard(self) -> GuardiaSitio:
        return GuardiaSitio(None, clock=lambda: self.clock[0], sitio="melilla.es")

    def secciones(self) -> list[str]:
        return [r.url.params.get("seccion", r.url.path) for r in self.requests]

    def fichas_pedidas(self) -> list[int]:
        return [
            int(r.url.params["dboidboletin"]) for r in self.requests if r.url.params.get("seccion") == "ficha_bome.jsp"
        ]


class Waits:
    """Injected back-off wait: records each slice, never sleeps, moves a fake clock."""

    def __init__(self, clock: list[float] | None = None, on_wait: Callable[[], bool] | None = None) -> None:
        self.slices: list[float] = []
        self.clock = clock
        self.on_wait = on_wait

    def __call__(self, seconds: float) -> bool:
        self.slices.append(seconds)
        if self.clock is not None:
            self.clock[0] += seconds
        return self.on_wait() if self.on_wait else False


@pytest.fixture
def portal() -> Portal:
    return Portal()


@pytest.fixture
def index(tmp_path: Path) -> SumarioIndex:
    idx = SumarioIndex(tmp_path / "sumarios.sqlite3")
    yield idx
    idx.close()


def make_sync(index: SumarioIndex, portal: Portal, **kwargs) -> SincronizadorPortalAntiguo:
    kwargs.setdefault("guard", portal.guard())
    kwargs.setdefault("wait", Waits(clock=portal.clock))
    return SincronizadorPortalAntiguo(index, portal.factory, hoy=lambda: TODAY, **kwargs)


def run(sync, **kwargs):
    sync.iniciar(**kwargs)
    assert sync.esperar(TIMEOUT), "sync thread did not finish"
    return sync.estado()


def row(index: SumarioIndex, key: str) -> tuple | None:
    found = index._conn().execute(
        "SELECT origen, dboid, estado, http_status, fallos_5xx, n_articulos FROM bulletins WHERE cve = ?", (key,)
    ).fetchone()
    return tuple(found) if found is not None else None


def bome_ref(cve: str, day: date) -> BulletinRef:
    return BulletinRef(cve=cve, number=int(cve.rsplit("-", 1)[1]), date=day, extraordinary="-BX-" in cve,
                       url=f"https://bomemelilla.es/bome/{cve}")


def bome_article(bulletin: BulletinRef, number: int, sumario: str) -> ArticuloEncontrado:
    return ArticuloEncontrado(
        bome_cve=bulletin.cve, bome_numero=bulletin.number, bome_fecha=bulletin.date,
        bome_extraordinario=bulletin.extraordinary, cve=f"BOME-A-{bulletin.date.year}-{number}", numero=number,
        sumario=sumario, departamento="CIUDAD AUTÓNOMA DE MELILLA", consejeria="CONSEJERÍA", organismo="",
        url=f"{bulletin.url}/articulo/{number}", pdf_url=None, listado_en_bome=True,
    )


# --------------------------------------------------------------------------- plan and storage


def test_the_default_range_is_1991_to_2017_newest_first(portal: Portal, index: SumarioIndex) -> None:
    assert (DESDE_POR_DEFECTO, HASTA_POR_DEFECTO) == (date(1991, 1, 1), date(2017, 12, 31))
    sync = make_sync(index, portal)
    started = sync.iniciar()
    assert started.estado == "en_curso" and started.origen == "melilla.es"
    assert sync.esperar(TIMEOUT)
    state = sync.estado()
    assert state.estado == "completado"
    assert state.origen == "melilla.es"
    assert (state.desde, state.hasta) == ("1991-01-01", "2017-12-31")
    assert (state.total_planificado, state.hechos, state.indexados, state.sin_sumarios, state.errores) == (6, 6, 4, 2, 0)
    assert (state.limite_boletines, state.pendientes_tras_limite) == (DEFAULT_MAX_BULLETINS_PER_RUN, 0)
    assert portal.fichas_pedidas() == DEFAULT_PLAN  # 2018 and 1986 are out of range
    assert set(portal.secciones()) == {"bome.jsp", "ficha_bome.jsp"}  # fichas only, never PDFs
    assert portal.secciones().count("bome.jsp") == 1
    assert row(index, "BOME-B-1999-3660") == ("melilla.es", 276000, "indexado", None, 0, 73)
    assert row(index, "BOME-B-1991-3175")[:3] == ("melilla.es", 278000, "indexado")
    assert row(index, "BOME-B-2016-5301")[:3] == ("melilla.es", 216807, "indexado")
    assert row(index, "BOME-B-1986-2899") is None
    assert row(index, "BOME-B-2018-5520") is None
    result = index.buscar("suscripciones", desde="1999-01-01", hasta="1999-12-31")
    assert result.total == 1 and result.articulos[0].origen == "melilla.es"
    assert index.lease() is None
    json.dumps(state.to_dict())


def test_repeated_identifiers_are_stored_with_their_dboid(portal: Portal, index: SumarioIndex) -> None:
    run(make_sync(index, portal))
    assert row(index, "BOME-BX-1991-1~278001")[:3] == ("melilla.es", 278001, "sin_sumarios")
    assert row(index, "BOME-BX-1991-1~278002")[:3] == ("melilla.es", 278002, "sin_sumarios")
    assert row(index, "BOME-BX-1991-1") is None


def test_an_explicit_desde_reaches_bulletins_before_1991(portal: Portal, index: SumarioIndex) -> None:
    state = run(make_sync(index, portal), desde="1986-01-01", hasta="1990-12-31")
    assert (state.desde, state.hasta) == ("1986-01-01", "1990-12-31")
    assert portal.fichas_pedidas() == [279997]
    assert (state.sin_sumarios, state.indexados) == (1, 0)  # 1986 fichas have no articles
    assert row(index, "BOME-B-1986-2899")[:3] == ("melilla.es", 279997, "sin_sumarios")


def test_a_desde_after_2017_without_hasta_runs_to_today(portal: Portal, index: SumarioIndex) -> None:
    state = run(make_sync(index, portal), desde="2018-01-01")
    assert (state.desde, state.hasta) == ("2018-01-01", TODAY.isoformat())
    assert portal.fichas_pedidas() == [300001]


def test_bulletins_bomemelilla_already_indexed_are_skipped_but_its_sin_sumarios_are_filled(
    portal: Portal, index: SumarioIndex
) -> None:
    indexed = bome_ref("BOME-B-2016-5302", date(2016, 1, 8))
    index.guardar_boletin(indexed, [bome_article(indexed, 27, "Resolución de Función Pública.")], "indexado")
    empty = bome_ref("BOME-B-2016-5301", date(2016, 1, 5))
    index.guardar_boletin(empty, [], "sin_sumarios")  # 2014-2016: no sumarios on bomemelilla.es
    state = run(make_sync(index, portal))
    assert 216808 not in portal.fichas_pedidas()
    assert portal.fichas_pedidas()[0] == 216807
    assert state.total_planificado == 5
    assert row(index, "BOME-B-2016-5302")[:3] == ("bomemelilla.es", None, "indexado")
    assert row(index, "BOME-B-2016-5301")[:3] == ("melilla.es", 216807, "indexado")  # the better outcome wins


def test_a_second_run_skips_what_the_old_portal_already_answered(portal: Portal, index: SumarioIndex) -> None:
    sync = make_sync(index, portal)
    run(sync)
    portal.requests.clear()
    state = run(sync)
    assert state.estado == "completado"
    assert state.total_planificado == 0
    assert portal.requests == []  # neither the fichas nor the catalog again


def test_the_catalog_is_fetched_once_across_runs_and_processes(
    portal: Portal, index: SumarioIndex, tmp_path: Path
) -> None:
    cache = tmp_path / "catalogo_portal_antiguo.json"

    def factory(*, guard: GuardiaSitio | None = None) -> PortalAntiguo:
        return portal.factory(guard=guard, cache_path=cache)

    sync = SincronizadorPortalAntiguo(index, factory, hoy=lambda: TODAY, guard=portal.guard(), wait=Waits())
    run(sync, max_boletines=1)
    run(sync, max_boletines=1)
    assert portal.secciones().count("bome.jsp") == 1
    other = SincronizadorPortalAntiguo(index, factory, hoy=lambda: TODAY, guard=portal.guard(), wait=Waits())
    run(other, max_boletines=1)  # e.g. after a restart: the cache file answers
    assert portal.secciones().count("bome.jsp") == 1
    assert portal.fichas_pedidas() == DEFAULT_PLAN[:3]


# --------------------------------------------------------------------------- the real catalog


def test_the_real_catalog_default_range_and_cap(index: SumarioIndex) -> None:
    portal = Portal(catalog=(ANTIGUO / "listado.html").read_bytes(), fichas={}, default_ficha="ficha_5302.html")
    state = run(make_sync(index, portal), max_boletines=1)
    # 2016 (131) + 2011 (132) + 2010 (123); 1985, 1986 and 2021 are out of the default range.
    assert (state.total_planificado, state.pendientes_tras_limite) == (1, 385)
    (dboid,) = portal.fichas_pedidas()
    newest = index._conn().execute("SELECT cve, date, origen FROM bulletins WHERE dboid = ?", (dboid,)).fetchone()
    assert newest[1].startswith("2016-12") and newest[2] == "melilla.es"
    mensaje = state.mensaje or ""
    assert "limit" in mensaje and "385" in mensaje


def test_the_real_catalog_1986_with_repeated_identifiers(index: SumarioIndex) -> None:
    portal = Portal(catalog=(ANTIGUO / "listado.html").read_bytes(), fichas={}, default_ficha="ficha_1986_2899.html")
    state = run(make_sync(index, portal), desde="1986-01-01", hasta="1986-12-31")
    assert state.estado == "completado"
    assert (state.total_planificado, state.sin_sumarios) == (54, 54)
    keys = index.estados_boletines(origen="melilla.es")
    assert keys["BOME-BX-1986-1~280058"] == keys["BOME-BX-1986-1~280087"] == "sin_sumarios"
    assert "BOME-BX-1986-1" not in keys
    assert len(keys) == 54


# --------------------------------------------------------------------------- cap, failures, rotos


def test_the_cap_keeps_the_newest_and_the_next_run_continues(portal: Portal, index: SumarioIndex) -> None:
    sync = make_sync(index, portal)
    state = run(sync, max_boletines=2)
    assert state.estado == "completado"
    assert portal.fichas_pedidas() == DEFAULT_PLAN[:2]
    assert (state.total_planificado, state.limite_boletines, state.pendientes_tras_limite) == (2, 2, 4)
    assert "4 bulletins" in (state.mensaje or "")
    portal.requests.clear()
    state = run(sync, max_boletines=2)
    assert portal.fichas_pedidas() == DEFAULT_PLAN[2:4]
    assert state.pendientes_tras_limite == 2
    stored = index.estado().ultimas_sincronizaciones["melilla.es"]
    assert (stored["limite_boletines"], stored["pendientes_tras_limite"]) == (2, 2)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "3"])
def test_arguments_are_validated_before_any_request(portal: Portal, index: SumarioIndex, bad: object) -> None:
    sync = make_sync(index, portal)
    with pytest.raises(BusquedaInvalidaError, match="max_boletines"):
        sync.iniciar(max_boletines=bad)  # type: ignore[arg-type]
    for wrong in ({"desde": "ayer"}, {"desde": "2000-01-02", "hasta": "2000-01-01"}):
        with pytest.raises(BusquedaInvalidaError):
            sync.iniciar(**wrong)
    assert portal.requests == []
    assert index.lease() is None
    assert sync.estado().estado == "inactivo"


def test_a_ficha_answering_5xx_twice_becomes_roto_and_is_skipped(portal: Portal, index: SumarioIndex) -> None:
    portal.fichas[276000] = lambda request: httpx.Response(500)
    draws: list[tuple[float, float]] = []

    def pausa(low: float, high: float) -> float:
        draws.append((low, high))
        return 45.0

    waits = Waits(clock=portal.clock)
    sync = make_sync(index, portal, wait=waits, pausa_aleatoria=pausa)
    state = run(sync)
    assert (state.errores, state.rotos, state.hechos) == (1, 0, 6)
    assert row(index, "BOME-B-1999-3660")[:5] == ("melilla.es", 276000, "error", 500, 1)
    assert draws == [(PAUSA_TRAS_ERROR_MIN_SEGUNDOS, PAUSA_TRAS_ERROR_MAX_SEGUNDOS)]
    assert sum(waits.slices) == pytest.approx(45)  # the pause before the next bulletin
    assert "BOME-B-1999-3660" in (state.ultimo_error or "")

    portal.requests.clear()
    state = run(sync)  # errors are retried by default...
    assert portal.fichas_pedidas() == [276000]
    assert (state.errores, state.rotos) == (1, 1)
    assert row(index, "BOME-B-1999-3660")[:5] == ("melilla.es", 276000, "roto", 500, 2)

    portal.requests.clear()
    assert run(sync).total_planificado == 0  # ...but a roto is not
    assert portal.fichas_pedidas() == []

    portal.fichas[276000] = portal.fixture("ficha_1999_3660.html")
    state = run(sync, reintentar_rotos=True)
    assert portal.fichas_pedidas() == [276000]
    assert state.indexados == 1
    assert row(index, "BOME-B-1999-3660")[:5] == ("melilla.es", 276000, "indexado", None, 0)


def test_errors_are_not_retried_when_disabled(portal: Portal, index: SumarioIndex) -> None:
    portal.fichas[276000] = lambda request: httpx.Response(404)
    sync = make_sync(index, portal)
    state = run(sync)
    assert (state.errores, state.rotos) == (1, 0)
    assert row(index, "BOME-B-1999-3660")[:5] == ("melilla.es", 276000, "error", 404, 0)
    portal.requests.clear()
    assert run(sync, reintentar_errores=False).total_planificado == 0
    assert portal.requests == []
    assert run(sync).total_planificado == 1


def test_a_failed_bomemelilla_bulletin_is_planned_for_the_old_portal(portal: Portal, index: SumarioIndex) -> None:
    broken = bome_ref("BOME-B-2016-5302", date(2016, 1, 8))
    index.guardar_boletin(broken, [], "error", error="HTTP 500 for x")
    state = run(make_sync(index, portal), reintentar_errores=False)
    assert 216808 in portal.fichas_pedidas()
    assert state.total_planificado == 6
    assert row(index, "BOME-B-2016-5302")[:3] == ("melilla.es", 216808, "indexado")


def test_an_old_portal_failure_leaves_a_bomemelilla_failure_alone(portal: Portal, index: SumarioIndex) -> None:
    broken = bome_ref("BOME-B-2016-5302", date(2016, 1, 8))
    for _ in range(2):
        index.guardar_boletin(broken, [], "error", error="HTTP 500 for x")
    index._conn().execute("UPDATE bulletins SET estado = 'roto' WHERE cve = ?", (broken.cve,))
    index._conn().commit()
    portal.fichas[216808] = lambda request: httpx.Response(500)
    state = run(make_sync(index, portal, pausa_aleatoria=lambda low, high: 30.0))
    assert (state.errores, state.rotos) == (1, 0)  # not an old-portal roto
    assert row(index, "BOME-B-2016-5302")[:3] == ("bomemelilla.es", None, "roto")


# --------------------------------------------------------------------------- guard


def test_a_guard_cooldown_ends_the_run_as_bloqueado_without_any_request(portal: Portal, index: SumarioIndex) -> None:
    guard = portal.guard()
    guard.registrar(429)
    portal.clock[0] += 600
    state = run(make_sync(index, portal, guard=guard))
    assert state.estado == "bloqueado"
    assert state.origen == "melilla.es"
    assert portal.requests == []  # not even the catalog
    assert state.reintentar_tras_segundos == pytest.approx(ENFRIAMIENTO_SEGUNDOS - 600)
    assert "melilla.es" in (state.mensaje or "")
    assert index.lease() is None
    assert index.estado().ultimas_sincronizaciones["melilla.es"]["estado"] == "bloqueado"


@pytest.mark.parametrize("status", [403, 429, 503])
def test_a_block_on_a_ficha_stops_the_run(portal: Portal, index: SumarioIndex, status: int) -> None:
    portal.fichas[216807] = lambda request: httpx.Response(status)
    sync = make_sync(index, portal)
    state = run(sync)
    assert state.estado == "bloqueado"
    assert portal.fichas_pedidas() == DEFAULT_PLAN[:2]
    assert (state.hechos, state.indexados) == (1, 1)
    assert row(index, "BOME-B-2016-5301") is None  # a refused bulletin is not recorded
    assert sync.guard.en_enfriamiento()
    assert state.reintentar_tras_segundos == pytest.approx(ENFRIAMIENTO_SEGUNDOS)


def test_a_catalog_the_portal_refuses_ends_the_run_as_bloqueado(portal: Portal, index: SumarioIndex) -> None:
    portal.catalog = lambda request: httpx.Response(403)
    state = run(make_sync(index, portal))
    assert state.estado == "bloqueado"
    assert portal.fichas_pedidas() == []
    assert "catalog" in (state.ultimo_error or "")


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda request: httpx.Response(500), id="http-500"),
        pytest.param(lambda request: httpx.Response(200, text="<html><body>Mantenimiento</body></html>"), id="unparsable"),
    ],
)
def test_a_catalog_that_cannot_be_read_ends_the_run_as_fallido(
    portal: Portal, index: SumarioIndex, answer: Handler
) -> None:
    portal.catalog = answer
    state = run(make_sync(index, portal))
    assert state.estado == "fallido"
    assert "catalog" in (state.mensaje or "")
    assert portal.fichas_pedidas() == []
    assert index.lease() is None


def test_the_run_waits_for_a_full_error_budget_before_the_next_ficha(portal: Portal, index: SumarioIndex) -> None:
    guard = portal.guard()
    body = (ANTIGUO / "ficha_5302.html").read_bytes()

    def page_while_others_err(request: httpx.Request) -> httpx.Response:
        portal.clock[0] -= 480
        for _ in range(3):  # e.g. the interactive tools got three errors 8 minutes ago
            guard.registrar(404)
        portal.clock[0] += 480
        return httpx.Response(200, content=body)

    portal.fichas[216808] = page_while_others_err
    seen: list[tuple[int, str | None]] = []
    holder: list[SincronizadorPortalAntiguo] = []
    waits = Waits(clock=portal.clock, on_wait=lambda: seen.append((len(portal.fichas_pedidas()), holder[0].estado().mensaje)) or False)
    sync = make_sync(index, portal, guard=guard, wait=waits)
    holder.append(sync)
    state = run(sync)
    assert state.estado == "completado"
    assert sum(waits.slices) == pytest.approx(120)
    assert {count for count, _ in seen} == {1}
    assert "preventive pause" in (seen[0][1] or "") and "BOME-B-2016-5301" in (seen[0][1] or "")
    assert state.hechos == 6


class PausingGuard(GuardiaSitio):
    """A guard whose budget fills once, just before its ``at``-th request (1-based)."""

    def __init__(self, portal: Portal, at: int, wait: float) -> None:
        super().__init__(None, clock=lambda: portal.clock[0], sitio="melilla.es")
        self.checks = 0
        self.at = at
        self.pause = wait

    def comprobar(self, url: str = "") -> None:
        self.checks += 1
        if self.checks == self.at:
            raise BomePausaPreventivaError("budget full", status=None, url=url, retry_after=self.pause)
        super().comprobar(url)


def test_a_preventive_pause_mid_run_resumes_the_same_bulletin(portal: Portal, index: SumarioIndex) -> None:
    guard = PausingGuard(portal, 4, 90.0)  # the catalog, 216808, 216807, then 276000 pauses
    waits = Waits(clock=portal.clock)
    state = run(make_sync(index, portal, guard=guard, wait=waits))
    assert state.estado == "completado"
    assert sum(waits.slices) == pytest.approx(90)
    assert max(waits.slices) <= 30
    assert portal.fichas_pedidas() == DEFAULT_PLAN  # 276000 asked once, after the pause
    assert (state.hechos, state.errores, state.indexados) == (6, 0, 4)
    assert state.mensaje is None


def test_the_sync_refuses_a_guard_of_another_site(portal: Portal, index: SumarioIndex) -> None:
    with pytest.raises(ValueError, match="melilla.es"):
        SincronizadorPortalAntiguo(index, portal.factory, guard=GuardiaSitio(None))


def test_the_default_guard_is_the_persisted_melilla_one(portal: Portal, index: SumarioIndex) -> None:
    sync = SincronizadorPortalAntiguo(index, portal.factory)
    assert sync.guard.sitio == "melilla.es"
    assert sync.guard.path is not None and sync.guard.path.name == "estado_sitio_melilla.json"


def test_the_portal_factory_gets_the_sync_guard(portal: Portal, index: SumarioIndex) -> None:
    sync = make_sync(index, portal)
    run(sync, max_boletines=1)
    assert portal.guards == [sync.guard]


def test_the_default_portal_uses_the_sync_pace(
    portal: Portal, index: SumarioIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, object]] = []

    def fake_portal(**kwargs: object) -> PortalAntiguo:
        built.append(kwargs)
        return PortalAntiguo(transport=httpx.MockTransport(portal), guard=kwargs["guard"], cache_path=None,
                             polite_delay=0.0, jitter=0.0)

    monkeypatch.setattr(sync_antiguo_module, "PortalAntiguo", fake_portal)
    guard = portal.guard()
    sync = SincronizadorPortalAntiguo(index, hoy=lambda: TODAY, guard=guard, wait=Waits())
    assert run(sync, max_boletines=1).estado == "completado"
    assert built == [{"polite_delay": SYNC_POLITE_DELAY, "jitter": SYNC_JITTER, "guard": guard}]
    assert (SYNC_POLITE_DELAY, SYNC_JITTER) == (2.0, 1.0)


# --------------------------------------------------------------------------- cancel, lease, state


def slow_ficha(name: str) -> tuple[Handler, threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()
    body = (ANTIGUO / name).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(TIMEOUT)
        return httpx.Response(200, content=body)

    return handler, entered, release


def test_cancel_mid_run(portal: Portal, index: SumarioIndex) -> None:
    portal.fichas[216808], entered, release = slow_ficha("ficha_5302.html")
    sync = make_sync(index, portal)
    sync.iniciar()
    assert entered.wait(TIMEOUT)
    running = sync.estado()
    assert (running.estado, running.cve_actual, running.origen) == ("en_curso", "BOME-B-2016-5302", "melilla.es")
    assert index.lease() is not None and index.lease()["origen"] == "melilla.es"
    assert sync.iniciar().estado == "en_curso"  # the running job, no second thread
    sync.cancelar()
    release.set()
    assert sync.esperar(TIMEOUT)
    state = sync.estado()
    assert state.estado == "cancelado"
    assert state.hechos == 1
    assert portal.fichas_pedidas() == [216808]
    assert index.lease() is None


class BomeSite:
    """bomemelilla.es behind a MockTransport whose calendar can be held open."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if request.url.path == "/api/bomes/calendar":
            self.entered.set()
            self.release.wait(TIMEOUT)
            return httpx.Response(200, text="[]")
        return httpx.Response(404)

    def factory(self, *, guard: GuardiaSitio | None = None) -> BomeClient:
        return BomeClient(transport=httpx.MockTransport(self), polite_delay=0, guard=guard)


def bome_sync(index: SumarioIndex, site: BomeSite) -> SincronizadorIndice:
    return SincronizadorIndice(index, site.factory, hoy=lambda: TODAY, guard=GuardiaSitio(None), wait=Waits())


def test_a_running_old_portal_sync_blocks_a_bomemelilla_sync(portal: Portal, index: SumarioIndex) -> None:
    portal.fichas[216808], entered, release = slow_ficha("ficha_5302.html")
    old = make_sync(index, portal)
    old.iniciar()
    assert entered.wait(TIMEOUT)
    site = BomeSite()
    other = bome_sync(index, site)
    answer = other.iniciar()
    assert answer.estado == "en_curso_en_otro_proceso"
    assert answer.origen == "bomemelilla.es"
    assert answer.lease["propietario"] == old.owner and answer.lease["origen"] == "melilla.es"
    assert "melilla.es" in (answer.mensaje or "")
    release.set()
    assert old.esperar(TIMEOUT) and other.esperar(TIMEOUT)
    assert site.requests == []
    assert old.estado().estado == "completado"


def test_a_running_bomemelilla_sync_blocks_the_old_portal_sync(portal: Portal, index: SumarioIndex) -> None:
    site = BomeSite()
    site.release.clear()
    bome = bome_sync(index, site)
    bome.iniciar()
    assert site.entered.wait(TIMEOUT)
    old = make_sync(index, portal)
    answer = old.iniciar()
    assert answer.estado == "en_curso_en_otro_proceso"
    assert answer.origen == "melilla.es"
    assert answer.lease["origen"] == "bomemelilla.es"
    site.release.set()
    assert bome.esperar(TIMEOUT) and old.esperar(TIMEOUT)
    assert portal.requests == []
    assert bome.estado().origen == "bomemelilla.es"
    assert run(old, max_boletines=1).estado == "completado"  # free again once it ends


def test_a_stale_lease_is_taken_over(portal: Portal, index: SumarioIndex) -> None:
    assert index.adquirir_lease("muerto", now=time.time() - 10_000) is None
    assert run(make_sync(index, portal), max_boletines=1).estado == "completado"
    assert index.lease() is None


def test_the_idle_state_names_the_origin(portal: Portal, index: SumarioIndex) -> None:
    idle = make_sync(index, portal).estado()
    assert (idle.estado, idle.origen) == ("inactivo", "melilla.es")
    assert bome_sync(index, BomeSite()).estado().origen == "bomemelilla.es"


def test_the_last_sync_is_kept_per_origin(portal: Portal, index: SumarioIndex) -> None:
    run(make_sync(index, portal), max_boletines=1)
    state = index.estado()
    assert state.ultima_sincronizacion is None  # still the bomemelilla.es one
    assert state.ultimas_sincronizaciones["bomemelilla.es"] is None
    old = state.ultimas_sincronizaciones["melilla.es"]
    assert (old["estado"], old["origen"], old["hechos"]) == ("completado", "melilla.es", 1)
    assert "lease" not in old

    site = BomeSite()
    run(bome_sync(index, site))
    state = index.estado()
    assert state.ultima_sincronizacion["origen"] == "bomemelilla.es"
    assert state.ultimas_sincronizaciones["bomemelilla.es"] == state.ultima_sincronizacion
    assert state.ultimas_sincronizaciones["melilla.es"] == old
    json.dumps(state.to_dict())


def test_the_worker_connection_is_closed_after_each_run(portal: Portal, index: SumarioIndex) -> None:
    sync = make_sync(index, portal)
    index.estado()
    baseline = index.conexiones_abiertas()
    run(sync, max_boletines=1)
    run(sync, max_boletines=1)
    assert index.conexiones_abiertas() == baseline
