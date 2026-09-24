"""Site-facing settings from the environment (pure parsing, no network)."""

from __future__ import annotations

import json

import pytest

from bome_navaja.ajustes import VARIABLES, Ajustes, ajustes_desde_entorno

DEFAULTS = {
    "pausa_sincronizacion_segundos": 2.0,
    "variacion_sincronizacion_segundos": 1.0,
    "max_boletines_por_ejecucion": 250,
    "pausa_consultas_segundos": 0.5,
    "guardia_max_errores": 3,
    "guardia_ventana_minutos": 10.0,
    "guardia_enfriamiento_minutos": 75.0,
    "pausa_tras_error_segundos": 30.0,
    "tiempo_espera_segundos": 30.0,
}

FIELD = {
    "BOME_NAVAJA_SYNC_DELAY": "pausa_sincronizacion_segundos",
    "BOME_NAVAJA_SYNC_JITTER": "variacion_sincronizacion_segundos",
    "BOME_NAVAJA_SYNC_MAX_BOLETINES": "max_boletines_por_ejecucion",
    "BOME_NAVAJA_QUERY_DELAY": "pausa_consultas_segundos",
    "BOME_NAVAJA_GUARD_MAX_ERRORS": "guardia_max_errores",
    "BOME_NAVAJA_GUARD_WINDOW_MINUTES": "guardia_ventana_minutos",
    "BOME_NAVAJA_GUARD_COOLDOWN_MINUTES": "guardia_enfriamiento_minutos",
    "BOME_NAVAJA_ERROR_PAUSE_SECONDS": "pausa_tras_error_segundos",
    "BOME_NAVAJA_TIMEOUT": "tiempo_espera_segundos",
}

COUNTS = {"BOME_NAVAJA_SYNC_MAX_BOLETINES", "BOME_NAVAJA_GUARD_MAX_ERRORS"}
POSITIVE = {"BOME_NAVAJA_GUARD_WINDOW_MINUTES", "BOME_NAVAJA_TIMEOUT"}
"""Variables where zero cannot work."""


def values(ajustes: Ajustes) -> dict[str, float]:
    return {name: getattr(ajustes, name) for name in DEFAULTS}


def one(environ: dict[str, str]) -> tuple[Ajustes, str]:
    """The settings and their single message."""
    ajustes = ajustes_desde_entorno(environ)
    (message,) = ajustes.mensajes
    return ajustes, message


# --------------------------------------------------------------------------- defaults and blanks


def test_every_variable_is_known() -> None:
    assert set(VARIABLES) == set(FIELD)
    assert len(VARIABLES) == 9


def test_defaults_without_any_variable() -> None:
    ajustes = ajustes_desde_entorno({})
    assert values(ajustes) == DEFAULTS
    assert ajustes == Ajustes()
    assert (ajustes.avisos, ajustes.riesgos) == ((), ())
    assert ajustes.guardia_ventana_segundos == 600
    assert ajustes.guardia_enfriamiento_segundos == 4500
    assert ajustes.pausa_tras_error_max_segundos == 60


@pytest.mark.parametrize("blank", ["", " ", "\t\n"])
def test_blank_values_count_as_unset(blank: str) -> None:
    assert ajustes_desde_entorno(dict.fromkeys(VARIABLES, blank)) == Ajustes()


def test_the_recommended_values_give_no_message() -> None:
    environ = {
        "BOME_NAVAJA_SYNC_DELAY": "2",
        "BOME_NAVAJA_SYNC_JITTER": "1.0",
        "BOME_NAVAJA_SYNC_MAX_BOLETINES": "250.0",
        "BOME_NAVAJA_QUERY_DELAY": " 0.5 ",
        "BOME_NAVAJA_GUARD_MAX_ERRORS": "3",
        "BOME_NAVAJA_GUARD_WINDOW_MINUTES": "10",
        "BOME_NAVAJA_GUARD_COOLDOWN_MINUTES": "75",
        "BOME_NAVAJA_ERROR_PAUSE_SECONDS": "30",
        "BOME_NAVAJA_TIMEOUT": "30",
        "OTHER": "x",
    }
    assert ajustes_desde_entorno(environ) == Ajustes()


