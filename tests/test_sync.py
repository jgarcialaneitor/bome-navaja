"""Background index synchronisation, offline (MockTransport, no sleeps)."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from bome_navaja import sync as sync_module
from bome_navaja.client import BomeClient
from bome_navaja.guard import ENFRIAMIENTO_SEGUNDOS, GuardiaSitio
from bome_navaja.index import SumarioIndex
from bome_navaja.search import BusquedaInvalidaError
from bome_navaja.sync import (
    DEFAULT_MAX_BULLETINS_PER_RUN,
    MAX_PAUSAS_PREVENTIVAS_SEGUIDAS,
    PAUSA_TRAS_ERROR_MAX_SEGUNDOS,
    PAUSA_TRAS_ERROR_MIN_SEGUNDOS,
    SYNC_JITTER,
    SYNC_POLITE_DELAY,
    SincronizadorIndice,
    SyncSettings,
    sync_settings_from_env,
)

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 23)
TIMEOUT = 10

CALENDAR = [
    ("BOME-B-2014-5092", "2014-01-03"),
    ("BOME-B-2026-6415", "2026-09-18"),
    ("BOME-BX-2026-41", "2026-09-18"),
    ("BOME-B-2026-6416", "2026-09-22"),
]


def b6415_html() -> str:
    """A bulletin page for 6415 built from 6416 with its own article CVEs."""
    return (
        (FIXTURES / "b6416.html")
        .read_text("utf-8")
        .replace("BOME-B-2026-6416", "BOME-B-2026-6415")
        .replace("BOME-A-2026-10", "BOME-A-2026-20")
    )


class Site:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.guards: list[GuardiaSitio | None] = []
        self.clock = [1_000_000.0]
        """Fake wall clock of the tests' guards; the default :class:`Waits` moves it."""
        self.lock = threading.Lock()
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {
            "/api/bomes/calendar": lambda request: httpx.Response(
                200,
                text=json.dumps(
                    [{"title": f"Nº {c.rsplit('-', 1)[1]}", "start": d, "url": f"/bome/{c}"} for c, d in CALENDAR]
                ),
            ),
            "/bome/BOME-B-2026-6416": self.fixture("b6416.html"),
            "/bome/BOME-BX-2026-41": self.fixture("bx41.html"),
            "/bome/BOME-B-2014-5092": self.fixture("b5092.html"),
            "/bome/BOME-B-2026-6415": lambda request: httpx.Response(500),
        }

    @staticmethod
    def fixture(name: str) -> Callable[[httpx.Request], httpx.Response]:
        body = (FIXTURES / name).read_bytes()
        return lambda request: httpx.Response(200, content=body)

    def fix_6415(self) -> None:
        html = b6415_html()
        self.routes["/bome/BOME-B-2026-6415"] = lambda request: httpx.Response(200, text=html)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(str(request.url))
        handler = self.routes.get(request.url.path)
        return handler(request) if handler else httpx.Response(404)

    def factory(self, *, guard: GuardiaSitio | None = None) -> BomeClient:
        self.guards.append(guard)
        return BomeClient(transport=httpx.MockTransport(self), polite_delay=0, guard=guard)

    def guard(self) -> GuardiaSitio:
        """A memory-only guard on the fake clock."""
        return GuardiaSitio(None, clock=lambda: self.clock[0])

    def bulletin_paths(self) -> list[str]:
        return [u.split("bomemelilla.es", 1)[1] for u in self.requests if "/bome/BOME-" in u]


@pytest.fixture
def site() -> Site:
    return Site()


@pytest.fixture
def index(tmp_path: Path) -> SumarioIndex:
    idx = SumarioIndex(tmp_path / "sumarios.sqlite3")
    yield idx
    idx.close()


def make_sync(index: SumarioIndex, site: Site, **kwargs) -> SincronizadorIndice:
    """A sync on a fake-clock guard whose waits never sleep (they move the clock)."""
    kwargs.setdefault("guard", site.guard())
    kwargs.setdefault("wait", Waits(clock=site.clock))
    return SincronizadorIndice(index, site.factory, hoy=lambda: TODAY, **kwargs)


def run(sync: SincronizadorIndice, **kwargs):
    sync.iniciar(**kwargs)
    assert sync.esperar(TIMEOUT), "sync thread did not finish"
    return sync.estado()


