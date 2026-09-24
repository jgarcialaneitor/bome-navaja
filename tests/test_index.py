"""Local SQLite FTS5 index of article sumarios."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date
from pathlib import Path

import pytest

from bome_navaja import index as index_module
from bome_navaja.antiguo import BoletinAntiguo, FichaAntigua, parse_ficha, url_ficha
from bome_navaja.index import SCHEMA_VERSION, SumarioIndex
from bome_navaja.models import (
    BomeBlockedError,
    BomeHTTPError,
    BomeIndexUnavailableError,
    BomeNotFoundError,
    BomeIndexVersionError,
    BulletinRef,
)
from bome_navaja.search import ArticuloEncontrado, BusquedaInvalidaError
from bome_navaja.text import matches, normalize

BASE = "https://bomemelilla.es"


def ref(cve: str, day: date) -> BulletinRef:
    number = int(cve.rsplit("-", 1)[1])
    return BulletinRef(
        cve=cve,
        number=number,
        date=day,
        extraordinary="-BX-" in cve,
        url=f"{BASE}/bome/{cve}",
    )


def art(
    bulletin: BulletinRef,
    number: int,
    sumario: str | None,
    consejeria: str = "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD",
    listed: bool = True,
) -> ArticuloEncontrado:
    kind = "AX" if bulletin.extraordinary else "A"
    year = bulletin.cve.split("-")[2]
    return ArticuloEncontrado(
        bome_cve=bulletin.cve,
        bome_numero=bulletin.number,
        bome_fecha=bulletin.date,
        bome_extraordinario=bulletin.extraordinary,
        cve=f"BOME-{kind}-{year}-{number}",
        numero=number,
        sumario=sumario,
        departamento="CIUDAD AUTÓNOMA DE MELILLA",
        consejeria=consejeria,
        organismo="PRESIDENCIA",
        url=f"{bulletin.url}/articulo/{number}",
        pdf_url=f"{BASE}/bome/descargar/BOME-{kind}-{year}-{number}.pdf",
        listado_en_bome=listed,
    )


B1 = ref("BOME-B-2025-6294", date(2025, 7, 22))
B2 = ref("BOME-B-2026-6375", date(2026, 5, 1))
BX = ref("BOME-BX-2026-41", date(2026, 9, 18))
OLD = ref("BOME-B-2014-5092", date(2014, 1, 3))


@pytest.fixture
def idx(tmp_path: Path) -> SumarioIndex:
    index = SumarioIndex(tmp_path / "sub" / "sumarios.sqlite3")
    yield index
    index.close()


@pytest.fixture
def filled(idx: SumarioIndex) -> SumarioIndex:
    idx.guardar_boletin(
        B1,
        [
            art(B1, 744, "Extracto de los Acuerdos adoptados por el Consejo de Gobierno."),
            art(B1, 745, "Decreto nº 124 relativo al cese de D. Alejandro Silva cómo Personal Eventual de Confianza.",
                consejeria="PRESIDENCIA", listed=False),
            art(B1, 746, "Orden relativa a la compañía de seguros y al Año Nuevo."),
        ],
        "indexado",
    )
    idx.guardar_boletin(
        B2,
        [
            art(B2, 300, "Nombramiento de D. Juan como personal eventual."),
            art(B2, 301, "Ceses y nombramientos de personal eventual de la Consejería de Hacienda.",
                consejeria="CONSEJERÍA DE HACIENDA"),
            art(B2, 302, "Destitución no existe: la norma dice cese del personal."),
        ],
        "indexado",
    )
    idx.guardar_boletin(
        BX,
        [art(BX, 102, "Orden nº 4266 relativa a nombramiento como alumnos en prácticas.")],
        "indexado",
    )
    idx.guardar_boletin(OLD, [art(OLD, 1, None), art(OLD, 2, None)], "sin_sumarios")
    return idx


def cves(result) -> list[str]:
    return [a.cve for a in result.articulos]


# --------------------------------------------------------------------------- schema


def test_creates_parent_dir_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "sumarios.sqlite3"
    index = SumarioIndex(path)
    try:
        assert path.is_file()
        assert index.version_esquema() == SCHEMA_VERSION
        journal = sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0]
        assert journal == "wal"
    finally:
        index.close()
    # Re-opening an existing index works.
    SumarioIndex(path).close()


def test_sqlite_has_fts5(tmp_path: Path) -> None:
    # Plain check that the running CPython build ships FTS5 (Windows CI too).
    SumarioIndex(tmp_path / "x.sqlite3").close()


def test_missing_fts5_raises_dedicated_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index_module, "_fts5_available", lambda conn: False)
    with pytest.raises(BomeIndexUnavailableError) as info:
        SumarioIndex(tmp_path / "x.sqlite3")
    assert info.value.error_code == "indice_no_disponible"


@pytest.mark.parametrize("stored", [str(SCHEMA_VERSION + 1), "banana"])
def test_newer_or_unknown_schema_version_is_rejected(tmp_path: Path, stored: str) -> None:
    path = tmp_path / "x.sqlite3"
    SumarioIndex(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (stored,))
    with pytest.raises(BomeIndexVersionError) as info:
        SumarioIndex(path)
    assert info.value.error_code == "indice_version_incompatible"
    assert isinstance(info.value, BomeIndexUnavailableError)


# --------------------------------------------------------------------------- storage


def test_guardar_boletin_is_idempotent(filled: SumarioIndex) -> None:
    before = filled.estado().to_dict()
    filled.guardar_boletin(B2, [art(B2, 300, "Nombramiento de D. Juan como personal eventual.")], "indexado")
    filled.guardar_boletin(B2, [art(B2, 300, "Nombramiento de D. Juan como personal eventual.")], "indexado")
    after = filled.estado()
    assert after.articulos == before["articulos"] - 2
    # Replaced articles disappear from the full-text index too.
    assert filled.buscar("hacienda").total == 0
    assert cves(filled.buscar("nombramiento")) == ["BOME-AX-2026-102", "BOME-A-2026-300"]


def test_error_keeps_previous_articles(filled: SumarioIndex) -> None:
    filled.guardar_boletin(B2, [], "error", error=RuntimeError("HTTP 500"))
    assert filled.buscar("hacienda").total == 1
    assert filled.estado_boletin("BOME-B-2026-6375") == "indexado"
    filled.guardar_boletin(ref("BOME-B-2026-6400", date(2026, 8, 1)), [], "error", error="boom")
    assert filled.estado_boletin("BOME-B-2026-6400") == "error"


def test_rejects_unknown_estado(idx: SumarioIndex) -> None:
    with pytest.raises(ValueError):
        idx.guardar_boletin(B1, [], "hecho")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- matching


CORPUS = [
    "Decreto nº 124 relativo al CESE de D. Alejandro Silva como Personal Eventual.",
    "Orden relativa a la compañía de seguros y al Año Nuevo.",
    "Destitución del director; ceses varios y nombramientos.",
    "Procese el expediente de los derechos humanos, según la Ley 7/1985, de 2 de abril.",
    "Pingüino Straße ﬁnanzas: 24.000.000,00€ (préstamo)",
    "Relación provisional de aspirantes admitidos y excluidos a la convocatoria.",
    "Nombramiento de personal eventual de confianza en la Consejería de Hacienda.",
    "BASES de la convocatoria nº 3/2026 para la provisión de plazas — anuncio",
    "Extracto de acuerdos; año 2025/2026; señor Muñoz.",
    "de",
]
PHRASES = [
    "cese", "ceses", "personal eventual", "Nº 124", "no 1", "compania", "COMPAÑÍA", "ano nuevo",
    "año", "ano", "destitucion", "pinguino", "strasse", "finanzas", "eventual personal", "7/1985",
    "7/", "/2026", "de", "d", "ión", "prov", "provisional de", "24.000", "€", "(", ";", "rel",
    "humanos", "manos", "muñoz", "unoz", "—", "de la convocatoria", "e", "xx", "nombra",
]


@pytest.fixture
def corpus(idx: SumarioIndex) -> SumarioIndex:
    bulletin = ref("BOME-B-2026-6000", date(2026, 1, 2))
    idx.guardar_boletin(bulletin, [art(bulletin, i + 1, s) for i, s in enumerate(CORPUS)], "indexado")
    return idx


@pytest.mark.parametrize("coincidencia", ["fragmento", "palabra"])
def test_single_phrases_match_text_matches(corpus: SumarioIndex, coincidencia: str) -> None:
    palabra = coincidencia == "palabra"
    for phrase in PHRASES:
        result = corpus.buscar(phrase, coincidencia=coincidencia, limite=200)
        found = {a.numero for a in result.articulos}
        expected = {i + 1 for i, s in enumerate(CORPUS) if matches(s, phrase, palabra=palabra)}
        assert found == expected, (coincidencia, phrase)
        assert result.total == len(expected)


@pytest.mark.parametrize("coincidencia", ["fragmento", "palabra"])
def test_random_boolean_queries_match_text_matches(corpus: SumarioIndex, coincidencia: str) -> None:
    import random

    from bome_navaja.text import Term

    palabra = coincidencia == "palabra"
    rng = random.Random(5)
    for _ in range(150):
        terms = [
            {
                "texto": rng.choice(PHRASES),
                "operador": rng.choice(["y", "y", "o"]),
                "modo": rng.choice(["contiene", "contiene", "no_contiene"]),
            }
            for _ in range(rng.randint(1, 4))
        ]
        query = [Term(t["texto"], operator=t["operador"], mode=t["modo"]) for t in terms]
        expected = {i + 1 for i, s in enumerate(CORPUS) if matches(s, query, palabra=palabra)}
        result = corpus.buscar(None, terms, coincidencia=coincidencia, limite=200)
        assert {a.numero for a in result.articulos} == expected, (coincidencia, terms)
        assert result.total == len(expected)


def test_fragmento_is_the_sites_substring_match(filled: SumarioIndex) -> None:
    assert set(cves(filled.buscar("cese"))) == {"BOME-A-2025-745", "BOME-A-2026-301", "BOME-A-2026-302"}
    assert filled.buscar("eventual personal").total == 0  # word order matters
    assert set(cves(filled.buscar("nombra"))) == {
        "BOME-A-2026-300", "BOME-A-2026-301", "BOME-AX-2026-102",
    }


def test_palabra_requires_a_word_start(idx: SumarioIndex) -> None:
    bulletin = ref("BOME-B-2026-6002", date(2026, 1, 4))
    idx.guardar_boletin(
        bulletin,
        [
            art(bulletin, 1, "Cese del director."),
            art(bulletin, 2, "Ceses varios."),
            art(bulletin, 3, "Procese el expediente."),
            art(bulletin, 4, "Año nuevo."),
            art(bulletin, 5, "Derechos humanos."),
        ],
        "indexado",
    )
    numbers = lambda result: [a.numero for a in result.articulos]  # noqa: E731
    assert numbers(idx.buscar("cese")) == [1, 2, 3]
    assert numbers(idx.buscar("cese", coincidencia="palabra")) == [1, 2]
    assert numbers(idx.buscar("ano")) == [4, 5]
    assert numbers(idx.buscar("ano", coincidencia="palabra")) == [4]
    # A trailing * is accepted in both modes and changes nothing.
    assert numbers(idx.buscar("cese*")) == [1, 2, 3]
    assert numbers(idx.buscar("cese*", coincidencia="palabra")) == [1, 2]
    with pytest.raises(BusquedaInvalidaError):
        idx.buscar("cese", coincidencia="exacta")  # type: ignore[arg-type]


def test_short_terms_fall_back_to_a_scan(corpus: SumarioIndex) -> None:
    # "de" and "d" are under the 3-character trigram minimum.
    assert corpus.buscar("de").total == sum(1 for s in CORPUS if matches(s, "de"))
    assert corpus.buscar("de", coincidencia="palabra").total == sum(
        1 for s in CORPUS if matches(s, "de", palabra=True)
    )
    mixed = corpus.buscar("7/", [{"texto": "humanos"}])
    assert [a.numero for a in mixed.articulos] == [4]
    either = corpus.buscar("€", [{"texto": "muñoz", "operador": "o"}])
    assert sorted(a.numero for a in either.articulos) == [5, 9]
    negated = corpus.buscar("provision", [{"texto": "de", "modo": "no_contiene"}])
    assert negated.total == 0


def test_accents_case_and_enye(filled: SumarioIndex) -> None:
    assert cves(filled.buscar("COMPANIA")) == ["BOME-A-2025-746"]
    assert cves(filled.buscar("año nuevo")) == cves(filled.buscar("ANO NUEVO")) == ["BOME-A-2025-746"]
    assert cves(filled.buscar("destitucion")) == ["BOME-A-2026-302"]
    assert cves(filled.buscar("nº 124")) == ["BOME-A-2025-745"]


def test_and_or_not(filled: SumarioIndex) -> None:
    both = filled.buscar("personal eventual", [{"texto": "cese"}])
    assert set(cves(both)) == {"BOME-A-2025-745", "BOME-A-2026-301"}
    either = filled.buscar(
        "personal eventual",
        [{"texto": "cese"}, {"texto": "alumnos", "operador": "o"}],
    )
    assert set(cves(either)) == {"BOME-A-2025-745", "BOME-A-2026-301", "BOME-AX-2026-102"}
    negated = filled.buscar("personal eventual", [{"texto": "hacienda", "modo": "no_contiene"}])
    assert set(cves(negated)) == {"BOME-A-2025-745", "BOME-A-2026-300"}


def test_negation_only_groups_scan_articles_with_a_sumario(filled: SumarioIndex) -> None:
    result = filled.buscar(None, [{"texto": "cese", "modo": "no_contiene"}])
    # Articles without sumario (the 2014 stubs) never match: unknown text is not evidence.
    assert set(cves(result)) == {
        "BOME-A-2025-744", "BOME-A-2025-746", "BOME-A-2026-300", "BOME-AX-2026-102",
    }
    either = filled.buscar(
        "alumnos", [{"texto": "personal", "operador": "o", "modo": "no_contiene"}]
    )
    assert set(cves(either)) == {"BOME-A-2025-744", "BOME-A-2025-746", "BOME-AX-2026-102"}


NASTY = ['"', "*", "NEAR(", "AND", "OR", "NOT", "-", ":", "(", ")", "a OR b", "sumario:cese",
         '"cese', 'cese"', "cese*)", "^cese", "{cese personal}", "o'brien", "NEAR(cese personal, 2)",
         "cese AND NOT personal", "**", "cese **", "+cese", "\\", "%", "_", '""', "' OR 1=1 --",
         "a", "\x00", "*cese", "ce\"se"]


@pytest.mark.parametrize("coincidencia", ["fragmento", "palabra"])
@pytest.mark.parametrize("text", NASTY)
def test_injection_inputs_never_reach_sqlite_errors(
    filled: SumarioIndex, text: str, coincidencia: str
) -> None:
    for call in (
        lambda: filled.buscar(text, coincidencia=coincidencia),
        lambda: filled.buscar("personal", [{"texto": text}], coincidencia=coincidencia),
        lambda: filled.buscar("personal", [{"texto": text, "operador": "o"}], coincidencia=coincidencia),
        lambda: filled.buscar(
            "personal", [{"texto": text, "modo": "no_contiene"}], coincidencia=coincidencia
        ),
        lambda: filled.buscar(None, [{"texto": text, "modo": "no_contiene"}], coincidencia=coincidencia),
        lambda: filled.buscar("personal", consejeria=text, coincidencia=coincidencia),
    ):
        try:
            call()
        except BusquedaInvalidaError:
            pass


def test_keywords_and_quotes_are_searched_literally(filled: SumarioIndex) -> None:
    bulletin = ref("BOME-B-2026-6001", date(2026, 1, 3))
    filled.guardar_boletin(
        bulletin, [art(bulletin, 9, 'Convenio NEAR AND OR NOT de "prueba" (anexo)')], "indexado"
    )
    assert cves(filled.buscar("near and or not")) == ["BOME-A-2026-9"]
    assert cves(filled.buscar('de "prueba" (')) == ["BOME-A-2026-9"]
    assert cves(filled.buscar('"')) == ["BOME-A-2026-9"]



# --------------------------------------------------------------------------- filters, order, pages


def test_date_and_extraordinary_filters(filled: SumarioIndex) -> None:
    assert set(cves(filled.buscar("nombramiento*", desde="2026-01-01"))) == {
        "BOME-A-2026-300", "BOME-A-2026-301", "BOME-AX-2026-102",
    }
    assert cves(filled.buscar("nombramiento*", hasta=date(2026, 5, 1), desde="2026-05-01")) == [
        "BOME-A-2026-300", "BOME-A-2026-301",
    ]
    assert cves(filled.buscar("nombramiento*", extraordinario=True)) == ["BOME-AX-2026-102"]
    assert "BOME-AX-2026-102" not in cves(filled.buscar("nombramiento*", extraordinario=False))
    with pytest.raises(BusquedaInvalidaError):
        filled.buscar("cese", desde="22/09/2026")


def test_consejeria_is_a_normalized_substring(filled: SumarioIndex) -> None:
    assert cves(filled.buscar("personal eventual", consejeria="hacienda")) == ["BOME-A-2026-301"]
    assert cves(filled.buscar("personal eventual", consejeria="Consejería de Presidencia")) == [
        "BOME-A-2026-300"
    ]
    assert filled.buscar("personal", consejeria="100%_x").total == 0


def test_no_terms_lists_by_date(filled: SumarioIndex) -> None:
    result = filled.buscar(limite=3)
    assert cves(result) == ["BOME-AX-2026-102", "BOME-A-2026-300", "BOME-A-2026-301"]
    assert result.total == 9


def test_order_by_date_then_number(filled: SumarioIndex) -> None:
    result = filled.buscar("personal", orden="fecha")
    assert cves(result) == ["BOME-A-2026-300", "BOME-A-2026-301", "BOME-A-2026-302", "BOME-A-2025-745"]


def test_order_by_relevance(idx: SumarioIndex) -> None:
    old = ref("BOME-B-2025-6000", date(2025, 1, 1))
    new = ref("BOME-B-2026-6100", date(2026, 1, 1))
    idx.guardar_boletin(old, [art(old, 1, "Cese cese cese.")], "indexado")
    idx.guardar_boletin(
        new,
        [art(new, 2, "Orden larga con muchas palabras sobre asuntos varios y un único cese al final "
             "de un texto bastante extenso que diluye la relevancia del término buscado.")],
        "indexado",
    )
    assert cves(idx.buscar("cese", orden="fecha")) == ["BOME-A-2026-2", "BOME-A-2025-1"]
    assert cves(idx.buscar("cese", orden="relevancia")) == ["BOME-A-2025-1", "BOME-A-2026-2"]
    with pytest.raises(BusquedaInvalidaError):
        idx.buscar("cese", orden="azar")  # type: ignore[arg-type]


def test_pagination(filled: SumarioIndex) -> None:
    first = filled.buscar("personal", limite=2)
    assert (first.total, first.limite, first.desplazamiento, first.siguiente) == (4, 2, 0, 2)
    second = filled.buscar("personal", limite=2, desplazamiento=first.siguiente)
    assert second.siguiente is None
    assert cves(first) + cves(second) == cves(filled.buscar("personal"))
    assert filled.buscar("personal", limite=10_000).limite == 200
    for bad in ({"limite": 0}, {"desplazamiento": -1}, {"limite": "5"}):
        with pytest.raises(BusquedaInvalidaError):
            filled.buscar("personal", **bad)


# --------------------------------------------------------------------------- result shape


def test_result_fields_and_highlight(filled: SumarioIndex) -> None:
    result = filled.buscar("destitucion")
    (found,) = result.articulos
    assert found.bome_cve == "BOME-B-2026-6375"
    assert found.bome_numero == 6375
    assert found.bome_fecha == date(2026, 5, 1)
    assert found.bome_extraordinario is False
    assert found.sumario == "Destitución no existe: la norma dice cese del personal."
    assert found.departamento == "CIUDAD AUTÓNOMA DE MELILLA"
    assert found.url.endswith("/articulo/302")
    assert found.pdf_url.endswith("BOME-A-2026-302.pdf")
    # Highlight comes from the ORIGINAL text, accents included.
    assert found.resaltado == "**Destitución** no existe: la norma dice cese del personal."
    hidden = filled.buscar("alejandro").articulos[0]
    assert hidden.listado_en_bome is False
    data = result.to_dict()
    json.dumps(data)
    assert data["articulos"][0]["bome_fecha"] == "2026-05-01"


def test_highlight_marks_every_positive_phrase(filled: SumarioIndex) -> None:
    found = filled.buscar("personal eventual", [{"texto": "cese"}]).articulos
    by_cve = {a.cve: a.resaltado for a in found}
    assert by_cve["BOME-A-2026-301"] == (
        "**Cese**s y nombramientos de **personal eventual** de la Consejería de Hacienda."
    )


def test_highlight_follows_the_mode_on_the_original_text(idx: SumarioIndex) -> None:
    bulletin = ref("BOME-B-2026-6003", date(2026, 1, 5))
    idx.guardar_boletin(bulletin, [art(bulletin, 1, "Año de los derechos humanos.")], "indexado")
    (fragment,) = idx.buscar("ano").articulos
    assert fragment.resaltado == "**Año** de los derechos hum**ano**s."
    (word,) = idx.buscar("ano", coincidencia="palabra").articulos
    assert word.resaltado == "**Año** de los derechos humanos."
    (short,) = idx.buscar("de", coincidencia="palabra").articulos
    assert short.resaltado == "Año **de** los **de**rechos humanos."


def test_highlight_windows_long_sumarios(idx: SumarioIndex) -> None:
    bulletin = ref("BOME-B-2026-6200", date(2026, 2, 2))
    text = ("palabra " * 60) + "Nombramiento clave " + ("resto " * 60)
    idx.guardar_boletin(bulletin, [art(bulletin, 5, text)], "indexado")
    snippet = idx.buscar("nombramiento clave").articulos[0].resaltado
    assert "**Nombramiento clave**" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) < 260


def test_cobertura_block(filled: SumarioIndex) -> None:
    cobertura = filled.buscar("cese").cobertura
    assert cobertura["boletines_indexados"] == 4
    assert cobertura["fecha_min"] == "2014-01-03"
    assert cobertura["fecha_max"] == "2026-09-18"
    assert cobertura["ultima_sincronizacion"] is None
    assert cobertura["sincronizacion_en_curso"] is False
    import time

    # A stale lease (dead process) does not count as a running sync.
    assert filled.adquirir_lease("dead", now=time.time() - 10_000) is None
    assert filled.buscar("cese").cobertura["sincronizacion_en_curso"] is False
    assert filled.adquirir_lease("alive", now=time.time()) is None
    assert filled.buscar("cese").cobertura["sincronizacion_en_curso"] is True
    filled.guardar_resumen_sincronizacion({"estado": "completado", "finalizado": "2026-09-23T10:00:00Z"})
    assert filled.buscar("cese").cobertura["ultima_sincronizacion"] == "2026-09-23T10:00:00Z"


# --------------------------------------------------------------------------- estado


def test_estado(filled: SumarioIndex) -> None:
    filled.registrar_calendario([B1, B2, BX, OLD, ref("BOME-B-2026-6416", date(2026, 9, 22))])
    filled.guardar_boletin(ref("BOME-B-2026-6400", date(2026, 8, 1)), [], "error", error="boom")
    state = filled.estado()
    assert state.boletines == {"indexado": 3, "sin_sumarios": 1, "error": 1, "roto": 0, "total": 5}
    assert state.articulos == 9
    assert state.articulos_con_sumario == 7
    assert (state.fecha_min, state.fecha_max) == (date(2014, 1, 3), date(2026, 9, 18))
    assert state.calendario_conocidos == 5
    assert state.pendientes == 1  # BOME-B-2026-6416 not processed yet
    assert state.version_esquema == SCHEMA_VERSION
    assert state.tamano_bytes > 0
    assert state.ruta.endswith("sumarios.sqlite3")
    assert state.ultima_sincronizacion is None
    json.dumps(state.to_dict())


def test_last_sync_summary_roundtrip(idx: SumarioIndex) -> None:
    idx.guardar_resumen_sincronizacion({"estado": "completado", "hechos": 3})
    assert idx.estado().ultima_sincronizacion == {"estado": "completado", "hechos": 3}


def test_the_last_sync_summary_is_kept_per_origin(idx: SumarioIndex) -> None:
    assert idx.estado().ultimas_sincronizaciones == {"bomemelilla.es": None, "melilla.es": None}
    old = {"estado": "completado", "origen": "melilla.es", "finalizado": "2026-09-24T10:00:00Z"}
    idx.guardar_resumen_sincronizacion(old)
    state = idx.estado()
    assert state.ultima_sincronizacion is None  # the field keeps meaning bomemelilla.es
    assert state.ultimas_sincronizaciones == {"bomemelilla.es": None, "melilla.es": old}
    assert idx.buscar("cese").cobertura["ultima_sincronizacion"] is None
    bome = {"estado": "bloqueado", "origen": "bomemelilla.es", "finalizado": "2026-09-24T11:00:00Z"}
    idx.guardar_resumen_sincronizacion(bome)
    state = idx.estado()
    assert state.ultima_sincronizacion == bome
    assert state.ultimas_sincronizaciones == {"bomemelilla.es": bome, "melilla.es": old}
    with pytest.raises(BusquedaInvalidaError):
        idx.guardar_resumen_sincronizacion({"estado": "completado", "origen": "boe.es"})


def test_the_lease_names_the_origin_of_its_holder(idx: SumarioIndex) -> None:
    assert idx.adquirir_lease("a", now=100.0, stale_after=60, origen="melilla.es") is None
    held = idx.adquirir_lease("b", now=110.0, stale_after=60, origen="bomemelilla.es")
    assert held is not None and (held["propietario"], held["origen"]) == ("a", "melilla.es")
    assert idx.lease(now=120.0, stale_after=60)["origen"] == "melilla.es"
    idx.liberar_lease("a")
    assert idx.adquirir_lease("b", now=130.0, stale_after=60) is None  # no origin given
    assert "origen" not in idx.lease(now=131.0, stale_after=60)


# --------------------------------------------------------------------------- lease


def test_lease_lifecycle(idx: SumarioIndex) -> None:
    assert idx.adquirir_lease("a", now=100.0, stale_after=60) is None
    held = idx.adquirir_lease("b", now=120.0, stale_after=60)
    assert held is not None and held["propietario"] == "a"
    assert idx.renovar_lease("a", now=150.0) is True
    assert idx.adquirir_lease("b", now=200.0, stale_after=60) is not None  # heartbeat 150 still fresh
    assert idx.adquirir_lease("b", now=211.0, stale_after=60) is None  # stale: taken over
    assert idx.renovar_lease("a", now=212.0) is False
    idx.liberar_lease("a")  # not the owner: no effect
    assert idx.lease(now=213.0, stale_after=60)["propietario"] == "b"
    idx.liberar_lease("b")
    assert idx.lease(now=214.0, stale_after=60) is None


def test_index_usable_from_another_thread(filled: SumarioIndex) -> None:
    import threading

    box: list[int] = []
    worker = threading.Thread(target=lambda: box.append(filled.buscar("cese").total))
    worker.start()
    worker.join(timeout=10)
    assert box == [3]


def test_thread_connection_can_be_closed(filled: SumarioIndex) -> None:
    import threading

    counts: list[int] = []

    def work() -> None:
        filled.buscar("cese")
        counts.append(filled.conexiones_abiertas())
        filled.cerrar_conexion_hilo()
        counts.append(filled.conexiones_abiertas())

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(timeout=10)
    assert counts[1] == counts[0] - 1
    # The main thread's connection still works.
    assert filled.buscar("cese").total == 3


# --------------------------------------------------------------------------- verify findings (task 5 review)


def test_duplicate_article_cves_are_deduped_last_wins(idx: SumarioIndex) -> None:
    idx.guardar_boletin(
        B2,
        [art(B2, 300, "Primera versión del cese."), art(B2, 300, "Segunda versión del nombramiento.")],
        "indexado",
    )
    assert idx.estado().articulos == 1
    assert idx.buscar("cese").total == 0
    assert cves(idx.buscar("nombramiento")) == ["BOME-A-2026-300"]


def test_sqlite_errors_inside_a_transaction_become_storage_errors(
    idx: SumarioIndex, tmp_path: Path
) -> None:
    from bome_navaja.models import BomeStorageError

    with sqlite3.connect(idx.path) as raw:
        raw.execute("DROP TABLE articles_fts")
    with pytest.raises(BomeStorageError) as info:
        idx.guardar_boletin(B2, [art(B2, 300, "Cese.")], "indexado")
    assert info.value.error_code == "error_almacenamiento"
    # The failed transaction was rolled back: nothing half-written, index still writable.
    assert idx.estado_boletin("BOME-B-2026-6375") is None
    idx.guardar_boletin(B2, [], "error", error="x")
    assert idx.estado_boletin("BOME-B-2026-6375") == "error"


def test_failed_rollback_does_not_mask_the_original_error(
    idx: SumarioIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bome_navaja.models import BomeStorageError

    with pytest.raises(BomeStorageError, match="boom-inside"):
        with idx._tx() as conn:
            conn.execute("ROLLBACK")  # the transaction is already gone
            raise sqlite3.OperationalError("boom-inside")


@pytest.mark.parametrize(
    "breaker",
    [
        "DROP TABLE calendar",
        "DROP TABLE bulletins",
        "DROP TABLE sync_lease",
        "UPDATE meta SET value = '{broken json' WHERE key = 'last_sync'",
    ],
)
def test_read_paths_wrap_sqlite_and_json_errors(filled: SumarioIndex, breaker: str) -> None:
    from bome_navaja.models import BomeError

    filled.guardar_resumen_sincronizacion({"estado": "completado"})
    with sqlite3.connect(filled.path) as raw:
        raw.execute(breaker)
    failures = 0
    for call in (
        filled.estado,
        lambda: filled.buscar("cese"),
        filled.estados_boletines,
        lambda: filled.estado_boletin("BOME-B-2026-6375"),
        filled.lease,
    ):
        try:
            call()
        except BomeError:
            failures += 1
        # Anything else (sqlite3.Error, ValueError) fails the test by propagating.
    assert failures >= 1


def test_new_thread_connection_failure_is_wrapped(
    filled: SumarioIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from bome_navaja.models import BomeError

    def refuse(path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(index_module, "_connect", refuse)
    outcome: list[BaseException] = []

    def work() -> None:
        try:
            filled.buscar("cese")
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(timeout=10)
    assert len(outcome) == 1 and isinstance(outcome[0], BomeError)


def test_desplazamiento_is_bounded(filled: SumarioIndex) -> None:
    assert filled.buscar("cese", desplazamiento=1_000_000).articulos == ()
    with pytest.raises(BusquedaInvalidaError):
        filled.buscar("cese", desplazamiento=1_000_001)


def test_concurrent_first_open_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "race.sqlite3"
    first = SumarioIndex(path)
    # The second opener read "no schema yet" just before the first one created it.
    monkeypatch.setattr(SumarioIndex, "_schema_exists", lambda self, conn: False)
    second = SumarioIndex(path)
    try:
        assert second.version_esquema() == SCHEMA_VERSION
        second.guardar_boletin(B1, [art(B1, 744, "Cese.")], "indexado")
        assert first.buscar("cese").total == 1
    finally:
        first.close()
        second.close()


def test_racing_opener_still_checks_the_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "race.sqlite3"
    SumarioIndex(path).close()
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
    monkeypatch.setattr(SumarioIndex, "_schema_exists", lambda self, conn: False)
    with pytest.raises(BomeIndexVersionError):
        SumarioIndex(path)


def test_datetimes_count_as_their_date(filled: SumarioIndex) -> None:
    from datetime import datetime

    day = ["BOME-A-2026-300", "BOME-A-2026-301"]
    assert cves(filled.buscar("nombramiento*", desde=datetime(2026, 5, 1, 18, 30), hasta=date(2026, 5, 1))) == day
    assert cves(filled.buscar("nombramiento*", desde=date(2026, 5, 1), hasta=datetime(2026, 5, 1, 0, 0))) == day
    assert cves(filled.buscar("nombramiento*", desde="2026-05-01", hasta="2026-05-01")) == day
    assert filled.buscar("nombramiento*", desde=datetime(2026, 5, 2, 0, 0)).total == 1  # only BX


# --------------------------------------------------------------------------- task 5b: schema v2 (trigram)

V1_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE bulletins (
    cve TEXT PRIMARY KEY, number INTEGER NOT NULL, date TEXT, extraordinary INTEGER NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN ('indexado', 'sin_sumarios', 'error')),
    error_code TEXT, error_message TEXT, n_articulos INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL
);
CREATE INDEX bulletins_date ON bulletins (date);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY, cve TEXT NOT NULL UNIQUE, bulletin_cve TEXT NOT NULL,
    number INTEGER NOT NULL, sumario TEXT, departamento TEXT NOT NULL, consejeria TEXT NOT NULL,
    organismo TEXT NOT NULL, consejeria_norm TEXT NOT NULL, url TEXT NOT NULL, pdf_url TEXT,
    listado_en_bome INTEGER NOT NULL
);
CREATE INDEX articles_bulletin ON articles (bulletin_cve);
CREATE VIRTUAL TABLE articles_fts USING fts5 (texto, tokenize = 'unicode61 remove_diacritics 2');
CREATE TABLE calendar (
    cve TEXT PRIMARY KEY, number INTEGER NOT NULL, date TEXT, extraordinary INTEGER NOT NULL
);
CREATE TABLE sync_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1), owner TEXT NOT NULL, heartbeat REAL NOT NULL,
    started REAL NOT NULL
);
"""


