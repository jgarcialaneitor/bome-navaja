"""Background synchronisation of the local sumario index.

:class:`SincronizadorIndice` crawls the site politely in a daemon thread:

1. Read the calendar for the range (default 2014-01-01..today) and record it.
2. Plan the bulletins not indexed yet, those published in the last
   ``reindexar_recientes_dias`` days (late corrections) and, if asked, those
   whose last attempt failed. Bulletins marked ``roto`` (their page answered
   HTTP 5xx twice, see :mod:`bome_navaja.index`) are skipped, even inside the
   recent window, unless ``reintentar_rotos``: the site's broken pages answer
   500 deterministically and its firewall bans the client after a few 500s.
   Newest first.
3. For each bulletin: fetch the page, recover hidden articles
   (:func:`bome_navaja.search.articulos_del_boletin`) and store everything in
   one transaction. A bulletin without any sumario is ``sin_sumarios``
   (2014-2016); a bulletin that fails to download, parse or save becomes an
   ``error`` row and the crawl goes on. Only a lost calendar, a lost lease or
   an index that rejects even the error rows of
   :data:`MAX_CONSECUTIVE_STORAGE_FAILURES` bulletins in a row is fatal
   (``fallido``).

Site guard. Every request of the sync goes through a
:class:`~bome_navaja.guard.GuardiaSitio` shared with the interactive tools
(the server gives both the one persisted in the data folder), which keeps the
crawl below the site's firewall threshold:

* A run that starts while the guard is cooling down ends at once as
  ``bloqueado`` (with ``reintentar_tras_segundos``) without a single request.
* Before the calendar and before each bulletin the sync waits
  ``guard.espera_necesaria()`` (the error budget: at most 3 HTTP error answers
  per 10 minutes), reported in ``mensaje`` as a preventive pause. If the budget
  fills inside a bulletin (e.g. hidden-article probes answering errors), the
  client raises :class:`~bome_navaja.models.BomePausaPreventivaError`: the sync
  waits its ``retry_after`` and resumes the same bulletin, replaying the
  answers it already has instead of asking the site again. After
  :data:`MAX_PAUSAS_PREVENTIVAS_SEGUIDAS` pauses in a row for one step the
  budget is considered stuck and the sync ends as ``bloqueado``.
* A bulletin page that answers a 5xx (not 503) is recorded as ``error`` and
  followed by a random pause of :data:`PAUSA_TRAS_ERROR_MIN_SEGUNDOS` to
  :data:`PAUSA_TRAS_ERROR_MAX_SEGUNDOS` seconds before the next bulletin.
* A block (403/429/503, or two requests in a row without any answer, which
  the guard turns into a cooldown) ends the sync as ``bloqueado`` promptly:
  a refused bulletin is not recorded; a bulletin lost to a timeout or reset is
  recorded as ``error`` (never ``roto``). There is no retry against a closed
  site: the guard's cooldown (75 minutes or ``Retry-After``) is far longer than
  a sync should hold its lease.

Every wait runs in cancellable slices (at most
:data:`BACKOFF_SLICE_SECONDS` or a quarter of the lease staleness) between
which the lease is renewed; a lease lost meanwhile ends the sync as
``fallido``.

Pace and budget. The sync is a long crawl, so it is slower than the
interactive tools: :data:`SYNC_POLITE_DELAY` seconds plus a random
``uniform(0, SYNC_JITTER)`` between requests, and at most
:data:`DEFAULT_MAX_BULLETINS_PER_RUN` bulletins per run (the newest of the
plan). A capped run still ends as ``completado``; its state reports the cap
(``limite_boletines``) and the planned bulletins left for a later run
(``pendientes_tras_limite``), so the full history is filled over several,
spread-out runs. :func:`sync_settings_from_env` reads the overrides
``BOME_NAVAJA_SYNC_DELAY`` and ``BOME_NAVAJA_SYNC_MAX_BOLETINES``.

Every bulletin is committed on its own, so an interrupted sync simply
continues next time. Cancellation is cooperative: it is checked between
bulletins, so it takes effect after the bulletin in flight (a few polite
requests, typically 1-3 s), or at once during a back-off wait; the client's
polite delay itself is not interrupted, which keeps the client untouched.

Two server processes never crawl at the same time: the sync holds a lease row
in the index (owner + heartbeat, renewed before every bulletin, considered
dead after :data:`bome_navaja.index.LEASE_STALE_SECONDS`). A second process's
:meth:`SincronizadorIndice.iniciar` answers ``en_curso_en_otro_proceso``.

The sync owns its :class:`BomeClient` (built by ``client_factory`` inside the
worker thread) because a client is not shared across threads.
"""

