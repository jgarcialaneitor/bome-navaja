"""Background synchronisation of the local sumario index.

:class:`SincronizadorIndice` crawls the site politely in a daemon thread:

1. Read the calendar for the range (default 2014-01-01..today) and record it.
2. Plan the bulletins not indexed yet, those published in the last
   ``reindexar_recientes_dias`` days (late corrections) and, if asked, those
   whose last attempt failed. Newest first.
3. For each bulletin: fetch the page, recover hidden articles
   (:func:`bome_navaja.search.articulos_del_boletin`) and store everything in
   one transaction. A bulletin without any sumario is ``sin_sumarios``
   (2014-2016); a bulletin that fails to download, parse or save becomes an
   ``error`` row and the crawl goes on. Only a lost calendar, a lost lease or
   an index that rejects even the error rows of
   :data:`MAX_CONSECUTIVE_STORAGE_FAILURES` bulletins in a row is fatal
   (``fallido``).

Circuit breaker. When the site refuses a bulletin (403/429/503, i.e.
:class:`~bome_navaja.models.BomeBlockedError`: rate limit or firewall), that
bulletin is *not* recorded: the sync backs off for
``max(Retry-After, backoff_base * 2**attempt)`` seconds (capped at
``backoff_max``) and retries the same bulletin, up to ``max_block_retries``
times. If the site still refuses, the sync ends as ``bloqueado``: what is
already indexed is kept and the user should wait (hours for a firewall block)
before syncing again. The back-off wait is cancellable and runs in slices
(at most :data:`BACKOFF_SLICE_SECONDS` or a quarter of the lease staleness)
between which the lease is renewed; a lease lost meanwhile ends the sync as
``fallido``. Failures without any HTTP answer (timeouts, resets: what a
firewall that silently drops looks like) are still recorded as ``error``
rows, but :data:`MAX_CONSECUTIVE_TRANSPORT_FAILURES` bulletins in a row
failing like that also end the sync as ``bloqueado``; any other outcome
resets that count.

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

import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from .client import BomeClient
from .index import LEASE_STALE_SECONDS, SumarioIndex, utc_iso
from .models import BomeBlockedError, BomeError, BomeHTTPError, BulletinRef, JsonModel
from .search import (
    FIRST_DATE,
    RECOMMENDED_POLITE_DELAY,
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

MAX_CONSECUTIVE_STORAGE_FAILURES = 3
"""Bulletins in a row whose failure could not even be recorded before the
index is declared unwritable and the sync ends as ``fallido``."""

BACKOFF_BASE_SECONDS = 60.0
"""First back-off after the site refuses a bulletin; doubled on each retry."""

BACKOFF_MAX_SECONDS = 900.0
"""Upper bound of one back-off wait, ``Retry-After`` included."""

MAX_BLOCK_RETRIES = 2
"""Retries of a refused bulletin before the sync ends as ``bloqueado``."""

BACKOFF_SLICE_SECONDS = 30.0
"""Longest single wait between lease renewals during a back-off."""

MAX_CONSECUTIVE_TRANSPORT_FAILURES = 3
"""Bulletins in a row failing without any HTTP answer before the sync ends as
``bloqueado``."""

_BLOCKED_MESSAGE = (
    "the BOME site is refusing our requests (rate limit or firewall){detail}. "
    "Bulletins already indexed are kept. Wait before syncing again (hours if it is a "
    "firewall block); the next sync continues where this one stopped."
)


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
    cve_actual: str | None = None
    iniciado: str | None = None
    finalizado: str | None = None
    segundos_por_boletin: float | None = None
    eta_segundos: int | None = None
    ultimo_error: str | None = None
    mensaje: str | None = None
    reintentar_tras_segundos: float | None = None
    """``Retry-After`` seconds of the site's last refusal, if it sent one."""
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
        client_factory: Callable[[], BomeClient] | None = None,
        *,
        polite_delay: float = RECOMMENDED_POLITE_DELAY,
        owner: str | None = None,
        hoy: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.time,
        stale_after: float = LEASE_STALE_SECONDS,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
        max_block_retries: int = MAX_BLOCK_RETRIES,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        """``wait(seconds)`` performs one back-off slice and returns ``True`` when
        cancelled; it defaults to waiting on the cancellation event (tests inject
        a fake so they never sleep)."""
        self.index = index
        self._client_factory = client_factory or (lambda: BomeClient(polite_delay=polite_delay))
        self.owner = owner or _default_owner()
        self._today = hoy
        self._clock = clock
        self._stale_after = stale_after
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._max_block_retries = max_block_retries
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._wait = wait or self._cancel.wait
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"estado": "inactivo"}
        self._started_clock: float | None = None
        self._storage_failures = 0
        self._transport_failures = 0

    # ------------------------------------------------------------------ public API

    def iniciar(
        self,
        desde: date | str | None = None,
        hasta: date | str | None = None,
        reindexar_recientes_dias: int = DEFAULT_RECENT_DAYS,
        reintentar_errores: bool = True,
    ) -> EstadoSincronizacion:
        """Start a background sync and return its status at once.

        While a sync of this object runs, returns that job. When another
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
                "iniciado": utc_iso(),
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(start, end, reindexar_recientes_dias, bool(reintentar_errores)),
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

    def _plan(self, refs: list[BulletinRef], recent_days: int, retry_errors: bool) -> list[BulletinRef]:
        known = self.index.estados_boletines()
        cutoff = self._today() - timedelta(days=recent_days)
        chosen: dict[str, BulletinRef] = {}
        for ref in refs:
            status = known.get(ref.cve)
            if (
                status is None
                or (recent_days > 0 and ref.date is not None and ref.date >= cutoff)
                or (retry_errors and status == "error")
            ):
                chosen[ref.cve] = ref
        return sorted(chosen.values(), key=lambda r: (r.date or date.min, r.number), reverse=True)

    def _run(self, start: date, end: date, recent_days: int, retry_errors: bool) -> None:
        client: BomeClient | None = None
        final: dict[str, Any] = {}
        self._storage_failures = 0
        self._transport_failures = 0
        try:
            client = self._client_factory()
            refs = client.calendar(start, end)
            self.index.registrar_calendario(refs)
            plan = self._plan(refs, recent_days, retry_errors)
            self._update(total_planificado=len(plan))
            final = {"estado": "completado"}
            for ref in plan:
                if self._cancel.is_set():
                    final = {"estado": "cancelado", "mensaje": "cancelled by request"}
                    break
                if not self.index.renovar_lease(self.owner, now=self._clock()):
                    final = {"estado": "fallido", "mensaje": "sync lease lost to another process"}
                    break
                self._update(cve_actual=ref.cve)
                try:
                    self._process(client, ref)
                except _StopSync as stop:
                    final = stop.final
                    break
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

        Raises :class:`_StopSync` when the site keeps refusing us, the sync is
        cancelled during a back-off or the lease is lost meanwhile.
        """
        try:
            articles, errors = self._fetch(client, ref)
        except _StopSync:
            raise
        except Exception as exc:  # a bad bulletin must not stop the crawl
            self._record_failure(ref, exc)
            self._count_transport_failure(exc)
            return
        self._transport_failures = 0
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

    def _fetch(self, client: BomeClient, ref: BulletinRef) -> tuple[list[Any], list[Any]]:
        """Download ``ref`` and its articles, backing off while the site refuses us."""
        attempt = 0
        while True:
            try:
                bulletin = client.bulletin(ref.cve)
                result = articulos_del_boletin(client, bulletin)
            except BomeBlockedError as exc:
                self._update(reintentar_tras_segundos=exc.retry_after, ultimo_error=f"{ref.cve}: {exc}")
                if attempt >= self._max_block_retries:
                    raise _StopSync(
                        {
                            "estado": "bloqueado",
                            "mensaje": _BLOCKED_MESSAGE.format(
                                detail=f": HTTP {exc.status} after {attempt + 1} attempts on {ref.cve}"
                            ),
                            "ultimo_error": str(exc),
                        }
                    ) from exc
                delay = min(
                    max(exc.retry_after or 0.0, self._backoff_base * 2**attempt), self._backoff_max
                )
                attempt += 1
                self._update(
                    mensaje=f"the site refused {ref.cve} (HTTP {exc.status}); waiting {delay:g} s "
                    f"before retry {attempt} of {self._max_block_retries}"
                )
                self._back_off(delay)
                continue
            if attempt:
                self._update(mensaje=None)
            return result

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

    def _count_transport_failure(self, exc: BaseException) -> None:
        """Track bulletins failing without an HTTP answer; too many in a row is a block."""
        if not (isinstance(exc, BomeHTTPError) and exc.status is None):
            self._transport_failures = 0
            return
        self._transport_failures += 1
        if self._transport_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
            raise _StopSync(
                {
                    "estado": "bloqueado",
                    "mensaje": _BLOCKED_MESSAGE.format(
                        detail=f": {self._transport_failures} bulletins in a row got no answer at "
                        "all (timeouts or dropped connections)"
                    ),
                }
            )

    def _record_failure(self, ref: BulletinRef, exc: BaseException) -> None:
        """Store ``ref`` as ``error``; if even that fails, count it toward giving up."""
        self._bump("hechos")
        self._bump("errores")
        try:
            self.index.guardar_boletin(ref, [], "error", error=exc)
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
        self._update(ultimo_error=f"{ref.cve}: {exc}")


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_MAX_SECONDS",
    "BACKOFF_SLICE_SECONDS",
    "DEFAULT_RECENT_DAYS",
    "MAX_BLOCK_RETRIES",
    "MAX_CONSECUTIVE_STORAGE_FAILURES",
    "MAX_CONSECUTIVE_TRANSPORT_FAILURES",
    "EstadoSincronizacion",
    "SincronizadorIndice",
]
