"""Site guard: keeps every request of bome-navaja below the bomemelilla.es firewall.

Field evidence (2026-09-24): the site bans the client IP right after the fifth
HTTP error answer (500s) within a short window, fail2ban-like; the ban lasts
about an hour and shows up as timeouts (dropped packets), never as a 403. New
broken pages keep appearing in bulletins never requested before, so avoiding
the known-broken ones is not enough: errors themselves must be paced.

:class:`GuardiaSitio` enforces two rules for every request of a process:

* **Error budget.** Every HTTP error answer (status >= 400, 404 included, to
  stay on the safe side) is timestamped. At most ``max_errores`` (default
  :data:`MAX_ERRORES_EN_VENTANA`) may fall inside a sliding window of
  ``ventana_segundos`` (default :data:`VENTANA_ERRORES_SEGUNDOS`); with the
  budget full, :meth:`comprobar` raises
  :class:`~bome_navaja.models.BomePausaPreventivaError` until the oldest
  counted error leaves the window. Three per ten minutes keeps a margin of two
  against the ban.
* **Cooldown** ("enfriamiento"). A block signal closes the site until a
  deadline: a 403/429/503 answer for ``max(Retry-After,
  enfriamiento_segundos)`` (``Retry-After`` capped at
  :data:`MAX_RETRY_AFTER_SEGUNDOS`; default cooldown
  :data:`ENFRIAMIENTO_SEGUNDOS`), or :data:`FALLOS_TRANSPORTE_BLOQUEO`
  requests in a row without any answer (timeouts, resets: what the ban looks
  like) for ``enfriamiento_segundos``. Any answer, even an error, resets
  that streak. While closed, :meth:`comprobar` raises a plain
  :class:`~bome_navaja.models.BomeBlockedError` with ``status=None``.

The three limits are per guard (the server builds its guards from
:mod:`bome_navaja.ajustes`); the defaults are the recommended values.

Both refusals happen before the network is touched.

Shared state. The interactive tools and the sync share the client IP, and
Claude Desktop may run several server processes or restart them, so the error
timestamps (pruned to the window), the cooldown deadline and its reason live
in a small JSON file (:data:`FICHERO_ESTADO` in the data folder). It is re-read
before each decision and written atomically (temporary file + ``os.replace``)
after each change, merged with what is on disk: the union of the timestamps
and the latest deadline. The transport-failure streak stays per process. A
missing file is an open site; a corrupt, unreadable or unwritable one never
breaks a tool: the guard keeps working in memory and logs once to stderr.
Read-merge-write is not locked across processes, so two processes erring in
the very same instant may lose one timestamp; the margin of two absorbs it.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import BomeBlockedError, BomePausaPreventivaError

VENTANA_ERRORES_SEGUNDOS = 600
"""Default sliding window of the error budget."""

MAX_ERRORES_EN_VENTANA = 3
"""Default HTTP error answers allowed inside the window (the site bans after the fifth)."""

ENFRIAMIENTO_SEGUNDOS = 4500
"""Default shortest cooldown after a block signal (the ban seen lasted about an hour)."""

MAX_RETRY_AFTER_SEGUNDOS = 86_400
"""Longest ``Retry-After`` honoured, so a bogus header cannot close the site for good."""

FALLOS_TRANSPORTE_BLOQUEO = 2
"""Requests in a row without any HTTP answer that count as a block."""

ESTADOS_BLOQUEO = frozenset({403, 429, 503})
"""Answers meaning the site is refusing us (see :class:`~bome_navaja.models.BomeBlockedError`)."""

FICHERO_ESTADO = "estado_sitio.json"
"""Name of the shared state file inside the data folder."""

FICHERO_ESTADO_MELILLA = "estado_sitio_melilla.json"
"""State file of the old BOME portal on melilla.es (its own budget and cooldown)."""

SITIO_POR_DEFECTO = "bomemelilla.es"
"""Site named in the model-facing messages unless another ``sitio`` is given."""

_FORMAT_VERSION = 1


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


class GuardiaSitio:
    """Error budget and cooldown shared by every request to the site.

    ``path`` is the shared state file (``None``: memory only, e.g. tests or no
    data folder). ``clock`` returns wall-clock seconds (it must be comparable
    across processes); tests inject a fake one. ``sitio`` is the host named in
    the model-facing messages: each guarded site gets its own guard and state
    file (the old portal on melilla.es uses :data:`FICHERO_ESTADO_MELILLA`).
    ``max_errores`` (an integer >= 1), ``ventana_segundos`` (> 0) and
    ``enfriamiento_segundos`` (>= 0) are the error budget, its window and the
    cooldown; anything else is a ``ValueError``. Thread-safe.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sitio: str = SITIO_POR_DEFECTO,
        max_errores: int = MAX_ERRORES_EN_VENTANA,
        ventana_segundos: float = VENTANA_ERRORES_SEGUNDOS,
        enfriamiento_segundos: float = ENFRIAMIENTO_SEGUNDOS,
    ) -> None:
        if isinstance(max_errores, bool) or not isinstance(max_errores, int) or max_errores < 1:
            raise ValueError(f"max_errores must be an integer >= 1, got {max_errores!r}")
        if not _finite_number(ventana_segundos) or ventana_segundos <= 0:
            raise ValueError(f"ventana_segundos must be a finite number > 0, got {ventana_segundos!r}")
        if not _finite_number(enfriamiento_segundos) or enfriamiento_segundos < 0:
            raise ValueError(f"enfriamiento_segundos must be a finite number >= 0, got {enfriamiento_segundos!r}")
        self.sitio = sitio
        """Host named in the messages (``bomemelilla.es`` by default)."""
        self._max_errores = max_errores
        self._ventana = float(ventana_segundos)
        self._enfriamiento = float(enfriamiento_segundos)
        self._path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._errors: Counter[float] = Counter()
        self._deadline = 0.0
        self._motivo: str | None = None
        self._streak = 0
        self._warned = False

    @property
    def path(self) -> Path | None:
        """The shared state file, or ``None`` for a memory-only guard."""
        return self._path

    @property
    def max_errores(self) -> int:
        """HTTP error answers allowed inside the window."""
        return self._max_errores

    @property
    def ventana_segundos(self) -> float:
        """Sliding window of the error budget, in seconds."""
        return self._ventana

    @property
    def enfriamiento_segundos(self) -> float:
        """Shortest cooldown after a block signal, in seconds."""
        return self._enfriamiento

    # ------------------------------------------------------------------ decisions

    def comprobar(self, url: str = "") -> None:
        """Raise before a request to ``url`` if the site must not be asked now.

        Raises :class:`~bome_navaja.models.BomeBlockedError` (``status=None``)
        during a cooldown and :class:`~bome_navaja.models.BomePausaPreventivaError`
        when the error budget is full; ``retry_after`` is the seconds to wait.
        Never touches the network.
        """
        with self._lock:
            now = self._clock()
            self._refresh(now)
            remaining = self._deadline - now
            if remaining > 0:
                raise BomeBlockedError(self._block_message(remaining), status=None, url=url, retry_after=remaining)
            wait = self._budget_wait(now)
            if wait > 0:
                raise BomePausaPreventivaError(
                    self._pause_message(self._count(now), wait), status=None, url=url, retry_after=wait
                )

    def espera_necesaria(self, now: float | None = None) -> float:
        """Seconds until the error budget has room again (0 when it has room now)."""
        with self._lock:
            self._refresh(self._clock())
            return self._budget_wait(self._clock() if now is None else now)

    def segundos_enfriamiento(self, now: float | None = None) -> float:
        """Seconds left of the cooldown (0 when the site is open)."""
        with self._lock:
            self._refresh(self._clock())
            moment = self._clock() if now is None else now
            return max(self._deadline - moment, 0.0)

    def en_enfriamiento(self, now: float | None = None) -> bool:
        """True while a cooldown keeps the site closed."""
        return self.segundos_enfriamiento(now) > 0

    def errores_en_ventana(self, now: float | None = None) -> int:
        """HTTP error answers counted inside the current window."""
        with self._lock:
            self._refresh(self._clock())
            return self._count(self._clock() if now is None else now)

    @property
    def motivo(self) -> str | None:
        """Short reason of the current cooldown, ``None`` when the site is open."""
        with self._lock:
            now = self._clock()
            self._refresh(now)
            return self._motivo if self._deadline > now else None

    def estado(self) -> dict[str, Any]:
        """JSON-safe snapshot for ``estado_servidor``."""
        with self._lock:
            now = self._clock()
            self._refresh(now)
            closed = self._deadline > now
            return {
                "enfriamiento_hasta": _utc(self._deadline).isoformat() if closed else None,
                "segundos_restantes": math.ceil(self._deadline - now) if closed else 0,
                "motivo": self._motivo if closed else None,
                "errores_en_ventana": self._count(now),
                "max_errores": self._max_errores,
                "ventana_segundos": self._ventana,
                "fichero": str(self._path) if self._path is not None else None,
            }

    # ------------------------------------------------------------------ recording

    def registrar(self, status: int | None, *, retry_after: float | None = None) -> None:
        """Record the outcome of one request.

        ``status`` is the HTTP status of the answer, or ``None`` when there was
        no answer at all (transport failure). ``retry_after`` is the site's
        ``Retry-After`` in seconds for a blocking answer.
        """
        with self._lock:
            now = self._clock()
            if status is None:
                self._streak += 1
                if self._streak < FALLOS_TRANSPORTE_BLOQUEO:
                    return
                self._streak = 0
                self._refresh(now)
                self._close(
                    now + self._enfriamiento,
                    f"{FALLOS_TRANSPORTE_BLOQUEO} peticiones seguidas sin respuesta del sitio "
                    "(tiempo de espera agotado o conexión cortada: así se ve su cortafuegos)",
                )
                self._save(now)
                return
            self._streak = 0
            if status < 400:
                return
            self._refresh(now)
            self._errors[now] += 1
            if status in ESTADOS_BLOQUEO:
                asked = retry_after if _finite_number(retry_after) and retry_after > 0 else None
                seconds = max(min(asked or 0.0, MAX_RETRY_AFTER_SEGUNDOS), self._enfriamiento)
                hint = f" con Retry-After {asked:g} s" if asked else ""
                self._close(now + seconds, f"el sitio respondió HTTP {status}{hint}")
            self._save(now)

    # ------------------------------------------------------------------ internals (lock held)

    def _count(self, now: float) -> int:
        start = now - self._ventana
        return sum(n for ts, n in self._errors.items() if ts > start)

    def _budget_wait(self, now: float) -> float:
        start = now - self._ventana
        stamps = sorted(ts for ts in self._errors.elements() if ts > start)
        if len(stamps) < self._max_errores:
            return 0.0
        # Enough errors must leave for the count to drop below the budget.
        oldest_to_leave = stamps[len(stamps) - self._max_errores]
        return max(oldest_to_leave + self._ventana - now, 0.0)

    def _close(self, deadline: float, motivo: str) -> None:
        if deadline > self._deadline:
            self._deadline = deadline
            self._motivo = motivo

    def _prune(self, now: float) -> None:
        start = now - self._ventana
        for stamp in [ts for ts in self._errors if ts <= start]:
            del self._errors[stamp]
        if self._deadline <= now:
            self._deadline = 0.0
            self._motivo = None

    def _warn(self, problem: str) -> None:
        if self._warned:
            return
        self._warned = True
        print(
            f"bome-navaja: the site guard file {self._path} {problem}; the error budget and "
            "cooldown are kept in memory for this process",
            file=sys.stderr,
        )

    def _refresh(self, now: float) -> None:
        """Merge the shared file into memory (union of errors, latest deadline)."""
        if self._path is not None:
            self._merge_file()
        self._prune(now)

    def _merge_file(self) -> None:
        assert self._path is not None
        try:
            raw = self._path.read_text("utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError) as exc:
            self._warn(f"cannot be read ({exc})")
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            stamps = data.get("errores", [])
            deadline = data.get("enfriamiento_hasta")
            motivo = data.get("motivo")
            if not isinstance(stamps, list) or not all(_finite_number(ts) for ts in stamps):
                raise ValueError("'errores' is not a list of timestamps")
            if deadline is not None and not _finite_number(deadline):
                raise ValueError("'enfriamiento_hasta' is not a timestamp")
            if motivo is not None and not isinstance(motivo, str):
                raise ValueError("'motivo' is not a string")
        except ValueError as exc:
            self._warn(f"is corrupt ({exc})")
            return
        self._errors |= Counter(float(ts) for ts in stamps)
        if deadline is not None:
            self._close(float(deadline), motivo or "bloqueo registrado por otro proceso")

    def _save(self, now: float) -> None:
        """Write the merged state atomically; a failure only logs (once)."""
        if self._path is None:
            return
        self._prune(now)
        closed = self._deadline > now
        data = {
            "version": _FORMAT_VERSION,
            "errores": sorted(self._errors.elements()),
            "enfriamiento_hasta": self._deadline if closed else None,
            "motivo": self._motivo if closed else None,
        }
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data), "utf-8")
            os.replace(temporary, self._path)
        except OSError as exc:
            self._warn(f"cannot be written ({exc})")
            with contextlib.suppress(OSError):
                temporary.unlink()

    # ------------------------------------------------------------------ messages (model-facing)

    def _block_message(self, remaining: float) -> str:
        until = _utc(self._deadline).strftime("%Y-%m-%d %H:%M")
        return (
            f"bloqueo del sitio: bome-navaja no hará ninguna petición a {self.sitio} hasta las "
            f"{until} UTC (faltan {math.ceil(remaining)} s) porque el sitio nos está bloqueando "
            f"({self._motivo or 'motivo desconocido'}). No es un fallo de este documento; "
            "reintenta pasado ese tiempo."
        )

    def _pause_message(self, errors: int, wait: float) -> str:
        reason = (
            "su cortafuegos bloquea la IP durante horas a partir del quinto"
            if self.sitio == SITIO_POR_DEFECTO
            else "bome-navaja limita los errores para no provocar un bloqueo de la IP"
        )
        return (
            f"pausa preventiva de bome-navaja, no es un bloqueo del sitio: {self.sitio} ya "
            f"respondió {errors} errores HTTP en los últimos {self._ventana / 60:g} min "
            f"y {reason}, así que no se le "
            f"pide nada más hasta que pase la ventana. Reintenta dentro de {math.ceil(wait)} s."
        )


__all__ = [
    "ENFRIAMIENTO_SEGUNDOS",
    "ESTADOS_BLOQUEO",
    "FALLOS_TRANSPORTE_BLOQUEO",
    "FICHERO_ESTADO",
    "FICHERO_ESTADO_MELILLA",
    "MAX_ERRORES_EN_VENTANA",
    "MAX_RETRY_AFTER_SEGUNDOS",
    "SITIO_POR_DEFECTO",
    "VENTANA_ERRORES_SEGUNDOS",
    "GuardiaSitio",
]