def build_v1(path: Path, sumarios: list[str | None]) -> None:
    """A schema-v1 index file (word tokenizer) as task 5 wrote it."""
    with sqlite3.connect(path) as conn:
        conn.executescript(V1_DDL)
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO meta VALUES ('last_sync', '{\"estado\": \"completado\"}')")
        conn.execute(
            "INSERT INTO bulletins VALUES ('BOME-B-2026-6375', 6375, '2026-05-01', 0, 'indexado', "
            "NULL, NULL, ?, '2026-09-23T10:00:00Z')",
            (len(sumarios),),
        )
        conn.execute("INSERT INTO calendar VALUES ('BOME-B-2026-6375', 6375, '2026-05-01', 0)")
        for number, sumario in enumerate(sumarios, start=1):
            cursor = conn.execute(
                "INSERT INTO articles (cve, bulletin_cve, number, sumario, departamento, consejeria, "
                "organismo, consejeria_norm, url, pdf_url, listado_en_bome) "
                "VALUES (?, 'BOME-B-2026-6375', ?, ?, 'CAM', 'HACIENDA', 'HACIENDA', 'hacienda', "
                "?, NULL, 1)",
                (f"BOME-A-2026-{number}", number, sumario, f"{BASE}/bome/BOME-B-2026-6375/articulo/{number}"),
            )
            if normalize(sumario):
                conn.execute(
                    "INSERT INTO articles_fts (rowid, texto) VALUES (?, ?)",
                    (cursor.lastrowid, normalize(sumario)),
                )


