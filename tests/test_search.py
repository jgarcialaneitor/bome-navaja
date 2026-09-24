"""Search layer tests: site query building, validation and article drill-down.

Everything is offline: search result pages are synthetic (same markup as the
captured s_pe.html) and bulletin pages come from the fixtures.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from bome_navaja import search as search_module
from bome_navaja.client import BomeClient
from bome_navaja.search import (
    BusquedaInvalidaError,
    buscar_articulos,
    buscar_bomes,
)

TODAY = date(2026, 9, 23)
MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

# (cve, date) as the site lists them, newest first.
B6416 = ("BOME-B-2026-6416", date(2026, 9, 22))
BX41 = ("BOME-BX-2026-41", date(2026, 9, 18))
B5092 = ("BOME-B-2014-5092", date(2014, 1, 3))


def results_html(items: list[tuple[str, date]], page: int, total_pages: int, total: int) -> str:
    """Search results markup identical in shape to tests/fixtures/s_pe.html."""
    links = "".join(
        f'<li><a class="text-brand hover" href="/bome/{cve}">BOME Nº {cve.rsplit("-", 1)[1]} '
        f"del lunes, {day.day} de {MESES[day.month]} de {day.year}</a></li>"
        for cve, day in items
    )
    return (
        '<div class="page-search-result"><h2>Resultados</h2><p class="lead">'
        f"Página {page} de {total_pages}. Mostrando {len(items)} elementos de un total "
        f"de {total} elementos.</p><ul>{links}</ul></div>"
    )


class Site:
    """MockTransport handler: search pages by ``page`` param, bulletins from fixtures."""

    def __init__(self, fixtures_dir: Path) -> None:
        self.fixtures_dir = fixtures_dir
        self.requests: list[httpx.Request] = []
        self.search_pages: dict[int, str] = {}
        self.search_handler: Callable[[httpx.Request], httpx.Response] | None = None
        self.bulletins: dict[str, Callable[[], httpx.Response]] = {
            "BOME-B-2026-6416": self._fixture("b6416.html"),
            "BOME-BX-2026-41": self._fixture("bx41.html"),
            "BOME-B-2014-5092": self._fixture("b5092.html"),
        }

    def _fixture(self, name: str) -> Callable[[], httpx.Response]:
        body = (self.fixtures_dir / name).read_bytes()
        return lambda: httpx.Response(200, content=body)

    def paged(self, pages: list[list[tuple[str, date]]]) -> None:
        total = sum(len(items) for items in pages)
        for number, items in enumerate(pages, start=1):
            self.search_pages[number] = results_html(items, number, len(pages), total)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/buscador-avanzado":
            if self.search_handler is not None:
                return self.search_handler(request)
            page = int(request.url.params.get("page", "1"))
            html = self.search_pages.get(page)
            return httpx.Response(200, text=html) if html else httpx.Response(404)
        if path.startswith("/bome/"):
            handler = self.bulletins.get(path.removeprefix("/bome/"))
            return handler() if handler else httpx.Response(404)
        return httpx.Response(404)

    def client(self) -> BomeClient:
        return BomeClient(transport=httpx.MockTransport(self))

    def search_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/buscador-avanzado"]

    def bulletin_paths(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.path.startswith("/bome/")]


@pytest.fixture
def site(fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    monkeypatch.setattr(search_module, "_today", lambda: TODAY)
    return Site(fixtures_dir)


def contenido(i: int, text: str, type_: str = "sumario_articulo", like: str = "like") -> list[tuple[str, str]]:
    return [
        (f"contenido[{i}][type]", type_),
        (f"contenido[{i}][like]", like),
        (f"contenido[{i}][content]", text),
        (f"contenido[{i}][operator]", "and"),
    ]


# --------------------------------------------------------------------------- buscar_bomes


def test_buscar_bomes_sends_exact_params(site: Site, fixtures_dir: Path) -> None:
    site.search_handler = lambda request: httpx.Response(
        200, content=(fixtures_dir / "s_pe.html").read_bytes()
    )
    with site.client() as client:
        result = buscar_bomes(
            client,
            texto="personal eventual",
            terminos=[{"texto": "cese"}],
            desde="2020-01-01",
            hasta=date(2026, 9, 23),
            departamento=1,
            consejeria=231,
            pagina=2,
        )
    (request,) = site.requests
    assert request.url.path == "/buscador-avanzado"
    assert request.url.params.multi_items() == [
        ("from", "2020-01-01"),
        ("to", "2026-09-23"),
        ("departamento", "1"),
        ("consejeria", "231"),
        *contenido(0, "personal eventual"),
        *contenido(1, "cese"),
        ("page", "2"),
    ]
    assert result.resultados.total_results == 30
    assert [ref.cve for ref in result.resultados.results][:2] == [
        "BOME-B-2026-6375",
        "BOME-B-2025-6294",
    ]
    data = result.to_dict()
    json.dumps(data)
    assert data["total_bomes"] == 30
    assert data["total_paginas"] == 3
    assert data["hay_mas"] is True
    assert data["bomes"][0]["cve"] == "BOME-B-2026-6375"
    assert data["consulta"]["desde"] == "2020-01-01"
    assert data["consulta"]["consejeria"] == 231
    assert data["consulta"]["terminos"] == [
        {"texto": "personal eventual", "operador": "y", "modo": "contiene", "ambito": "sumario"},
        {"texto": "cese", "operador": "y", "modo": "contiene", "ambito": "sumario"},
    ]
    assert data["parametros_sitio"][0] == ["from", "2020-01-01"]


def test_buscar_bomes_always_sends_from_and_to(site: Site) -> None:
    site.paged([[B6416]])
    with site.client() as client:
        result = buscar_bomes(client, texto="personal eventual")
    params = site.requests[0].url.params
    assert params["from"] == "2014-01-01"
    assert params["to"] == "2026-09-23"
    assert params["page"] == "1"
    assert result.consulta["desde"] == "2014-01-01"
    assert result.consulta["hasta"] == "2026-09-23"


def test_buscar_bomes_contenido_ambito_and_negation(site: Site) -> None:
    site.paged([[B6416]])
    with site.client() as client:
        buscar_bomes(
            client,
            texto="oferta de empleo",
            ambito="contenido",
            terminos=[
                {"texto": "interinos", "modo": "no_contiene"},
                {"texto": "cese", "ambito": "sumario"},
            ],
        )
    items = site.requests[0].url.params.multi_items()
    assert items[2:14] == [
        *contenido(0, "oferta de empleo", type_="pagina"),
        *contenido(1, "interinos", type_="pagina", like="nlike"),
        *contenido(2, "cese"),
    ]


def test_buscar_bomes_numeric_criteria(site: Site) -> None:
    site.paged([[B6416]])
    with site.client() as client:
        buscar_bomes(
            client, numero_bome=6416, numero_articulo=1051, numero_pagina=4784, anio=2026
        )
    items = site.requests[0].url.params.multi_items()
    assert items == [
        ("from", "2014-01-01"),
        ("to", "2026-09-23"),
        ("numero[0][type]", "bome"),
        ("numero[0][like]", "like"),
        ("numero[0][number]", "6416"),
        ("numero[0][operator]", "and"),
        ("numero[1][type]", "articulo"),
        ("numero[1][like]", "like"),
        ("numero[1][number]", "1051"),
        ("numero[1][operator]", "and"),
        ("numero[2][type]", "pagina"),
        ("numero[2][like]", "like"),
        ("numero[2][number]", "4784"),
        ("numero[2][operator]", "and"),
        ("numero[3][type]", "year"),
        ("numero[3][like]", "like"),
        ("numero[3][number]", "2026"),
        ("numero[3][operator]", "and"),
        ("page", "1"),
    ]


def test_buscar_bomes_allows_date_only_search(site: Site) -> None:
    site.paged([[B6416]])
    with site.client() as client:
        result = buscar_bomes(client, desde="2026-09-01")
    assert result.resultados.results[0].cve == "BOME-B-2026-6416"
    assert site.requests[0].url.params["from"] == "2026-09-01"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # would silently list every bulletin
        {"texto": "   "},
        {"texto": "cese", "ambito": "todo"},
        {"texto": "cese", "desde": "2026-09-10", "hasta": "2026-09-01"},
        {"texto": "cese", "desde": "10/09/2026"},
        {"texto": "cese", "pagina": 0},
        {"numero_bome": 0},
        {"numero_bome": "6416"},
        {"consejeria": -1},
        {"texto": "cese", "terminos": [{"texto": "hacienda", "operador": "o"}]},
        {"texto": "cese", "terminos": [{"text": "typo"}]},
        {"texto": "cese", "terminos": [{"texto": "x", "ambito": "boe"}]},
        {"texto": "cese", "terminos": "cese"},
    ],
)
def test_buscar_bomes_validation(site: Site, kwargs: dict) -> None:
    with site.client() as client, pytest.raises(BusquedaInvalidaError):
        buscar_bomes(client, **kwargs)
    assert site.requests == []


def test_validation_error_is_a_value_error() -> None:
    assert issubclass(BusquedaInvalidaError, ValueError)


def test_or_is_rejected_with_an_explanation(site: Site) -> None:
    with site.client() as client, pytest.raises(BusquedaInvalidaError, match="OR"):
        buscar_bomes(client, texto="cese", terminos=[{"texto": "hacienda", "operador": "o"}])


# --------------------------------------------------------------------------- buscar_articulos


def test_drill_down_across_two_result_pages(site: Site) -> None:
    site.paged([[B6416, BX41], [B5092]])
    with site.client() as client:
        result = buscar_articulos(client, texto="RELACION provisional")
    # Result pages requested in order, each with from/to and the sumario term.
    assert [r.url.params["page"] for r in site.search_requests()] == ["1", "2"]
    for request in site.search_requests():
        assert request.url.params["from"] == "2014-01-01"
        assert request.url.params["contenido[0][type]"] == "sumario_articulo"
    assert site.bulletin_paths() == [
        "/bome/BOME-B-2026-6416",
        "/bome/BOME-BX-2026-41",
        "/bome/BOME-B-2014-5092",
    ]
    assert [a.numero for a in result.articulos] == [1055, 1056, 1057, 1058, 1059]
    first = result.articulos[0]
    assert first.cve == "BOME-A-2026-1055"
    assert first.bome_cve == "BOME-B-2026-6416"
    assert first.bome_numero == 6416
    assert first.bome_fecha == date(2026, 9, 22)
    assert first.bome_extraordinario is False
    assert first.departamento == "CIUDAD AUTÓNOMA DE MELILLA"
    assert first.consejeria == "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD"
    assert first.organismo == "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD"
    assert "relación provisional" in (first.sumario or "")
    assert first.url == "https://bomemelilla.es/bome/BOME-B-2026-6416/articulo/1055"
    assert first.pdf_url == "https://bomemelilla.es/bome/descargar/BOME-A-2026-1055.pdf"

    assert result.bomes_revisados == 3
    assert result.total_bomes == 3
    assert result.truncado is False
    assert result.motivo_truncado is None
    assert result.errores == ()
    # The 2014 stub has no sumarios; BX-41 has none that match locally.
    assert [ref.cve for ref in result.bomes_sin_coincidencia] == [
        "BOME-BX-2026-41",
        "BOME-B-2014-5092",
    ]
    data = result.to_dict()
    json.dumps(data)
    assert data["articulos"][0]["bome_fecha"] == "2026-09-22"
    assert data["bomes_sin_coincidencia"][1]["cve"] == "BOME-B-2014-5092"


def test_drill_down_stops_at_max_bomes(site: Site) -> None:
    site.paged([[B6416, BX41], [B5092]])
    with site.client() as client:
        result = buscar_articulos(client, texto="relacion provisional", max_bomes=2)
    assert result.bomes_revisados == 2
    assert result.truncado is True
    assert result.motivo_truncado == "max_bomes"
    assert "/bome/BOME-B-2014-5092" not in site.bulletin_paths()
    assert len(result.articulos) == 5


def test_drill_down_not_truncated_when_budget_fits_exactly(site: Site) -> None:
    site.paged([[B6416, BX41]])
    with site.client() as client:
        result = buscar_articulos(client, texto="relacion provisional", max_bomes=2)
    assert result.bomes_revisados == 2
    assert result.truncado is False


def test_drill_down_stops_at_max_articulos(site: Site) -> None:
    site.paged([[B6416, BX41], [B5092]])
    with site.client() as client:
        result = buscar_articulos(client, texto="relacion provisional", max_articulos=3)
    assert [a.numero for a in result.articulos] == [1055, 1056, 1057]
    assert result.truncado is True
    assert result.motivo_truncado == "max_articulos"
    assert site.bulletin_paths() == ["/bome/BOME-B-2026-6416"]


def test_drill_down_bounds_are_clamped_and_validated(site: Site) -> None:
    site.paged([[B6416]])
    with site.client() as client:
        result = buscar_articulos(
            client, texto="relacion provisional", max_bomes=1000, max_articulos=10_000
        )
        assert result.consulta["max_bomes"] == 100
        assert result.consulta["max_articulos"] == 500
        with pytest.raises(BusquedaInvalidaError):
            buscar_articulos(client, texto="cese", max_bomes=0)
        with pytest.raises(BusquedaInvalidaError):
            buscar_articulos(client, texto="cese", max_articulos=0)


def test_failing_bulletin_is_recorded_and_search_continues(site: Site) -> None:
    site.paged([[B6416, BX41], [B5092]])
    site.bulletins["BOME-BX-2026-41"] = lambda: httpx.Response(500)
    with site.client() as client:
        result = buscar_articulos(client, texto="relacion provisional")
    assert len(result.articulos) == 5
    assert result.bomes_revisados == 3
    (error,) = result.errores
    assert error.cve == "BOME-BX-2026-41"
    assert error.etapa == "bome"
    assert error.error_code == "error_http"
    assert "500" in error.mensaje
    assert [ref.cve for ref in result.bomes_sin_coincidencia] == ["BOME-B-2014-5092"]
    json.dumps(result.to_dict())


def test_failing_later_search_page_is_recorded(site: Site) -> None:
    site.paged([[B6416, BX41], [B5092]])
    del site.search_pages[2]  # page 2 answers 404
    with site.client() as client:
        result = buscar_articulos(client, texto="relacion provisional")
    assert result.bomes_revisados == 2
    assert result.truncado is True
    assert result.motivo_truncado == "error_busqueda"
    assert [e.etapa for e in result.errores] == ["busqueda"]
    assert result.errores[0].error_code == "no_encontrado"


def test_or_query_merges_one_site_search_per_and_group(site: Site) -> None:
    site.paged([[B6416, BX41]])
    with site.client() as client:
        result = buscar_articulos(
            client,
            texto="relacion provisional",
            terminos=[{"texto": "tramitacion urgente", "operador": "o"}],
        )
    sent = [r.url.params["contenido[0][content]"] for r in site.search_requests()]
    assert sent == ["relacion provisional", "tramitacion urgente"]
    for request in site.search_requests():
        assert "contenido[1][content]" not in request.url.params
    # Each bulletin is fetched once even though both searches listed it.
    assert site.bulletin_paths() == ["/bome/BOME-B-2026-6416", "/bome/BOME-BX-2026-41"]
    assert [a.numero for a in result.articulos] == list(range(1052, 1060))
    assert result.total_bomes == 4
    assert result.total_bomes_exacto is False
    assert len(result.parametros_sitio) == 2


def test_local_consejeria_and_article_filters(site: Site) -> None:
    site.paged([[B6416]])
    with site.client() as client:
        by_consejeria = buscar_articulos(client, texto="orden", consejeria=8)
        by_number = buscar_articulos(client, texto="orden", numero_articulo=1052)
    assert [a.numero for a in by_consejeria.articulos] == [1060]
    assert [a.numero for a in by_number.articulos] == [1052]
    params = site.search_requests()[0].url.params
    assert params["consejeria"] == "8"


def test_negation_is_applied_locally(site: Site) -> None:
    site.paged([[B6416]])
    with site.client() as client:
        result = buscar_articulos(
            client,
            texto="relacion provisional",
            terminos=[{"texto": "psicologo", "modo": "no_contiene"}],
        )
    assert [a.numero for a in result.articulos] == [1055, 1056, 1057, 1059]


def test_buscar_articulos_rejects_page_text_terms(site: Site) -> None:
    with site.client() as client, pytest.raises(BusquedaInvalidaError):
        buscar_articulos(client, texto="cese", terminos=[{"texto": "x", "ambito": "contenido"}])
    with site.client() as client, pytest.raises(BusquedaInvalidaError):
        buscar_articulos(client)
    assert site.requests == []


def _without_article(html: str, cve: str) -> str:
    """Drop one article block from a bulletin page, as the live site sometimes does."""
    pattern = re.compile(
        r'<ul class="articulo-list">(?:(?!<ul class="articulo-list">).)*?'
        + re.escape(cve)
        + r".*?</ul>",
        re.DOTALL,
    )
    stripped, count = pattern.subn("", html, count=1)
    assert count == 1
    return stripped


def test_articles_missing_from_the_bulletin_page_are_fetched(
    site: Site, fixtures_dir: Path
) -> None:
    # Live 2026-09-23: BOME-B-2025-6294 lists 744 and 746-750 but not 745,
    # which exists at /articulo/745 and is the "cese ... Personal Eventual" match.
    html = _without_article((fixtures_dir / "b6416.html").read_text("utf-8"), "BOME-A-2026-1051")
    site.bulletins["BOME-B-2026-6416"] = lambda: httpx.Response(200, text=html)
    article_body = (fixtures_dir / "art1051.html").read_bytes()
    site.bulletins["BOME-B-2026-6416/articulo/1051"] = lambda: httpx.Response(
        200, content=article_body
    )
    site.paged([[B6416]])
    with site.client() as client:
        result = buscar_articulos(client, texto="oferta de empleo publico")
    assert site.bulletin_paths() == [
        "/bome/BOME-B-2026-6416",
        "/bome/BOME-B-2026-6416/articulo/1051",
    ]
    (article,) = result.articulos
    assert article.cve == "BOME-A-2026-1051"
    assert article.listado_en_bome is False
    assert article.departamento == "CIUDAD AUTÓNOMA DE MELILLA"
    assert article.consejeria == "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD"
    assert article.organismo == "CONSEJERÍA DE PRESIDENCIA, ADMINISTRACIÓN PÚBLICA E IGUALDAD"
    assert article.bome_fecha == date(2026, 9, 22)
    assert article.url == "https://bomemelilla.es/bome/BOME-B-2026-6416/articulo/1051"
    assert article.pdf_url == "https://bomemelilla.es/bome/descargar/BOME-A-2026-1051.pdf"
    assert result.bomes_sin_coincidencia == ()


def test_hidden_articles_respect_local_filters(site: Site, fixtures_dir: Path) -> None:
    html = _without_article((fixtures_dir / "b6416.html").read_text("utf-8"), "BOME-A-2026-1051")
    site.bulletins["BOME-B-2026-6416"] = lambda: httpx.Response(200, text=html)
    article_body = (fixtures_dir / "art1051.html").read_bytes()
    site.bulletins["BOME-B-2026-6416/articulo/1051"] = lambda: httpx.Response(
        200, content=article_body
    )
    site.paged([[B6416]])
    with site.client() as client:
        same = buscar_articulos(client, texto="oferta de empleo", consejeria=231)
        other = buscar_articulos(client, texto="oferta de empleo", consejeria=8)
        by_number = buscar_articulos(client, texto="orden", numero_articulo=1052)
    assert [a.numero for a in same.articulos] == [1051]
    assert other.articulos == ()
    assert [a.numero for a in by_number.articulos] == [1052]
    # The numero_articulo filter avoids fetching unrelated hidden articles.
    assert site.bulletin_paths().count("/bome/BOME-B-2026-6416/articulo/1051") == 2


def test_listed_articles_are_flagged_as_listed(site: Site) -> None:
    site.paged([[B6416]])
    with site.client() as client:
        result = buscar_articulos(client, texto="relacion provisional")
    assert all(article.listado_en_bome for article in result.articulos)


def test_failing_hidden_article_is_recorded(site: Site, fixtures_dir: Path) -> None:
    html = _without_article((fixtures_dir / "b6416.html").read_text("utf-8"), "BOME-A-2026-1056")
    site.bulletins["BOME-B-2026-6416"] = lambda: httpx.Response(200, text=html)
    site.paged([[B6416]])
    with site.client() as client:
        result = buscar_articulos(client, texto="relacion provisional")
    assert [a.numero for a in result.articulos] == [1055, 1057, 1058, 1059]
    (error,) = result.errores
    assert error.etapa == "articulo"
    assert error.cve == "BOME-A-2026-1056"
    assert error.error_code == "no_encontrado"


def test_blocked_bulletin_stops_drill_down(site: Site) -> None:
    from bome_navaja.models import BomeBlockedError

    site.paged([[B6416, BX41], [B5092]])
    site.bulletins["BOME-BX-2026-41"] = lambda: httpx.Response(429, headers={"retry-after": "60"})
    with site.client() as client, pytest.raises(BomeBlockedError) as info:
        buscar_articulos(client, texto="relacion provisional")
    assert info.value.retry_after == 60.0
    # Nothing is requested after the site starts refusing.
    assert site.bulletin_paths() == ["/bome/BOME-B-2026-6416", "/bome/BOME-BX-2026-41"]
    assert len(site.search_requests()) == 1


def test_blocked_hidden_article_stops_drill_down(site: Site, fixtures_dir: Path) -> None:
    from bome_navaja.models import BomeBlockedError

    html = _without_article((fixtures_dir / "b6416.html").read_text("utf-8"), "BOME-A-2026-1056")
    site.bulletins["BOME-B-2026-6416"] = lambda: httpx.Response(200, text=html)
    site.bulletins["BOME-B-2026-6416/articulo/1056"] = lambda: httpx.Response(403)
    site.paged([[B6416, BX41]])
    with site.client() as client, pytest.raises(BomeBlockedError) as info:
        buscar_articulos(client, texto="relacion provisional")
    assert info.value.status == 403
    assert "/bome/BOME-BX-2026-41" not in site.bulletin_paths()


def test_blocked_later_search_page_propagates(site: Site) -> None:
    from bome_navaja.models import BomeBlockedError

    site.paged([[B6416, BX41], [B5092]])

    def search(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 2:
            return httpx.Response(503)
        return httpx.Response(200, text=site.search_pages[page])

    site.search_handler = search
    with site.client() as client, pytest.raises(BomeBlockedError):
        buscar_articulos(client, texto="relacion provisional")


def test_first_search_page_failure_propagates(site: Site) -> None:
    site.search_handler = lambda request: httpx.Response(500)
    from bome_navaja.models import BomeHTTPError

    with site.client() as client, pytest.raises(BomeHTTPError):
        buscar_articulos(client, texto="cese")


# --------------------------------------------------------------------------- articulos_del_boletin


def test_articulos_del_boletin_lists_every_article(site: Site, fixtures_dir: Path) -> None:
    from bome_navaja.parsers import parse_bulletin_page
    from bome_navaja.search import articulos_del_boletin

    bulletin = parse_bulletin_page((fixtures_dir / "b6416.html").read_text("utf-8"), "BOME-B-2026-6416")
    with site.client() as client:
        articles, errors = articulos_del_boletin(client, bulletin)
    assert [a.numero for a in articles] == list(range(1050, 1063))
    assert errors == ()
    assert site.requests == []
    assert articles[0].consejeria == "CONSEJO DE GOBIERNO"
    assert all(a.listado_en_bome for a in articles)


def test_articulos_del_boletin_fetches_hidden_articles(site: Site, fixtures_dir: Path) -> None:
    from bome_navaja.parsers import parse_bulletin_page
    from bome_navaja.search import articulos_del_boletin

    html = _without_article((fixtures_dir / "b6416.html").read_text("utf-8"), "BOME-A-2026-1051")
    html = _without_article(html, "BOME-A-2026-1056")
    bulletin = parse_bulletin_page(html, "BOME-B-2026-6416")
    article_body = (fixtures_dir / "art1051.html").read_bytes()
    site.bulletins["BOME-B-2026-6416/articulo/1051"] = lambda: httpx.Response(200, content=article_body)
    with site.client() as client:
        articles, errors = articulos_del_boletin(client, bulletin)
    assert [a.numero for a in articles] == [n for n in range(1050, 1063) if n != 1056]
    hidden = next(a for a in articles if a.numero == 1051)
    assert hidden.listado_en_bome is False
    (error,) = errors
    assert (error.etapa, error.cve, error.error_code) == ("articulo", "BOME-A-2026-1056", "no_encontrado")


def test_articulos_del_boletin_propagates_blocking(site: Site, fixtures_dir: Path) -> None:
    from bome_navaja.models import BomeBlockedError
    from bome_navaja.parsers import parse_bulletin_page
    from bome_navaja.search import articulos_del_boletin

    html = _without_article((fixtures_dir / "b6416.html").read_text("utf-8"), "BOME-A-2026-1051")
    html = _without_article(html, "BOME-A-2026-1056")
    bulletin = parse_bulletin_page(html, "BOME-B-2026-6416")
    site.bulletins["BOME-B-2026-6416/articulo/1051"] = lambda: httpx.Response(429)
    with site.client() as client, pytest.raises(BomeBlockedError):
        articulos_del_boletin(client, bulletin)
    # The second hidden article is never requested once the site refuses.
    assert site.bulletin_paths() == ["/bome/BOME-B-2026-6416/articulo/1051"]