from __future__ import annotations

import math
import os
import random
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal, cast

from .client import BomeClient
from .guard import VENTANA_ERRORES_SEGUNDOS, GuardiaSitio
from .index import LEASE_STALE_SECONDS, SumarioIndex, utc_iso
from .models import (
    Article,
    BomeBlockedError,
    BomeError,
    BomeHTTPError,
    BomePausaPreventivaError,
    BulletinRef,
    JsonModel,
)
from .search import (
    FIRST_DATE,
    BusquedaInvalidaError,
    articulos_del_boletin,
)

EstadoSync = Literal[
    "inactivo",
    "en_curso",
    "completado",
    "cancelado",
    "fallido",
    "bloqueado",
    "en_curso_en_otro_proceso",
]

DEFAULT_RECENT_DAYS = 7

SYNC_POLITE_DELAY = 2.0
"""Seconds between two requests of the sync (interactive tools use
:data:`bome_navaja.search.RECOMMENDED_POLITE_DELAY`)."""

SYNC_JITTER = 1.0
"""Upper bound of the random seconds added to each sync pause."""

MIN_SYNC_POLITE_DELAY = 1.0
"""Lowest pause accepted from ``BOME_NAVAJA_SYNC_DELAY``; lower values are raised to it."""

DEFAULT_MAX_BULLETINS_PER_RUN = 250
"""Bulletins one sync run indexes at most; the rest wait for the next run."""

ENV_SYNC_DELAY = "BOME_NAVAJA_SYNC_DELAY"
ENV_SYNC_MAX_BOLETINES = "BOME_NAVAJA_SYNC_MAX_BOLETINES"

MAX_CONSECUTIVE_STORAGE_FAILURES = 3
"""Bulletins in a row whose failure could not even be recorded before the
index is declared unwritable and the sync ends as ``fallido``."""

BACKOFF_SLICE_SECONDS = 30.0
"""Longest single wait between lease renewals during a pause."""

PAUSA_TRAS_ERROR_MIN_SEGUNDOS = 30.0
PAUSA_TRAS_ERROR_MAX_SEGUNDOS = 60.0
"""Bounds of the random pause after a bulletin page answered a 5xx."""

MAX_PAUSAS_PREVENTIVAS_SEGUIDAS = 6
"""Preventive pauses in a row for one step (calendar or bulletin) before the
error budget is considered stuck and the sync ends as ``bloqueado``."""

_BLOCKED_MESSAGE = (
    "the BOME site is refusing our requests (rate limit or firewall){detail}. "
    "Bulletins already indexed are kept. bome-navaja will not ask the site anything for "
    "{wait} s (reintentar_tras_segundos); sync again after that, the next sync continues "
    "where this one stopped."
)

_PAUSE_MESSAGE = (
    "preventive pause (our own error budget, not a block by the site): {errors} HTTP error "
    "answers from the site in the last {window} min, and its firewall bans the IP from the "
    "fifth; waiting {wait} s before {what}"
)


_CAPPED_MESSAGE = (
    "per-run limit of {cap} bulletins reached: {left} bulletins remain to be indexed. "
    "Call sincronizar_indice again later to continue; spreading the runs out is gentler "
    "on the BOME site."
)


@dataclass(frozen=True, slots=True)
class SyncSettings:
    """Pace and budget of the sync, as resolved by :func:`sync_settings_from_env`."""

    polite_delay: float = SYNC_POLITE_DELAY
    max_boletines: int = DEFAULT_MAX_BULLETINS_PER_RUN
    jitter: float = SYNC_JITTER
    warnings: tuple[str, ...] = ()
    """One line per ignored or adjusted override, for the operator's log."""


