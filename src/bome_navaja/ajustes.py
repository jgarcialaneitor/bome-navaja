"""Site-facing settings ("ajustes") of bome-navaja, from ``BOME_NAVAJA_*`` variables.

Every setting that governs how hard bome-navaja hits the sites is tunable, one
shared value for bomemelilla.es and the old melilla.es portal (user decisions
2026-09-24):

========================================  =======  =========================================
Variable                                  Default  Riskier than recommended when
========================================  =======  =========================================
``BOME_NAVAJA_SYNC_DELAY`` (s)            2        below 2
``BOME_NAVAJA_SYNC_JITTER`` (s)           1        below 1
``BOME_NAVAJA_SYNC_MAX_BOLETINES``        250      above 250
``BOME_NAVAJA_QUERY_DELAY`` (s)           0.5      below 0.5 (bomemelilla.es tools only)
``BOME_NAVAJA_GUARD_MAX_ERRORS``          3        above 3; much riskier from 5 (the ban)
``BOME_NAVAJA_GUARD_WINDOW_MINUTES``      10       below 10
``BOME_NAVAJA_GUARD_COOLDOWN_MINUTES``    75       below 75; much riskier below 60 (the ban)
``BOME_NAVAJA_ERROR_PAUSE_SECONDS``       30       below 30 (the pause is ``min``..``2*min``)
``BOME_NAVAJA_TIMEOUT`` (s)               30       below 30 (a slow site looks like a block)
========================================  =======  =========================================

There are no hard limits: any physically valid value is used as given, and a
value riskier than recommended adds a line to :attr:`Ajustes.riesgos`. A value
that cannot work (not a number, NaN or infinite, negative, zero where it must
be positive, a non-integral count, or a time longer than one year) keeps the
default and adds a line to :attr:`Ajustes.avisos`. The one-year cap is technical
validity, not a safety limit: larger finite times crash at runtime (a socket
timeout overflows, a cooldown deadline no longer fits a ``datetime``, the
sync delay plus its jitter can reach infinity). It applies to the longest time
in seconds a value produces (minutes times 60; twice the error pause, its top),
never to the counts. Blank values count as unset; counts accept an integral
float such as ``250.0`` (Claude Desktop may render numbers that way), and a
Spanish decimal comma is accepted (``0,5``) when it is the only separator.

:func:`ajustes_desde_entorno` is pure and never raises. The server reads the
settings once per process, when a client, guard or sync is first needed, and
prints its messages to stderr then: changing a variable needs a restart.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from .client import DEFAULT_TIMEOUT
from .guard import ENFRIAMIENTO_SEGUNDOS, MAX_ERRORES_EN_VENTANA, VENTANA_ERRORES_SEGUNDOS
from .search import RECOMMENDED_POLITE_DELAY
from .sync import (
    DEFAULT_MAX_BULLETINS_PER_RUN,
    ENV_SYNC_DELAY,
    ENV_SYNC_JITTER,
    ENV_SYNC_MAX_BOLETINES,
    PAUSA_TRAS_ERROR_MIN_SEGUNDOS,
    SYNC_JITTER,
    SYNC_POLITE_DELAY,
)

ENV_QUERY_DELAY = "BOME_NAVAJA_QUERY_DELAY"
ENV_GUARD_MAX_ERRORS = "BOME_NAVAJA_GUARD_MAX_ERRORS"
ENV_GUARD_WINDOW_MINUTES = "BOME_NAVAJA_GUARD_WINDOW_MINUTES"
ENV_GUARD_COOLDOWN_MINUTES = "BOME_NAVAJA_GUARD_COOLDOWN_MINUTES"
ENV_ERROR_PAUSE_SECONDS = "BOME_NAVAJA_ERROR_PAUSE_SECONDS"
ENV_TIMEOUT = "BOME_NAVAJA_TIMEOUT"

UMBRAL_ERRORES_BLOQUEO = 5
"""Error count at which the site was seen banning the IP (field evidence 2026-09-24)."""

DURACION_BLOQUEO_MINUTOS = 60
"""Approximate length of the site's ban (field evidence 2026-09-24)."""