def test_v1_index_is_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    build_v1(path, ["Cese del director.", "Ceses varios.", "Procese el expediente.", None, "  "])
    index = SumarioIndex(path)
    try:
        assert index.version_esquema() == SCHEMA_VERSION == 4
        state = index.estado()
        assert state.articulos == 5
        assert state.boletines["indexado"] == 1
        assert state.ultima_sincronizacion == {"estado": "completado"}
        # Substring semantics now work on the preserved rows.
        assert [a.numero for a in index.buscar("cese").articulos] == [1, 2, 3]
        assert [a.numero for a in index.buscar("cese", coincidencia="palabra").articulos] == [1, 2]
        tokenizer = index._conn().execute(
            "SELECT sql FROM sqlite_master WHERE name = 'articles_fts'"
        ).fetchone()[0]
        assert "trigram" in tokenizer
    finally:
        index.close()
    # A second open finds v2 and changes nothing.
    again = SumarioIndex(path)
    try:
        assert again.buscar("cese").total == 3
    finally:
        again.close()


def test_sqlite_without_trigram_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index_module, "_sqlite_version_info", lambda: (3, 33, 0))
    with pytest.raises(BomeIndexUnavailableError, match="3.34"):
        SumarioIndex(tmp_path / "x.sqlite3")


def test_trigram_index_size_is_sane(tmp_path: Path) -> None:
    import random

    from bome_navaja.parsers import parse_bulletin_page

    fixtures = Path(__file__).parent / "fixtures"
    base = [
        a.sumario
        for name, cve in (("b6416.html", "BOME-B-2026-6416"), ("bx41.html", "BOME-BX-2026-41"))
        for a in parse_bulletin_page((fixtures / name).read_text("utf-8"), cve).articles
    ]
    rng = random.Random(3)
    sumarios = [f"{rng.choice(base)} Expediente {rng.randint(1, 99999)}." for _ in range(600)]

    def size(conn: sqlite3.Connection) -> int:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute(
            "PRAGMA page_size"
        ).fetchone()[0]

    v1_path = tmp_path / "v1.sqlite3"
    build_v1(v1_path, sumarios)
    with sqlite3.connect(v1_path) as raw:
        v1_size = size(raw)
    v2 = SumarioIndex(tmp_path / "v2.sqlite3")
    try:
        bulletin = ref("BOME-B-2026-6375", date(2026, 5, 1))
        v2.guardar_boletin(bulletin, [art(bulletin, i + 1, s) for i, s in enumerate(sumarios)], "indexado")
        v2_size = size(v2._conn())
    finally:
        v2.close()
    assert v2_size < 5 * v1_size, (v1_size, v2_size)