def sync_settings_from_env(environ: Mapping[str, str]) -> SyncSettings:
    """Sync pace and budget from the environment overrides (pure; never raises).

    ``BOME_NAVAJA_SYNC_DELAY`` is seconds between requests: a value below
    :data:`MIN_SYNC_POLITE_DELAY` is raised to it and an unparsable one keeps
    :data:`SYNC_POLITE_DELAY`. ``BOME_NAVAJA_SYNC_MAX_BOLETINES`` is an integer
    >= 1; anything else keeps :data:`DEFAULT_MAX_BULLETINS_PER_RUN`. Empty
    values count as unset. Every adjustment adds a line to ``warnings``.
    """
    warnings: list[str] = []
    delay = SYNC_POLITE_DELAY
    raw = environ.get(ENV_SYNC_DELAY, "").strip()
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = math.nan
        if not math.isfinite(parsed):
            warnings.append(
                f"{ENV_SYNC_DELAY}={raw!r} is not a number of seconds; using {SYNC_POLITE_DELAY:g} s"
            )
        elif parsed < MIN_SYNC_POLITE_DELAY:
            delay = MIN_SYNC_POLITE_DELAY
            warnings.append(
                f"{ENV_SYNC_DELAY}={raw} is below the minimum of {MIN_SYNC_POLITE_DELAY:g} s; "
                f"using {MIN_SYNC_POLITE_DELAY:g} s"
            )
        else:
            delay = parsed
    cap = DEFAULT_MAX_BULLETINS_PER_RUN
    raw = environ.get(ENV_SYNC_MAX_BOLETINES, "").strip()
    if raw:
        try:
            parsed_cap = int(raw)
        except ValueError:
            parsed_cap = 0
        if parsed_cap >= 1:
            cap = parsed_cap
        else:
            warnings.append(
                f"{ENV_SYNC_MAX_BOLETINES}={raw!r} is not an integer >= 1; "
                f"using {DEFAULT_MAX_BULLETINS_PER_RUN}"
            )
    return SyncSettings(polite_delay=delay, max_boletines=cap, warnings=tuple(warnings))


