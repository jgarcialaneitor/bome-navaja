"""Site guard: error budget, cooldown and its shared state file (no network, fake clock)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from bome_navaja import guard as guard_module
from bome_navaja.guard import (
    ENFRIAMIENTO_SEGUNDOS,
    FALLOS_TRANSPORTE_BLOQUEO,
    FICHERO_ESTADO,
    MAX_ERRORES_EN_VENTANA,
    VENTANA_ERRORES_SEGUNDOS,
    GuardiaSitio,
)
from bome_navaja.models import BomeBlockedError, BomeHTTPError, BomePausaPreventivaError

T0 = 1_000_000.0
URL = "https://bomemelilla.es/bome/BOME-B-2026-6416"


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def memory_guard(clock: Clock) -> GuardiaSitio:
    return GuardiaSitio(None, clock=clock)


# --------------------------------------------------------------------------- constants and exceptions


def test_constants() -> None:
    assert (VENTANA_ERRORES_SEGUNDOS, MAX_ERRORES_EN_VENTANA) == (600, 3)
    assert (ENFRIAMIENTO_SEGUNDOS, FALLOS_TRANSPORTE_BLOQUEO) == (4500, 2)
    assert FICHERO_ESTADO == "estado_sitio.json"


def test_pausa_preventiva_is_a_blocked_error_without_status() -> None:
    exc = BomePausaPreventivaError("pausa", status=None, url=URL, retry_after=12.0)
    assert isinstance(exc, BomeBlockedError) and isinstance(exc, BomeHTTPError)
    assert (exc.status, exc.retry_after, exc.url) == (None, 12.0, URL)


# --------------------------------------------------------------------------- error budget


def test_budget_allows_up_to_max_errors_then_waits_for_the_oldest(clock: Clock) -> None:
    guard = memory_guard(clock)
    assert guard.espera_necesaria() == 0
    guard.registrar(500)
    clock.now += 100
    guard.registrar(404)
    assert guard.espera_necesaria() == 0  # two errors: one more is still allowed
    clock.now += 50
    guard.registrar(500)
    assert guard.errores_en_ventana() == 3
    # The oldest error (T0) leaves the window at T0 + 600; now is T0 + 150.
    assert guard.espera_necesaria() == pytest.approx(450)
    assert guard.espera_necesaria(T0 + 599) == pytest.approx(1)
    assert guard.espera_necesaria(T0 + 600) == 0


def test_the_window_slides(clock: Clock) -> None:
    guard = memory_guard(clock)
    for _ in range(3):
        guard.registrar(500)
        clock.now += 10
    assert guard.espera_necesaria() > 0
    clock.now = T0 + 600
    assert guard.errores_en_ventana() == 2
    assert guard.espera_necesaria() == 0
    clock.now = T0 + 700
    assert guard.errores_en_ventana() == 0


def test_more_errors_than_the_budget_wait_until_enough_leave(clock: Clock) -> None:
    guard = memory_guard(clock)
    for step in range(4):  # e.g. two threads erred at once
        clock.now = T0 + step * 10
        guard.registrar(500)
    # Four errors at T0, +10, +20, +30: two must leave, the second at T0 + 610.
    assert guard.espera_necesaria() == pytest.approx(610 - 30)


def test_only_error_answers_spend_the_budget(clock: Clock) -> None:
    guard = memory_guard(clock)
    for status in (200, 204, 301, 302, 399):
        guard.registrar(status)
    assert guard.errores_en_ventana() == 0
    for status in (400, 404, 410):
        guard.registrar(status)
    assert guard.errores_en_ventana() == 3  # 404 counts too: the firewall may count it
    assert not guard.en_enfriamiento()


def test_errors_at_the_same_instant_are_all_counted(clock: Clock) -> None:
    guard = memory_guard(clock)
    guard.registrar(500)
    guard.registrar(500)
    guard.registrar(500)
    assert guard.errores_en_ventana() == 3


# --------------------------------------------------------------------------- cooldown


@pytest.mark.parametrize("status", [403, 429, 503])
def test_blocking_statuses_start_a_cooldown(clock: Clock, status: int) -> None:
    guard = memory_guard(clock)
    guard.registrar(status)
    assert guard.en_enfriamiento()
    assert guard.segundos_enfriamiento() == pytest.approx(ENFRIAMIENTO_SEGUNDOS)
    assert str(status) in (guard.motivo or "")
    assert guard.errores_en_ventana() == 1  # it is an error answer as well
    clock.now += ENFRIAMIENTO_SEGUNDOS
    assert not guard.en_enfriamiento()
    assert guard.segundos_enfriamiento() == 0


def test_a_longer_retry_after_extends_the_cooldown(clock: Clock) -> None:
    guard = memory_guard(clock)
    guard.registrar(429, retry_after=9000)
    assert guard.segundos_enfriamiento() == pytest.approx(9000)
    other = memory_guard(clock)
    other.registrar(503, retry_after=60)  # shorter than the minimum cooldown
    assert other.segundos_enfriamiento() == pytest.approx(ENFRIAMIENTO_SEGUNDOS)


def test_a_cooldown_never_shrinks(clock: Clock) -> None:
    guard = memory_guard(clock)
    guard.registrar(429, retry_after=9000)
    clock.now += 100
    guard.registrar(403)
    assert guard.segundos_enfriamiento() == pytest.approx(8900)


def test_two_transport_failures_in_a_row_start_a_cooldown(clock: Clock) -> None:
    guard = memory_guard(clock)
    guard.registrar(None)
    assert not guard.en_enfriamiento()
    guard.registrar(None)
    assert guard.en_enfriamiento()
    assert guard.segundos_enfriamiento() == pytest.approx(ENFRIAMIENTO_SEGUNDOS)
    assert guard.motivo
    assert guard.errores_en_ventana() == 0  # no answer is not an error answer


@pytest.mark.parametrize("answer", [200, 404, 500])
def test_any_answer_resets_the_transport_failure_streak(clock: Clock, answer: int) -> None:
    guard = memory_guard(clock)
    guard.registrar(None)
    guard.registrar(answer)
    guard.registrar(None)
    assert not guard.en_enfriamiento()
    guard.registrar(None)
    assert guard.en_enfriamiento()


# --------------------------------------------------------------------------- comprobar


def test_comprobar_passes_when_the_site_is_open(clock: Clock) -> None:
    guard = memory_guard(clock)
    guard.registrar(500)
    guard.registrar(500)
    assert guard.comprobar(URL) is None


def test_comprobar_raises_a_preventive_pause_when_the_budget_is_full(clock: Clock) -> None:
    guard = memory_guard(clock)
    for _ in range(3):
        guard.registrar(500)
    clock.now += 100
    with pytest.raises(BomePausaPreventivaError) as info:
        guard.comprobar(URL)
    exc = info.value
    assert (exc.status, exc.url) == (None, URL)
    assert exc.retry_after == pytest.approx(500)
    text = str(exc).lower()
    assert "pausa preventiva" in text and "no es un bloqueo" in text and "500 s" in text


def test_comprobar_raises_a_block_during_the_cooldown(clock: Clock) -> None:
    guard = memory_guard(clock)
    guard.registrar(403)
    clock.now += 500
    with pytest.raises(BomeBlockedError) as info:
        guard.comprobar(URL)
    exc = info.value
    assert not isinstance(exc, BomePausaPreventivaError)
    assert (exc.status, exc.url) == (None, URL)
    assert exc.retry_after == pytest.approx(ENFRIAMIENTO_SEGUNDOS - 500)
    text = str(exc)
    assert "bloque" in text and "403" in text and "4000 s" in text and "UTC" in text
    clock.now += ENFRIAMIENTO_SEGUNDOS
    assert guard.comprobar(URL) is None


def test_the_cooldown_wins_over_the_budget(clock: Clock) -> None:
    guard = memory_guard(clock)
    for _ in range(3):
        guard.registrar(500)
    guard.registrar(429)
    with pytest.raises(BomeBlockedError) as info:
        guard.comprobar(URL)
    assert not isinstance(info.value, BomePausaPreventivaError)


def test_estado_reports_the_guard(clock: Clock, tmp_path: Path) -> None:
    guard = GuardiaSitio(tmp_path / FICHERO_ESTADO, clock=clock)
    state = guard.estado()
    assert state == {
        "enfriamiento_hasta": None,
        "segundos_restantes": 0,
        "motivo": None,
        "errores_en_ventana": 0,
        "max_errores": 3,
        "ventana_segundos": 600,
        "fichero": str(tmp_path / FICHERO_ESTADO),
    }
    clock.now = 1_790_000_000.0
    guard.registrar(503, retry_after=10_000)
    state = guard.estado()
    assert state["enfriamiento_hasta"] == "2026-09-21T17:00:00+00:00"
    assert state["segundos_restantes"] == 10_000
    assert "503" in state["motivo"]
    assert state["errores_en_ventana"] == 1
    assert memory_guard(clock).estado()["fichero"] is None


# --------------------------------------------------------------------------- persistence


def test_state_survives_a_restart(clock: Clock, tmp_path: Path) -> None:
    path = tmp_path / "datos" / FICHERO_ESTADO  # the folder is created on the first write
    first = GuardiaSitio(path, clock=clock)
    first.registrar(500)
    first.registrar(429, retry_after=6000)
    stored = json.loads(path.read_text("utf-8"))
    assert stored["errores"] == [T0, T0]
    assert stored["enfriamiento_hasta"] == T0 + 6000
    assert "429" in stored["motivo"]

    clock.now += 10
    restarted = GuardiaSitio(path, clock=clock)
    assert restarted.errores_en_ventana() == 2
    assert restarted.segundos_enfriamiento() == pytest.approx(5990)
    assert restarted.motivo == first.motivo
    assert list(path.parent.iterdir()) == [path]  # atomic write leaves no temp file


def test_old_errors_are_pruned_from_the_file(clock: Clock, tmp_path: Path) -> None:
    path = tmp_path / FICHERO_ESTADO
    guard = GuardiaSitio(path, clock=clock)
    guard.registrar(500)
    clock.now += 700
    guard.registrar(404)
    assert json.loads(path.read_text("utf-8"))["errores"] == [T0 + 700]


def test_two_guards_on_one_file_share_budget_and_cooldown(clock: Clock, tmp_path: Path) -> None:
    path = tmp_path / FICHERO_ESTADO
    one = GuardiaSitio(path, clock=clock)
    two = GuardiaSitio(path, clock=clock)
    one.registrar(500)
    clock.now += 1
    two.registrar(500)
    clock.now += 1
    one.registrar(404)
    for guard in (one, two):
        assert guard.errores_en_ventana() == 3
        assert guard.espera_necesaria() == pytest.approx(598)
    with pytest.raises(BomePausaPreventivaError):
        two.comprobar(URL)

    two.registrar(None)
    two.registrar(None)
    with pytest.raises(BomeBlockedError) as info:
        one.comprobar(URL)
    assert not isinstance(info.value, BomePausaPreventivaError)
    assert one.motivo == two.motivo
    stored = json.loads(path.read_text("utf-8"))
    assert sorted(stored["errores"]) == [T0, T0 + 1, T0 + 2]  # merged, nothing lost


def test_a_write_merges_with_what_another_process_wrote(clock: Clock, tmp_path: Path) -> None:
    path = tmp_path / FICHERO_ESTADO
    mine = GuardiaSitio(path, clock=clock)
    mine.registrar(500)
    # Another process rewrites the file with its own error and a longer cooldown.
    path.write_text(
        json.dumps({"errores": [T0 + 5], "enfriamiento_hasta": T0 + 9000, "motivo": "otro"}), "utf-8"
    )
    clock.now += 10
    mine.registrar(403)
    stored = json.loads(path.read_text("utf-8"))
    assert sorted(stored["errores"]) == [T0, T0 + 5, T0 + 10]
    assert stored["enfriamiento_hasta"] == T0 + 9000  # the longest deadline wins
    assert stored["motivo"] == "otro"
    assert mine.segundos_enfriamiento() == pytest.approx(8990)


def test_a_missing_file_is_an_open_site_and_logs_nothing(
    clock: Clock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = GuardiaSitio(tmp_path / "nada" / FICHERO_ESTADO, clock=clock)
    assert guard.espera_necesaria() == 0
    guard.comprobar(URL)
    assert guard.estado()["segundos_restantes"] == 0
    assert not (tmp_path / "nada").exists()  # reading never creates anything
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("content", ["{nope", "[1, 2]", '{"errores": "x", "enfriamiento_hasta": "y"}'])
def test_a_corrupt_file_falls_back_to_memory(
    clock: Clock, tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    path = tmp_path / FICHERO_ESTADO
    path.write_text(content, "utf-8")
    guard = GuardiaSitio(path, clock=clock)
    assert guard.espera_necesaria() == 0
    guard.registrar(500)
    guard.registrar(500)
    guard.registrar(500)
    assert guard.espera_necesaria() == pytest.approx(600)
    with pytest.raises(BomePausaPreventivaError):
        guard.comprobar(URL)
    err = capsys.readouterr().err
    assert err.count("bome-navaja:") == 1  # logged once, not on every read
    assert str(path) in err


def test_an_unwritable_file_falls_back_to_memory(
    clock: Clock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "fichero"
    blocker.write_text("not a folder", "utf-8")
    guard = GuardiaSitio(blocker / FICHERO_ESTADO, clock=clock)  # its folder is a file
    guard.registrar(403)
    guard.registrar(500)
    assert guard.en_enfriamiento()
    assert guard.errores_en_ventana() == 2
    with pytest.raises(BomeBlockedError):
        guard.comprobar(URL)
    err = capsys.readouterr().err
    assert err.count("bome-navaja:") == 1
    assert blocker.read_text("utf-8") == "not a folder"


def test_a_memory_only_guard_touches_no_file(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    guard = memory_guard(clock)
    guard.registrar(500)
    guard.registrar(None)
    guard.registrar(None)
    assert guard.en_enfriamiento()
    assert list(tmp_path.iterdir()) == []


def test_the_guard_is_thread_safe(clock: Clock, tmp_path: Path) -> None:
    guard = GuardiaSitio(tmp_path / FICHERO_ESTADO, clock=clock)
    barrier = threading.Barrier(8)

    def worker(offset: int) -> None:
        barrier.wait()
        for step in range(5):
            guard.registrar(500)
            guard.espera_necesaria()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert guard.errores_en_ventana() == 40
    assert len(json.loads((tmp_path / FICHERO_ESTADO).read_text("utf-8"))["errores"]) == 40


def test_the_default_clock_is_the_wall_clock() -> None:
    assert GuardiaSitio(None)._clock is guard_module.time.time


# --------------------------------------------------------------------------- other sites (old portal on melilla.es)


def test_the_old_portal_has_its_own_state_file_name() -> None:
    assert guard_module.FICHERO_ESTADO_MELILLA == "estado_sitio_melilla.json"
    assert guard_module.FICHERO_ESTADO_MELILLA != FICHERO_ESTADO


def test_the_default_site_is_bomemelilla(clock: Clock) -> None:
    guard = memory_guard(clock)
    assert guard.sitio == "bomemelilla.es"
    guard.registrar(403)
    with pytest.raises(BomeBlockedError) as info:
        guard.comprobar(URL)
    assert "bomemelilla.es" in str(info.value)


def test_messages_name_the_guarded_site(clock: Clock) -> None:
    guard = GuardiaSitio(None, clock=clock, sitio="melilla.es")
    assert guard.sitio == "melilla.es"
    for _ in range(3):
        guard.registrar(500)
    with pytest.raises(BomePausaPreventivaError) as pause:
        guard.comprobar(URL)
    guard.registrar(429)
    with pytest.raises(BomeBlockedError) as block:
        guard.comprobar(URL)
    for text in (str(pause.value), str(block.value)):
        assert "melilla.es" in text and "bomemelilla.es" not in text
    assert "pausa preventiva" in str(pause.value) and "no es un bloqueo" in str(pause.value)


def test_two_sites_keep_separate_state_files(clock: Clock, tmp_path: Path) -> None:
    bome = GuardiaSitio(tmp_path / FICHERO_ESTADO, clock=clock)
    portal = GuardiaSitio(tmp_path / guard_module.FICHERO_ESTADO_MELILLA, clock=clock, sitio="melilla.es")
    portal.registrar(403)
    assert portal.en_enfriamiento()
    assert not bome.en_enfriamiento()
    assert bome.comprobar(URL) is None