def test_full_run(site: Site, index: SumarioIndex) -> None:
    sync = make_sync(index, site)
    first = sync.iniciar()
    assert first.estado == "en_curso"
    assert sync.esperar(TIMEOUT)
    state = sync.estado()
    assert state.estado == "completado"
    assert (state.total_planificado, state.hechos) == (4, 4)
    assert (state.indexados, state.sin_sumarios, state.errores, state.rotos) == (2, 1, 1, 0)
    assert state.cve_actual is None
    assert state.iniciado and state.finalizado
    assert "BOME-B-2026-6415" in (state.ultimo_error or "")
    assert state.segundos_por_boletin is not None
    assert state.eta_segundos == 0
    json.dumps(state.to_dict())

    calendar = next(u for u in site.requests if "/api/bomes/calendar" in u)
    assert "start=2014-01-01" in calendar and "end=2026-09-23" in calendar
    # Newest first.
    paths = site.bulletin_paths()
    assert paths[0] == "/bome/BOME-B-2026-6416"
    assert paths[-1] == "/bome/BOME-B-2014-5092"

    stored = index.estado()
    assert stored.boletines == {"indexado": 2, "sin_sumarios": 1, "error": 1, "roto": 0, "total": 4}
    assert stored.articulos == 13 + 2 + 4
    assert stored.calendario_conocidos == 4
    assert stored.pendientes == 1
    assert stored.ultima_sincronizacion["estado"] == "completado"
    assert stored.sincronizacion_en_curso is None  # lease released
    assert index.estado_boletin("BOME-B-2014-5092") == "sin_sumarios"
    assert index.buscar("relacion provisional").total == 5


def test_errors_are_retried_on_the_next_run(site: Site, index: SumarioIndex) -> None:
    sync = make_sync(index, site)
    run(sync)
    assert index.estado_boletin("BOME-B-2026-6415") == "error"
    site.fix_6415()
    site.requests.clear()
    state = run(sync, reindexar_recientes_dias=0)
    assert state.total_planificado == 1
    assert site.bulletin_paths() == ["/bome/BOME-B-2026-6415"]
    assert index.estado_boletin("BOME-B-2026-6415") == "indexado"
    assert index.estado().pendientes == 0


def test_errors_not_retried_when_disabled(site: Site, index: SumarioIndex) -> None:
    sync = make_sync(index, site)
    run(sync)
    site.requests.clear()
    state = run(sync, reindexar_recientes_dias=0, reintentar_errores=False)
    assert state.total_planificado == 0
    assert state.estado == "completado"
    assert site.bulletin_paths() == []