def _check_cap(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BusquedaInvalidaError(f"'max_boletines' must be an integer >= 1, got {value!r}")
    return value


def _broken_page(exc: BaseException) -> bool:
    """True for a page that answered a 5xx other than 503 (503 is a block)."""
    return (
        isinstance(exc, BomeHTTPError)
        and not isinstance(exc, BomeBlockedError)
        and exc.status is not None
        and exc.status >= 500
    )


class _BulletinMemo:
    """The requests of one bulletin, remembered across preventive pauses.

    When the error budget fills in the middle of a bulletin (typically while
    probing hidden articles) the bulletin is resumed, not restarted: the page
    and every article already answered, including failures, are replayed from
    here, so a retry never asks the site the same failing question twice.
    Blocks are not remembered: they end the sync.
    """

    def __init__(self, client: BomeClient) -> None:
        self._client = client
        self._bulletin: Any = None
        self._articles: dict[tuple[str, str], Article | BomeError] = {}

    def bulletin(self, cve: str) -> Any:
        if self._bulletin is None:
            self._bulletin = self._client.bulletin(cve)
        return self._bulletin

    def article(self, bulletin_cve: Any, n: Any) -> Article:
        key = (str(bulletin_cve), str(n))
        known = self._articles.get(key)
        if isinstance(known, BomeError):
            raise known
        if known is not None:
            return known
        try:
            page = self._client.article(bulletin_cve, n)
        except BomeBlockedError:
            raise
        except BomeError as exc:
            self._articles[key] = exc
            raise
        self._articles[key] = page
        return page


class _IndexUnwritable(Exception):
    """Internal: the index refused every write for several bulletins in a row."""


class _StopSync(Exception):
    """Internal: end the crawl now with ``final`` as the final state."""

    def __init__(self, final: dict[str, Any]) -> None:
        super().__init__(final.get("mensaje"))
        self.final = final


@dataclass(frozen=True, slots=True)
class EstadoSincronizacion(JsonModel):
    """Thread-safe snapshot of a sync job."""

    estado: EstadoSync
    desde: str | None = None
    hasta: str | None = None
    total_planificado: int = 0
    hechos: int = 0
    indexados: int = 0
    sin_sumarios: int = 0
    errores: int = 0
    rotos: int = 0
    """Of ``errores``, the bulletins this run left ``roto`` (page answered 5xx twice)."""
    cve_actual: str | None = None
    iniciado: str | None = None
    finalizado: str | None = None
    segundos_por_boletin: float | None = None
    eta_segundos: int | None = None
    ultimo_error: str | None = None
    mensaje: str | None = None
    reintentar_tras_segundos: float | None = None
    """Seconds before the site may be asked again when the sync ends ``bloqueado``
    (what is left of the site guard's cooldown, or the site's ``Retry-After``)."""
    limite_boletines: int | None = None
    """Most bulletins this run indexes (the per-run cap)."""
    pendientes_tras_limite: int = 0
    """Planned bulletins deferred to a later run by the cap."""
    lease: dict[str, Any] | None = None
    """Holder of the lease when another process is syncing."""
    propietario: str | None = field(default=None)


def _default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _date(value: date | str | None, name: str) -> date | None:
    if isinstance(value, datetime):  # a datetime counts as its calendar day
        return value.date()
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise BusquedaInvalidaError(f"'{name}' must be an ISO date YYYY-MM-DD, got {value!r}")


class SincronizadorIndice:
    """Runs one background sync at a time for a :class:`SumarioIndex`."""

    def __init__(
        self,
        index: SumarioIndex,
        client_factory: Callable[..., BomeClient] | None = None,
        *,
        polite_delay: float = SYNC_POLITE_DELAY,
        jitter: float = SYNC_JITTER,
        max_boletines: int = DEFAULT_MAX_BULLETINS_PER_RUN,
        owner: str | None = None,
        hoy: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.time,
        stale_after: float = LEASE_STALE_SECONDS,
        wait: Callable[[float], bool] | None = None,
        guard: GuardiaSitio | None = None,
        pausa_aleatoria: Callable[[float, float], float] = random.uniform,
    ) -> None:
        """``wait(seconds)`` performs one pause slice and returns ``True`` when
        cancelled; it defaults to waiting on the cancellation event (tests inject
        a fake so they never sleep). ``guard`` is the site guard (default: a
        memory-only one); ``client_factory`` is called as
        ``client_factory(guard=guard)`` and must wire it into the client.
        ``pausa_aleatoria(low, high)`` draws the pause after a broken page.
        ``polite_delay`` and ``jitter`` only shape the default client;
        ``max_boletines`` is the cap of runs started without one."""
        self.index = index
        self.guard = guard if guard is not None else GuardiaSitio(None)
        self._client_factory: Callable[..., BomeClient] = client_factory or (
            lambda *, guard=None: BomeClient(polite_delay=polite_delay, jitter=jitter, guard=guard)
        )
        self._max_boletines = _check_cap(max_boletines)
        self.owner = owner or _default_owner()
        self._today = hoy
        self._clock = clock
        self._stale_after = stale_after
        self._random_pause = pausa_aleatoria
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._wait = wait or self._cancel.wait
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"estado": "inactivo"}
        self._started_clock: float | None = None
        self._storage_failures = 0
        self._pending_pause: str | None = None

    # ------------------------------------------------------------------ public API

    def iniciar(
        self,
        desde: date | str | None = None,
        hasta: date | str | None = None,
        reindexar_recientes_dias: int = DEFAULT_RECENT_DAYS,
        reintentar_errores: bool = True,
        max_boletines: int | None = None,
        reintentar_rotos: bool = False,
    ) -> EstadoSincronizacion:
        """Start a background sync and return its status at once.

        At most ``max_boletines`` bulletins (default: the constructor's) are
        indexed, the newest of the plan. ``reintentar_errores`` replans failed
        bulletins; ``roto`` ones are only replanned with ``reintentar_rotos``
        (each costs an HTTP 500 that the site's firewall counts). While a sync of this object runs,
        returns that job. When another
        process holds a live lease, returns ``en_curso_en_otro_proceso`` with
        the lease and starts nothing.
        """
        start = _date(desde, "desde") or FIRST_DATE
        end = _date(hasta, "hasta") or self._today()
        if end < start:
            raise BusquedaInvalidaError(f"'hasta' ({end}) is before 'desde' ({start})")
        if (
            isinstance(reindexar_recientes_dias, bool)
            or not isinstance(reindexar_recientes_dias, int)
            or reindexar_recientes_dias < 0
        ):
            raise BusquedaInvalidaError(
                f"'reindexar_recientes_dias' must be an integer >= 0, got {reindexar_recientes_dias!r}"
            )
        cap = self._max_boletines if max_boletines is None else _check_cap(max_boletines)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._snapshot()
            held = self.index.adquirir_lease(
                self.owner, now=self._clock(), stale_after=self._stale_after
            )
            if held is not None:
                return EstadoSincronizacion(
                    estado="en_curso_en_otro_proceso",
                    lease=held,
                    propietario=self.owner,
                    mensaje="another bome-navaja process is already syncing this index",
                )
            self._cancel.clear()
            self._started_clock = self._clock()
            self._state = {
                "estado": "en_curso",
                "desde": start.isoformat(),
                "hasta": end.isoformat(),
                "total_planificado": 0,
                "hechos": 0,
                "indexados": 0,
                "sin_sumarios": 0,
                "errores": 0,
                "rotos": 0,
                "iniciado": utc_iso(),
                "limite_boletines": cap,
                "pendientes_tras_limite": 0,
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(
                    start,
                    end,
                    reindexar_recientes_dias,
                    bool(reintentar_errores),
                    bool(reintentar_rotos),
                    cap,
                ),
                name="bome-navaja-index-sync",
                daemon=True,
            )
            self._thread.start()
            return self._snapshot()

    def estado(self) -> EstadoSincronizacion:
        """Current progress (safe to call from any thread)."""
        with self._lock:
            return self._snapshot()

    def cancelar(self) -> EstadoSincronizacion:
        """Ask the running sync to stop after the bulletin in flight (at once if backing off)."""
        self._cancel.set()
        return self.estado()

    def esperar(self, timeout: float | None = None) -> bool:
        """Wait for the worker thread; ``True`` when no sync is running."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ------------------------------------------------------------------ internals

    def _snapshot(self) -> EstadoSincronizacion:
        state = dict(self._state)
        done = state.get("hechos", 0)
        planned = state.get("total_planificado", 0)
        per_bulletin: float | None = None
        eta: int | None = None
        if done and self._started_clock is not None:
            end_clock = state.get("_end_clock") or self._clock()
            per_bulletin = round((end_clock - self._started_clock) / done, 3)
            eta = round(per_bulletin * max(planned - done, 0))
        state.pop("_end_clock", None)
        return EstadoSincronizacion(
            **state,
            segundos_por_boletin=per_bulletin,
            eta_segundos=eta,
            propietario=self.owner,
        )

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def _bump(self, key: str) -> None:
        with self._lock:
            self._state[key] = self._state.get(key, 0) + 1

    def _plan(
        self, refs: list[BulletinRef], recent_days: int, retry_errors: bool, retry_broken: bool
    ) -> list[BulletinRef]:
        known = self.index.estados_boletines()
        cutoff = self._today() - timedelta(days=recent_days)
        chosen: dict[str, BulletinRef] = {}
        for ref in refs:
            status = known.get(ref.cve)
            if status == "roto":
                if retry_broken:
                    chosen[ref.cve] = ref
                continue
            if (
                status is None
                or (recent_days > 0 and ref.date is not None and ref.date >= cutoff)
                or (retry_errors and status == "error")
            ):
                chosen[ref.cve] = ref
        return sorted(chosen.values(), key=lambda r: (r.date or date.min, r.number), reverse=True)

    def _run(
        self,
        start: date,
        end: date,
        recent_days: int,
        retry_errors: bool,
        retry_broken: bool,
        cap: int,
    ) -> None:
        client: BomeClient | None = None
        final: dict[str, Any] = {}
        self._storage_failures = 0
        self._pending_pause = None
        try:
            self._stop_if_closed()  # a closed site gets no request at all
            self._wait_for_budget("the calendar")
            client = self._client_factory(guard=self.guard)
            refs = self._guarded("the calendar", lambda: client.calendar(start, end))
            self.index.registrar_calendario(refs)
            plan = self._plan(refs, recent_days, retry_errors, retry_broken)
            deferred = max(len(plan) - cap, 0)
            plan = plan[:cap]  # newest first: the cap defers the oldest
            self._update(total_planificado=len(plan), pendientes_tras_limite=deferred)
            final = {"estado": "completado"}
            if deferred:
                final["mensaje"] = _CAPPED_MESSAGE.format(cap=cap, left=deferred)
            for ref in plan:
                if self._cancel.is_set():
                    final = {"estado": "cancelado", "mensaje": "cancelled by request"}
                    break
                if not self.index.renovar_lease(self.owner, now=self._clock()):
                    final = {"estado": "fallido", "mensaje": "sync lease lost to another process"}
                    break
                self._stop_if_closed()
                self._pause_after_error()
                self._wait_for_budget(ref.cve)
                self._update(cve_actual=ref.cve)
                self._process(client, ref)
        except _StopSync as stop:
            final = stop.final
        except BaseException as exc:  # noqa: BLE001 - the worker must always report
            final = {"estado": "fallido", "mensaje": f"{type(exc).__name__}: {exc}"}
        finally:
            if client is not None:
                client.close()
            with self._lock:
                self._state.update(final)
                self._state.update(cve_actual=None, finalizado=utc_iso(), _end_clock=self._clock())
                summary = self._snapshot().to_dict()
            summary.pop("lease", None)
            try:
                self.index.guardar_resumen_sincronizacion(summary)
            except BomeError:
                pass
            try:
                self.index.liberar_lease(self.owner)
            except BomeError:
                pass
            self.index.cerrar_conexion_hilo()

    def _process(self, client: BomeClient, ref: BulletinRef) -> None:
        """Fetch and store one bulletin; any failure becomes that bulletin's ``error``.

        Raises :class:`_StopSync` when the site blocks us (or the guard is closed
        after this bulletin), the sync is cancelled during a pause or the lease is
        lost meanwhile.
        """
        memo = _BulletinMemo(client)
        try:
            articles, errors = self._guarded(
                ref.cve, lambda: articulos_del_boletin(cast(BomeClient, memo), memo.bulletin(ref.cve))
            )
        except _StopSync:
            raise
        except Exception as exc:  # a bad bulletin must not stop the crawl
            self._record_failure(ref, exc)
            if _broken_page(exc):
                self._pending_pause = f"an HTTP {cast(BomeHTTPError, exc).status} on {ref.cve}"
            self._stop_if_closed()  # e.g. a second request in a row without any answer
            return
        has_text = any(article.sumario and article.sumario.strip() for article in articles)
        estado = "indexado" if has_text else "sin_sumarios"
        note = f"{errors[0].error_code}: {errors[0].mensaje}" if errors else None
        try:
            self.index.guardar_boletin(ref, articles, estado, error=note)
        except Exception as exc:  # e.g. a row the index rejects: record, go on
            self._record_failure(ref, exc)
            return
        self._storage_failures = 0
        self._bump("hechos")
        self._bump("indexados" if has_text else "sin_sumarios")

    def _guarded(self, what: str, step: Callable[[], Any]) -> Any:
        """Run ``step`` (the calendar or one bulletin) under the site guard.

        A preventive pause raised inside ``step`` is waited out and ``step`` runs
        again (a bulletin replays the answers it already has); any other block
        ends the sync as ``bloqueado``.
        """
        pauses = 0
        while True:
            try:
                result = step()
            except BomePausaPreventivaError as exc:
                pauses += 1
                if pauses > MAX_PAUSAS_PREVENTIVAS_SEGUIDAS:
                    raise _StopSync(self._stuck_budget(what)) from exc
                wait = exc.retry_after if exc.retry_after else self.guard.espera_necesaria()
                self._preventive_pause(max(wait, 1.0), what)
                self._stop_if_closed()
                continue
            except BomeBlockedError as exc:
                raise _StopSync(self._blocked(exc, what)) from exc
            if pauses:
                self._update(mensaje=None)
            return result

    def _wait_for_budget(self, what: str) -> None:
        """Wait while the error budget is full before asking the site for ``what``."""
        for _ in range(MAX_PAUSAS_PREVENTIVAS_SEGUIDAS):
            wait = self.guard.espera_necesaria()
            if wait <= 0:
                return
            self._preventive_pause(wait, what)
            self._update(mensaje=None)
            self._stop_if_closed()
        if self.guard.espera_necesaria() > 0:
            raise _StopSync(self._stuck_budget(what))

    def _preventive_pause(self, seconds: float, what: str) -> None:
        self._update(
            mensaje=_PAUSE_MESSAGE.format(
                errors=self.guard.errores_en_ventana(),
                window=f"{VENTANA_ERRORES_SEGUNDOS / 60:g}",
                wait=math.ceil(seconds),
                what=what,
            )
        )
        self._back_off(seconds)

    def _pause_after_error(self) -> None:
        """The random pause owed after a bulletin page answered a 5xx, if any."""
        cause, self._pending_pause = self._pending_pause, None
        if cause is None:
            return
        seconds = self._random_pause(PAUSA_TRAS_ERROR_MIN_SEGUNDOS, PAUSA_TRAS_ERROR_MAX_SEGUNDOS)
        self._update(mensaje=f"pausing {seconds:.0f} s after {cause} (gentle on the site's firewall)")
        self._back_off(seconds)
        self._update(mensaje=None)

    def _stop_if_closed(self) -> None:
        """End the sync as ``bloqueado`` when the guard is cooling down."""
        remaining = self.guard.segundos_enfriamiento()
        if remaining > 0:
            motivo = self.guard.motivo or "the site guard is closed"
            raise _StopSync(
                {
                    "estado": "bloqueado",
                    "mensaje": _BLOCKED_MESSAGE.format(detail=f": {motivo}", wait=math.ceil(remaining)),
                    "reintentar_tras_segundos": remaining,
                }
            )

    def _blocked(self, exc: BomeBlockedError, what: str) -> dict[str, Any]:
        remaining = self.guard.segundos_enfriamiento()
        wait = remaining if remaining > 0 else exc.retry_after
        if exc.status is not None:
            detail = f": HTTP {exc.status} on {what}"
        else:
            detail = f": {self.guard.motivo or 'the site guard is closed'}"
        return {
            "estado": "bloqueado",
            "mensaje": _BLOCKED_MESSAGE.format(
                detail=detail, wait=math.ceil(wait) if wait is not None else "a while"
            ),
            "ultimo_error": f"{what}: {exc}",
            "reintentar_tras_segundos": wait,
        }

    def _stuck_budget(self, what: str) -> dict[str, Any]:
        wait = self.guard.espera_necesaria()
        return {
            "estado": "bloqueado",
            "mensaje": f"the error budget stayed full after {MAX_PAUSAS_PREVENTIVAS_SEGUIDAS} "
            f"preventive pauses before {what} (other bome-navaja processes keep getting errors "
            "from the site); bulletins already indexed are kept, sync again later",
            "reintentar_tras_segundos": wait or None,
        }

    def _back_off(self, seconds: float) -> None:
        """Wait ``seconds`` in slices, renewing the lease; stop on cancel or lease loss."""
        step = min(BACKOFF_SLICE_SECONDS, self._stale_after / 4)
        remaining = seconds
        while remaining > 0:
            chunk = min(step, remaining)
            if self._wait(chunk) or self._cancel.is_set():
                raise _StopSync({"estado": "cancelado", "mensaje": "cancelled by request"})
            remaining -= chunk
            if not self.index.renovar_lease(self.owner, now=self._clock()):
                raise _StopSync({"estado": "fallido", "mensaje": "sync lease lost to another process"})

    def _record_failure(self, ref: BulletinRef, exc: BaseException) -> None:
        """Store ``ref`` as ``error`` (the index may make it ``roto``); if even that
        fails, count it toward giving up."""
        self._bump("hechos")
        self._bump("errores")
        try:
            stored = self.index.guardar_boletin(ref, [], "error", error=exc)
        except Exception as store_exc:
            self._storage_failures += 1
            self._update(
                ultimo_error=f"{ref.cve}: {exc} (the error could not be recorded: {store_exc})"
            )
            if self._storage_failures >= MAX_CONSECUTIVE_STORAGE_FAILURES:
                raise _IndexUnwritable(
                    f"the index rejected every write for {self._storage_failures} bulletins "
                    f"in a row: {store_exc}"
                ) from store_exc
            return
        self._storage_failures = 0
        if stored == "roto":
            self._bump("rotos")
        self._update(ultimo_error=f"{ref.cve}: {exc}")


__all__ = [
    "BACKOFF_SLICE_SECONDS",
    "DEFAULT_MAX_BULLETINS_PER_RUN",
    "DEFAULT_RECENT_DAYS",
    "ENV_SYNC_DELAY",
    "ENV_SYNC_MAX_BOLETINES",
    "MAX_CONSECUTIVE_STORAGE_FAILURES",
    "MAX_PAUSAS_PREVENTIVAS_SEGUIDAS",
    "MIN_SYNC_POLITE_DELAY",
    "PAUSA_TRAS_ERROR_MAX_SEGUNDOS",
    "PAUSA_TRAS_ERROR_MIN_SEGUNDOS",
    "SYNC_JITTER",
    "SYNC_POLITE_DELAY",
    "EstadoSincronizacion",
    "SincronizadorIndice",
    "SyncSettings",
    "sync_settings_from_env",
]