def test_safer_values_are_used_without_any_message() -> None:
    environ = {
        "BOME_NAVAJA_SYNC_DELAY": "5",
        "BOME_NAVAJA_SYNC_JITTER": "2.5",
        "BOME_NAVAJA_SYNC_MAX_BOLETINES": "40",
        "BOME_NAVAJA_QUERY_DELAY": "1",
        "BOME_NAVAJA_GUARD_MAX_ERRORS": "1",
        "BOME_NAVAJA_GUARD_WINDOW_MINUTES": "30",
        "BOME_NAVAJA_GUARD_COOLDOWN_MINUTES": "120",
        "BOME_NAVAJA_ERROR_PAUSE_SECONDS": "90",
        "BOME_NAVAJA_TIMEOUT": "60",
    }
    ajustes = ajustes_desde_entorno(environ)
    assert ajustes.mensajes == ()
    assert values(ajustes) == {
        "pausa_sincronizacion_segundos": 5.0,
        "variacion_sincronizacion_segundos": 2.5,
        "max_boletines_por_ejecucion": 40,
        "pausa_consultas_segundos": 1.0,
        "guardia_max_errores": 1,
        "guardia_ventana_minutos": 30.0,
        "guardia_enfriamiento_minutos": 120.0,
        "pausa_tras_error_segundos": 90.0,
        "tiempo_espera_segundos": 60.0,
    }
    assert isinstance(ajustes.guardia_max_errores, int) and isinstance(ajustes.max_boletines_por_ejecucion, int)
    assert (ajustes.guardia_ventana_segundos, ajustes.guardia_enfriamiento_segundos) == (1800, 7200)
    assert ajustes.pausa_tras_error_max_segundos == 180


# --------------------------------------------------------------------------- invalid values


@pytest.mark.parametrize("variable", sorted(FIELD))
@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "-inf", "1,5", "-1", "2s"])
def test_an_invalid_value_keeps_the_default_with_a_warning(variable: str, raw: str) -> None:
    ajustes, message = one({variable: raw})
    assert getattr(ajustes, FIELD[variable]) == DEFAULTS[FIELD[variable]]
    assert ajustes.avisos == (message,) and ajustes.riesgos == ()
    assert message.startswith(f"{variable}={raw!r} no es válido: tiene que ser ")
    assert "Se usa el valor por defecto" in message


@pytest.mark.parametrize("variable", sorted(COUNTS))
@pytest.mark.parametrize("raw", ["0", "2.5", "250.5", "0.0"])
def test_a_count_must_be_an_integer_of_at_least_one(variable: str, raw: str) -> None:
    ajustes, message = one({variable: raw})
    assert getattr(ajustes, FIELD[variable]) == DEFAULTS[FIELD[variable]]
    assert "un número entero mayor o igual que 1" in message


@pytest.mark.parametrize("variable", sorted(COUNTS))
def test_a_count_accepts_an_integral_float(variable: str) -> None:
    ajustes = ajustes_desde_entorno({variable: " 2.0 "})
    assert getattr(ajustes, FIELD[variable]) == 2
    assert isinstance(getattr(ajustes, FIELD[variable]), int)


@pytest.mark.parametrize("variable", sorted(POSITIVE))
def test_zero_is_invalid_where_it_cannot_work(variable: str) -> None:
    ajustes, message = one({variable: "0"})
    assert getattr(ajustes, FIELD[variable]) == DEFAULTS[FIELD[variable]]
    assert "mayor que 0" in message and "mayor o igual" not in message


@pytest.mark.parametrize(
    "variable",
    ["BOME_NAVAJA_SYNC_DELAY", "BOME_NAVAJA_SYNC_JITTER", "BOME_NAVAJA_QUERY_DELAY",
     "BOME_NAVAJA_GUARD_COOLDOWN_MINUTES", "BOME_NAVAJA_ERROR_PAUSE_SECONDS"],
)
def test_zero_is_accepted_where_it_can_work(variable: str) -> None:
    ajustes, message = one({variable: "0"})
    assert getattr(ajustes, FIELD[variable]) == 0
    assert ajustes.riesgos == (message,)


def test_every_bad_variable_gets_its_own_warning() -> None:
    ajustes = ajustes_desde_entorno(dict.fromkeys(VARIABLES, "x"))
    assert ajustes == Ajustes(avisos=ajustes.avisos)
    assert len(ajustes.avisos) == 9
    assert [message.split("=", 1)[0] for message in ajustes.avisos] == list(VARIABLES)


# --------------------------------------------------------------------------- risk warnings