def test_resume_skips_indexed_except_recent_window(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    sync = make_sync(index, site)
    run(sync)
    site.requests.clear()
    assert run(sync, reindexar_recientes_dias=0).total_planificado == 0
    site.requests.clear()
    state = run(sync, reindexar_recientes_dias=7)
    assert state.total_planificado == 3
    assert "/bome/BOME-B-2014-5092" not in site.bulletin_paths()
    assert index.estado().articulos == 13 + 13 + 2 + 4  # re-indexing is idempotent


def test_date_range_is_forwarded(site: Site, index: SumarioIndex) -> None:
    sync = make_sync(index, site)
    run(sync, desde="2026-09-01", hasta=date(2026, 9, 30))
    calendar = next(u for u in site.requests if "/api/bomes/calendar" in u)
    assert "start=2026-09-01" in calendar and "end=2026-09-30" in calendar


def test_cancel_mid_run_and_second_start(site: Site, index: SumarioIndex) -> None:
    entered = threading.Event()
    release = threading.Event()
    body = (FIXTURES / "b6416.html").read_bytes()

    def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(TIMEOUT)
        return httpx.Response(200, content=body)

    site.routes["/bome/BOME-B-2026-6416"] = slow
    sync = make_sync(index, site)
    sync.iniciar()
    assert entered.wait(TIMEOUT)
    running = sync.estado()
    assert running.estado == "en_curso"
    assert running.cve_actual == "BOME-B-2026-6416"
    assert index.estado().sincronizacion_en_curso is not None

    threads_before = threading.active_count()
    again = sync.iniciar()
    assert again.estado == "en_curso"
    assert threading.active_count() == threads_before

    assert sync.cancelar().estado == "en_curso"  # cooperative: finishes the current bulletin
    release.set()
    assert sync.esperar(TIMEOUT)
    state = sync.estado()
    assert state.estado == "cancelado"
    assert state.hechos == 1
    assert index.estado_boletin("BOME-B-2026-6416") == "indexado"
    assert index.estado().sincronizacion_en_curso is None
    assert index.estado().ultima_sincronizacion["estado"] == "cancelado"


def test_live_lease_from_another_process_blocks(site: Site, index: SumarioIndex) -> None:
    assert index.adquirir_lease("otro-proceso", now=time.time()) is None
    sync = make_sync(index, site)
    state = sync.iniciar()
    assert state.estado == "en_curso_en_otro_proceso"
    assert state.lease is not None and state.lease["propietario"] == "otro-proceso"
    assert sync.esperar(TIMEOUT)
    assert site.requests == []
    assert index.lease()["propietario"] == "otro-proceso"


def test_stale_lease_is_taken_over(site: Site, index: SumarioIndex) -> None:
    assert index.adquirir_lease("muerto", now=time.time() - 10_000) is None
    state = run(make_sync(index, site))
    assert state.estado == "completado"
    assert index.lease() is None


def test_calendar_failure_is_fatal(site: Site, index: SumarioIndex) -> None:
    site.routes["/api/bomes/calendar"] = lambda request: httpx.Response(500)
    state = run(make_sync(index, site))
    assert state.estado == "fallido"
    assert "500" in (state.mensaje or "")
    assert state.total_planificado == 0
    assert index.lease() is None
    assert index.estado().ultima_sincronizacion["estado"] == "fallido"


def test_idle_state_and_argument_validation(site: Site, index: SumarioIndex) -> None:
    from bome_navaja.search import BusquedaInvalidaError

    sync = make_sync(index, site)
    idle = sync.estado()
    assert idle.estado == "inactivo"
    assert idle.to_dict()["hechos"] == 0
    assert sync.cancelar().estado == "inactivo"
    for bad in ({"desde": "ayer"}, {"reindexar_recientes_dias": -1}, {"desde": "2026-09-10", "hasta": "2026-09-01"}):
        with pytest.raises(BusquedaInvalidaError):
            sync.iniciar(**bad)
    assert site.requests == []
    assert index.lease() is None


def test_worker_connection_is_closed_after_each_run(site: Site, index: SumarioIndex) -> None:
    sync = make_sync(index, site)
    index.estado()  # main-thread connection
    baseline = index.conexiones_abiertas()
    run(sync)
    run(sync, reindexar_recientes_dias=0)
    assert index.conexiones_abiertas() == baseline


# --------------------------------------------------------------------------- verify findings (task 5 review)


def test_a_bulletin_whose_save_fails_is_recorded_and_the_crawl_goes_on(
    site: Site, index: SumarioIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bome_navaja.models import BomeStorageError

    real = index.guardar_boletin

    def flaky(ref, articles, estado, error=None):
        if ref.cve == "BOME-BX-2026-41" and estado != "error":
            raise BomeStorageError("UNIQUE constraint failed: articles.cve", path=str(index.path))
        return real(ref, articles, estado, error)

    monkeypatch.setattr(index, "guardar_boletin", flaky)
    site.fix_6415()
    state = run(make_sync(index, site))
    assert state.estado == "completado"
    assert (state.hechos, state.errores, state.indexados, state.sin_sumarios) == (4, 1, 2, 1)
    assert "BOME-BX-2026-41" in (state.ultimo_error or "")
    assert index.estado_boletin("BOME-BX-2026-41") == "error"


def test_an_unwritable_index_ends_the_sync_as_failed(
    site: Site, index: SumarioIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bome_navaja.models import BomeStorageError

    def broken(ref, articles, estado, error=None):
        raise BomeStorageError("database or disk is full", path=str(index.path))

    monkeypatch.setattr(index, "guardar_boletin", broken)
    site.fix_6415()
    state = run(make_sync(index, site))
    assert state.estado == "fallido"
    assert state.hechos == 3  # stops after 3 consecutive bulletins that could not even be recorded
    assert "disk is full" in (state.mensaje or "")
    assert "could not be recorded" in (state.ultimo_error or "")
    assert index.lease() is None


def test_sync_accepts_datetimes_as_dates(site: Site, index: SumarioIndex) -> None:
    from datetime import datetime

    state = run(make_sync(index, site), desde=datetime(2026, 9, 1, 12, 0), hasta=date(2026, 9, 30))
    assert state.estado == "completado"
    assert (state.desde, state.hasta) == ("2026-09-01", "2026-09-30")
    calendar = httpx.URL(next(u for u in site.requests if "/api/bomes/calendar" in u))
    assert (calendar.params["start"], calendar.params["end"]) == ("2026-09-01", "2026-09-30")


# --------------------------------------------------------------------------- circuit breaker (polite-sync task 2)

B6416 = "/bome/BOME-B-2026-6416"


def blocked(status: int = 429, retry_after: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return lambda request: httpx.Response(status, headers=headers)


def dropped(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection reset by peer", request=request)


def sequence(*handlers: Callable[[httpx.Request], httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer with each handler in turn, repeating the last one."""
    remaining = list(handlers)

    def handler(request: httpx.Request) -> httpx.Response:
        current = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return current(request)

    return handler


class Waits:
    """Injected back-off wait: records each slice, never sleeps, optionally moves a fake clock."""

    def __init__(self, clock: list[float] | None = None, on_wait: Callable[[], bool] | None = None) -> None:
        self.slices: list[float] = []
        self.clock = clock
        self.on_wait = on_wait

    def __call__(self, seconds: float) -> bool:
        self.slices.append(seconds)
        if self.clock is not None:
            self.clock[0] += seconds
        return self.on_wait() if self.on_wait else False



def seeded_guard(site: Site, errors: int, *, age: float = 0.0) -> GuardiaSitio:
    """A fake-clock guard that already counted ``errors`` error answers ``age`` seconds ago."""
    guard = site.guard()
    site.clock[0] -= age
    for _ in range(errors):
        guard.registrar(500)
    site.clock[0] += age
    return guard


def _without_article(html: str, cve: str) -> str:
    """Drop one article block from a bulletin page, as the live site sometimes does."""
    pattern = re.compile(
        r'<ul class="articulo-list">(?:(?!<ul class="articulo-list">).)*?' + re.escape(cve) + r".*?</ul>",
        re.DOTALL,
    )
    stripped, count = pattern.subn("", html, count=1)
    assert count == 1
    return stripped


@pytest.mark.parametrize("status", [403, 429, 503])
def test_a_site_block_ends_the_sync_at_once_and_closes_the_guard(
    site: Site, index: SumarioIndex, status: int
) -> None:
    site.fix_6415()
    site.routes[B6416] = blocked(status)
    waits = Waits(clock=site.clock)
    sync = make_sync(index, site, wait=waits)
    state = run(sync)
    assert state.estado == "bloqueado"
    assert site.bulletin_paths() == [B6416]  # no retry against a closed guard, no later bulletin
    assert waits.slices == []
    assert (state.hechos, state.errores, state.indexados) == (0, 0, 0)
    assert index.estado_boletin("BOME-B-2026-6416") is None  # not recorded at all
    assert str(status) in (state.ultimo_error or "")
    mensaje = state.mensaje or ""
    assert "firewall" in mensaje and "kept" in mensaje
    assert state.reintentar_tras_segundos == pytest.approx(ENFRIAMIENTO_SEGUNDOS)
    assert sync.guard.en_enfriamiento()
    assert index.lease() is None
    assert index.estado().ultima_sincronizacion["estado"] == "bloqueado"


def test_a_long_retry_after_is_reported(site: Site, index: SumarioIndex) -> None:
    site.routes[B6416] = blocked(429, "9000")
    state = run(make_sync(index, site))
    assert state.estado == "bloqueado"
    assert state.reintentar_tras_segundos == pytest.approx(9000)


def test_a_sync_started_during_a_cooldown_ends_at_once_without_any_request(
    site: Site, index: SumarioIndex
) -> None:
    guard = site.guard()
    guard.registrar(429)
    site.clock[0] += 500
    state = run(make_sync(index, site, guard=guard))
    assert state.estado == "bloqueado"
    assert site.requests == []
    assert state.reintentar_tras_segundos == pytest.approx(ENFRIAMIENTO_SEGUNDOS - 500)
    assert "429" in (state.mensaje or "")
    assert state.total_planificado == 0
    assert index.lease() is None
    assert index.estado().ultima_sincronizacion["estado"] == "bloqueado"


def test_a_cooldown_set_meanwhile_by_another_process_stops_the_sync(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    guard = site.guard()
    body = (FIXTURES / "b6416.html").read_bytes()

    def page_then_blocked_elsewhere(request: httpx.Request) -> httpx.Response:
        guard.registrar(None)  # e.g. the interactive tools lost two requests in a row
        guard.registrar(None)
        return httpx.Response(200, content=body)

    site.routes[B6416] = page_then_blocked_elsewhere
    state = run(make_sync(index, site, guard=guard))
    assert state.estado == "bloqueado"
    assert site.bulletin_paths() == [B6416]
    assert state.reintentar_tras_segundos == pytest.approx(ENFRIAMIENTO_SEGUNDOS)


def test_a_block_on_the_calendar_is_bloqueado(site: Site, index: SumarioIndex) -> None:
    site.routes["/api/bomes/calendar"] = blocked(503)
    sync = make_sync(index, site)
    state = run(sync)
    assert state.estado == "bloqueado"
    assert state.reintentar_tras_segundos == pytest.approx(ENFRIAMIENTO_SEGUNDOS)
    assert sync.guard.en_enfriamiento()
    assert site.bulletin_paths() == []


def test_the_sync_waits_for_a_full_error_budget_before_a_bulletin(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    guard = site.guard()
    body = (FIXTURES / "b6416.html").read_bytes()

    def page_while_others_err(request: httpx.Request) -> httpx.Response:
        site.clock[0] -= 480
        for _ in range(3):  # e.g. the interactive tools got three 404s 8 minutes ago
            guard.registrar(404)
        site.clock[0] += 480
        return httpx.Response(200, content=body)

    site.routes[B6416] = page_while_others_err
    seen: list[tuple[int, str | None]] = []
    waits = Waits(
        clock=site.clock,
        on_wait=lambda: seen.append((len(site.bulletin_paths()), sync.estado().mensaje)) or False,
    )
    sync = make_sync(index, site, guard=guard, wait=waits)
    state = run(sync)
    assert state.estado == "completado"
    assert sum(waits.slices) == pytest.approx(120)  # until the oldest error leaves the window
    assert max(waits.slices) <= 30  # cancellable slices that renew the lease
    assert {count for count, _ in seen} == {1}  # after 6416, before 6415
    mensaje = seen[0][1] or ""
    assert "preventive pause" in mensaje and "3 HTTP error" in mensaje and "BOME-B-2026-6415" in mensaje
    assert "not a block" in mensaje
    assert state.mensaje is None  # the note is cleared once the crawl goes on
    assert (state.hechos, state.indexados) == (4, 3)


def test_the_sync_waits_for_a_full_error_budget_before_the_calendar(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    seen: list[int] = []
    waits = Waits(clock=site.clock, on_wait=lambda: seen.append(len(site.requests)) or False)
    state = run(make_sync(index, site, guard=seeded_guard(site, 3, age=590), wait=waits))
    assert state.estado == "completado"
    assert sum(waits.slices) == pytest.approx(10)
    assert set(seen) == {0}


def test_a_preventive_pause_inside_a_bulletin_resumes_the_same_bulletin(
    site: Site, index: SumarioIndex
) -> None:
    site.fix_6415()
    html = _without_article((FIXTURES / "b6416.html").read_text("utf-8"), "BOME-A-2026-1051")
    html = _without_article(html, "BOME-A-2026-1056")
    site.routes[B6416] = lambda request: httpx.Response(200, text=html)
    guard = seeded_guard(site, 2)
    # Probe 1051 answers 404 (third error: budget full), so probe 1056 must wait.
    waits = Waits(clock=site.clock)
    state = run(make_sync(index, site, guard=guard, wait=waits))
    assert state.estado == "completado"
    paths = site.bulletin_paths()
    assert paths.count(B6416) == 1  # the page is not fetched again
    assert paths.count(B6416 + "/articulo/1051") == 1  # nor the probe that already failed
    assert paths.count(B6416 + "/articulo/1056") == 1
    assert paths.index(B6416 + "/articulo/1056") > paths.index(B6416 + "/articulo/1051")
    assert sum(waits.slices) == pytest.approx(600)
    assert (state.hechos, state.errores) == (4, 0)
    assert index.estado_boletin("BOME-B-2026-6416") == "indexado"


def test_a_budget_that_stays_full_ends_the_sync_instead_of_waiting_forever(
    site: Site, index: SumarioIndex
) -> None:
    guard = seeded_guard(site, 3)
    waits = Waits()  # the guard's clock never moves: the budget never frees up
    state = run(make_sync(index, site, guard=guard, wait=waits))
    assert state.estado == "bloqueado"
    assert len([s for s in waits.slices if s]) >= MAX_PAUSAS_PREVENTIVAS_SEGUIDAS
    assert site.bulletin_paths() == []
    assert "error budget" in (state.mensaje or "")


def test_a_broken_page_is_followed_by_a_random_pause(site: Site, index: SumarioIndex) -> None:
    draws: list[tuple[float, float]] = []

    def pausa(low: float, high: float) -> float:
        draws.append((low, high))
        return 42.0

    waits = Waits(clock=site.clock)
    state = run(make_sync(index, site, wait=waits, pausa_aleatoria=pausa))
    assert state.estado == "completado"
    assert draws == [(PAUSA_TRAS_ERROR_MIN_SEGUNDOS, PAUSA_TRAS_ERROR_MAX_SEGUNDOS)]
    assert (PAUSA_TRAS_ERROR_MIN_SEGUNDOS, PAUSA_TRAS_ERROR_MAX_SEGUNDOS) == (30, 60)
    assert waits.slices == [30, 12]
    assert state.errores == 1


def test_other_failures_need_no_pause(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    site.routes["/bome/BOME-BX-2026-41"] = lambda request: httpx.Response(404)
    waits = Waits(clock=site.clock)
    state = run(make_sync(index, site, wait=waits, pausa_aleatoria=lambda low, high: 42.0))
    assert state.errores == 1
    assert waits.slices == []


def test_cancel_during_a_preventive_pause_ends_the_sync_promptly(site: Site, index: SumarioIndex) -> None:
    guard = seeded_guard(site, 3)  # the guard's clock is frozen: the pause would last forever
    waiting = threading.Event()
    sync = SincronizadorIndice(index, site.factory, hoy=lambda: TODAY, guard=guard)  # real, cancellable wait
    real_wait = sync._wait

    def wait(seconds: float) -> bool:
        waiting.set()
        return real_wait(seconds)

    sync._wait = wait
    started = time.monotonic()
    sync.iniciar()
    assert waiting.wait(TIMEOUT)
    sync.cancelar()
    assert sync.esperar(TIMEOUT), "the preventive pause did not honour the cancellation"
    assert time.monotonic() - started < TIMEOUT
    state = sync.estado()
    assert state.estado == "cancelado"
    assert (state.hechos, state.errores) == (0, 0)
    assert site.bulletin_paths() == []
    assert index.lease() is None


def test_the_lease_is_renewed_during_a_long_preventive_pause(
    site: Site, index: SumarioIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    site.fix_6415()
    clock = site.clock
    guard = seeded_guard(site, 3, age=540)  # 60 s to wait
    renewals: list[float | None] = []
    real = index.renovar_lease

    def counting(owner, *, now=None):
        renewals.append(now)
        return real(owner, now=now)

    monkeypatch.setattr(index, "renovar_lease", counting)
    waits = Waits(clock=clock)
    start = clock[0]
    sync = make_sync(index, site, guard=guard, stale_after=40, clock=lambda: clock[0], wait=waits)
    state = run(sync)
    assert state.estado == "completado"
    assert waits.slices == [10] * 6  # slices of stale_after / 4
    assert len(renewals) == 6 + 4  # one per slice + one per planned bulletin
    assert renewals[:6] == [start + 10 * i for i in range(1, 7)]


def test_losing_the_lease_during_a_preventive_pause_fails_the_sync(site: Site, index: SumarioIndex) -> None:
    holder: list[SincronizadorIndice] = []

    def steal() -> bool:
        index.liberar_lease(holder[0].owner)
        index.adquirir_lease("otro-proceso", now=time.time())
        return False

    sync = make_sync(index, site, guard=seeded_guard(site, 3), wait=Waits(clock=site.clock, on_wait=steal))
    holder.append(sync)
    state = run(sync)
    assert state.estado == "fallido"
    assert "lease" in (state.mensaje or "")
    assert site.bulletin_paths() == []
    assert index.lease()["propietario"] == "otro-proceso"


def test_two_transport_failures_in_a_row_end_the_sync_as_bloqueado(site: Site, index: SumarioIndex) -> None:
    for path in (B6416, "/bome/BOME-B-2026-6415", "/bome/BOME-BX-2026-41"):
        site.routes[path] = dropped
    waits = Waits(clock=site.clock)
    sync = make_sync(index, site, wait=waits)
    state = run(sync)
    assert state.estado == "bloqueado"
    assert (state.hechos, state.errores, state.rotos) == (2, 2, 0)
    for cve in ("BOME-B-2026-6416", "BOME-B-2026-6415"):
        assert index.estado_boletin(cve) == "error"  # recorded, never roto
    assert site.bulletin_paths() == [B6416, "/bome/BOME-B-2026-6415"]
    assert waits.slices == []  # transport failures are not retried
    assert "BOME-B-2026-6415" in (state.ultimo_error or "")
    assert "firewall" in (state.mensaje or "")
    assert state.reintentar_tras_segundos == pytest.approx(ENFRIAMIENTO_SEGUNDOS)
    assert sync.guard.en_enfriamiento()
    assert index.lease() is None


@pytest.mark.parametrize(
    "middle",
    [
        pytest.param(lambda request: httpx.Response(200, text=b6415_html()), id="success"),
        pytest.param(lambda request: httpx.Response(500), id="non-transport-failure"),
    ],
)
def test_any_answer_resets_the_transport_failure_streak(
    site: Site, index: SumarioIndex, middle: Callable[[httpx.Request], httpx.Response]
) -> None:
    # Plan order: 6416, 6415, BX-41, 5092. Two transport failures, but not in a row.
    site.routes[B6416] = dropped
    site.routes["/bome/BOME-B-2026-6415"] = middle
    site.routes["/bome/BOME-BX-2026-41"] = dropped
    state = run(make_sync(index, site))
    assert state.estado == "completado"
    assert state.hechos == 4
    assert index.estado_boletin("BOME-BX-2026-41") == "error"
    assert index.estado_boletin("BOME-B-2014-5092") == "sin_sumarios"


# --------------------------------------------------------------------------- pace and budget (polite-sync task 3)

NEWEST_FIRST = [B6416, "/bome/BOME-B-2026-6415", "/bome/BOME-BX-2026-41", "/bome/BOME-B-2014-5092"]


def test_sync_pace_and_budget_defaults() -> None:
    assert (SYNC_POLITE_DELAY, SYNC_JITTER, DEFAULT_MAX_BULLETINS_PER_RUN) == (2.0, 1.0, 250)


def test_the_cap_keeps_the_newest_bulletins_and_reports_the_rest(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    sync = make_sync(index, site)
    started = sync.iniciar(max_boletines=2)
    assert started.limite_boletines == 2
    assert sync.esperar(TIMEOUT)
    state = sync.estado()
    assert state.estado == "completado"
    assert site.bulletin_paths() == NEWEST_FIRST[:2]
    assert (state.total_planificado, state.hechos, state.errores) == (2, 2, 0)
    assert (state.limite_boletines, state.pendientes_tras_limite) == (2, 2)
    assert state.eta_segundos == 0
    mensaje = state.mensaje or ""
    assert "limit" in mensaje and "2 bulletins remain" in mensaje and "sincronizar_indice" in mensaje
    assert index.estado_boletin("BOME-BX-2026-41") is None  # deferred, not touched
    stored = index.estado().ultima_sincronizacion
    assert (stored["limite_boletines"], stored["pendientes_tras_limite"]) == (2, 2)

    # The next run picks up where the cap stopped.
    site.requests.clear()
    state = run(sync, max_boletines=2, reindexar_recientes_dias=0)
    assert site.bulletin_paths() == NEWEST_FIRST[2:]
    assert (state.total_planificado, state.pendientes_tras_limite) == (2, 0)
    assert state.mensaje is None


def test_an_uncapped_run_reports_the_default_limit_and_no_message(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    state = run(make_sync(index, site))
    assert state.estado == "completado"
    assert state.total_planificado == 4
    assert (state.limite_boletines, state.pendientes_tras_limite) == (DEFAULT_MAX_BULLETINS_PER_RUN, 0)
    assert state.mensaje is None


def test_a_plan_exactly_at_the_cap_is_not_capped(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    state = run(make_sync(index, site), max_boletines=4)
    assert (state.total_planificado, state.pendientes_tras_limite) == (4, 0)
    assert state.mensaje is None


def test_the_constructor_sets_the_default_cap(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    state = run(make_sync(index, site, max_boletines=1))
    assert site.bulletin_paths() == NEWEST_FIRST[:1]
    assert (state.limite_boletines, state.pendientes_tras_limite) == (1, 3)
    site.requests.clear()
    state = run(make_sync(index, site, max_boletines=1), max_boletines=3, reindexar_recientes_dias=0)
    assert site.bulletin_paths() == NEWEST_FIRST[1:]


def test_a_capped_run_that_gets_blocked_keeps_the_blocked_message(site: Site, index: SumarioIndex) -> None:
    site.routes[B6416] = blocked(403)
    state = run(make_sync(index, site), max_boletines=2)
    assert state.estado == "bloqueado"
    assert "firewall" in (state.mensaje or "")
    assert state.pendientes_tras_limite == 2


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "3"])
def test_max_boletines_is_validated(site: Site, index: SumarioIndex, bad: object) -> None:
    sync = make_sync(index, site)
    with pytest.raises(BusquedaInvalidaError, match="max_boletines"):
        sync.iniciar(max_boletines=bad)  # type: ignore[arg-type]
    assert site.requests == []
    assert index.lease() is None


def test_the_default_client_factory_uses_the_sync_pace(
    site: Site, index: SumarioIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, object]] = []

    def fake_client(**kwargs: object) -> BomeClient:
        built.append(kwargs)
        return BomeClient(transport=httpx.MockTransport(site))

    monkeypatch.setattr(sync_module, "BomeClient", fake_client)
    site.fix_6415()
    default = SincronizadorIndice(index, hoy=lambda: TODAY)
    assert run(default).estado == "completado"
    assert built == [{"polite_delay": SYNC_POLITE_DELAY, "jitter": SYNC_JITTER, "guard": default.guard}]
    assert default.guard.estado()["fichero"] is None  # memory-only unless one is given
    guard = site.guard()
    run(SincronizadorIndice(index, hoy=lambda: TODAY, polite_delay=5.0, jitter=0.5, guard=guard))
    assert built[-1] == {"polite_delay": 5.0, "jitter": 0.5, "guard": guard}


def test_the_client_factory_gets_the_sync_guard(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    sync = make_sync(index, site)
    run(sync)
    assert site.guards == [sync.guard]


# --------------------------------------------------------------------------- settings from the environment


def test_settings_default_without_overrides() -> None:
    settings = sync_settings_from_env({})
    assert settings == SyncSettings(
        polite_delay=SYNC_POLITE_DELAY, max_boletines=DEFAULT_MAX_BULLETINS_PER_RUN, jitter=SYNC_JITTER
    )
    assert settings.warnings == ()
    blank = {"BOME_NAVAJA_SYNC_DELAY": "  ", "BOME_NAVAJA_SYNC_MAX_BOLETINES": ""}
    assert sync_settings_from_env(blank) == settings  # empty means unset


def test_settings_accept_valid_overrides() -> None:
    settings = sync_settings_from_env(
        {"BOME_NAVAJA_SYNC_DELAY": " 3.5 ", "BOME_NAVAJA_SYNC_MAX_BOLETINES": "100", "OTHER": "x"}
    )
    assert (settings.polite_delay, settings.max_boletines, settings.jitter) == (3.5, 100, SYNC_JITTER)
    assert settings.warnings == ()
    assert sync_settings_from_env({"BOME_NAVAJA_SYNC_DELAY": "1"}).polite_delay == 1.0


@pytest.mark.parametrize("value", ["0.2", "0", "-3"])
def test_a_delay_below_one_second_is_clamped_with_a_warning(value: str) -> None:
    settings = sync_settings_from_env({"BOME_NAVAJA_SYNC_DELAY": value})
    assert settings.polite_delay == 1.0
    (warning,) = settings.warnings
    assert "BOME_NAVAJA_SYNC_DELAY" in warning and value in warning and "1" in warning


@pytest.mark.parametrize("value", ["abc", "nan", "inf", "2s"])
def test_an_unparsable_delay_falls_back_to_the_default(value: str) -> None:
    settings = sync_settings_from_env({"BOME_NAVAJA_SYNC_DELAY": value})
    assert settings.polite_delay == SYNC_POLITE_DELAY
    (warning,) = settings.warnings
    assert "BOME_NAVAJA_SYNC_DELAY" in warning and repr(value) in warning


@pytest.mark.parametrize("value", ["0", "-5", "abc", "2.5", "1e3"])
def test_an_invalid_cap_falls_back_to_the_default(value: str) -> None:
    settings = sync_settings_from_env({"BOME_NAVAJA_SYNC_MAX_BOLETINES": value})
    assert settings.max_boletines == DEFAULT_MAX_BULLETINS_PER_RUN
    (warning,) = settings.warnings
    assert "BOME_NAVAJA_SYNC_MAX_BOLETINES" in warning and repr(value) in warning


def test_both_bad_overrides_give_two_warnings() -> None:
    settings = sync_settings_from_env({"BOME_NAVAJA_SYNC_DELAY": "x", "BOME_NAVAJA_SYNC_MAX_BOLETINES": "y"})
    assert len(settings.warnings) == 2


# --------------------------------------------------------------------------- broken pages (site-guard task 2)

B6415 = "/bome/BOME-B-2026-6415"


def failure(index: SumarioIndex, cve: str) -> tuple[str, int | None, int]:
    row = index._conn().execute(
        "SELECT estado, http_status, fallos_5xx FROM bulletins WHERE cve = ?", (cve,)
    ).fetchone()
    return (row[0], row[1], row[2])


def test_a_bulletin_page_500_is_an_error_with_its_status(site: Site, index: SumarioIndex) -> None:
    state = run(make_sync(index, site))
    assert (state.errores, state.rotos) == (1, 0)
    assert failure(index, "BOME-B-2026-6415") == ("error", 500, 1)


def test_a_second_500_in_a_later_run_makes_the_page_roto_and_it_is_skipped(
    site: Site, index: SumarioIndex
) -> None:
    sync = make_sync(index, site)
    run(sync)
    site.requests.clear()
    state = run(sync, reindexar_recientes_dias=0)
    assert site.bulletin_paths() == [B6415]  # the error is retried once more...
    assert (state.hechos, state.errores, state.rotos) == (1, 1, 1)
    assert failure(index, "BOME-B-2026-6415") == ("roto", 500, 2)
    assert index.estado().ultima_sincronizacion["rotos"] == 1
    # ...and then never again: not as an error, not even inside the recent window.
    site.requests.clear()
    state = run(sync)
    assert B6415 not in site.bulletin_paths()
    assert state.total_planificado == 2  # 6416 and BX-41 of the recent window
    assert state.rotos == 0
    site.requests.clear()
    state = run(sync, reindexar_recientes_dias=0)
    assert (state.total_planificado, site.bulletin_paths()) == (0, [])
    assert index.estado().pendientes == 0


def test_reintentar_rotos_requests_broken_pages_again(site: Site, index: SumarioIndex) -> None:
    sync = make_sync(index, site)
    run(sync)
    run(sync, reindexar_recientes_dias=0)
    assert index.estado_boletin("BOME-B-2026-6415") == "roto"
    site.requests.clear()
    state = run(sync, reindexar_recientes_dias=0, reintentar_errores=False, reintentar_rotos=True)
    assert site.bulletin_paths() == [B6415]
    assert (state.errores, state.rotos) == (1, 1)  # still broken
    assert failure(index, "BOME-B-2026-6415") == ("roto", 500, 3)
    site.fix_6415()
    site.requests.clear()
    state = run(sync, reindexar_recientes_dias=0, reintentar_rotos=True)
    assert site.bulletin_paths() == [B6415]
    assert (state.indexados, state.errores, state.rotos) == (1, 0, 0)
    assert failure(index, "BOME-B-2026-6415") == ("indexado", None, 0)


def test_errors_and_rotos_follow_their_own_switch(site: Site, index: SumarioIndex) -> None:
    site.routes["/bome/BOME-BX-2026-41"] = lambda request: httpx.Response(404)
    sync = make_sync(index, site)
    run(sync)
    run(sync, reindexar_recientes_dias=0)
    assert index.estado_boletin("BOME-B-2026-6415") == "roto"
    assert failure(index, "BOME-BX-2026-41") == ("error", 404, 0)
    site.requests.clear()
    run(sync, reindexar_recientes_dias=0)
    assert site.bulletin_paths() == ["/bome/BOME-BX-2026-41"]
    site.requests.clear()
    run(sync, reindexar_recientes_dias=0, reintentar_errores=False, reintentar_rotos=True)
    assert site.bulletin_paths() == [B6415]
    site.requests.clear()
    run(sync, reindexar_recientes_dias=0, reintentar_errores=False)
    assert site.bulletin_paths() == []


def test_timeouts_never_make_a_page_roto(site: Site, index: SumarioIndex) -> None:
    site.routes[B6415] = sequence(lambda request: httpx.Response(500), dropped)
    sync = make_sync(index, site, wait=Waits())
    for _ in range(3):
        state = run(sync, reindexar_recientes_dias=0)
        assert state.rotos == 0
    assert site.bulletin_paths().count(B6415) == 3  # timeouts keep being retried
    assert failure(index, "BOME-B-2026-6415") == ("error", None, 1)

