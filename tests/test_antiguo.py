"""Old BOME portal on melilla.es: parsers, URL whitelist and client (MockTransport, no network)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from bome_navaja import antiguo
from bome_navaja.antiguo import (
    ORIGEN,
    PORTAL_URL,
    BoletinAmbiguoError,
    BoletinAntiguo,
    CatalogoAntiguo,
    PaginaAntigua,
    PortalAntiguo,
    UrlPdfInvalidaError,
    guardia_portal_antiguo,
    parse_busqueda,
    parse_catalogo,
    parse_ficha,
    pdf_url_valida,
    url_ficha,
)
from bome_navaja.cve import InvalidCveError
from bome_navaja.guard import FICHERO_ESTADO_MELILLA, GuardiaSitio
from bome_navaja.models import (
    BomeBlockedError,
    BomeDocumentTooLargeError,
    BomeError,
    BomeHTTPError,
    BomeNotFoundError,
    BomeParseError,
)
from bome_navaja.search import BusquedaInvalidaError

FIXTURES = Path(__file__).parent / "fixtures" / "antiguo"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(scope="module")
def catalogo() -> tuple[list[BoletinAntiguo], list[str]]:
    return parse_catalogo(fixture_bytes("listado.html"))


def by_dboid(boletines: list[BoletinAntiguo], dboid: int) -> BoletinAntiguo:
    matches = [b for b in boletines if b.dboid == dboid]
    assert len(matches) == 1, matches
    return matches[0]


def entry(dboid: int, text: str) -> str:
    return (
        '<li><a href="contenedor.jsp?seccion=ficha_bome.jsp&amp;dboidboletin='
        f'{dboid}&amp;codResi=1&amp;language=es&amp;codAdirecto=15">{text}</a></li>'
    )


def catalog_page(year: str, *entries: str) -> str:
    """A minimal catalog page with one accordion set, shaped like the real one."""
    return (
        '<html><body><div class="bandaNo"><div id="accordion3" class="accordionWrapper">'
        f'<div class="set set1"><div class="title"><img src="resid/1/img/{year}.jpg"/></div>'
        '<div class="content"><div class="cBome"><div class="c45">'
        '<div class="listado1"><a href="">Marzo</a></div><div class="listado2"><ul class="menu">'
        + "".join(entries)
        + "</ul></div></div></div></div></div></div></div></body></html>"
    )


# --------------------------------------------------------------------------- catalog


def test_catalog_counts_per_year_and_kind(catalogo: tuple[list[BoletinAntiguo], list[str]]) -> None:
    boletines, _ = catalogo
    assert len(boletines) == 476
    assert len({b.dboid for b in boletines}) == 476
    per_year = Counter(b.fecha.year for b in boletines)
    assert per_year == {2021: 35, 2016: 131, 2011: 132, 2010: 123, 1986: 54, 1985: 1}
    extras = Counter(b.fecha.year for b in boletines if b.extraordinario)
    assert extras == {2021: 16, 2016: 26, 2011: 28, 2010: 18, 1986: 4}
    assert all(b.cve.startswith("BOME-BX-") == b.extraordinario for b in boletines)
    assert all(b.origen == ORIGEN == "melilla.es" for b in boletines)


def test_catalog_newest_entry_and_the_ultimo_boletin_block_is_deduplicated(
    catalogo: tuple[list[BoletinAntiguo], list[str]],
) -> None:
    boletines, _ = catalogo
    assert boletines[0] == BoletinAntiguo(
        cve="BOME-B-2021-5842",
        numero=5842,
        extraordinario=False,
        sufijo=None,
        fecha=date(2021, 3, 12),
        dboid=267229,
        url_ficha=url_ficha(267229),
        cve_oficial=True,
    )
    assert url_ficha(267229) == (
        f"{PORTAL_URL}/contenedor.jsp?seccion=ficha_bome.jsp&dboidboletin=267229"
        "&codResi=1&language=es&codAdirecto=15"
    )
    assert PORTAL_URL == "https://www.melilla.es/melillaPortal"
    assert sum(b.dboid == 267229 for b in boletines) == 1


@pytest.mark.parametrize(
    ("dboid", "cve", "sufijo", "fecha"),
    [
        (267209, "BOME-BX-2021-16", None, date(2021, 3, 9)),  # nº Extra16
        (137149, "BOME-BX-2010-17", "17,17(II)", date(2010, 11, 20)),  # Extraordinario 17,17(II)
        (115209, "BOME-BX-2010-14", None, date(2010, 9, 6)),  # Extraordinario14
        (140729, "BOME-BX-2011-1", None, date(2011, 1, 3)),  # Extraordinario 1
        (82728, "BOME-BX-2010-2", "2, 2(II)", date(2010, 2, 20)),  # Extraordinario 2, 2(II)
        (216808, "BOME-B-2016-5302", None, date(2016, 1, 8)),  # nº 5302
        (280058, "BOME-BX-1986-1", None, date(1986, 5, 20)),  # nº Extra1
    ],
)
def test_catalog_number_variants(
    catalogo: tuple[list[BoletinAntiguo], list[str]],
    dboid: int,
    cve: str,
    sufijo: str | None,
    fecha: date,
) -> None:
    boletin = by_dboid(catalogo[0], dboid)
    assert (boletin.cve, boletin.sufijo, boletin.fecha) == (cve, sufijo, fecha)
    assert boletin.numero == int(cve.rsplit("-", 1)[1])
    assert boletin.extraordinario is cve.startswith("BOME-BX-")


def test_catalog_marks_which_identifiers_are_real_bomemelilla_cves(
    catalogo: tuple[list[BoletinAntiguo], list[str]],
) -> None:
    boletines, _ = catalogo
    assert all(b.cve_oficial is (b.fecha.year >= 2014) for b in boletines)
    assert by_dboid(boletines, 216808).cve_oficial is True
    assert by_dboid(boletines, 137149).cve_oficial is False


def test_catalog_keeps_the_1985_bulletin_filed_under_a_1927_header_and_warns(
    catalogo: tuple[list[BoletinAntiguo], list[str]],
) -> None:
    boletines, avisos = catalogo
    boletin = by_dboid(boletines, 280106)
    assert (boletin.cve, boletin.fecha) == ("BOME-B-1985-2796", date(1985, 1, 3))
    assert len(avisos) == 1
    assert "1927" in avisos[0] and "280106" in avisos[0] and "1985-01-03" in avisos[0]


def test_catalog_skips_entries_without_a_parsable_number_or_date_with_a_counted_warning() -> None:
    html = catalog_page(
        "2021",
        entry(1, "nº 5842 / 12-03-2021"),
        entry(2, "nº 5841 / sin fecha"),
        entry(3, "nº 5840 / 31-02-2021"),
        entry(4, "boletín sin número / 01-03-2021"),
    )
    boletines, avisos = parse_catalogo(html)
    assert [b.dboid for b in boletines] == [1]
    assert len(avisos) == 3
    assert all("omitido" in aviso for aviso in avisos)
    assert "dboid 2" in avisos[0] and "dboid 3" in avisos[1] and "dboid 4" in avisos[2]


def test_catalog_deduplicates_by_dboid() -> None:
    html = catalog_page("2021", entry(7, "nº 5842 / 12-03-2021"), entry(7, "nº 5842 / 12-03-2021"))
    boletines, avisos = parse_catalogo(html)
    assert [b.cve for b in boletines] == ["BOME-B-2021-5842"]
    assert avisos == []


def test_catalog_without_bulletin_links_is_a_parse_error() -> None:
    with pytest.raises(BomeParseError):
        parse_catalogo("<html><body><p>Mantenimiento</p></body></html>")


def test_catalog_lookups(catalogo: tuple[list[BoletinAntiguo], list[str]]) -> None:
    boletines, avisos = catalogo
    cat = CatalogoAntiguo(boletines=tuple(boletines), avisos=tuple(avisos), fetched_at="2026-09-24T17:00:00+00:00")
    assert [b.dboid for b in cat.por_cve(" bome-b-2016-05302 ")] == [216808]
    # Pre-2014 numbering is not unique: two "Extra1" bulletins in 1986.
    assert sorted(b.dboid for b in cat.por_cve("BOME-BX-1986-1")) == [280058, 280087]
    assert cat.por_cve("BOME-B-2099-1") == []
    assert cat.por_dboid(216808) is not None and cat.por_dboid(216808).cve == "BOME-B-2016-5302"
    assert cat.por_dboid(1) is None
    week = cat.entre(date(2016, 1, 1), date(2016, 1, 8))
    assert {"BOME-B-2016-5300", "BOME-B-2016-5301", "BOME-B-2016-5302"} <= {b.cve for b in week}
    assert all(date(2016, 1, 1) <= b.fecha <= date(2016, 1, 8) for b in week)
    assert len(cat.entre(date(1985, 1, 1), None)) == 476
    assert [b.dboid for b in cat.entre(None, date(1985, 12, 31))] == [280106]
    for bad in ("BOME-A-2016-1", "5302", "BOME-B-2016-0"):
        with pytest.raises(InvalidCveError):
            cat.por_cve(bad)


# --------------------------------------------------------------------------- search


@pytest.fixture(scope="module")
def resultados() -> list[antiguo.ArticuloAntiguo]:
    return parse_busqueda(fixture_bytes("busqueda_personal_eventual.html"))


def test_search_parses_every_result(resultados: list[antiguo.ArticuloAntiguo]) -> None:
    assert len(resultados) == 76
    years = [r.fecha.year for r in resultados if r.fecha is not None]
    assert len(years) == 76 and (min(years), max(years)) == (1991, 2020)
    assert all(r.cve_boletin and r.numero and r.paginas and r.tipo for r in resultados)


def test_search_first_result_fields(resultados: list[antiguo.ArticuloAntiguo]) -> None:
    first = resultados[0]
    assert first.cve_boletin == "BOME-B-2020-5722"
    assert first.fecha == date(2020, 1, 17)
    assert first.numero == 28
    assert first.tipo == "Notificación"
    assert first.sumario == (
        "Decreto nº 20 de fecha 17 de enero de 2020, relativo a nombramiento como personal "
        "eventual de Dª Paula Villalobos Bravo."
    )
    assert first.ruta == ("CIUDAD AUTÓNOMA DE MELILLA", "CONSEJERÍA DE PRESIDENCIA Y ADMINISTRACIÓN PÚBLICA.")
    assert first.paginas == (PaginaAntigua(47, "https://www.melilla.es/mandar.php/n/12/3334/5722_47.pdf"),)
    assert first.dboid_boletin == 257569
    assert first.url_ficha == url_ficha(257569)


def test_search_multiple_pages_and_extraordinary_bulletins(resultados: list[antiguo.ArticuloAntiguo]) -> None:
    second = resultados[1]
    assert [p.numero for p in second.paginas] == [38, 39]
    assert second.paginas[1].url_pdf == "https://www.melilla.es/mandar.php/n/12/3315/5721_39.pdf"
    cves = {r.cve_boletin for r in resultados}
    assert {"BOME-BX-2019-29", "BOME-BX-2003-9", "BOME-BX-2000-20"} <= cves
    extra26 = [r for r in resultados if r.cve_boletin == "BOME-BX-2019-26"]
    assert [r.numero for r in extra26] == [96, 97]
    assert [p.numero for p in extra26[0].paginas] == [450, 451, 452]


def test_search_rewrites_every_pdf_link_to_https(resultados: list[antiguo.ArticuloAntiguo]) -> None:
    urls = [p.url_pdf for r in resultados for p in r.paginas]
    assert len(set(urls)) == 80
    assert all(url.startswith("https://www.melilla.es/mandar.php/") for url in urls)
    assert all(pdf_url_valida(url) == url for url in urls)


def test_empty_search_is_an_empty_list() -> None:
    assert parse_busqueda(fixture_bytes("busqueda_sin_resultados.html")) == []


def test_search_on_an_unexpected_page_is_a_parse_error() -> None:
    with pytest.raises(BomeParseError):
        parse_busqueda("<html><body><p>Error 500</p></body></html>")


# --------------------------------------------------------------------------- ficha


def test_ficha_5302() -> None:
    ficha = parse_ficha(fixture_bytes("ficha_5302.html"), 216808)
    assert (ficha.cve, ficha.numero, ficha.extraordinario, ficha.sufijo) == ("BOME-B-2016-5302", 5302, False, None)
    assert ficha.fecha == date(2016, 1, 8)
    assert ficha.dboid == 216808 and ficha.url_ficha == url_ficha(216808)
    assert ficha.url_pdf == "https://www.melilla.es/mandar.php/n/9/4913/5302.pdf"
    assert ficha.cve_oficial is True and ficha.origen == "melilla.es"
    assert [a.numero for a in ficha.articulos] == [27, 28, 29, 30]
    first = ficha.articulos[0]
    assert first.ruta == (
        "CIUDAD AUTÓNOMA DE MELILLA",
        "CONSEJERÍA DE HACIENDA Y ADMINISTRACIONES PÚBLICAS",
        "Dirección General de Función Pública",
        "Personal Funcionario",
    )
    assert first.paginas == (PaginaAntigua(73, "https://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf"),)
    assert first.tipo == "Notificación"
    assert first.sumario.startswith("Resolución n.º 4328 de fecha 22 de diciembre de 2015")
    assert (first.cve_boletin, first.fecha, first.dboid_boletin) == ("BOME-B-2016-5302", date(2016, 1, 8), 216808)
    assert ficha.articulos[1].ruta[-1] == "Protección Social, Acceso y Promoción"
    assert ficha.articulos[2].ruta == (
        "CIUDAD AUTÓNOMA DE MELILLA",
        "CONSEJERÍA DE HACIENDA Y ADMINISTRACIONES PÚBLICAS",
        "Secretaría Técnica",
    )
    last = [p.numero for p in ficha.articulos[3].paginas]
    assert last[0] == 76 and last == list(range(76, 76 + len(last))) and len(last) > 3


def test_ficha_takes_the_numbering_from_the_catalog_entry(
    catalogo: tuple[list[BoletinAntiguo], list[str]],
) -> None:
    hint = by_dboid(catalogo[0], 216808)
    ficha = parse_ficha(fixture_bytes("ficha_5302.html"), 216808, boletin=hint)
    assert (ficha.cve, ficha.fecha, ficha.sufijo) == (hint.cve, hint.fecha, hint.sufijo)
    with pytest.raises(ValueError):
        parse_ficha(fixture_bytes("ficha_5302.html"), 1, boletin=hint)


def test_ficha_without_a_bulletin_header_is_a_parse_error() -> None:
    with pytest.raises(BomeParseError):
        parse_ficha('<html><body><div class="bandaNo"><p>x</p></div></body></html>', 5)


# --------------------------------------------------------------------------- PDF URL whitelist


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf", "https://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf"),
        ("https://www.melilla.es/mandar.php/n/9/4913/5302.pdf", "https://www.melilla.es/mandar.php/n/9/4913/5302.pdf"),
        ("  https://www.melilla.es/mandar.php/n/0/1235/9_452.pdf ", "https://www.melilla.es/mandar.php/n/0/1235/9_452.pdf"),
    ],
)
def test_pdf_url_valida_accepts_mandar_php_pdfs(url: str, expected: str) -> None:
    assert pdf_url_valida(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://bomemelilla.es/bome/descargar/BOME-B-2016-5302.pdf",
        "https://melilla.es/mandar.php/n/9/4913/5302.pdf",
        "https://www.melilla.es.evil.com/mandar.php/n/9/4913/5302.pdf",
        "https://www.melilla.es@evil.com/mandar.php/n/9/4913/5302.pdf",
        "https://www.melilla.es:8443/mandar.php/n/9/4913/5302.pdf",
        "https://www.melilla.es/mandar.php/n/9/4913/5302.pdf?x=1",
        "https://www.melilla.es/mandar.php/n/9/4913/5302.pdf#p",
        "https://www.melilla.es/mandar.php/n/9/../4913/5302.pdf",
        "https://www.melilla.es/mandar.php/n/9/4913/5302.html",
        "https://www.melilla.es/mandar.php/n/a/4913/5302.pdf",
        "https://www.melilla.es/melillaPortal/contenedor.jsp?seccion=bome.jsp",
        "ftp://www.melilla.es/mandar.php/n/9/4913/5302.pdf",
        "file:///etc/passwd",
    ],
)
def test_pdf_url_valida_rejects_anything_else(url: str) -> None:
    with pytest.raises(UrlPdfInvalidaError) as info:
        pdf_url_valida(url)
    assert isinstance(info.value, BomeError) and isinstance(info.value, ValueError)
    assert "mandar.php" in str(info.value)


# --------------------------------------------------------------------------- client


Handler = Callable[[httpx.Request], httpx.Response]


class Portal:
    """MockTransport routes by ``seccion`` (portal pages) or path (PDFs); records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Handler] = {}

    def page(self, key: str, name: str, content_type: str = "text/html;charset=ISO-8859-1") -> None:
        body = fixture_bytes(name)
        self.routes[key] = lambda request: httpx.Response(200, content=body, headers={"content-type": content_type})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.params.get("seccion") or request.url.path
        handler = self.routes.get(key)
        if handler is None:
            return httpx.Response(404, text="no route")
        return handler(request)

    def client(self, **kwargs: object) -> PortalAntiguo:
        kwargs.setdefault("guard", GuardiaSitio(None, sitio="melilla.es"))
        kwargs.setdefault("cache_path", None)
        kwargs.setdefault("polite_delay", 0.0)
        kwargs.setdefault("jitter", 0.0)
        return PortalAntiguo(transport=httpx.MockTransport(self), **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def portal() -> Portal:
    return Portal()


def test_client_defaults() -> None:
    with PortalAntiguo(cache_path=None) as client:
        assert client.polite_delay == 1.0
        assert client.jitter > 0
        assert client.guard.path is not None and client.guard.path.name == FICHERO_ESTADO_MELILLA
        assert client.guard.sitio == "melilla.es"
    assert client.closed


def test_guardia_portal_antiguo_lives_in_the_data_folder() -> None:
    guard = guardia_portal_antiguo()
    assert guard.path is not None and guard.path.name == "estado_sitio_melilla.json"
    assert guard.sitio == "melilla.es"


def test_search_posts_a_latin1_form(portal: Portal) -> None:
    portal.page("busqueda_bome.jsp", "busqueda_personal_eventual.html")
    with portal.client() as client:
        results = client.buscar("  personal   eventual  ")
    assert len(results) == 76
    (request,) = portal.requests
    assert request.method == "POST"
    assert request.url.scheme == "https" and request.url.host == "www.melilla.es"
    assert request.url.path == "/melillaPortal/contenedor.jsp"
    assert dict(request.url.params) == {
        "seccion": "busqueda_bome.jsp",
        "codResi": "1",
        "language": "es",
        "codAdirecto": "15",
    }
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.content == b"textobome=personal+eventual"


def test_search_encodes_non_ascii_text_in_latin1(portal: Portal) -> None:
    portal.page("busqueda_bome.jsp", "busqueda_sin_resultados.html")
    with portal.client() as client:
        assert client.buscar("año prórroga") == []
    body = portal.requests[0].content
    assert body == b"textobome=a%F1o+pr%F3rroga"
    assert parse_qs(body.decode("ascii"), encoding="latin-1") == {"textobome": ["año prórroga"]}


@pytest.mark.parametrize("texto", ["", "   ", "ab", " ab ", "euro €", "x" * 201])
def test_search_rejects_invalid_text_without_a_request(portal: Portal, texto: str) -> None:
    with portal.client() as client, pytest.raises(BusquedaInvalidaError):
        client.buscar(texto)
    assert portal.requests == []


def test_the_guard_is_consulted_and_records_outcomes(portal: Portal) -> None:
    portal.routes["busqueda_bome.jsp"] = lambda request: httpx.Response(500, text="boom")
    guard = GuardiaSitio(None, sitio="melilla.es")
    with portal.client(guard=guard) as client:
        with pytest.raises(BomeHTTPError) as info:
            client.buscar("personal eventual")
        assert info.value.status == 500
        assert guard.errores_en_ventana() == 1
        guard.registrar(403)
        with pytest.raises(BomeBlockedError) as blocked:
            client.buscar("personal eventual")
    assert len(portal.requests) == 1
    assert blocked.value.status is None
    assert "melilla.es" in str(blocked.value) and "bomemelilla.es" not in str(blocked.value)


def test_pages_are_decoded_as_latin1_even_without_a_charset(portal: Portal) -> None:
    portal.page("busqueda_bome.jsp", "busqueda_personal_eventual.html", content_type="text/html")
    with portal.client() as client:
        results = client.buscar("personal eventual")
    assert results[0].ruta[0] == "CIUDAD AUTÓNOMA DE MELILLA"


def test_search_response_size_is_capped(portal: Portal, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(antiguo, "MAX_RESPUESTA_BYTES", 10_000)
    portal.page("busqueda_bome.jsp", "busqueda_personal_eventual.html")
    with portal.client() as client, pytest.raises(BomeDocumentTooLargeError) as info:
        client.buscar("personal eventual")
    assert info.value.limit == 10_000
    assert "más concreto" in str(info.value)


def test_ficha_by_dboid(portal: Portal) -> None:
    portal.page("ficha_bome.jsp", "ficha_5302.html")
    with portal.client() as client:
        ficha = client.ficha(216808)
    assert ficha.cve == "BOME-B-2016-5302" and len(ficha.articulos) == 4
    (request,) = portal.requests
    assert request.method == "GET"
    assert dict(request.url.params) == {
        "seccion": "ficha_bome.jsp",
        "dboidboletin": "216808",
        "codResi": "1",
        "language": "es",
        "codAdirecto": "15",
    }


@pytest.mark.parametrize("dboid", [0, -3, "12a", True])
def test_ficha_rejects_invalid_dboids(portal: Portal, dboid: object) -> None:
    with portal.client() as client, pytest.raises(ValueError):
        client.ficha(dboid)  # type: ignore[arg-type]
    assert portal.requests == []


def test_ficha_by_cve_uses_the_catalog(portal: Portal) -> None:
    portal.page("bome.jsp", "listado.html")
    portal.page("ficha_bome.jsp", "ficha_5302.html")
    with portal.client() as client:
        ficha = client.ficha_por_cve("BOME-B-2016-5302")
        assert ficha.dboid == 216808 and ficha.cve == "BOME-B-2016-5302"
        with pytest.raises(BoletinAmbiguoError) as ambiguous:
            client.ficha_por_cve("BOME-BX-1986-1")
        assert {b.dboid for b in ambiguous.value.candidatos} == {280058, 280087}
        assert "280058" in str(ambiguous.value) and "280087" in str(ambiguous.value)
        with pytest.raises(BomeNotFoundError):
            client.ficha_por_cve("BOME-B-2099-1")
    seccions = [r.url.params["seccion"] for r in portal.requests]
    assert seccions == ["bome.jsp", "ficha_bome.jsp"]


def test_catalog_request_shape(portal: Portal) -> None:
    portal.page("bome.jsp", "listado.html", content_type="text/html")
    with portal.client() as client:
        cat = client.catalogo()
    assert len(cat.boletines) == 476 and len(cat.avisos) == 1 and cat.desde_cache is False
    (request,) = portal.requests
    assert request.method == "GET"
    assert dict(request.url.params) == {
        "seccion": "bome.jsp",
        "language": "es",
        "codResi": "1",
        "layout": "contenedor.jsp",
        "codAdirecto": "15",
    }


def test_catalog_cache_round_trip(portal: Portal, tmp_path: Path) -> None:
    cache = tmp_path / "datos" / antiguo.FICHERO_CATALOGO
    assert antiguo.FICHERO_CATALOGO == "catalogo_portal_antiguo.json"
    portal.page("bome.jsp", "listado.html")
    with portal.client(cache_path=cache) as client:
        first = client.catalogo()
        assert client.catalogo() is first  # memory, no second request
    assert len(portal.requests) == 1
    stored = json.loads(cache.read_text("utf-8"))
    assert stored["version"] == 1 and stored["fetched_at"] == first.fetched_at
    assert len(stored["boletines"]) == 476

    offline = Portal()  # a fresh process: no routes, every request would 404
    with offline.client(cache_path=cache) as client:
        cached = client.catalogo()
    assert offline.requests == []
    assert cached.desde_cache is True
    assert cached.boletines == first.boletines and cached.avisos == first.avisos
    assert cached.fetched_at == first.fetched_at


def test_catalog_refrescar_fetches_again(portal: Portal, tmp_path: Path) -> None:
    cache = tmp_path / antiguo.FICHERO_CATALOGO
    portal.page("bome.jsp", "listado.html")
    with portal.client(cache_path=cache) as client:
        client.catalogo()
        refreshed = client.catalogo(refrescar=True)
    assert len(portal.requests) == 2 and refreshed.desde_cache is False
    with portal.client(cache_path=cache) as client:
        client.catalogo(refrescar=True)
    assert len(portal.requests) == 3


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 1, "fetched_at": "x", "boletines": [{"cve": "BOME-B-2016-5302"}], "avisos": []}),
        json.dumps({"version": 99, "fetched_at": "x", "boletines": [], "avisos": []}),
        json.dumps({"version": 1, "fetched_at": "x", "boletines": [], "avisos": []}),
    ],
)
def test_a_corrupt_cache_is_refetched_and_rewritten(portal: Portal, tmp_path: Path, content: str) -> None:
    cache = tmp_path / antiguo.FICHERO_CATALOGO
    cache.write_text(content, "utf-8")
    portal.page("bome.jsp", "listado.html")
    with portal.client(cache_path=cache) as client:
        cat = client.catalogo()
    assert len(portal.requests) == 1 and len(cat.boletines) == 476
    assert len(json.loads(cache.read_text("utf-8"))["boletines"]) == 476


