"""Background sync of the old BOME portal (melilla.es) into the local index.

:class:`SincronizadorPortalAntiguo` fills the local index with the article
sumarios of the old portal's bulletin fichas, the same way
:class:`~bome_navaja.sync.SincronizadorIndice` does with bomemelilla.es: it
shares its run loop (:class:`~bome_navaja.sync.SincronizadorBase`), so the
lease, the cancellation, the site guard rules, the pause after a 5xx page, the
per-run cap, the progress/ETA and the final states (``completado``,
``cancelado``, ``fallido``, ``bloqueado``) are the same. Only the source
differs:

1. Plan. The catalog (:meth:`PortalAntiguo.catalogo`: one ~1 MB GET, cached
   on disk by the portal client and in memory by this object, so it is
   downloaded at most once) filtered to ``desde``..``hasta``, by default
   :data:`DESDE_POR_DEFECTO` (1991-01-01: older fichas list no articles, only
   the whole-bulletin PDF) to :data:`HASTA_POR_DEFECTO` (2017-12-31: from 2018
   bomemelilla.es is complete). A bulletin is keyed by its identifier, or
   ``<cve>~<dboid>`` when the identifier repeats in the catalog (pre-2014).
   Skipped: bulletins already ``indexado`` from any origin, and those the old
   portal already answered ``sin_sumarios``; the old portal's ``roto`` ones
   unless ``reintentar_rotos``; its ``error`` ones unless
   ``reintentar_errores`` (default true). A bomemelilla.es ``sin_sumarios``
   (2014-2016), ``error`` or ``roto`` bulletin IS planned: the old portal may
   have its sumarios, and the index lets the better outcome win. Newest first.
2. Per bulletin: one ficha request by ``dboid`` (never a PDF), stored with
   :meth:`~bome_navaja.index.SumarioIndex.guardar_boletin_antiguo`. A failure
   is recorded as ``error`` (``roto`` after two 5xx answers, see
   :mod:`bome_navaja.index`) and the crawl goes on.

Mitigations required by the user's decision to crawl fichas despite the
portal's ``robots.txt``: manual start only (nothing here starts by itself), a
slow pace (the sync's own :class:`PortalAntiguo`, :data:`SYNC_POLITE_DELAY`
seconds + up to :data:`SYNC_JITTER` between requests, not the interactive 1 s
+ 0.5 s), the per-run cap and the melilla.es site guard
(:func:`~bome_navaja.antiguo.guardia_portal_antiguo`, state file
``estado_sitio_melilla.json``): a run started during its cooldown ends
``bloqueado`` without a single request, a block ends the run, and the error
budget is waited out (lease renewed) before resuming the same bulletin.

Both syncs write the same index, so they share its single lease: while one
runs, the other's ``iniciar`` answers ``en_curso_en_otro_proceso`` with the
holder (and the ``origen`` it syncs). Every state carries ``origen``
(``melilla.es`` here), and the final summary is stored per origin
(:attr:`~bome_navaja.index.EstadoIndice.ultimas_sincronizaciones`).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Final

from .antiguo import SITIO, BoletinAntiguo, CatalogoAntiguo, PortalAntiguo, guardia_portal_antiguo
from .guard import GuardiaSitio
from .index import LEASE_STALE_SECONDS, ORIGEN_ANTIGUO, ORIGEN_BOME, SumarioIndex
from .models import BomeBlockedError, BomeError
from .search import BusquedaInvalidaError
from .sync import (
    DEFAULT_MAX_BULLETINS_PER_RUN,
    SYNC_JITTER,
    SYNC_POLITE_DELAY,
    EstadoSincronizacion,
    SincronizadorBase,
    _check_cap,
    _date,
    _StopSync,
)

DESDE_POR_DEFECTO: Final = date(1991, 1, 1)
"""First day synced without ``desde``: fichas before 1991 list no articles
(evidence 2026-09-24: the 1986 ficha has only the whole-bulletin PDF)."""

HASTA_POR_DEFECTO: Final = date(2017, 12, 31)
"""Last day synced without ``hasta`` (2018 onwards is bomemelilla.es). A
``desde`` after it without ``hasta`` runs to today (the catalog ends in 2021)."""

_CAPPED_MESSAGE = (
    "per-run limit of {cap} bulletins reached: {left} bulletins of the old portal remain to be "
    "indexed. Call sincronizar_indice(origen=\"melilla.es\") again later to continue; spreading "
    "the runs out is gentler on melilla.es."
)


@dataclass(frozen=True, slots=True)
class _Planificado:
    """A catalog bulletin and its key in the index."""

    boletin: BoletinAntiguo
    clave: str


class SincronizadorPortalAntiguo(SincronizadorBase):
    """Runs one background sync of the old portal at a time for a :class:`SumarioIndex`."""

    origen = ORIGEN_ANTIGUO
    _sitio = "the old BOME portal (melilla.es)"
    _capped_message = _CAPPED_MESSAGE
    _thread_name = "bome-navaja-old-portal-sync"

    def __init__(
        self,
        index: SumarioIndex,
        portal_factory: Callable[..., PortalAntiguo] | None = None,
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
        """Arguments as :class:`~bome_navaja.sync.SincronizadorIndice`, except:

        ``guard`` is the melilla.es site guard (default: the persisted
        :func:`~bome_navaja.antiguo.guardia_portal_antiguo`; a guard of
        another site is a ``ValueError``). ``portal_factory`` is called as
        ``portal_factory(guard=guard)`` inside the worker thread and must wire
        the guard into the :class:`PortalAntiguo` it returns; by default a
        portal client at the sync pace (``polite_delay`` + ``jitter``) with
        the default catalog cache in the data folder.
        """
        guard = guard if guard is not None else guardia_portal_antiguo()
        if guard.sitio != SITIO:
            raise ValueError(f"the old-portal sync needs the {SITIO} site guard, got one for {guard.sitio}")
        super().__init__(
            index,
            guard=guard,
            max_boletines=max_boletines,
            owner=owner,
            hoy=hoy,
            clock=clock,
            stale_after=stale_after,
            wait=wait,
            pausa_aleatoria=pausa_aleatoria,
        )
        self._portal_factory: Callable[..., PortalAntiguo] = portal_factory or (
            lambda *, guard=None: PortalAntiguo(polite_delay=polite_delay, jitter=jitter, guard=guard)
        )
        self._catalogo: CatalogoAntiguo | None = None
        self._ajenos_fallidos: frozenset[str] = frozenset()

    # ------------------------------------------------------------------ public API

    def iniciar(
        self,
        desde: date | str | None = None,
        hasta: date | str | None = None,
        reintentar_errores: bool = True,
        max_boletines: int | None = None,
        reintentar_rotos: bool = False,
    ) -> EstadoSincronizacion:
        """Start a background sync of the old portal and return its status at once.

        Bulletins dated ``desde``..``hasta`` (default
        :data:`DESDE_POR_DEFECTO`..:data:`HASTA_POR_DEFECTO`), at most
        ``max_boletines`` (default: the constructor's), the newest of the
        plan. While a sync of this object runs, returns that job; while any
        other sync holds the index lease, returns ``en_curso_en_otro_proceso``
        with the holder and starts nothing.
        """
        start = _date(desde, "desde") or DESDE_POR_DEFECTO
        end = _date(hasta, "hasta")
        if end is None:
            end = HASTA_POR_DEFECTO if start <= HASTA_POR_DEFECTO else max(self._today(), start)
        if end < start:
            raise BusquedaInvalidaError(f"'hasta' ({end}) is before 'desde' ({start})")
        cap = self._max_boletines if max_boletines is None else _check_cap(max_boletines)
        return self._arrancar(
            {"desde": start.isoformat(), "hasta": end.isoformat()},
            cap,
            (start, end, bool(reintentar_errores), bool(reintentar_rotos)),
        )

    # ------------------------------------------------------------------ source hooks

    def _open_client(self) -> PortalAntiguo:
        return self._portal_factory(guard=self.guard)

    def _label(self, item: _Planificado) -> str:
        return item.clave

    def _catalog(self, portal: PortalAntiguo) -> CatalogoAntiguo:
        """The catalog: this object's copy, else the portal's cache, else one guarded GET."""
        if self._catalogo is not None:
            return self._catalogo
        self._wait_for_budget("the catalog")
        try:
            catalogo = self._guarded("the catalog", portal.catalogo)
        except (_StopSync, BomeBlockedError):
            raise
        except BomeError as exc:
            raise _StopSync(
                {
                    "estado": "fallido",
                    "mensaje": f"could not read the old portal's catalog ({type(exc).__name__}: {exc}); "
                    "nothing was indexed, try again later",
                    "ultimo_error": f"the catalog: {exc}",
                }
            ) from exc
        self._catalogo = catalogo
        return catalogo

    def _plan_run(
        self, portal: PortalAntiguo, start: date, end: date, retry_errors: bool, retry_broken: bool
    ) -> list[_Planificado]:
        catalogo = self._catalog(portal)
        claves = catalogo.claves()
        every = self.index.estados_boletines()
        ours = self.index.estados_boletines(origen=ORIGEN_ANTIGUO)
        theirs = self.index.estados_boletines(origen=ORIGEN_BOME)
        # Failures stored by bomemelilla.es: an old-portal failure never touches
        # them, so it can neither be counted as roto here nor stop a retry.
        self._ajenos_fallidos = frozenset(k for k, v in theirs.items() if v in ("error", "roto"))
        chosen: list[_Planificado] = []
        for boletin in catalogo.entre(start, end):
            clave = claves[boletin.dboid]
            if every.get(clave) == "indexado":
                continue
            status = ours.get(clave)
            if (
                status == "sin_sumarios"
                or (status == "roto" and not retry_broken)
                or (status == "error" and not retry_errors)
            ):
                continue
            chosen.append(_Planificado(boletin, clave))
        return sorted(
            chosen,
            key=lambda p: (p.boletin.fecha, p.boletin.numero, p.boletin.dboid),
            reverse=True,
        )

    def _process(self, portal: PortalAntiguo, item: _Planificado) -> None:
        """Fetch one ficha (one request) and store it; a failure becomes its ``error``.

        Raises :class:`~bome_navaja.sync._StopSync` when the portal blocks us
        (or the guard closes), the sync is cancelled during a pause or the
        lease is lost meanwhile.
        """
        boletin = item.boletin
        try:
            ficha = self._guarded(item.clave, lambda: portal.ficha(boletin.dboid, boletin=boletin))
        except _StopSync:
            raise
        except Exception as exc:  # a bad ficha must not stop the crawl
            self._failed(item, exc)
            return
        try:
            stored = self.index.guardar_boletin_antiguo(boletin, ficha, "indexado", clave=item.clave)
        except Exception as exc:  # e.g. a row the index rejects: record, go on
            self._record_failure(item, exc)
            return
        self._storage_failures = 0
        self._bump("hechos")
        self._bump("indexados" if stored == "indexado" else "sin_sumarios")

    def _store_failure(self, item: _Planificado, exc: BaseException) -> bool:
        stored = self.index.guardar_boletin_antiguo(item.boletin, None, "error", error=exc, clave=item.clave)
        return stored == "roto" and item.clave not in self._ajenos_fallidos


__all__ = [
    "DESDE_POR_DEFECTO",
    "HASTA_POR_DEFECTO",
    "SincronizadorPortalAntiguo",
]