def test_nota_and_consulta_describe_the_mode(filled: SumarioIndex) -> None:
    fragment = filled.buscar("cese")
    assert fragment.consulta["coincidencia"] == "fragmento"
    assert "substring" in fragment.nota and "whole-word" not in fragment.nota
    word = filled.buscar("cese", coincidencia="palabra")
    assert word.consulta["coincidencia"] == "palabra"
    assert "word" in word.nota


def test_relevance_without_trigram_terms_falls_back_to_date(filled: SumarioIndex) -> None:
    assert cves(filled.buscar("de", orden="relevancia")) == cves(filled.buscar("de", orden="fecha"))
    assert cves(filled.buscar(None, orden="relevancia", limite=3)) == cves(filled.buscar(None, limite=3))


# --------------------------------------------------------------------------- task 5b review findings


def _big_index(idx: SumarioIndex, total: int = 5000, common_every: int = 10) -> None:
    """``total`` articles of ~240 chars; all but one in ``common_every`` contain "orden"."""
    import random

    rng = random.Random(11)
    words = ["relativa", "a", "la", "provisión", "de", "plazas", "personal", "consejería",
             "hacienda", "expediente", "convocatoria", "nombramiento", "bases", "anuncio"]
    per_bulletin = 100
    for bulletin_index in range(total // per_bulletin):
        bulletin = ref(f"BOME-B-2025-{6000 + bulletin_index}", date(2025, 1, 1 + bulletin_index % 28))
        articles = []
        for offset in range(per_bulletin):
            number = bulletin_index * per_bulletin + offset + 1
            filler = " ".join(rng.choice(words) for _ in range(30))
            head = "Anuncio" if number % common_every == 0 else f"Orden nº {number}"
            special = " Sello único" if number % 50 == 0 else ""
            articles.append(art(bulletin, number, f"{head}{special} {filler}."[:240]))
        idx.guardar_boletin(bulletin, articles, "indexado")


def test_relevance_scales_linearly(idx: SumarioIndex) -> None:
    _big_index(idx)
    started = time.perf_counter()
    ranked = idx.buscar("orden", orden="relevancia")
    elapsed = time.perf_counter() - started
    assert ranked.total == 4500
    assert elapsed < 2.0, f"relevancia took {elapsed:.2f}s"
    by_date = idx.buscar("orden", orden="fecha")
    assert by_date.total == ranked.total
    # A narrower query: same set of hits whatever the order.
    narrow_ranked = idx.buscar("sello unico", orden="relevancia", limite=200)
    narrow_dated = idx.buscar("sello unico", orden="fecha", limite=200)
    assert narrow_ranked.total == narrow_dated.total == 100
    assert {a.cve for a in narrow_ranked.articulos} == {a.cve for a in narrow_dated.articulos}


def test_relevance_query_plan_has_no_per_row_fts_scan(filled: SumarioIndex) -> None:
    conn = filled._conn()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        filled.buscar("personal", [{"texto": "cese"}], orden="relevancia")
    finally:
        conn.set_trace_callback(None)
    (rows_sql,) = [s for s in statements if s.lstrip().startswith(("SELECT a.cve", "WITH"))]
    plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + rows_sql)]
    assert not any("articles_fts" in step and "LEFT-JOIN" in step for step in plan), plan


def test_relevance_orders_by_bm25_and_keeps_total_exact(idx: SumarioIndex) -> None:
    bulletin = ref("BOME-B-2026-6100", date(2026, 1, 1))
    idx.guardar_boletin(
        bulletin,
        [
            art(bulletin, 1, "Texto largo con muchas palabras que no ayudan y un único cese al final."),
            art(bulletin, 2, "Cese cese cese."),
            art(bulletin, 3, "Nada que ver aquí."),
            art(bulletin, 4, "de"),  # only reachable through the short-term OR branch
        ],
        "indexado",
    )
    result = idx.buscar("cese", [{"texto": "de", "operador": "o"}], orden="relevancia")
    numbers = [a.numero for a in result.articulos]
    assert result.total == 3
    assert numbers[0] == 2
    assert set(numbers) == {1, 2, 4}
    assert numbers[-1] == 4  # no bm25 score: ranked last


def test_nul_in_a_stored_sumario_behaves_like_text_matches(idx: SumarioIndex) -> None:
    bulletin = ref("BOME-B-2026-6101", date(2026, 1, 2))
    sumarios = ["ab\x00cde xyz", "provi\u00adsional", "normal"]
    idx.guardar_boletin(bulletin, [art(bulletin, i + 1, s) for i, s in enumerate(sumarios)], "indexado")
    for phrase in ("abc", "b\x00c", "provisional", "xyz", "ab cd"):
        for coincidencia in ("fragmento", "palabra"):
            found = {a.numero for a in idx.buscar(phrase, coincidencia=coincidencia).articulos}
            expected = {
                i + 1
                for i, s in enumerate(sumarios)
                if matches(s, phrase, palabra=coincidencia == "palabra")
            }
            assert found == expected, (phrase, coincidencia)


