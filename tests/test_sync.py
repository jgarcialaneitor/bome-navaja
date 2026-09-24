"""Background index synchronisation, offline (MockTransport, no sleeps)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from bome_navaja.client import BomeClient
from bome_navaja.index import SumarioIndex
from bome_navaja.sync import SincronizadorIndice

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

    def factory(self) -> BomeClient:
        return BomeClient(transport=httpx.MockTransport(self), polite_delay=0)

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
    assert (state.indexados, state.sin_sumarios, state.errores) == (2, 1, 1)
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
    assert stored.boletines == {"indexado": 2, "sin_sumarios": 1, "error": 1, "total": 4}
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
    site.routes["/api/bomes/calendar"] = lambda request: httpx.Response(503)
    state = run(make_sync(index, site))
    assert state.estado == "fallido"
    assert "503" in (state.mensaje or "")
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


def test_a_blocked_bulletin_is_retried_after_back_off_and_not_recorded_as_error(
    site: Site, index: SumarioIndex
) -> None:
    site.fix_6415()
    site.routes[B6416] = sequence(blocked(429), Site.fixture("b6416.html"))
    waits = Waits()
    state = run(make_sync(index, site, backoff_base=1, backoff_max=100, wait=waits))
    assert state.estado == "completado"
    assert (state.hechos, state.indexados, state.sin_sumarios, state.errores) == (4, 3, 1, 0)
    assert waits.slices == [1]
    assert site.bulletin_paths().count(B6416) == 2
    assert index.estado_boletin("BOME-B-2026-6416") == "indexado"
    assert index.estado().boletines.get("error", 0) == 0
    assert state.mensaje is None  # the back-off note is cleared once the bulletin is done


def test_a_persistent_block_ends_the_sync_as_bloqueado(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    site.routes[B6416] = blocked(403)
    waits = Waits()
    state = run(make_sync(index, site, backoff_base=1, backoff_max=100, wait=waits))
    assert state.estado == "bloqueado"
    assert waits.slices == [1, 2]  # exponential back-off, 2 retries by default
    assert site.bulletin_paths() == [B6416] * 3  # later bulletins are never requested
    assert (state.hechos, state.errores, state.indexados) == (0, 0, 0)
    assert index.estado_boletin("BOME-B-2026-6416") is None  # not recorded at all
    assert "403" in (state.ultimo_error or "")
    mensaje = state.mensaje or ""
    assert "firewall" in mensaje and "kept" in mensaje and "hours" in mensaje
    assert state.reintentar_tras_segundos is None
    assert index.lease() is None
    assert index.estado().ultima_sincronizacion["estado"] == "bloqueado"


def test_retry_after_longer_than_the_back_off_is_honoured(site: Site, index: SumarioIndex) -> None:
    site.fix_6415()
    site.routes[B6416] = sequence(blocked(429, "120"), Site.fixture("b6416.html"))
    waits = Waits()
    state = run(make_sync(index, site, backoff_base=1, backoff_max=1000, wait=waits))
    assert state.estado == "completado"
    assert sum(waits.slices) == 120
    assert max(waits.slices) <= 30  # waited in slices so the lease can be renewed
    assert state.reintentar_tras_segundos == 120


def test_back_off_grows_exponentially_and_is_capped(site: Site, index: SumarioIndex) -> None:
    site.routes[B6416] = blocked(429)
    waits = Waits()
    state = run(make_sync(index, site, backoff_base=10, backoff_max=25, max_block_retries=3, wait=waits))
    assert state.estado == "bloqueado"
    assert waits.slices == [10, 20, 25]
    assert site.bulletin_paths() == [B6416] * 4

    site.routes[B6416] = blocked(503, "5000")
    waits = Waits()
    state = run(make_sync(index, site, backoff_base=10, backoff_max=25, max_block_retries=3, wait=waits))
    assert state.estado == "bloqueado"
    assert waits.slices == [25, 25, 25]  # Retry-After 5000 is capped at backoff_max
    assert state.reintentar_tras_segundos == 5000


def test_cancel_during_back_off_ends_the_sync_promptly(site: Site, index: SumarioIndex) -> None:
    site.routes[B6416] = blocked(429)
    sync = make_sync(index, site, backoff_base=60)  # real, cancellable wait
    started = time.monotonic()
    sync.iniciar()
    deadline = time.monotonic() + TIMEOUT
    while not site.bulletin_paths() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert site.bulletin_paths() == [B6416]
    sync.cancelar()
    assert sync.esperar(TIMEOUT), "the back-off did not honour the cancellation"
    assert time.monotonic() - started < TIMEOUT
    state = sync.estado()
    assert state.estado == "cancelado"
    assert (state.hechos, state.errores) == (0, 0)
    assert index.estado_boletin("BOME-B-2026-6416") is None
    assert site.bulletin_paths() == [B6416]
    assert index.lease() is None


def test_the_lease_is_renewed_during_a_long_back_off(
    site: Site, index: SumarioIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    site.fix_6415()
    site.routes[B6416] = sequence(blocked(429), Site.fixture("b6416.html"))
    clock = [1_000_000.0]
    renewals: list[float | None] = []
    real = index.renovar_lease

    def counting(owner, *, now=None):
        renewals.append(now)
        return real(owner, now=now)

    monkeypatch.setattr(index, "renovar_lease", counting)
    waits = Waits(clock=clock)
    sync = make_sync(
        index, site, backoff_base=60, backoff_max=900, stale_after=40, clock=lambda: clock[0], wait=waits
    )
    state = run(sync)
    assert state.estado == "completado"
    assert waits.slices == [10] * 6  # slices of stale_after / 4
    assert len(renewals) == 4 + 6  # one per planned bulletin + one per slice
    during = renewals[1:7]
    assert during == [1_000_000.0 + 10 * i for i in range(1, 7)]


def test_losing_the_lease_during_back_off_fails_the_sync(site: Site, index: SumarioIndex) -> None:
    site.routes[B6416] = blocked(429)
    holder: list[SincronizadorIndice] = []

    def steal() -> bool:
        index.liberar_lease(holder[0].owner)
        index.adquirir_lease("otro-proceso", now=time.time())
        return False

    sync = make_sync(index, site, backoff_base=1, wait=Waits(on_wait=steal))
    holder.append(sync)
    state = run(sync)
    assert state.estado == "fallido"
    assert "lease" in (state.mensaje or "")
    assert index.estado_boletin("BOME-B-2026-6416") is None
    assert site.bulletin_paths() == [B6416]
    assert index.lease()["propietario"] == "otro-proceso"


def test_consecutive_transport_failures_end_the_sync_as_bloqueado(site: Site, index: SumarioIndex) -> None:
    for path in (B6416, "/bome/BOME-B-2026-6415", "/bome/BOME-BX-2026-41"):
        site.routes[path] = dropped
    waits = Waits()
    state = run(make_sync(index, site, wait=waits))
    assert state.estado == "bloqueado"
    assert (state.hechos, state.errores) == (3, 3)
    for cve in ("BOME-B-2026-6416", "BOME-B-2026-6415", "BOME-BX-2026-41"):
        assert index.estado_boletin(cve) == "error"
    assert "/bome/BOME-B-2014-5092" not in site.bulletin_paths()
    assert waits.slices == []  # transport failures are not retried
    assert "BOME-BX-2026-41" in (state.ultimo_error or "")
    assert "firewall" in (state.mensaje or "")
    assert index.lease() is None


@pytest.mark.parametrize(
    "middle",
    [
        pytest.param(lambda request: httpx.Response(200, text=b6415_html()), id="success"),
        pytest.param(lambda request: httpx.Response(500), id="non-transport-failure"),
    ],
)
def test_anything_but_a_transport_failure_resets_the_count(
    site: Site, index: SumarioIndex, middle: Callable[[httpx.Request], httpx.Response]
) -> None:
    # Plan order: 6416, 6415, BX-41, 5092. Three transport failures, but never in a row.
    site.routes[B6416] = dropped
    site.routes["/bome/BOME-B-2026-6415"] = middle
    site.routes["/bome/BOME-BX-2026-41"] = dropped
    site.routes["/bome/BOME-B-2014-5092"] = dropped
    state = run(make_sync(index, site, wait=Waits()))
    assert state.estado == "completado"
    assert state.hechos == 4
    assert index.estado_boletin("BOME-B-2014-5092") == "error"
