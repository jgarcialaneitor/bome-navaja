"""Parser tests against real pages captured from bomemelilla.es."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

import pytest

from bome_navaja.models import (
    Article,
    BomeParseError,
    Bulletin,
    BulletinRef,
    Entity,
    SearchPage,
    Sumario,
)
from bome_navaja.parsers import (
    parse_article_page,
    parse_bulletin_page,
    parse_calendar,
    parse_entities,
    parse_search_page,
    parse_sumario_page,
    parse_year_slider,
)

Reader = Callable[[str], str]
BASE = "https://bomemelilla.es"


# --------------------------------------------------------------------------- calendar


def test_calendar_items(read_fixture: Reader) -> None:
    refs = parse_calendar(read_fixture("cal.json"))
    assert len(refs) == 8
    assert refs[0] == BulletinRef(
        cve="BOME-B-2026-6410",
        number=6410,
        date=date(2026, 9, 1),
        extraordinary=False,
        url=f"{BASE}/bome/BOME-B-2026-6410",
        title="Nº 6410",
    )
    assert refs[-1].cve == "BOME-B-2026-6416"
    assert refs[-1].date == date(2026, 9, 22)
    extras = [ref for ref in refs if ref.extraordinary]
    assert [ref.cve for ref in extras] == ["BOME-BX-2026-41"]
    assert extras[0].number == 41


def test_calendar_to_dict_is_json_safe(read_fixture: Reader) -> None:
    ref = parse_calendar(read_fixture("cal.json"))[0]
    data = ref.to_dict()
    assert data["date"] == "2026-09-01"
    json.dumps(data)


def test_calendar_skips_items_without_cve() -> None:
    text = json.dumps([{"title": "x", "start": "2026-01-01", "url": "/otra"}])
    assert parse_calendar(text) == []


def test_calendar_skips_malformed_cves_keeps_good_ones() -> None:
    text = json.dumps(
        [
            {"title": "Nº 6410", "start": "2026-09-01", "url": "/bome/BOME-B-2026-6410"},
            {"title": "Nº 0", "start": "2026-09-02", "url": "/bome/BOME-B-2026-0"},
            {"title": "Nº 41", "start": "2026-09-18", "url": "/bome/BOME-BX-2026-41"},
        ]
    )
    assert [ref.cve for ref in parse_calendar(text)] == ["BOME-B-2026-6410", "BOME-BX-2026-41"]


def test_year_slider_skips_malformed_cves() -> None:
    html = (
        '<div class="swiper-slide"><a href="/bome/BOME-B-2026-0">BOME Nº 0</a>'
        '<a href="/bome/BOME-B-2026-6416">BOME Nº 6416 del martes, 22 de septiembre de 2026</a>'
        "</div>"
    )
    assert [ref.cve for ref in parse_year_slider(html)] == ["BOME-B-2026-6416"]


def test_calendar_rejects_non_json() -> None:
    with pytest.raises(BomeParseError):
        parse_calendar("<html>oops</html>")


# --------------------------------------------------------------------------- bulletin page


def test_ordinary_bulletin_tree(read_fixture: Reader) -> None:
    bulletin = parse_bulletin_page(read_fixture("b6416.html"), "BOME-B-2026-6416")
    assert isinstance(bulletin, Bulletin)
    assert bulletin.cve == "BOME-B-2026-6416"
    assert bulletin.number == 6416
    assert bulletin.date == date(2026, 9, 22)
    assert bulletin.extraordinary is False
    assert bulletin.url == f"{BASE}/bome/BOME-B-2026-6416"
    assert bulletin.pdf_url == f"{BASE}/bome/descargar/BOME-B-2026-6416.pdf"
    assert bulletin.sumario_pdf_url == f"{BASE}/bome/descargar/BOME-S-2026-6416.pdf"
    assert bulletin.sumario_url == f"{BASE}/bome/BOME-B-2026-6416/sumario"

    assert len(bulletin.sections) == 1
    section = bulletin.sections[0]
    assert section.name == "CIUDAD AUTÓNOMA DE MELILLA"
    assert section.page_count == 35
    assert section.article_count == 13
    assert [c.name for c in section.consejerias] == [
        "CONSEJO DE GOBIERNO",
        "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD",
        "CONSEJERÍA DE HACIENDA",
        "CONSEJERÍA DE CULTURA, PATRIMONIO CULTURAL Y DEL MAYOR",
    ]
    assert [c.id for c in section.consejerias] == [18, 231, 8, 232]
    presidencia = section.consejerias[1]
    assert presidencia.article_count == 9
    assert presidencia.page_count == 25
    assert len(presidencia.organismos) == 1
    assert len(presidencia.organismos[0].articles) == 9
    # The organismo name may differ from the consejería name (no accent here).
    assert section.consejerias[2].organismos[0].name == "CONSEJERIA DE HACIENDA"

    articles = bulletin.articles
    assert len(articles) == 13
    assert [a.number for a in articles] == list(range(1050, 1063))
    first = articles[1]
    assert first.cve == "BOME-A-2026-1051"
    assert first.number == 1051
    assert first.sumario == (
        "Acuerdo del Consejo de Gobierno, de fecha 7 de septiembre de 2026, "
        "por el que se aprueba la Oferta de Empleo Público extraordinaria 2026."
    )
    assert first.url == f"{BASE}/bome/BOME-B-2026-6416/articulo/1051"
    assert first.pdf_url == f"{BASE}/bome/descargar/BOME-A-2026-1051.pdf"
    assert first.first_page is None


def test_extraordinary_bulletin(read_fixture: Reader) -> None:
    bulletin = parse_bulletin_page(read_fixture("bx41.html"), "BOME-BX-2026-41")
    assert bulletin.cve == "BOME-BX-2026-41"
    assert bulletin.number == 41
    assert bulletin.extraordinary is True
    assert bulletin.date == date(2026, 9, 18)
    assert bulletin.sumario_pdf_url == f"{BASE}/bome/descargar/BOME-SX-2026-41.pdf"
    assert [c.name for c in bulletin.sections[0].consejerias] == [
        "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD",
        "CONSEJERÍA DE SEGURIDAD CIUDADANA",
    ]
    assert [a.cve for a in bulletin.articles] == ["BOME-AX-2026-102", "BOME-AX-2026-103"]
    assert bulletin.articles[0].url == f"{BASE}/bome/BOME-BX-2026-41/articulo/102"
    assert "Bombero-Conductor" in (bulletin.articles[0].sumario or "")


def test_2014_bulletin_has_empty_article_stubs(read_fixture: Reader) -> None:
    bulletin = parse_bulletin_page(read_fixture("b5092.html"), "BOME-B-2014-5092")
    assert bulletin.number == 5092
    assert bulletin.date == date(2014, 1, 3)
    assert bulletin.sumario_pdf_url is None
    assert bulletin.sumario_url is None
    consejeria = bulletin.sections[0].consejerias[0]
    assert consejeria.name == "CONSEJERÍA DE BIENESTAR SOCIAL Y SANIDAD"
    assert [o.name for o in consejeria.organismos] == [
        "Dirección General de Sanidad y Consumo",
        "Dirección General de Servicios Sociales",
        "Secretaría Técnica",
    ]
    assert [len(o.articles) for o in consejeria.organismos] == [1, 2, 1]
    for article in bulletin.articles:
        assert article.sumario is None
        assert article.pdf_url is None
    assert bulletin.articles[1].url == f"{BASE}/bome/BOME-B-2014-5092/articulo/2"


def test_bulletin_to_dict_is_json_safe(read_fixture: Reader) -> None:
    data = parse_bulletin_page(read_fixture("b6416.html"), "BOME-B-2026-6416").to_dict()
    json.dumps(data)
    assert data["date"] == "2026-09-22"
    assert data["sections"][0]["consejerias"][0]["organismos"][0]["articles"][0]["cve"] == (
        "BOME-A-2026-1050"
    )


def test_bulletin_page_mismatched_cve_raises(read_fixture: Reader) -> None:
    with pytest.raises(BomeParseError):
        parse_bulletin_page(read_fixture("b6416.html"), "BOME-B-2026-6415")


def test_bulletin_page_with_unknown_article_cve_kind_raises(read_fixture: Reader) -> None:
    html = read_fixture("b6416.html").replace("(CVE: BOME-A-2026-1050)", "(CVE: BOME-Q-2026-1050)")
    with pytest.raises(BomeParseError):
        parse_bulletin_page(html, "BOME-B-2026-6416")


def test_bulletin_page_without_header_raises(read_fixture: Reader) -> None:
    with pytest.raises(BomeParseError):
        parse_bulletin_page(read_fixture("y2005.html"), "BOME-B-2026-6416")


def test_no_raw_html_in_sumarios(read_fixture: Reader) -> None:
    bulletin = parse_bulletin_page(read_fixture("b6416.html"), "BOME-B-2026-6416")
    for article in bulletin.articles:
        assert "<" not in (article.sumario or "")
        assert "  " not in (article.sumario or "")


# --------------------------------------------------------------------------- article page


def test_article_page_split_on_page_anchors(read_fixture: Reader) -> None:
    article = parse_article_page(read_fixture("art1051.html"))
    assert isinstance(article, Article)
    assert article.cve == "BOME-A-2026-1051"
    assert article.number == 1051
    assert article.bulletin_cve == "BOME-B-2026-6416"
    assert article.bulletin_number == 6416
    assert article.bulletin_date == date(2026, 9, 22)
    assert article.heading == (
        "CIUDAD AUTÓNOMA DE MELILLA - "
        "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD - "
        "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD"
    )
    assert article.sumario is not None
    assert article.sumario.startswith("Acuerdo del Consejo de Gobierno, de fecha 7 de septiembre")
    assert article.url == f"{BASE}/bome/BOME-B-2026-6416/articulo/1051"
    assert article.pdf_url == f"{BASE}/bome/descargar/BOME-A-2026-1051.pdf"

    assert [p.number for p in article.pages] == [4784, 4785, 4786, 4787]
    first = article.pages[0]
    assert first.cve == "BOME-P-2026-4784"
    assert first.pdf_url == f"{BASE}/bome/descargar/BOME-P-2026-4784.pdf"
    assert first.text.startswith(
        "El Consejo de Gobierno, en sesión resolutiva Ordinaria celebrada el 7 de septiembre"
    )
    assert "III.- PLAZAS IMPLICADAS" in article.pages[1].text
    assert "PLAZAS IMPLICADAS" not in first.text
    # Table rows keep their cells, separated by " | ".
    assert "Oficial 1ª Administrativo/a | L0430001 | 1" in article.pages[3].text
    # Paragraphs become lines; no raw HTML or entities survive.
    assert "\n" in first.text
    assert article.text is not None
    assert "<p" not in article.text and "&nbsp;" not in article.text
    assert article.text.startswith(first.text)
    assert article.pages[3].text in article.text


def test_article_page_accepts_extraordinary_page_cves(read_fixture: Reader) -> None:
    # Pages of extraordinary bulletins are ``PX`` (seen with BOME-BX-2021-46 → BOME-PX-2021-362).
    html = read_fixture("art1051.html").replace("BOME-P-2026-4784", "BOME-PX-2026-4784")
    article = parse_article_page(html)
    first = article.pages[0]
    assert first.cve == "BOME-PX-2026-4784"
    assert first.pdf_url == f"{BASE}/bome/descargar/BOME-PX-2026-4784.pdf"
    assert article.pages[1].cve == "BOME-P-2026-4785"


def test_article_page_with_unknown_page_cve_kind_raises(read_fixture: Reader) -> None:
    html = read_fixture("art1051.html").replace("BOME-P-2026-4784", "BOME-PQ-2026-4784")
    with pytest.raises(BomeParseError):
        parse_article_page(html)


def test_article_2014_stub_has_empty_fields(read_fixture: Reader) -> None:
    article = parse_article_page(read_fixture("art2014.html"))
    assert article.cve == "BOME-A-2014-2"
    assert article.number == 2
    assert article.bulletin_cve == "BOME-B-2014-5092"
    assert article.bulletin_date == date(2014, 1, 3)
    assert article.heading == (
        "CIUDAD AUTÓNOMA DE MELILLA - CONSEJERÍA DE BIENESTAR SOCIAL Y SANIDAD - "
        "Dirección General de Servicios Sociales"
    )
    assert article.sumario is None
    assert article.text is None
    assert article.pages == ()
    assert article.pdf_url is None
    json.dumps(article.to_dict())


def test_article_page_with_malformed_bulletin_link_raises_parse_error(
    read_fixture: Reader,
) -> None:
    html = read_fixture("art1051.html").replace(
        'href="/bome/BOME-B-2026-6416"', 'href="/bome/BOME-B-2026-0"'
    )
    with pytest.raises(BomeParseError):
        parse_article_page(html)


def test_article_page_without_header_raises(read_fixture: Reader) -> None:
    with pytest.raises(BomeParseError):
        parse_article_page(read_fixture("y2005.html"))


# --------------------------------------------------------------------------- sumario page


def test_sumario_page_flat_entries(read_fixture: Reader) -> None:
    sumario = parse_sumario_page(read_fixture("sum6416.html"), "BOME-B-2026-6416")
    assert isinstance(sumario, Sumario)
    assert sumario.bulletin_cve == "BOME-B-2026-6416"
    assert sumario.cve == "BOME-S-2026-6416"
    assert sumario.bulletin_number == 6416
    assert sumario.date == date(2026, 9, 22)
    assert sumario.url == f"{BASE}/bome/BOME-B-2026-6416/sumario"
    assert sumario.pdf_url == f"{BASE}/bome/descargar/BOME-S-2026-6416.pdf"
    assert sumario.pages == (4781, 4782)

    entries = sumario.entries
    assert len(entries) == 13
    assert [e.article.number for e in entries] == list(range(1050, 1063))
    assert {e.section for e in entries} == {"CIUDAD AUTÓNOMA DE MELILLA"}
    assert [e.consejeria for e in entries].count(
        "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD"
    ) == 9
    # Consejería headings that sit on the second sumario page still apply.
    assert entries[10].consejeria == "CONSEJERÍA DE HACIENDA"
    assert entries[12].consejeria == "CONSEJERÍA DE CULTURA, PATRIMONIO CULTURAL Y DEL MAYOR"

    first = entries[0].article
    assert first.cve == "BOME-A-2026-1050"
    assert first.first_page == 4783
    assert first.sumario == (
        "Extracto de los acuerdos adoptados por el Consejo de Gobierno, "
        "en sesión ordinaria, de fecha 14 de septiembre de 2026."
    )
    assert first.url == f"{BASE}/bome/BOME-B-2026-6416/articulo/1050"
    assert first.pdf_url == f"{BASE}/bome/descargar/BOME-A-2026-1050.pdf"
    assert entries[-1].article.first_page == 4813
    json.dumps(sumario.to_dict())


def test_sumario_page_accepts_sumario_cve(read_fixture: Reader) -> None:
    sumario = parse_sumario_page(read_fixture("sum6416.html"), "BOME-S-2026-6416")
    assert sumario.bulletin_cve == "BOME-B-2026-6416"


# --------------------------------------------------------------------------- section APIs


def test_parse_entities_consejerias(read_fixture: Reader) -> None:
    entities = parse_entities(read_fixture("cons1.json"))
    assert len(entities) == 128
    assert entities[0] == Entity(id=39, name="AGENCIA TRIBUTARIA")
    assert entities[-1] == Entity(id=241, name="VICECONSEJERÍA DE IGUALDAD Y MUJER")
    assert Entity(id=18, name="CONSEJO DE GOBIERNO") in entities
    assert entities[0].to_dict() == {"id": 39, "name": "AGENCIA TRIBUTARIA"}


def test_parse_entities_organismos(read_fixture: Reader) -> None:
    assert parse_entities(read_fixture("org38.json")) == [
        Entity(id=91, name="CIUDAD AUTONOMA DE MELILLA")
    ]


def test_parse_entities_rejects_bad_shape() -> None:
    with pytest.raises(BomeParseError):
        parse_entities('{"id": 1}')


# --------------------------------------------------------------------------- search pages


EXPECTED_SEARCH_CVES = [
    "BOME-B-2026-6375",
    "BOME-B-2025-6294",
    "BOME-BX-2025-44",
    "BOME-BX-2025-1",
    "BOME-B-2024-6204",
    "BOME-B-2024-6174",
    "BOME-B-2023-6125",
    "BOME-B-2023-6120",
    "BOME-B-2023-6117",
    "BOME-B-2023-6109",
]


@pytest.mark.parametrize("name", ["s_pe.html", "q_pe.html"])
def test_search_page(read_fixture: Reader, name: str) -> None:
    page = parse_search_page(read_fixture(name))
    assert isinstance(page, SearchPage)
    assert page.page == 1
    assert page.total_pages == 3
    assert page.total_results == 30
    assert page.per_page == 10
    assert page.has_next is True
    assert [hit.cve for hit in page.results] == EXPECTED_SEARCH_CVES
    extra = page.results[2]
    assert extra.extraordinary is True
    assert extra.number == 44
    assert extra.date == date(2025, 7, 18)
    assert extra.title == "BOME EXTRA Nº 44 del viernes, 18 de julio de 2025"
    assert extra.url == f"{BASE}/bome/BOME-BX-2025-44"
    assert page.results[0].date == date(2026, 5, 1)
    json.dumps(page.to_dict())


def test_search_page_ignores_navigation_links(read_fixture: Reader) -> None:
    # home.html has dozens of /bome/ links in its slider but no results block.
    page = parse_search_page(read_fixture("home.html"))
    assert page.results == ()
    assert page.total_results == 0
    assert page.total_pages == 0
    assert page.has_next is False


def test_search_page_zero_results_shape() -> None:
    html = '<div class="page-search-result"><p class="lead">No hay resultados.</p><ul></ul></div>'
    page = parse_search_page(html)
    assert page.results == ()
    assert page.total_results == 0
    assert page.page == 1


def test_search_page_zero_of_zero_counter() -> None:
    # Live shape for a query without hits: "Página 0 de 0".
    html = (
        '<div class="page-search-result"><p class="lead">'
        "Página 0 de 0. Mostrando 0 elementos de un total de 0 elementos."
        "</p><ul></ul></div>"
    )
    page = parse_search_page(html)
    assert page == SearchPage(results=(), page=1, total_pages=0, total_results=0)
    assert page.has_next is False


def test_search_page_thousands_separator() -> None:
    # Live: counts of 1,000 or more are printed with a dot, e.g. "1.934".
    html = (
        '<div class="page-search-result"><p class="lead">'
        "Página 3 de 1.194. Mostrando 10 elementos de un total de 11.934 elementos."
        "</p><ul></ul></div>"
    )
    page = parse_search_page(html)
    assert (page.page, page.total_pages, page.total_results) == (3, 1194, 11934)


# --------------------------------------------------------------------------- year slider


def test_year_slider_empty_year(read_fixture: Reader) -> None:
    assert parse_year_slider(read_fixture("y2005.html")) == []


def test_year_slider_home_year(read_fixture: Reader) -> None:
    refs = parse_year_slider(read_fixture("home.html"))
    assert len(refs) == 117
    assert refs[0].cve == "BOME-B-2026-6416"
    assert refs[0].date == date(2026, 9, 22)
    assert refs[-1].cve == "BOME-B-2026-6341"
    assert refs[-1].date == date(2026, 1, 2)
    assert sum(1 for ref in refs if ref.extraordinary) == 41