@pytest.mark.parametrize("bad", ["\ud800", "cese \udfff", "ok"])
def test_lone_surrogates_in_queries_are_invalid_input(filled: SumarioIndex, bad: str) -> None:
    calls = [
        lambda: filled.buscar(bad),
        lambda: filled.buscar("personal", [{"texto": bad}]),
        lambda: filled.buscar("personal", consejeria=bad),
    ]
    for call in calls:
        if bad == "ok":
            call()
            continue
        with pytest.raises(BusquedaInvalidaError):
            call()


def test_lone_surrogates_in_stored_text_are_sanitised(idx: SumarioIndex) -> None:
    bulletin = ref("BOME-B-2026-6102", date(2026, 1, 3))
    idx.guardar_boletin(
        bulletin,
        [art(bulletin, 1, "Cese \ud800 del director", consejeria="HACIENDA \udfff")],
        "indexado",
        error=RuntimeError("bad \ud800 text"),
    )
    (found,) = idx.buscar("cese").articulos
    assert found.sumario == "Cese ? del director"  # the lone surrogate became "?"
    assert idx.buscar("cese", consejeria="hacienda").total == 1
    idx.guardar_resumen_sincronizacion({"ultimo_error": "x \ud800 y"})
    assert idx.estado().ultima_sincronizacion is not None


def test_migration_failure_reports_index_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v1.sqlite3"
    build_v1(path, ["Cese del director.", "Ceses varios."])
    monkeypatch.setattr(index_module, "_FTS_DDL", "CREATE VIRTUAL TABLE articles_fts USING nope(x)")
    with pytest.raises(BomeIndexUnavailableError) as info:
        SumarioIndex(path)
    assert type(info.value) is BomeIndexUnavailableError
    assert info.value.error_code == "indice_no_disponible"
    assert info.value.__cause__ is not None
    # Rolled back: still a readable v1 file with its rows.
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "1"
        assert raw.execute("SELECT count(*) FROM articles").fetchone()[0] == 2


# --------------------------------------------------------------------------- task 6: migration advisory


def test_migration_normalizes_each_sumario_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v1.sqlite3"
    sumarios = [f"Cese número {i} del personal" for i in range(40)] + [None, "  "]
    build_v1(path, sumarios)
    calls: list[object] = []
    real = index_module.normalize

    def counting(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(index_module, "normalize", counting)
    index = SumarioIndex(path)
    try:
        assert len(calls) <= len(sumarios)
        monkeypatch.setattr(index_module, "normalize", real)
        assert index.buscar("cese").total == 40
    finally:
        index.close()


def test_busy_timeout_covers_a_long_migration(idx: SumarioIndex) -> None:
    timeout_ms = idx._conn().execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms >= 15_000


# --------------------------------------------------------------------------- site-guard task 2: schema v3


def http_error(status: int | None, cve: str = "BOME-B-2018-5522") -> BomeHTTPError:
    url = f"{BASE}/bome/{cve}"
    if status is None:
        return BomeHTTPError(f"request to {url!r} failed: timed out", status=None, url=url)
    return BomeHTTPError(f"HTTP {status} for {url}", status=status, url=url)


def failure(index: SumarioIndex, cve: str) -> tuple[str, int | None, int]:
    """``(estado, http_status, fallos_5xx)`` of a stored bulletin."""
    row = index._conn().execute(
        "SELECT estado, http_status, fallos_5xx FROM bulletins WHERE cve = ?", (cve,)
    ).fetchone()
    return (row[0], row[1], row[2])


BROKEN = ref("BOME-B-2018-5522", date(2018, 3, 2))


def test_a_new_5xx_bulletin_is_an_error_with_its_status(idx: SumarioIndex) -> None:
    assert idx.guardar_boletin(BROKEN, [], "error", error=http_error(500)) == "error"
    assert failure(idx, BROKEN.cve) == ("error", 500, 1)


def test_a_second_5xx_makes_the_bulletin_roto(idx: SumarioIndex) -> None:
    idx.guardar_boletin(BROKEN, [], "error", error=http_error(500))
    assert idx.guardar_boletin(BROKEN, [], "error", error=http_error(502)) == "roto"
    assert failure(idx, BROKEN.cve) == ("roto", 502, 2)
    assert idx.estado_boletin(BROKEN.cve) == "roto"
    assert idx.estados_boletines()[BROKEN.cve] == "roto"
    # A further failure without an HTTP answer does not un-break it.
    assert idx.guardar_boletin(BROKEN, [], "error", error=http_error(None)) == "roto"
    assert failure(idx, BROKEN.cve) == ("roto", None, 2)


def test_non_5xx_failures_never_make_a_bulletin_roto(idx: SumarioIndex) -> None:
    not_found = BomeNotFoundError(f"not found: {BASE}/bome/X", status=404, url=f"{BASE}/bome/X")
    for error in (not_found, not_found, http_error(None), http_error(None), "parse failure", RuntimeError("x")):
        assert idx.guardar_boletin(BROKEN, [], "error", error=error) == "error"
    assert failure(idx, BROKEN.cve) == ("error", None, 0)
    idx.guardar_boletin(BROKEN, [], "error", error=not_found)
    assert failure(idx, BROKEN.cve) == ("error", 404, 0)


def test_a_timeout_between_two_5xx_does_not_reset_the_count(idx: SumarioIndex) -> None:
    idx.guardar_boletin(BROKEN, [], "error", error=http_error(500))
    idx.guardar_boletin(BROKEN, [], "error", error=http_error(None))
    assert failure(idx, BROKEN.cve) == ("error", None, 1)
    idx.guardar_boletin(BROKEN, [], "error", error=http_error(500))
    assert failure(idx, BROKEN.cve) == ("roto", 500, 2)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: BomeBlockedError("HTTP 503 for x", status=503, url="x"), id="blocked-503"),
        pytest.param(lambda: BomeHTTPError("HTTP 503 for x", status=503, url="x"), id="plain-503"),
    ],
)
def test_a_503_is_the_site_refusing_not_a_broken_page(idx: SumarioIndex, make) -> None:
    for _ in range(3):
        assert idx.guardar_boletin(BROKEN, [], "error", error=make()) == "error"
    assert failure(idx, BROKEN.cve) == ("error", 503, 0)


def test_success_resets_the_failure_info(idx: SumarioIndex) -> None:
    idx.guardar_boletin(BROKEN, [], "error", error=http_error(500))
    idx.guardar_boletin(BROKEN, [], "error", error=http_error(500))
    assert idx.guardar_boletin(BROKEN, [art(BROKEN, 1, "Cese del director.")], "indexado") == "indexado"
    assert failure(idx, BROKEN.cve) == ("indexado", None, 0)
    assert idx.buscar("cese").total == 1
    # Counting starts again from zero.
    idx.guardar_boletin(BROKEN, [], "error", error=http_error(500))
    assert failure(idx, BROKEN.cve) == ("indexado", 500, 1)


def test_an_indexed_bulletin_is_never_downgraded_to_roto(filled: SumarioIndex) -> None:
    for _ in range(3):
        assert filled.guardar_boletin(B2, [], "error", error=http_error(500, B2.cve)) == "indexado"
    assert failure(filled, B2.cve) == ("indexado", 500, 3)
    assert filled.buscar("hacienda").total == 1  # articles kept
    for _ in range(2):
        assert filled.guardar_boletin(OLD, [], "error", error=http_error(500, OLD.cve)) == "sin_sumarios"
    assert failure(filled, OLD.cve) == ("sin_sumarios", 500, 2)


def test_roto_cannot_be_stored_directly(idx: SumarioIndex) -> None:
    with pytest.raises(ValueError):
        idx.guardar_boletin(BROKEN, [], "roto")  # type: ignore[arg-type]


def test_pending_counts_only_the_default_sync_range(filled: SumarioIndex) -> None:
    # An index synced from 2014 by an earlier version keeps those calendar rows.
    new = ref("BOME-B-2018-5500", date(2018, 1, 2))
    new_error = ref("BOME-B-2026-6400", date(2026, 8, 1))
    old = ref("BOME-B-2016-5300", date(2016, 5, 3))
    old_error = ref("BOME-B-2017-5450", date(2017, 12, 29))
    filled.registrar_calendario([B1, B2, BX, OLD, new, new_error, old, old_error])
    filled.guardar_boletin(new_error, [], "error", error="boom")
    filled.guardar_boletin(old_error, [], "error", error="boom")
    state = filled.estado()
    assert index_module.SYNC_DEFAULT_START == date(2018, 1, 1)
    assert state.pendientes == 2  # 2018-5500 (never processed) and the 2026 error
    assert state.pendientes_anteriores_2018 == 2  # reported, not hidden
    cobertura = filled.buscar("cese").cobertura
    assert (cobertura["pendientes"], cobertura["pendientes_anteriores_2018"]) == (2, 2)
    assert state.to_dict()["pendientes_anteriores_2018"] == 2


def test_the_first_day_of_2018_is_pending_and_the_last_of_2017_is_not(idx: SumarioIndex) -> None:
    idx.registrar_calendario(
        [ref("BOME-B-2017-5499", date(2017, 12, 31)), ref("BOME-B-2018-5500", date(2018, 1, 1))]
    )
    state = idx.estado()
    assert (state.pendientes, state.pendientes_anteriores_2018) == (1, 1)


def test_rotos_are_counted_and_are_not_pending_work(filled: SumarioIndex) -> None:
    other = ref("BOME-B-2026-6400", date(2026, 8, 1))
    filled.registrar_calendario([B1, B2, BX, OLD, BROKEN, other])
    filled.guardar_boletin(other, [], "error", error=http_error(500, other.cve))
    filled.guardar_boletin(BROKEN, [], "error", error=http_error(500))
    filled.guardar_boletin(BROKEN, [], "error", error=http_error(500))
    state = filled.estado()
    assert state.boletines == {"indexado": 3, "sin_sumarios": 1, "error": 1, "roto": 1, "total": 6}
    assert state.pendientes == 1  # the error only: the roto is skipped work, not pending
    cobertura = filled.buscar("cese").cobertura
    assert (cobertura["pendientes"], cobertura["rotos"]) == (1, 1)
    assert cobertura["boletines_indexados"] == 4


V2_DDL = V1_DDL.replace("'unicode61 remove_diacritics 2'", "'trigram case_sensitive 1'")