@pytest.mark.parametrize(
    ("variable", "risky", "shown", "recommended"),
    [
        ("BOME_NAVAJA_SYNC_DELAY", "1.5", "1.5 s", "2 s o más"),
        ("BOME_NAVAJA_SYNC_JITTER", "0.5", "0.5 s", "1 s o más"),
        ("BOME_NAVAJA_SYNC_MAX_BOLETINES", "251", "251", "250 o menos"),
        ("BOME_NAVAJA_QUERY_DELAY", "0.1", "0.1 s", "0.5 s o más"),
        ("BOME_NAVAJA_GUARD_MAX_ERRORS", "4", "4", "3 o menos"),
        ("BOME_NAVAJA_GUARD_WINDOW_MINUTES", "9.5", "9.5 min", "10 min o más"),
        ("BOME_NAVAJA_GUARD_COOLDOWN_MINUTES", "74", "74 min", "75 min o más"),
        ("BOME_NAVAJA_GUARD_COOLDOWN_MINUTES", "60", "60 min", "75 min o más"),
        ("BOME_NAVAJA_ERROR_PAUSE_SECONDS", "29", "29 s", "30 s o más"),
        ("BOME_NAVAJA_TIMEOUT", "10", "10 s", "30 s o más"),
    ],
)
def test_a_riskier_value_is_used_with_a_risk_warning(
    variable: str, risky: str, shown: str, recommended: str
) -> None:
    ajustes, message = one({variable: risky})
    assert getattr(ajustes, FIELD[variable]) == float(risky)
    assert ajustes.riesgos == (message,) and ajustes.avisos == ()
    assert message.startswith(f"{variable}={shown}: más arriesgado que lo recomendado ({recommended}); ")
    assert "mucho más" not in message


@pytest.mark.parametrize("raw", ["5", "5.0", "12"])
def test_a_guard_that_would_not_stop_before_the_ban_is_much_riskier(raw: str) -> None:
    ajustes, message = one({"BOME_NAVAJA_GUARD_MAX_ERRORS": raw})
    assert ajustes.guardia_max_errores == int(float(raw))
    assert message.startswith(f"BOME_NAVAJA_GUARD_MAX_ERRORS={int(float(raw))}: mucho más arriesgado que lo recomendado")
    assert "antes del bloqueo" in message and "3 o menos" in message


@pytest.mark.parametrize("raw", ["59.9", "30", "0"])
def test_a_cooldown_shorter_than_the_ban_is_much_riskier(raw: str) -> None:
    ajustes, message = one({"BOME_NAVAJA_GUARD_COOLDOWN_MINUTES": raw})
    assert ajustes.guardia_enfriamiento_minutos == float(raw)
    assert message.startswith(f"BOME_NAVAJA_GUARD_COOLDOWN_MINUTES={float(raw):g} min: mucho más arriesgado")
    assert "1 h" in message and "75 min o más" in message


def test_the_timeout_warning_explains_the_false_block() -> None:
    _, message = one({"BOME_NAVAJA_TIMEOUT": "5"})
    assert "bloqueo" in message and "sin respuesta" in message


def test_warnings_and_risks_are_kept_apart() -> None:
    ajustes = ajustes_desde_entorno({"BOME_NAVAJA_SYNC_DELAY": "-1", "BOME_NAVAJA_QUERY_DELAY": "0"})
    assert len(ajustes.avisos) == 1 and "BOME_NAVAJA_SYNC_DELAY" in ajustes.avisos[0]
    assert len(ajustes.riesgos) == 1 and "BOME_NAVAJA_QUERY_DELAY" in ajustes.riesgos[0]
    assert ajustes.mensajes == ajustes.avisos + ajustes.riesgos


# --------------------------------------------------------------------------- report


def test_to_dict_reports_values_variables_and_messages() -> None:
    ajustes = ajustes_desde_entorno({"BOME_NAVAJA_ERROR_PAUSE_SECONDS": "10", "BOME_NAVAJA_TIMEOUT": "no"})
    data = ajustes.to_dict()
    json.dumps(data)
    assert {name: data[name] for name in DEFAULTS} == {**DEFAULTS, "pausa_tras_error_segundos": 10.0}
    assert data["pausa_tras_error_max_segundos"] == 20.0
    assert data["variables"] == {field: variable for variable, field in FIELD.items()}
    assert data["avisos"] == list(ajustes.avisos) and len(data["avisos"]) == 1
    assert data["riesgos"] == list(ajustes.riesgos) and len(data["riesgos"]) == 1
