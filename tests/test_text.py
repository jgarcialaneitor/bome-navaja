"""Tests for text normalisation and the local boolean matcher."""

from __future__ import annotations

import pytest

from bome_navaja.text import Term, contains, matches, normalize, parse_terms, phrase_starts


# --------------------------------------------------------------------------- normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Destitución", "destitucion"),
        ("CONSEJERÍA DE PRESIDENCIA", "consejeria de presidencia"),
        ("  personal\n\teventual  ", "personal eventual"),
        ("Ceses\u00a0y  nombramientos", "ceses y nombramientos"),
        ("pingüino", "pinguino"),
        ("Straße", "strasse"),
        ("", ""),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_folds_enye_like_the_site() -> None:
    # Verified live 2026-09-23: "año" and "ano" return the same 835 BOMEs,
    # and "compañia" / "compania" the same 14, so the site folds ñ into n.
    assert normalize("Año") == normalize("ano") == "ano"
    assert normalize("COMPAÑÍA") == "compania"


def test_normalize_is_idempotent() -> None:
    text = "  Relación PROVISIONAL de  aspirantes "
    assert normalize(normalize(text)) == normalize(text)


# --------------------------------------------------------------------------- matches

SUMARIO = (
    "Orden nº 1234, de fecha 3 de marzo de 2025, relativa a nombramiento y cese "
    "de personal eventual de la Consejería de Presidencia."
)


def test_plain_string_query_is_a_literal_phrase() -> None:
    assert matches(SUMARIO, "PERSONAL EVENTUAL")
    assert matches(SUMARIO, "consejeria de presidencia")
    # Word order matters and there is no stemming or synonyms, as on the site.
    assert not matches(SUMARIO, "eventual personal")
    assert not matches(SUMARIO, "destitución")
    # Substring semantics: "cese" also matches inside longer words.
    assert matches("ceses de personal", "cese")


def test_and_terms() -> None:
    query = [Term("personal eventual"), Term("cese")]
    assert matches(SUMARIO, query)
    assert not matches(SUMARIO, [Term("personal eventual"), Term("hacienda")])


def test_or_terms() -> None:
    query = [Term("hacienda"), Term("personal eventual", operator="o")]
    assert matches(SUMARIO, query)
    assert not matches(SUMARIO, [Term("hacienda"), Term("cultura", operator="o")])


def test_not_contains() -> None:
    assert matches(SUMARIO, [Term("personal eventual"), Term("hacienda", mode="no_contiene")])
    assert not matches(SUMARIO, [Term("personal eventual"), Term("cese", mode="no_contiene")])


def test_and_binds_tighter_than_or() -> None:
    # (hacienda AND cese) OR (personal eventual AND nombramiento)
    query = [
        Term("hacienda"),
        Term("cese"),
        Term("personal eventual", operator="o"),
        Term("nombramiento"),
    ]
    assert matches(SUMARIO, query)
    # (hacienda AND cese) OR (cultura AND cese) → false
    query = [Term("hacienda"), Term("cese"), Term("cultura", operator="o"), Term("cese")]
    assert not matches(SUMARIO, query)


def test_first_term_operator_is_ignored() -> None:
    assert matches(SUMARIO, [Term("cese", operator="o")])


def test_empty_query_matches_everything_but_missing_text_matches_nothing() -> None:
    assert matches(SUMARIO, [])
    assert matches(None, [])
    assert not matches(None, "cese")
    # Unknown text never satisfies a query, not even a pure negation.
    assert not matches(None, [Term("cese", mode="no_contiene")])
    assert not matches("", "cese")


def test_term_validation() -> None:
    with pytest.raises(ValueError):
        Term("   ")
    with pytest.raises(ValueError):
        Term("x", operator="and")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Term("x", mode="like")  # type: ignore[arg-type]


def test_parse_terms_from_dicts() -> None:
    terms = parse_terms(
        [
            {"texto": "personal eventual"},
            {"texto": "cese", "operador": "y", "modo": "contiene"},
            {"texto": "hacienda", "operador": "o", "modo": "no_contiene"},
        ]
    )
    assert terms == (
        Term("personal eventual"),
        Term("cese"),
        Term("hacienda", operator="o", mode="no_contiene"),
    )
    assert terms[2].to_dict() == {"texto": "hacienda", "operador": "o", "modo": "no_contiene"}


def test_parse_terms_rejects_bad_items() -> None:
    with pytest.raises(ValueError):
        parse_terms([{"text": "typo in key"}])
    with pytest.raises(ValueError):
        parse_terms([{"texto": "x", "extra": 1}])
    with pytest.raises(ValueError):
        parse_terms(["not a dict"])  # type: ignore[list-item]


# --------------------------------------------------------------------------- word-start mode (task 5b)


def test_phrase_starts_substring_and_word_start() -> None:
    folded = normalize("Cese, ceses y procese; año y humanos")
    assert folded == "cese, ceses y procese; ano y humanos"
    inside = folded.index("procese") + 3
    year = folded.index("ano ")
    assert phrase_starts(folded, "cese") == [0, 6, inside]
    assert phrase_starts(folded, "cese", palabra=True) == [0, 6]
    assert phrase_starts(folded, "ano") == [year, folded.index("anos")]
    assert phrase_starts(folded, "ano", palabra=True) == [year]
    assert phrase_starts(folded, "") == []


def test_word_boundary_is_any_non_alphanumeric_char() -> None:
    assert contains("Ley 7/1985, de 2 de abril", "1985", palabra=True)
    assert not contains("Ley 71985", "1985", palabra=True)
    assert contains("(cese) del cargo", "cese", palabra=True)
    assert contains("nº 124", "124", palabra=True)
    assert contains("personal_eventual", "eventual", palabra=True)


def test_contains_normalizes_both_sides() -> None:
    assert contains("AÑO NUEVO", "ano nuevo")
    assert contains("Destitución", "DESTITUCION", palabra=True)
    assert not contains(None, "x")
    assert not contains("texto", "   ")


def test_matches_word_start_mode() -> None:
    sumario = "Procese el expediente y comunique los ceses del personal"
    assert matches(sumario, "cese")
    assert matches(sumario, "cese", palabra=True)  # "ceses" starts a word
    assert not matches("Procese el expediente", "cese", palabra=True)
    query = [Term("expediente"), Term("cese", mode="no_contiene")]
    assert not matches(sumario, query)
    assert matches("Procese el expediente", query, palabra=True)
    # The default stays the site's substring semantics.
    assert matches("Procese el expediente", "cese")


# --------------------------------------------------------------------------- control characters (task 5b review)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ab\x00cde", "abcde"),  # NUL: FTS5 trigram ignores it, so normalize must too
        ("provi\u00adsional", "provisional"),  # soft hyphen (Cf)
        ("per\u200bsonal", "personal"),  # zero-width space (Cf)
        ("a\x07b\x1fc\x7fd", "abcd"),  # other C0/C1 controls
        ("a\tb\nc\r\x0bd", "a b c d"),  # whitespace controls still become one space
    ],
)
def test_normalize_drops_control_and_format_characters(raw: str, expected: str) -> None:
    assert normalize(raw) == expected
    assert contains(raw, expected)