def build_v2(path: Path) -> None:
    """A schema-v2 index file as v0.0.2 wrote it, with every kind of stored failure."""
    url = BASE + "/bome/{}"
    rows = [
        # cve, number, date, estado, error_code, error_message, n_articulos
        ("BOME-B-2026-6375", 6375, "2026-05-01", "indexado", None, None, 2),
        ("BOME-B-2026-6376", 6376, "2026-05-02", "indexado", "BomeHTTPError",
         "HTTP 500 for " + url.format("BOME-B-2026-6376"), 1),
        ("BOME-B-2014-5092", 5092, "2014-01-03", "sin_sumarios", None, None, 0),
        ("BOME-B-2018-5522", 5522, "2018-03-02", "error", "BomeHTTPError",
         "HTTP 500 for " + url.format("BOME-B-2018-5522"), 0),
        ("BOME-B-2017-5400", 5400, "2017-01-10", "error", "BomeHTTPError",
         "HTTP 502 for " + url.format("BOME-B-2017-5400"), 0),
        ("BOME-B-2017-5401", 5401, "2017-01-11", "error", "BomeNotFoundError",
         "not found: " + url.format("BOME-B-2017-5401"), 0),
        ("BOME-B-2017-5402", 5402, "2017-01-12", "error", "BomeHTTPError",
         "HTTP 404 for " + url.format("BOME-B-2017-5402"), 0),
        ("BOME-B-2017-5403", 5403, "2017-01-13", "error", "BomeHTTPError",
         f"request to '{url.format('BOME-B-2017-5403')}' failed: timed out", 0),
        ("BOME-B-2017-5404", 5404, "2017-01-14", "error", "BomeParseError", "no bulletin header", 0),
    ]
    with sqlite3.connect(path) as conn:
        conn.executescript(V2_DDL)
        conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
        conn.execute("INSERT INTO meta VALUES ('last_sync', '{\"estado\": \"completado\"}')")
        for cve, number, day, estado, code, message, count in rows:
            conn.execute(
                "INSERT INTO bulletins VALUES (?, ?, ?, 0, ?, ?, ?, ?, '2026-09-23T10:00:00Z')",
                (cve, number, day, estado, code, message, count),
            )
            conn.execute("INSERT INTO calendar VALUES (?, ?, ?, 0)", (cve, number, day))
        articles = [
            ("BOME-B-2026-6375", 1, "Cese del director."),
            ("BOME-B-2026-6375", 2, "Nombramiento de personal eventual."),
            ("BOME-B-2026-6376", 3, "Ceses varios."),
        ]
        for bulletin, number, sumario in articles:
            cursor = conn.execute(
                "INSERT INTO articles (cve, bulletin_cve, number, sumario, departamento, consejeria, "
                "organismo, consejeria_norm, url, pdf_url, listado_en_bome) "
                "VALUES (?, ?, ?, ?, 'CAM', 'HACIENDA', 'HACIENDA', 'hacienda', ?, NULL, 1)",
                (f"BOME-A-2026-{number}", bulletin, number, sumario, f"{url.format(bulletin)}/articulo/{number}"),
            )
            conn.execute(
                "INSERT INTO articles_fts (rowid, texto) VALUES (?, ?)", (cursor.lastrowid, normalize(sumario))
            )


def test_v2_index_is_migrated_to_v3_in_place(tmp_path: Path) -> None:
    path = tmp_path / "v2.sqlite3"
    build_v2(path)
    index = SumarioIndex(path)
    try:
        assert index.version_esquema() == SCHEMA_VERSION == 4
        assert failure(index, "BOME-B-2018-5522") == ("roto", 500, 1)
        assert failure(index, "BOME-B-2017-5400") == ("roto", 502, 1)
        assert failure(index, "BOME-B-2017-5401") == ("error", 404, 0)
        assert failure(index, "BOME-B-2017-5402") == ("error", 404, 0)
        assert failure(index, "BOME-B-2017-5403") == ("error", None, 0)
        assert failure(index, "BOME-B-2017-5404") == ("error", None, 0)
        # Stored successes are untouched, even one that recorded a later error.
        assert failure(index, "BOME-B-2026-6375") == ("indexado", None, 0)
        assert failure(index, "BOME-B-2026-6376") == ("indexado", None, 0)
        assert failure(index, "BOME-B-2014-5092") == ("sin_sumarios", None, 0)
        message = index._conn().execute(
            "SELECT error_code, error_message, n_articulos, indexed_at FROM bulletins WHERE cve = ?",
            ("BOME-B-2018-5522",),
        ).fetchone()
        assert tuple(message) == (
            "BomeHTTPError", f"HTTP 500 for {BASE}/bome/BOME-B-2018-5522", 0, "2026-09-23T10:00:00Z"
        )
        state = index.estado()
        assert state.boletines == {"indexado": 2, "sin_sumarios": 1, "error": 4, "roto": 2, "total": 9}
        assert state.articulos == 3
        assert state.pendientes == 0  # the 4 remaining errors are all from 2017
        assert state.pendientes_anteriores_2018 == 4
        assert state.ultima_sincronizacion == {"estado": "completado"}
        assert [a.numero for a in index.buscar("cese").articulos] == [3, 1]
        assert index.buscar("cese").cobertura["rotos"] == 2
        # The rebuilt table keeps its date index and its CHECK.
        conn = index._conn()
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(bulletins)")}
        assert "bulletins_date" in indexes
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE bulletins SET estado = 'banana' WHERE cve = 'BOME-B-2017-5404'")
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE '%bulletins%old%'").fetchone()[0] == 0
        # Writes keep working on migrated rows: the migrated 500 already counts once.
        index.guardar_boletin(ref("BOME-B-2017-5403", date(2017, 1, 13)), [], "error", error=http_error(500))
        assert failure(index, "BOME-B-2017-5403") == ("error", 500, 1)
    finally:
        index.close()
    again = SumarioIndex(path)
    try:
        assert again.version_esquema() == 4
        assert failure(again, "BOME-B-2018-5522") == ("roto", 500, 1)
        assert again.buscar("cese").total == 2
    finally:
        again.close()


