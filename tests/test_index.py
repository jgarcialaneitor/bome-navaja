"""Local SQLite FTS5 index of article sumarios."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from bome_navaja import index as index_module
from bome_navaja.index import SCHEMA_VERSION, SumarioIndex
from bome_navaja.models import (
    BomeIndexUnavailableError,
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


SAMPLES = [
    "Decreto nº 124 relativo al CESE de D. Alejandro Silva como Personal Eventual.",
    "Orden relativa a la compañía de seguros y al Año Nuevo.",
    "Destitución del director; ceses varios y nombramientos.",
    "Pingüino Straße ﬁnanzas",
]
PHRASES = ["cese", "personal eventual", "Nº 124", "compania", "COMPAÑÍA", "ano nuevo", "año",
           "destitucion", "ceses", "pinguino", "strasse", "finanzas", "eventual personal"]


def _token_oracle(sumario: str, phrase: str) -> bool:
    words = re.findall(r"[^\W_]+", normalize(sumario))
    wanted = re.findall(r"[^\W_]+", normalize(phrase))
    return any(words[i : i + len(wanted)] == wanted for i in range(len(words) - len(wanted) + 1))


def test_matching_equals_normalized_token_phrases(idx: SumarioIndex) -> None:
    bulletin = ref("BOME-B-2026-6000", date(2026, 1, 2))
    idx.guardar_boletin(bulletin, [art(bulletin, i + 1, s) for i, s in enumerate(SAMPLES)], "indexado")
    for phrase in PHRASES:
        found = {a.numero for a in idx.buscar(phrase, limite=200).articulos}
        expected = {i + 1 for i, s in enumerate(SAMPLES) if _token_oracle(s, phrase)}
        assert found == expected, phrase
        # A token-phrase hit is always a site-style substring hit too.
        for number in found:
            assert matches(SAMPLES[number - 1], phrase)


def test_accents_case_and_enye(filled: SumarioIndex) -> None:
    assert cves(filled.buscar("COMPANIA")) == ["BOME-A-2025-746"]
    assert cves(filled.buscar("año nuevo")) == cves(filled.buscar("ANO NUEVO")) == ["BOME-A-2025-746"]
    assert cves(filled.buscar("destitucion")) == ["BOME-A-2026-302"]
    assert cves(filled.buscar("nº 124")) == ["BOME-A-2025-745"]


def test_phrase_word_order_and_whole_words(filled: SumarioIndex) -> None:
    assert filled.buscar("eventual personal").total == 0
    # Token semantics: "cese" does not find "Ceses" unless a prefix is asked for.
    assert set(cves(filled.buscar("cese"))) == {"BOME-A-2025-745", "BOME-A-2026-302"}
    assert set(cves(filled.buscar("cese*"))) == {"BOME-A-2025-745", "BOME-A-2026-301", "BOME-A-2026-302"}
    assert set(cves(filled.buscar("personal event*"))) == {
        "BOME-A-2025-745", "BOME-A-2026-300", "BOME-A-2026-301",
    }


def test_and_or_not(filled: SumarioIndex) -> None:
    both = filled.buscar("personal eventual", [{"texto": "cese*"}])
    assert set(cves(both)) == {"BOME-A-2025-745", "BOME-A-2026-301"}
    either = filled.buscar(
        "personal eventual",
        [{"texto": "cese"}, {"texto": "alumnos", "operador": "o"}],
    )
    assert set(cves(either)) == {"BOME-A-2025-745", "BOME-AX-2026-102"}
    negated = filled.buscar("personal eventual", [{"texto": "hacienda", "modo": "no_contiene"}])
    assert set(cves(negated)) == {"BOME-A-2025-745", "BOME-A-2026-300"}


@pytest.mark.parametrize(
    "terminos",
    [
        [{"texto": "cese", "modo": "no_contiene"}],
        [{"texto": "cese"}, {"texto": "hacienda", "operador": "o", "modo": "no_contiene"}],
    ],
)
def test_negation_only_group_is_rejected(filled: SumarioIndex, terminos: list) -> None:
    with pytest.raises(BusquedaInvalidaError, match="no_contiene"):
        filled.buscar(None, terminos)


NASTY = ['"', "*", "NEAR(", "AND", "OR", "NOT", "-", ":", "(", ")", "a OR b", "sumario:cese",
         '"cese', 'cese"', "cese*)", "^cese", "{cese personal}", "o'brien", "NEAR(cese personal, 2)",
         "cese AND NOT personal", "**", "cese **", "+cese", "\\", "%", "_"]


@pytest.mark.parametrize("text", NASTY)
def test_injection_inputs_never_reach_sqlite_errors(filled: SumarioIndex, text: str) -> None:
    for call in (
        lambda: filled.buscar(text),
        lambda: filled.buscar("personal", [{"texto": text}]),
        lambda: filled.buscar("personal", [{"texto": text, "operador": "o"}]),
        lambda: filled.buscar("personal", [{"texto": text, "modo": "no_contiene"}]),
        lambda: filled.buscar("personal", consejeria=text),
    ):
        try:
            call()
        except BusquedaInvalidaError:
            pass


def test_keywords_are_searched_literally(filled: SumarioIndex) -> None:
    bulletin = ref("BOME-B-2026-6001", date(2026, 1, 3))
    filled.guardar_boletin(bulletin, [art(bulletin, 9, "Convenio NEAR AND OR NOT de prueba")], "indexado")
    assert cves(filled.buscar("near and or not")) == ["BOME-A-2026-9"]
    assert cves(filled.buscar('"NEAR" (AND) OR: NOT*')) == ["BOME-A-2026-9"]


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


def test_highlight_marks_every_positive_phrase_and_prefix(filled: SumarioIndex) -> None:
    found = filled.buscar("personal eventual", [{"texto": "cese*"}]).articulos
    by_cve = {a.cve: a.resaltado for a in found}
    assert by_cve["BOME-A-2026-301"] == (
        "**Ceses** y nombramientos de **personal eventual** de la Consejería de Hacienda."
    )


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
    assert state.boletines == {"indexado": 3, "sin_sumarios": 1, "error": 1, "total": 5}
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
    assert box == [2]


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
    assert filled.buscar("cese").total == 2


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