def test_an_unwritable_cache_keeps_the_catalog_in_memory(
    portal: Portal, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", "utf-8")
    cache = blocker / antiguo.FICHERO_CATALOGO  # parent is a file: cannot be written
    portal.page("bome.jsp", "listado.html")
    with portal.client(cache_path=cache) as client:
        assert len(client.catalogo().boletines) == 476
        assert len(client.catalogo().boletines) == 476
    assert len(portal.requests) == 1
    assert "catalog" in capsys.readouterr().err


def test_ficha_uses_a_cached_catalog_entry_without_fetching_the_catalog(portal: Portal, tmp_path: Path) -> None:
    cache = tmp_path / antiguo.FICHERO_CATALOGO
    portal.page("bome.jsp", "listado.html")
    portal.page("ficha_bome.jsp", "ficha_5302.html")
    with portal.client(cache_path=cache) as client:
        client.catalogo()
    with portal.client(cache_path=cache) as client:
        ficha = client.ficha(216808)
    assert ficha.cve == "BOME-B-2016-5302"
    assert [r.url.params["seccion"] for r in portal.requests] == ["bome.jsp", "ficha_bome.jsp"]


def test_pdf_download_uses_https_and_checks_the_signature(portal: Portal) -> None:
    pdf = fixture_bytes("5302_73.pdf")
    portal.routes["/mandar.php/n/9/4914/5302_73.pdf"] = lambda request: httpx.Response(
        200, content=pdf, headers={"content-type": "application/pdf"}
    )
    with portal.client() as client:
        data = client.pdf("http://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf")
    assert data == pdf and data.startswith(b"%PDF")
    (request,) = portal.requests
    assert str(request.url) == "https://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf"


def test_pdf_text_is_extractable() -> None:
    import io

    from pypdf import PdfReader

    text = PdfReader(io.BytesIO(fixture_bytes("5302_73.pdf"))).pages[0].extract_text()
    assert "5302" in text and "4328" in text


def test_pdf_download_rejects_non_pdf_bodies(portal: Portal) -> None:
    portal.routes["/mandar.php/n/9/4914/5302_73.pdf"] = lambda request: httpx.Response(
        200, content=b"<html>error</html>", headers={"content-type": "text/html"}
    )
    with portal.client() as client, pytest.raises(BomeParseError):
        client.pdf("https://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf")


def test_pdf_download_rejects_foreign_urls_without_a_request(portal: Portal) -> None:
    with portal.client() as client, pytest.raises(UrlPdfInvalidaError):
        client.pdf("https://evil.example/mandar.php/n/9/4914/5302_73.pdf")
    assert portal.requests == []


def test_pdf_download_is_size_capped(portal: Portal) -> None:
    pdf = fixture_bytes("5302_73.pdf")
    portal.routes["/mandar.php/n/9/4914/5302_73.pdf"] = lambda request: httpx.Response(
        200, content=pdf, headers={"content-type": "application/pdf"}
    )
    with portal.client() as client, pytest.raises(BomeDocumentTooLargeError):
        client.pdf("https://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf", max_bytes=1000)


# --------------------------------------------------------------------------- catalog cache status (task 7)


def test_estado_catalogo_reads_the_cache_without_network(portal: Portal, tmp_path: Path) -> None:
    cache = tmp_path / antiguo.FICHERO_CATALOGO
    assert antiguo.estado_catalogo(cache) == {"ruta": str(cache), "existe": False, "fetched_at": None, "boletines": None}
    portal.page("bome.jsp", "listado.html")
    with portal.client(cache_path=cache) as client:
        cat = client.catalogo()
    assert antiguo.estado_catalogo(cache) == {
        "ruta": str(cache),
        "existe": True,
        "fetched_at": cat.fetched_at,
        "boletines": 476,
    }
    cache.write_text("[]", "utf-8")
    broken = antiguo.estado_catalogo(cache)
    assert broken["existe"] is True and broken["boletines"] is None and broken["error"]
    assert antiguo.estado_catalogo(None) == {"ruta": None, "existe": False, "fetched_at": None, "boletines": None}


# --------------------------------------------------------------------------- index keys (old-portal-index task 1)


def test_bulletin_key_is_the_cve_unless_it_repeats(catalogo: tuple[list[BoletinAntiguo], list[str]]) -> None:
    boletines, _ = catalogo
    unique = by_dboid(boletines, 216808)
    assert antiguo.clave_boletin_antiguo(unique, False) == "BOME-B-2016-5302"
    first, second = (by_dboid(boletines, dboid) for dboid in (280058, 280087))
    assert antiguo.clave_boletin_antiguo(first, True) == "BOME-BX-1986-1~280058"
    assert antiguo.clave_boletin_antiguo(second, True) == "BOME-BX-1986-1~280087"


def test_catalog_knows_which_identifiers_repeat_and_their_keys(
    catalogo: tuple[list[BoletinAntiguo], list[str]],
) -> None:
    boletines, avisos = catalogo
    cat = CatalogoAntiguo(boletines=tuple(boletines), avisos=tuple(avisos), fetched_at="2026-09-24T17:00:00+00:00")
    assert cat.cves_repetidos() == frozenset({"BOME-BX-1986-1"})
    claves = cat.claves()
    assert len(claves) == len(boletines) == len(set(claves.values()))
    assert claves[216808] == "BOME-B-2016-5302"
    assert claves[280058] == "BOME-BX-1986-1~280058"
    assert claves[280087] == "BOME-BX-1986-1~280087"
    assert cat.clave(by_dboid(boletines, 280087)) == "BOME-BX-1986-1~280087"
    assert cat.clave(by_dboid(boletines, 279997)) == "BOME-B-1986-2899"


def _article(numero: int | None, sumario: str = "x", ruta: tuple[str, ...] = ("A",)) -> antiguo.ArticuloAntiguo:
    return antiguo.ArticuloAntiguo(
        cve_boletin="BOME-B-1999-3660", fecha=date(1999, 12, 30), numero=numero, tipo=None,
        sumario=sumario, ruta=ruta, paginas=(),
    )


def test_article_keys_are_synthetic_and_duplicates_are_suffixed_in_page_order() -> None:
    articles = [_article(7), _article(8), _article(7), _article(None), _article(7), _article(None)]
    assert antiguo.claves_articulos_antiguos(4242, articles) == [
        "MEL-4242-7", "MEL-4242-8", "MEL-4242-7-2", "MEL-4242-0", "MEL-4242-7-3", "MEL-4242-0-2",
    ]
    assert antiguo.claves_articulos_antiguos(4242, []) == []


BOLETIN_1999 = BoletinAntiguo(
    cve="BOME-B-1999-3660", numero=3660, extraordinario=False, sufijo=None, fecha=date(1999, 12, 30),
    dboid=276000, url_ficha=url_ficha(276000), cve_oficial=False,
)


def test_ficha_articles_map_onto_index_rows() -> None:
    ficha = parse_ficha(fixture_bytes("ficha_1999_3660.html"), 276000, boletin=BOLETIN_1999)
    rows = antiguo.articulos_para_indice(BOLETIN_1999, ficha, "BOME-B-1999-3660")
    assert len(rows) == len(ficha.articulos) == 73
    first = rows[0]
    assert (first.bome_cve, first.bome_numero, first.bome_fecha, first.bome_extraordinario) == (
        "BOME-B-1999-3660", 3660, date(1999, 12, 30), False,
    )
    assert (first.cve, first.numero) == ("MEL-276000-3260", 3260)
    assert first.sumario == "Renovación de suscripciones al Boletín Oficial de la Ciudad para el ano 2000."
    assert (first.departamento, first.consejeria, first.organismo) == (
        "CIUDAD AUTÓNOMA DE MELILLA", "Presidencia (Boletín Oficial)", "",
    )
    assert first.url == url_ficha(276000)
    assert first.pdf_url == "https://www.melilla.es/mandar.php/n/14/7953/3660_3119.pdf"
    assert first.listado_en_bome is True
    assert len({row.cve for row in rows}) == 73


def test_index_rows_join_deeper_headings_into_organismo_and_tolerate_missing_ones() -> None:
    ficha = parse_ficha(fixture_bytes("ficha_5302.html"), 216808)
    boletin = BoletinAntiguo(
        cve=ficha.cve, numero=ficha.numero, extraordinario=False, sufijo=None, fecha=ficha.fecha,
        dboid=216808, url_ficha=url_ficha(216808), cve_oficial=True,
    )
    first = antiguo.articulos_para_indice(boletin, ficha, ficha.cve)[0]
    assert (first.departamento, first.consejeria, first.organismo) == (
        "CIUDAD AUTÓNOMA DE MELILLA",
        "CONSEJERÍA DE HACIENDA Y ADMINISTRACIONES PÚBLICAS",
        "Dirección General de Función Pública / Personal Funcionario",
    )
    bare = antiguo.FichaAntigua(
        cve=ficha.cve, numero=ficha.numero, extraordinario=False, sufijo=None, fecha=ficha.fecha,
        dboid=216808, url_ficha=url_ficha(216808), cve_oficial=True, url_pdf=None,
        articulos=(_article(None, "Sin encabezados", ruta=()),),
    )
    only = antiguo.articulos_para_indice(boletin, bare, ficha.cve)[0]
    assert (only.departamento, only.consejeria, only.organismo, only.pdf_url, only.numero) == ("", "", "", None, 0)
    with pytest.raises(ValueError):
        antiguo.articulos_para_indice(boletin, bare, "BOME-B-2016-9999")