def test_v1_index_with_errors_is_migrated_to_v3(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    build_v1(path, ["Cese del director."])
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO bulletins VALUES ('BOME-B-2018-5522', 5522, '2018-03-02', 0, 'error', "
            "'BomeHTTPError', ?, 0, '2026-09-23T10:00:00Z')",
            (f"HTTP 500 for {BASE}/bome/BOME-B-2018-5522",),
        )
    index = SumarioIndex(path)
    try:
        assert index.version_esquema() == 4
        assert failure(index, "BOME-B-2018-5522") == ("roto", 500, 1)
        assert failure(index, "BOME-B-2026-6375") == ("indexado", None, 0)
        assert index.buscar("cese").total == 1
        tokenizer = index._conn().execute(
            "SELECT sql FROM sqlite_master WHERE name = 'articles_fts'"
        ).fetchone()[0]
        assert "trigram" in tokenizer
    finally:
        index.close()


def test_v3_migration_failure_rolls_back_to_v2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v2.sqlite3"
    build_v2(path)
    monkeypatch.setattr(index_module, "_BULLETINS_DDL", "CREATE TABLE bulletins (broken")
    with pytest.raises(BomeIndexUnavailableError) as info:
        SumarioIndex(path)
    assert info.value.error_code == "indice_no_disponible"
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "2"
        assert raw.execute("SELECT count(*) FROM bulletins").fetchone()[0] == 9
        columns = {row[1] for row in raw.execute("PRAGMA table_info(bulletins)")}
        assert "http_status" not in columns


# --------------------------------------------------------------------------- old-portal-index task 1: schema v4

ANTIGUO = Path(__file__).parent / "fixtures" / "antiguo"


def boletin_antiguo(cve: str, day: date, dboid: int, extraordinario: bool = False) -> BoletinAntiguo:
    return BoletinAntiguo(
        cve=cve, numero=int(cve.rsplit("-", 1)[1]), extraordinario=extraordinario, sufijo=None,
        fecha=day, dboid=dboid, url_ficha=url_ficha(dboid), cve_oficial=day.year >= 2014,
    )


def ficha(name: str, boletin: BoletinAntiguo) -> FichaAntigua:
    return parse_ficha((ANTIGUO / f"{name}.html").read_bytes(), boletin.dboid, boletin=boletin)


B1991 = boletin_antiguo("BOME-B-1991-3175", date(1991, 12, 26), 281500)
B1999 = boletin_antiguo("BOME-B-1999-3660", date(1999, 12, 30), 276000)
B1986 = boletin_antiguo("BOME-B-1986-2899", date(1986, 12, 25), 279997)
EXTRA_A = boletin_antiguo("BOME-BX-1986-1", date(1986, 3, 1), 280058, extraordinario=True)
EXTRA_B = boletin_antiguo("BOME-BX-1986-1", date(1986, 9, 1), 280087, extraordinario=True)


@pytest.fixture
def mixed(filled: SumarioIndex) -> SumarioIndex:
    """``filled`` (bomemelilla.es) plus the 1991 and 1999 fichas of the old portal."""
    assert filled.guardar_boletin_antiguo(B1991, ficha("ficha_1991_3175", B1991), "indexado", clave=B1991.cve) == "indexado"
    assert filled.guardar_boletin_antiguo(B1999, ficha("ficha_1999_3660", B1999), "indexado", clave=B1999.cve) == "indexado"
    return filled


def bulletin_row(index: SumarioIndex, key: str) -> tuple:
    row = index._conn().execute(
        "SELECT origen, dboid, estado, n_articulos, number, date, extraordinary FROM bulletins WHERE cve = ?",
        (key,),
    ).fetchone()
    return tuple(row) if row is not None else None


def test_schema_v4_is_current() -> None:
    assert SCHEMA_VERSION == 4


def test_old_portal_bulletins_are_stored_with_origin_and_dboid(mixed: SumarioIndex) -> None:
    assert bulletin_row(mixed, B1991.cve) == ("melilla.es", 281500, "indexado", 30, 3175, "1991-12-26", 0)
    assert bulletin_row(mixed, B1999.cve) == ("melilla.es", 276000, "indexado", 73, 3660, "1999-12-30", 0)
    assert bulletin_row(mixed, B1.cve)[:2] == ("bomemelilla.es", None)
    origins = dict(mixed._conn().execute("SELECT origen, count(*) FROM articles GROUP BY origen").fetchall())
    assert origins == {"bomemelilla.es": 9, "melilla.es": 103}


def test_old_portal_articles_are_found_with_their_origin(mixed: SumarioIndex) -> None:
    found = mixed.buscar("suscripciones")
    assert found.total == 1
    hit = found.articulos[0]
    assert hit.origen == "melilla.es"
    assert (hit.bome_cve, hit.bome_numero, hit.bome_fecha, hit.bome_extraordinario) == (
        "BOME-B-1999-3660", 3660, date(1999, 12, 30), False,
    )
    assert (hit.cve, hit.numero, hit.consejeria) == ("MEL-276000-3260", 3260, "Presidencia (Boletín Oficial)")
    assert hit.url == url_ficha(276000)
    assert hit.pdf_url == "https://www.melilla.es/mandar.php/n/14/7953/3660_3119.pdf"
    assert hit.resaltado and "**suscripciones**" in hit.resaltado
    assert hit.to_dict()["origen"] == "melilla.es"
    # bomemelilla.es results carry their origin too.
    assert {a.origen for a in mixed.buscar("nombramiento").articulos} == {"bomemelilla.es"}


def test_boolean_search_spans_both_origins(mixed: SumarioIndex) -> None:
    both = mixed.buscar(terminos=[{"texto": "juicio de faltas"}, {"texto": "citación"}])
    assert both.total > 0 and all(a.bome_cve == B1999.cve for a in both.articulos)
    assert all("Citación" in a.sumario and "Faltas" in a.sumario for a in both.articulos)
    either = mixed.buscar(terminos=[{"texto": "desahucio"}, {"texto": "cese", "operador": "o"}])
    origins = {a.origen for a in either.articulos}
    assert origins == {"melilla.es", "bomemelilla.es"}
    assert {a.bome_cve for a in either.articulos if a.origen == "melilla.es"} == {B1991.cve, B1999.cve}
    negated = mixed.buscar(terminos=[{"texto": "notificación"}, {"texto": "faltas", "modo": "no_contiene"}])
    assert negated.total > 0 and not any("faltas" in a.sumario.lower() for a in negated.articulos)
    assert {a.origen for a in negated.articulos} == {"melilla.es"}
    dated = mixed.buscar("notificación", desde="1991-01-01", hasta="1991-12-31")
    assert dated.total > 0 and {a.bome_cve for a in dated.articulos} == {B1991.cve}
    by_consejeria = mixed.buscar("tribunal", consejeria="recursos humanos")
    assert by_consejeria.total == 2 and {a.origen for a in by_consejeria.articulos} == {"melilla.es"}


def test_a_ficha_without_articles_is_sin_sumarios(idx: SumarioIndex) -> None:
    empty = ficha("ficha_1986_2899", B1986)
    assert empty.articulos == ()
    assert idx.guardar_boletin_antiguo(B1986, empty, "indexado", clave=B1986.cve) == "sin_sumarios"
    assert bulletin_row(idx, B1986.cve) == ("melilla.es", 279997, "sin_sumarios", 0, 2899, "1986-12-25", 0)


def test_a_ficha_whose_articles_have_no_sumario_is_sin_sumarios(idx: SumarioIndex) -> None:
    full = ficha("ficha_1991_3175", B1991)
    blank = FichaAntigua(**{**{f: getattr(full, f) for f in full.__slots__},
                            "articulos": tuple(a.__class__(**{**{f: getattr(a, f) for f in a.__slots__}, "sumario": None})
                                               for a in full.articulos)})
    assert idx.guardar_boletin_antiguo(B1991, blank, "indexado", clave=B1991.cve) == "sin_sumarios"
    assert idx.estado().articulos == 30
    assert idx.buscar("edicto").total == 0


def test_repeated_pre_2014_identifiers_are_both_stored_by_dboid(idx: SumarioIndex) -> None:
    for boletin in (EXTRA_A, EXTRA_B):
        body = ficha("ficha_1986_2899", boletin)
        key = f"{boletin.cve}~{boletin.dboid}"
        assert idx.guardar_boletin_antiguo(boletin, body, "indexado", clave=key) == "sin_sumarios"
    assert bulletin_row(idx, "BOME-BX-1986-1~280058")[:3] == ("melilla.es", 280058, "sin_sumarios")
    assert bulletin_row(idx, "BOME-BX-1986-1~280087")[:3] == ("melilla.es", 280087, "sin_sumarios")
    assert bulletin_row(idx, "BOME-BX-1986-1") is None
    assert idx.estados_boletines(origen="melilla.es") == {
        "BOME-BX-1986-1~280058": "sin_sumarios",
        "BOME-BX-1986-1~280087": "sin_sumarios",
    }


def test_the_key_must_be_the_cve_or_the_cve_with_its_dboid(idx: SumarioIndex) -> None:
    body = ficha("ficha_1986_2899", B1986)
    for bad in ("BOME-B-1986-2900", "BOME-B-1986-2899~1", "x"):
        with pytest.raises(ValueError):
            idx.guardar_boletin_antiguo(B1986, body, "indexado", clave=bad)
    with pytest.raises(ValueError):  # the ficha of another bulletin
        idx.guardar_boletin_antiguo(B1991, body, "indexado", clave=B1991.cve)
    with pytest.raises(ValueError):  # a success needs its ficha
        idx.guardar_boletin_antiguo(B1986, None, "indexado", clave=B1986.cve)
    with pytest.raises(ValueError):
        idx.guardar_boletin_antiguo(B1986, body, "roto", clave=B1986.cve)  # type: ignore[arg-type]
    assert idx.estados_boletines() == {}


def test_duplicate_article_numbers_in_one_ficha_get_suffixed_keys(idx: SumarioIndex) -> None:
    full = ficha("ficha_1999_3660", B1999)
    first, second = full.articulos[0], full.articulos[1]
    twin = second.__class__(**{**{f: getattr(second, f) for f in second.__slots__}, "numero": first.numero})
    body = FichaAntigua(**{**{f: getattr(full, f) for f in full.__slots__}, "articulos": (first, twin, full.articulos[2])})
    assert idx.guardar_boletin_antiguo(B1999, body, "indexado", clave=B1999.cve) == "indexado"
    keys = [row[0] for row in idx._conn().execute("SELECT cve FROM articles ORDER BY id")]
    assert keys == ["MEL-276000-3260", "MEL-276000-3260-2", "MEL-276000-3262"]
    assert bulletin_row(idx, B1999.cve)[3] == 3


def test_storing_an_old_portal_bulletin_again_replaces_its_articles(mixed: SumarioIndex) -> None:
    body = ficha("ficha_1999_3660", B1999)
    shorter = FichaAntigua(**{**{f: getattr(body, f) for f in body.__slots__}, "articulos": body.articulos[:2]})
    assert mixed.guardar_boletin_antiguo(B1999, shorter, "indexado", clave=B1999.cve) == "indexado"
    assert bulletin_row(mixed, B1999.cve)[3] == 2
    assert mixed.buscar("faltas", desde="1999-01-01", hasta="1999-12-31").total == 0


def test_an_old_portal_page_answering_5xx_twice_becomes_roto(idx: SumarioIndex) -> None:
    url = B1999.url_ficha
    first = BomeHTTPError(f"HTTP 500 for {url}", status=500, url=url)
    assert idx.guardar_boletin_antiguo(B1999, None, "error", first, clave=B1999.cve) == "error"
    assert failure(idx, B1999.cve) == ("error", 500, 1)
    assert idx.guardar_boletin_antiguo(B1999, None, "error", first, clave=B1999.cve) == "roto"
    assert failure(idx, B1999.cve) == ("roto", 500, 2)
    assert bulletin_row(idx, B1999.cve)[:2] == ("melilla.es", 276000)
    assert idx.estados_boletines(origen="melilla.es") == {B1999.cve: "roto"}
    # A success afterwards resets it, like bomemelilla.es bulletins.
    assert idx.guardar_boletin_antiguo(B1999, ficha("ficha_1999_3660", B1999), "indexado", clave=B1999.cve) == "indexado"
    assert failure(idx, B1999.cve) == ("indexado", None, 0)
    # An error never discards what was indexed.
    assert idx.guardar_boletin_antiguo(B1999, None, "error", first, clave=B1999.cve) == "indexado"
    assert idx.buscar("suscripciones").total == 1


def test_old_portal_writes_leave_bomemelilla_rows_alone(mixed: SumarioIndex) -> None:
    assert mixed.buscar("cese").total == 3
    assert mixed.estados_boletines(origen="bomemelilla.es") == {
        B1.cve: "indexado", B2.cve: "indexado", BX.cve: "indexado", OLD.cve: "sin_sumarios",
    }
    assert mixed.estados_boletines(origen="melilla.es") == {B1991.cve: "indexado", B1999.cve: "indexado"}
    assert len(mixed.estados_boletines()) == 6  # every origin, as the bomemelilla.es sync plans with
    # A bomemelilla.es bulletin that is already indexed is never taken over by the old portal.
    same = boletin_antiguo(B2.cve, B2.date, 999)
    body = FichaAntigua(**{**{f: getattr(ficha("ficha_1999_3660", B1999), f) for f in FichaAntigua.__slots__},
                           "cve": B2.cve, "dboid": 999})
    assert mixed.guardar_boletin_antiguo(same, body, "indexado", clave=B2.cve) == "indexado"
    assert mixed.guardar_boletin_antiguo(same, None, "error", "boom", clave=B2.cve) == "indexado"
    assert bulletin_row(mixed, B2.cve)[:4] == ("bomemelilla.es", None, "indexado", 3)
    assert failure(mixed, B2.cve) == ("indexado", None, 0)
    assert mixed.buscar("hacienda", desde="2018-01-01").total == 1


def test_a_failed_bomemelilla_bulletin_is_replaced_by_its_old_portal_copy(idx: SumarioIndex) -> None:
    broken = ref("BOME-B-2016-5302", date(2016, 1, 8))
    idx.guardar_boletin(broken, [], "error", error=http_error(500, broken.cve))
    idx.guardar_boletin(broken, [], "error", error=http_error(500, broken.cve))
    assert idx.estado_boletin(broken.cve) == "roto"
    old = boletin_antiguo(broken.cve, broken.date, 216808)
    # An old-portal failure does not touch the bomemelilla.es failure counters.
    assert idx.guardar_boletin_antiguo(old, None, "error", "timeout", clave=old.cve) == "roto"
    assert failure(idx, broken.cve) == ("roto", 500, 2)
    body = parse_ficha((ANTIGUO / "ficha_5302.html").read_bytes(), 216808, boletin=old)
    assert idx.guardar_boletin_antiguo(old, body, "indexado", clave=old.cve) == "indexado"
    assert bulletin_row(idx, broken.cve)[:4] == ("melilla.es", 216808, "indexado", 4)
    assert failure(idx, broken.cve) == ("indexado", None, 0)
    assert idx.estados_boletines(origen="bomemelilla.es") == {}
    # A later bomemelilla.es failure keeps the old-portal copy; a success replaces it.
    assert idx.guardar_boletin(broken, [], "error", error=http_error(500, broken.cve)) == "indexado"
    assert bulletin_row(idx, broken.cve)[:3] == ("melilla.es", 216808, "indexado")
    assert idx.guardar_boletin(broken, [art(broken, 27, "Cese del director.")], "indexado") == "indexado"
    assert bulletin_row(idx, broken.cve)[:4] == ("bomemelilla.es", None, "indexado", 1)
    assert [a.origen for a in idx.buscar(desde="2016-01-01", hasta="2016-12-31").articulos] == ["bomemelilla.es"]


def test_the_better_outcome_wins_across_origins(idx: SumarioIndex) -> None:
    # 2014-2016 bulletins have no sumarios on bomemelilla.es; the old portal has them.
    bulletin = ref("BOME-B-2016-5302", date(2016, 1, 8))
    assert idx.guardar_boletin(bulletin, [], "sin_sumarios") == "sin_sumarios"
    old = boletin_antiguo(bulletin.cve, bulletin.date, 216808)
    body = parse_ficha((ANTIGUO / "ficha_5302.html").read_bytes(), 216808, boletin=old)
    assert idx.guardar_boletin_antiguo(old, body, "indexado", clave=old.cve) == "indexado"
    assert bulletin_row(idx, bulletin.cve)[:3] == ("melilla.es", 216808, "indexado")
    # A later bomemelilla.es sin_sumarios never downgrades the old-portal sumarios.
    assert idx.guardar_boletin(bulletin, [], "sin_sumarios") == "indexado"
    assert bulletin_row(idx, bulletin.cve)[:3] == ("melilla.es", 216808, "indexado")
    # An empty old-portal ficha does not replace a bomemelilla.es sin_sumarios (tie: it wins).
    other = ref("BOME-B-2016-5303", date(2016, 1, 12))
    assert idx.guardar_boletin(other, [], "sin_sumarios") == "sin_sumarios"
    empty = FichaAntigua(**{**{f: getattr(body, f) for f in FichaAntigua.__slots__},
                            "cve": other.cve, "dboid": 1, "articulos": ()})
    old_other = boletin_antiguo(other.cve, other.date, 1)
    assert idx.guardar_boletin_antiguo(old_other, empty, "indexado", clave=other.cve) == "sin_sumarios"
    assert bulletin_row(idx, other.cve)[:2] == ("bomemelilla.es", None)


def test_estado_counts_per_origin(mixed: SumarioIndex) -> None:
    mixed.guardar_boletin_antiguo(B1986, ficha("ficha_1986_2899", B1986), "indexado", clave=B1986.cve)
    mixed.guardar_boletin_antiguo(EXTRA_A, None, "error", http_error(500), clave=f"{EXTRA_A.cve}~{EXTRA_A.dboid}")
    mixed.registrar_calendario([B1, B2, BX, OLD])
    state = mixed.estado()
    assert state.boletines == {"indexado": 5, "sin_sumarios": 2, "error": 1, "roto": 0, "total": 8}
    assert state.articulos == 112
    assert (state.fecha_min, state.fecha_max) == (date(1986, 12, 25), date(2026, 9, 18))
    assert state.pendientes == 0
    assert state.por_origen == {
        "bomemelilla.es": {
            "boletines": {"indexado": 3, "sin_sumarios": 1, "error": 0, "roto": 0, "total": 4},
            "articulos": 9,
            "articulos_con_sumario": 7,
            "fecha_min": date(2014, 1, 3),
            "fecha_max": date(2026, 9, 18),
        },
        "melilla.es": {
            "boletines": {"indexado": 2, "sin_sumarios": 1, "error": 1, "roto": 0, "total": 4},
            "articulos": 103,
            "articulos_con_sumario": 103,
            "fecha_min": date(1986, 12, 25),
            "fecha_max": date(1999, 12, 30),
        },
    }
    data = state.to_dict()
    assert data["por_origen"]["melilla.es"]["fecha_min"] == "1986-12-25"
    json.dumps(data)
    cobertura = mixed.buscar("cese").cobertura
    assert cobertura["boletines_indexados"] == 7
    assert cobertura["por_origen"] == {
        "bomemelilla.es": {"boletines_indexados": 4, "fecha_min": "2014-01-03", "fecha_max": "2026-09-18", "rotos": 0},
        "melilla.es": {"boletines_indexados": 3, "fecha_min": "1986-12-25", "fecha_max": "1999-12-30", "rotos": 0},
    }


def test_estados_boletines_rejects_an_unknown_origin(idx: SumarioIndex) -> None:
    with pytest.raises(BusquedaInvalidaError):
        idx.estados_boletines(origen="boe.es")


def test_empty_index_counts_every_origin(idx: SumarioIndex) -> None:
    state = idx.estado()
    assert set(state.por_origen) == {"bomemelilla.es", "melilla.es"}
    assert state.por_origen["melilla.es"]["boletines"]["total"] == 0
    assert state.por_origen["melilla.es"]["fecha_min"] is None


V3_DDL = V2_DDL.replace(
    "estado TEXT NOT NULL CHECK (estado IN ('indexado', 'sin_sumarios', 'error')),\n"
    "    error_code TEXT, error_message TEXT, n_articulos INTEGER NOT NULL DEFAULT 0,\n"
    "    indexed_at TEXT NOT NULL\n",
    "estado TEXT NOT NULL CHECK (estado IN ('indexado', 'sin_sumarios', 'error', 'roto')),\n"
    "    error_code TEXT, error_message TEXT, n_articulos INTEGER NOT NULL DEFAULT 0,\n"
    "    indexed_at TEXT NOT NULL, http_status INTEGER, fallos_5xx INTEGER NOT NULL DEFAULT 0\n",
)


def build_v3(path: Path) -> None:
    """A schema-v3 index file as v0.0.3 wrote it."""
    assert V3_DDL != V2_DDL
    with sqlite3.connect(path) as conn:
        conn.executescript(V3_DDL)
        conn.execute("INSERT INTO meta VALUES ('schema_version', '3')")
        conn.execute("INSERT INTO meta VALUES ('last_sync', '{\"estado\": \"completado\"}')")
        rows = [
            ("BOME-B-2026-6375", 6375, "2026-05-01", "indexado", None, None, 2, None, 0),
            ("BOME-B-2018-5522", 5522, "2018-03-02", "roto", "BomeHTTPError", "HTTP 500 for x", 0, 500, 2),
            ("BOME-B-2014-5092", 5092, "2014-01-03", "sin_sumarios", None, None, 0, None, 0),
        ]
        for cve, number, day, estado, code, message, count, status, fails in rows:
            conn.execute(
                "INSERT INTO bulletins VALUES (?, ?, ?, 0, ?, ?, ?, ?, '2026-09-23T10:00:00Z', ?, ?)",
                (cve, number, day, estado, code, message, count, status, fails),
            )
            conn.execute("INSERT INTO calendar VALUES (?, ?, ?, 0)", (cve, number, day))
        for number, sumario in ((1, "Cese del director."), (2, "Nombramiento de personal eventual.")):
            cursor = conn.execute(
                "INSERT INTO articles (cve, bulletin_cve, number, sumario, departamento, consejeria, "
                "organismo, consejeria_norm, url, pdf_url, listado_en_bome) "
                "VALUES (?, 'BOME-B-2026-6375', ?, ?, 'CAM', 'HACIENDA', 'HACIENDA', 'hacienda', ?, NULL, 1)",
                (f"BOME-A-2026-{number}", number, sumario, f"{BASE}/bome/BOME-B-2026-6375/articulo/{number}"),
            )
            conn.execute(
                "INSERT INTO articles_fts (rowid, texto) VALUES (?, ?)", (cursor.lastrowid, normalize(sumario))
            )


def test_v3_index_is_migrated_to_v4_in_place(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite3"
    build_v3(path)
    index = SumarioIndex(path)
    try:
        assert index.version_esquema() == 4
        assert bulletin_row(index, "BOME-B-2026-6375") == ("bomemelilla.es", None, "indexado", 2, 6375, "2026-05-01", 0)
        assert failure(index, "BOME-B-2018-5522") == ("roto", 500, 2)
        assert bulletin_row(index, "BOME-B-2018-5522")[:2] == ("bomemelilla.es", None)
        origins = index._conn().execute("SELECT DISTINCT origen FROM articles").fetchall()
        assert [row[0] for row in origins] == ["bomemelilla.es"]
        found = index.buscar("cese")
        assert [(a.cve, a.origen) for a in found.articulos] == [("BOME-A-2026-1", "bomemelilla.es")]
        state = index.estado()
        assert state.boletines == {"indexado": 1, "sin_sumarios": 1, "error": 0, "roto": 1, "total": 3}
        assert state.por_origen["bomemelilla.es"]["boletines"]["total"] == 3
        assert state.por_origen["melilla.es"]["boletines"]["total"] == 0
        assert state.ultima_sincronizacion == {"estado": "completado"}
        with pytest.raises(sqlite3.IntegrityError):
            index._conn().execute("UPDATE bulletins SET origen = 'banana'")
        # The migrated file takes old-portal bulletins.
        assert index.guardar_boletin_antiguo(B1999, ficha("ficha_1999_3660", B1999), "indexado", clave=B1999.cve) == "indexado"
    finally:
        index.close()
    again = SumarioIndex(path)
    try:
        assert again.version_esquema() == 4
        assert again.buscar("cese").total == 1 and again.buscar("suscripciones").total == 1
    finally:
        again.close()


def test_v4_migration_failure_rolls_back_to_v3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v3.sqlite3"
    build_v3(path)

    def boom(tx: sqlite3.Connection) -> None:
        tx.execute("ALTER TABLE bulletins ADD COLUMN origen TEXT NOT NULL DEFAULT 'bomemelilla.es'")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SumarioIndex, "_upgrade_v3_to_v4", staticmethod(boom))
    with pytest.raises(BomeIndexUnavailableError):
        SumarioIndex(path)
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "3"
        columns = {row[1] for row in raw.execute("PRAGMA table_info(bulletins)")}
        assert "origen" not in columns


def test_v2_and_v1_files_reach_v4_through_the_chain(tmp_path: Path) -> None:
    build_v2(tmp_path / "v2.sqlite3")
    build_v1(tmp_path / "v1.sqlite3", ["Cese del director."])
    for name in ("v2.sqlite3", "v1.sqlite3"):
        index = SumarioIndex(tmp_path / name)
        try:
            assert index.version_esquema() == 4
            columns = {row[1] for row in index._conn().execute("PRAGMA table_info(bulletins)")}
            assert {"origen", "dboid", "http_status", "fallos_5xx"} <= columns
            assert {row[1] for row in index._conn().execute("PRAGMA table_info(articles)")} >= {"origen"}
            assert {a.origen for a in index.buscar("cese").articulos} == {"bomemelilla.es"}
            assert index.estados_boletines(origen="melilla.es") == {}
        finally:
            index.close()