MAX_TIEMPO_SEGUNDOS = 24 * 60 * 60
"""Longest valid time, one day in seconds: a technical limit, not a safety one.

Larger values are not portable: on Windows ``socket.settimeout`` rejected
one year with ``OverflowError`` (CI evidence). One day stays far below an
``INT_MAX``-millisecond limit (about 24.8 days).
"""


@dataclass(frozen=True, slots=True)
class Ajustes:
    """Effective site-facing settings and the messages produced reading them."""

    pausa_sincronizacion_segundos: float = SYNC_POLITE_DELAY
    variacion_sincronizacion_segundos: float = SYNC_JITTER
    max_boletines_por_ejecucion: int = DEFAULT_MAX_BULLETINS_PER_RUN
    pausa_consultas_segundos: float = RECOMMENDED_POLITE_DELAY
    guardia_max_errores: int = MAX_ERRORES_EN_VENTANA
    guardia_ventana_minutos: float = VENTANA_ERRORES_SEGUNDOS / 60
    guardia_enfriamiento_minutos: float = ENFRIAMIENTO_SEGUNDOS / 60
    pausa_tras_error_segundos: float = PAUSA_TRAS_ERROR_MIN_SEGUNDOS
    tiempo_espera_segundos: float = DEFAULT_TIMEOUT
    avisos: tuple[str, ...] = ()
    """Invalid values replaced by their default, one Spanish line each."""
    riesgos: tuple[str, ...] = ()
    """Values used as given but riskier than recommended, one Spanish line each."""

    @property
    def guardia_ventana_segundos(self) -> float:
        return self.guardia_ventana_minutos * 60

    @property
    def guardia_enfriamiento_segundos(self) -> float:
        return self.guardia_enfriamiento_minutos * 60

    @property
    def pausa_tras_error_max_segundos(self) -> float:
        """Upper bound of the random pause after a broken page (twice the lower one)."""
        return 2 * self.pausa_tras_error_segundos

    @property
    def mensajes(self) -> tuple[str, ...]:
        """Every message, invalid values first, for the operator's log."""
        return self.avisos + self.riesgos

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view for ``estado_servidor``: values, variables and messages."""
        data: dict[str, Any] = {
            spec.campo: getattr(self, spec.campo) for spec in _AJUSTES
        }
        data["pausa_tras_error_max_segundos"] = self.pausa_tras_error_max_segundos
        data["variables"] = {spec.campo: spec.variable for spec in _AJUSTES}
        data["avisos"] = list(self.avisos)
        data["riesgos"] = list(self.riesgos)
        return data


@dataclass(frozen=True, slots=True)
class _Ajuste:
    """How one variable is parsed, validated and judged."""

    variable: str
    campo: str
    unidad: str
    """``"s"``, ``"min"`` or ``""`` (a count)."""
    minimo: float
    minimo_incluido: bool
    """``True``: the value may equal ``minimo``; ``False``: it must exceed it."""
    recomendado: str
    arriesgado: Callable[[float], bool]
    consecuencia: str
    muy_arriesgado: Callable[[float], bool] | None = None
    consecuencia_grave: str = ""
    factor_segundos: float = 1.0
    """Largest number of seconds derived from a value of 1 (60 for minutes; 2 for the
    error pause, whose top is twice the value); counts ignore it."""

    @property
    def entero(self) -> bool:
        return self.unidad == ""

    @property
    def defecto(self) -> float:
        return next(f.default for f in fields(Ajustes) if f.name == self.campo)  # type: ignore[return-value]

    def mostrar(self, value: float) -> str:
        text = str(int(value)) if self.entero else f"{value:g}"
        return f"{text} {self.unidad}" if self.unidad else text

    def requisito(self) -> str:
        kind = {"s": "un número de segundos", "min": "un número de minutos", "": "un número entero"}[self.unidad]
        relation = "mayor o igual que" if self.minimo_incluido else "mayor que"
        return f"{kind} {relation} {self.minimo:g}"


_AJUSTES: tuple[_Ajuste, ...] = (
    _Ajuste(
        ENV_SYNC_DELAY,
        "pausa_sincronizacion_segundos",
        "s",
        0,
        True,
        f"{SYNC_POLITE_DELAY:g} s o más",
        lambda v: v < SYNC_POLITE_DELAY,
        "la sincronización pide páginas más deprisa y el cortafuegos del sitio puede bloquear tu IP",
    ),
    _Ajuste(
        ENV_SYNC_JITTER,
        "variacion_sincronizacion_segundos",
        "s",
        0,
        True,
        f"{SYNC_JITTER:g} s o más",
        lambda v: v < SYNC_JITTER,
        "la sincronización pide a un ritmo más regular, más fácil de tomar por un robot y bloquear",
    ),
    _Ajuste(
        ENV_SYNC_MAX_BOLETINES,
        "max_boletines_por_ejecucion",
        "",
        1,
        True,
        f"{DEFAULT_MAX_BULLETINS_PER_RUN} o menos",
        lambda v: v > DEFAULT_MAX_BULLETINS_PER_RUN,
        "cada ejecución hace más peticiones seguidas al sitio y aumenta el riesgo de que bloquee tu IP",
    ),
    _Ajuste(
        ENV_QUERY_DELAY,
        "pausa_consultas_segundos",
        "s",
        0,
        True,
        f"{RECOMMENDED_POLITE_DELAY:g} s o más",
        lambda v: v < RECOMMENDED_POLITE_DELAY,
        "las herramientas piden páginas a bomemelilla.es más deprisa y el sitio puede bloquear tu IP",
    ),
    _Ajuste(
        ENV_GUARD_MAX_ERRORS,
        "guardia_max_errores",
        "",
        1,
        True,
        f"{MAX_ERRORES_EN_VENTANA} o menos",
        lambda v: v > MAX_ERRORES_EN_VENTANA,
        "la guardia admite más errores del sitio antes de parar y deja menos margen frente a su "
        f"bloqueo, que salta hacia el {UMBRAL_ERRORES_BLOQUEO}.º error",
        lambda v: v >= UMBRAL_ERRORES_BLOQUEO,
        f"el sitio bloquea la IP hacia el {UMBRAL_ERRORES_BLOQUEO}.º error, así que la guardia ya no "
        "parará antes del bloqueo",
    ),
    _Ajuste(
        ENV_GUARD_WINDOW_MINUTES,
        "guardia_ventana_minutos",
        "min",
        0,
        False,
        f"{VENTANA_ERRORES_SEGUNDOS / 60:g} min o más",
        lambda v: v < VENTANA_ERRORES_SEGUNDOS / 60,
        "los errores se olvidan antes y caben más en poco tiempo, lo que acerca el bloqueo del sitio",
        factor_segundos=60,
    ),
    _Ajuste(
        ENV_GUARD_COOLDOWN_MINUTES,
        "guardia_enfriamiento_minutos",
        "min",
        0,
        True,
        f"{ENFRIAMIENTO_SEGUNDOS / 60:g} min o más",
        lambda v: v < ENFRIAMIENTO_SEGUNDOS / 60,
        "tras una señal de bloqueo se vuelve a pedir antes y el sitio puede seguir bloqueándote",
        lambda v: v < DURACION_BLOQUEO_MINUTOS,
        "el bloqueo del sitio dura cerca de 1 h, así que volver antes choca con un sitio que aún te "
        "bloquea y puede alargar el bloqueo",
        factor_segundos=60,
    ),
    _Ajuste(
        ENV_ERROR_PAUSE_SECONDS,
        "pausa_tras_error_segundos",
        "s",
        0,
        True,
        f"{PAUSA_TRAS_ERROR_MIN_SEGUNDOS:g} s o más",
        lambda v: v < PAUSA_TRAS_ERROR_MIN_SEGUNDOS,
        "tras una página rota la sincronización vuelve a pedir antes, y los errores seguidos "
        "provocan el bloqueo del sitio",
        factor_segundos=2,
    ),
    _Ajuste(
        ENV_TIMEOUT,
        "tiempo_espera_segundos",
        "s",
        0,
        False,
        f"{DEFAULT_TIMEOUT:g} s o más",
        lambda v: v < DEFAULT_TIMEOUT,
        "una respuesta lenta del sitio puede tomarse por un bloqueo (dos peticiones seguidas sin "
        "respuesta cierran el sitio durante el enfriamiento)",
    ),
)

VARIABLES: tuple[str, ...] = tuple(spec.variable for spec in _AJUSTES)
"""Every variable read by :func:`ajustes_desde_entorno`, in table order."""


def _parse(spec: _Ajuste, raw: str) -> float | str:
    """The valid value of ``raw`` (stripped, not blank) for ``spec``, or why it is invalid.

    A Spanish decimal comma is accepted when it is the only separator
    (``"0,5"``); ``"1.000,5"`` or ``"1,000.5"`` stay invalid.
    """
    text = raw.replace(",", ".") if raw.count(",") == 1 and "." not in raw else raw
    requisito = f"tiene que ser {spec.requisito()}"
    try:
        value = float(text)
    except ValueError:
        return requisito
    if not math.isfinite(value):
        return requisito
    if spec.entero:
        if not value.is_integer():
            return requisito
        value = int(value)
    if value < spec.minimo or (value == spec.minimo and not spec.minimo_incluido):
        return requisito
    if not spec.entero and value * spec.factor_segundos > MAX_TIEMPO_SEGUNDOS:
        return "es un tiempo demasiado grande (más de un día)"
    return value


def ajustes_desde_entorno(environ: Mapping[str, str]) -> Ajustes:
    """The site-facing settings from ``environ`` (pure; never raises).

    Unset or blank variables keep their default silently; invalid ones keep it
    with an ``avisos`` line; valid values riskier than recommended are used
    with a ``riesgos`` line (the "much riskier" wording for a guard that would
    no longer stop before the ban, or a cooldown shorter than the ban).
    """
    values: dict[str, Any] = {}
    avisos: list[str] = []
    riesgos: list[str] = []
    for spec in _AJUSTES:
        raw = environ.get(spec.variable, "").strip()
        if not raw:
            continue
        value = _parse(spec, raw)
        if isinstance(value, str):
            avisos.append(
                f"{spec.variable}={raw!r} no es válido: {value}. "
                f"Se usa el valor por defecto, {spec.mostrar(spec.defecto)}."
            )
            continue
        values[spec.campo] = value if spec.entero else float(value)
        shown = f"{spec.variable}={spec.mostrar(value)}"
        if spec.muy_arriesgado is not None and spec.muy_arriesgado(value):
            riesgos.append(
                f"{shown}: mucho más arriesgado que lo recomendado ({spec.recomendado}); "
                f"{spec.consecuencia_grave}."
            )
        elif spec.arriesgado(value):
            riesgos.append(
                f"{shown}: más arriesgado que lo recomendado ({spec.recomendado}); {spec.consecuencia}."
            )
    return Ajustes(**values, avisos=tuple(avisos), riesgos=tuple(riesgos))


__all__ = [
    "DURACION_BLOQUEO_MINUTOS",
    "ENV_ERROR_PAUSE_SECONDS",
    "ENV_GUARD_COOLDOWN_MINUTES",
    "ENV_GUARD_MAX_ERRORS",
    "ENV_GUARD_WINDOW_MINUTES",
    "ENV_QUERY_DELAY",
    "ENV_SYNC_DELAY",
    "ENV_SYNC_JITTER",
    "ENV_SYNC_MAX_BOLETINES",
    "ENV_TIMEOUT",
    "MAX_TIEMPO_SEGUNDOS",
    "UMBRAL_ERRORES_BLOQUEO",
    "VARIABLES",
    "Ajustes",
    "ajustes_desde_entorno",
]
