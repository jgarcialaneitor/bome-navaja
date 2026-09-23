"""BomeClient tests over httpx.MockTransport; no network."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from bome_navaja import client as client_module
from bome_navaja.client import BomeClient
from bome_navaja.cve import InvalidCveError
from bome_navaja.models import (
    BomeError,
    BomeHTTPError,
    BomeNotFoundError,
    BomeParseError,
)

BASE = "https://bomemelilla.es"
Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Routes requests by path to canned responses and records them."""

    def __init__(self, fixtures_dir: Path) -> None:
        self.fixtures_dir = fixtures_dir
        self.routes: dict[str, Handler] = {}
        self.requests: list[httpx.Request] = []

    def fixture(self, path: str, name: str, content_type: str = "text/html") -> None:
        body = (self.fixtures_dir / name).read_bytes()
        self.routes[path] = lambda request: httpx.Response(
            200, content=body, headers={"content-type": content_type}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, text="Not found")
        return handler(request)

    def client(self, **kwargs: object) -> BomeClient:
        return BomeClient(transport=httpx.MockTransport(self), **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def recorder(fixtures_dir: Path) -> Recorder:
    return Recorder(fixtures_dir)


# --------------------------------------------------------------------------- basics


def test_sends_browser_user_agent(recorder: Recorder) -> None:
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    with recorder.client() as bome:
        bome.organismos(38)
    user_agent = recorder.requests[0].headers["user-agent"]
    assert user_agent.startswith("Mozilla/5.0")
    assert "python-httpx" not in user_agent
    assert str(recorder.requests[0].url).startswith(BASE)


def test_context_manager_closes(recorder: Recorder) -> None:
    with recorder.client() as bome:
        pass
    assert bome.closed


def test_default_timeout_is_30_seconds(recorder: Recorder) -> None:
    with recorder.client() as bome:
        assert bome.timeout == 30.0


# --------------------------------------------------------------------------- calendar


def test_calendar_sends_start_and_end(recorder: Recorder) -> None:
    recorder.fixture("/api/bomes/calendar", "cal.json", "application/json")
    with recorder.client() as bome:
        refs = bome.calendar(date(2026, 9, 1), date(2026, 9, 30))
    assert len(refs) == 8
    assert refs[-1].cve == "BOME-B-2026-6416"
    params = recorder.requests[0].url.params
    assert params["start"] == "2026-09-01"
    assert params["end"] == "2026-09-30"


def test_calendar_rejects_inverted_range(recorder: Recorder) -> None:
    with recorder.client() as bome, pytest.raises(ValueError):
        bome.calendar(date(2026, 9, 30), date(2026, 9, 1))
    assert recorder.requests == []


def test_all_bulletins_spans_2014_to_today(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder.fixture("/api/bomes/calendar", "cal.json", "application/json")
    monkeypatch.setattr(client_module, "_today", lambda: date(2026, 9, 23))
    with recorder.client() as bome:
        refs = bome.all_bulletins()
    assert len(refs) == 8
    params = recorder.requests[0].url.params
    assert params["start"] == "2014-01-01"
    # The site treats ``end`` as inclusive (verified live 2026-09-23).
    assert params["end"] == "2026-09-23"


def test_year_bulletins(recorder: Recorder) -> None:
    recorder.fixture("/api/bomes/2005", "y2005.html")
    with recorder.client() as bome:
        assert bome.year_bulletins(2005) == []
    assert recorder.requests[0].url.path == "/api/bomes/2005"


# --------------------------------------------------------------------------- pages


def test_bulletin(recorder: Recorder) -> None:
    recorder.fixture("/bome/BOME-B-2026-6416", "b6416.html")
    with recorder.client() as bome:
        bulletin = bome.bulletin(" bome-b-2026-6416 ")
    assert bulletin.number == 6416
    assert len(bulletin.articles) == 13
    assert recorder.requests[0].url.path == "/bome/BOME-B-2026-6416"


def test_bulletin_rejects_article_cve_without_request(recorder: Recorder) -> None:
    with recorder.client() as bome, pytest.raises(InvalidCveError):
        bome.bulletin("BOME-A-2026-1051")
    assert recorder.requests == []


def test_redirects_are_followed_for_pages(recorder: Recorder, fixtures_dir: Path) -> None:
    recorder.routes["/bome/BOME-BX-2026-41"] = lambda request: httpx.Response(
        301, headers={"location": "/bome/BOME-BX-2026-41/"}
    )
    body = (fixtures_dir / "bx41.html").read_bytes()
    recorder.routes["/bome/BOME-BX-2026-41/"] = lambda request: httpx.Response(200, content=body)
    with recorder.client() as bome:
        bulletin = bome.bulletin("BOME-BX-2026-41")
    assert bulletin.extraordinary is True
    assert len(recorder.requests) == 2


def test_sumario_accepts_bulletin_or_sumario_cve(recorder: Recorder) -> None:
    recorder.fixture("/bome/BOME-B-2026-6416/sumario", "sum6416.html")
    with recorder.client() as bome:
        first = bome.sumario("BOME-S-2026-6416")
        second = bome.sumario("BOME-B-2026-6416")
    assert first == second
    assert len(first.entries) == 13
    assert [r.url.path for r in recorder.requests] == ["/bome/BOME-B-2026-6416/sumario"] * 2


def test_article(recorder: Recorder) -> None:
    recorder.fixture("/bome/BOME-B-2026-6416/articulo/1051", "art1051.html")
    with recorder.client() as bome:
        article = bome.article("BOME-B-2026-6416", 1051)
    assert article.cve == "BOME-A-2026-1051"
    assert len(article.pages) == 4


def test_article_accepts_article_cve_as_number(recorder: Recorder) -> None:
    recorder.fixture("/bome/BOME-B-2026-6416/articulo/1051", "art1051.html")
    with recorder.client() as bome:
        article = bome.article("BOME-B-2026-6416", "BOME-A-2026-1051")
    assert article.number == 1051


def test_consejerias_and_organismos(recorder: Recorder) -> None:
    recorder.fixture("/api/section/consejerias/1", "cons1.json", "application/json")
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    with recorder.client() as bome:
        consejerias = bome.consejerias(1)
        organismos = bome.organismos(38)
    assert len(consejerias) == 128
    assert organismos[0].name == "CIUDAD AUTONOMA DE MELILLA"
    assert [r.url.path for r in recorder.requests] == [
        "/api/section/consejerias/1",
        "/api/section/organismos/38",
    ]


# --------------------------------------------------------------------------- CVE resolver


def test_resolve_cve_reads_location_and_confirms_target(recorder: Recorder) -> None:
    recorder.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "/bome/BOME-B-2026-6416/articulo/1051"}
    )
    recorder.fixture("/bome/BOME-B-2026-6416/articulo/1051", "art1051.html")
    with recorder.client() as bome:
        url = bome.resolve_cve("bome-a-2026-1051")
    assert url == f"{BASE}/bome/BOME-B-2026-6416/articulo/1051"
    # One resolver call (redirect not followed) plus one existence check.
    assert [r.url.path for r in recorder.requests] == [
        "/buscar-cve",
        "/bome/BOME-B-2026-6416/articulo/1051",
    ]
    assert recorder.requests[0].url.params["cve"] == "BOME-A-2026-1051"


def test_resolve_cve_redirect_to_missing_page_is_not_found(recorder: Recorder) -> None:
    # Live: /buscar-cve?cve=BOME-B-2099-1 redirects to /bome/BOME-B-2099-1, which is 404.
    recorder.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "/bome/BOME-B-2099-1"}
    )
    with recorder.client() as bome, pytest.raises(BomeNotFoundError) as info:
        bome.resolve_cve("BOME-B-2099-1")
    assert info.value.status == 404
    assert [r.url.path for r in recorder.requests] == ["/buscar-cve", "/bome/BOME-B-2099-1"]


def test_resolve_cve_without_redirect_is_not_found(recorder: Recorder) -> None:
    recorder.routes["/buscar-cve"] = lambda request: httpx.Response(200, text="<html></html>")
    with recorder.client() as bome, pytest.raises(BomeNotFoundError):
        bome.resolve_cve("BOME-A-2026-999999")


def test_resolve_cve_refuses_off_site_redirect(recorder: Recorder) -> None:
    recorder.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "https://evil.example/bome/BOME-B-2026-6416"}
    )
    with recorder.client() as bome, pytest.raises(BomeNotFoundError):
        bome.resolve_cve("BOME-B-2026-6416")
    # The off-site target is never requested.
    assert [r.url.path for r in recorder.requests] == ["/buscar-cve"]


def test_resolve_cve_redirect_to_home_is_not_found(recorder: Recorder) -> None:
    recorder.routes["/buscar-cve"] = lambda request: httpx.Response(302, headers={"location": "/"})
    with recorder.client() as bome, pytest.raises(BomeNotFoundError):
        bome.resolve_cve("BOME-A-2026-999999")


# --------------------------------------------------------------------------- search


def test_search_page_keeps_params_in_order_and_adds_page(recorder: Recorder) -> None:
    recorder.fixture("/buscador-avanzado", "s_pe.html")
    params = [
        ("from", "1990-01-01"),
        ("contenido[0][type]", "sumario_articulo"),
        ("contenido[0][like]", "like"),
        ("contenido[0][content]", "personal eventual"),
        ("contenido[0][operator]", "and"),
        ("contenido[1][type]", "sumario_articulo"),
        ("contenido[1][content]", "cese"),
        ("dup", "a"),
        ("dup", "b"),
    ]
    with recorder.client() as bome:
        page = bome.search_page("/buscador-avanzado", params, page=2)
    assert page.total_results == 30
    sent = recorder.requests[0].url.params.multi_items()
    assert sent == [*params, ("page", "2")]


def test_search_page_replaces_caller_page_param(recorder: Recorder) -> None:
    recorder.fixture("/buscar", "q_pe.html")
    with recorder.client() as bome:
        bome.search_page("/buscar", [("page", "9"), ("contenido", "personal eventual")])
    assert recorder.requests[0].url.params.multi_items() == [
        ("contenido", "personal eventual"),
        ("page", "1"),
    ]


def test_search_page_rejects_unknown_path(recorder: Recorder) -> None:
    with recorder.client() as bome, pytest.raises(ValueError):
        bome.search_page("/admin", [])  # type: ignore[arg-type]
    with recorder.client() as bome, pytest.raises(ValueError):
        bome.search_page("/buscar", [], page=0)
    assert recorder.requests == []


# --------------------------------------------------------------------------- downloads


def test_download_pdf(recorder: Recorder) -> None:
    recorder.routes["/bome/descargar/BOME-P-2026-4784.pdf"] = lambda request: httpx.Response(
        200, content=b"%PDF-1.7\n...", headers={"content-type": "application/pdf"}
    )
    with recorder.client() as bome:
        data = bome.download("bome-p-2026-4784")
    assert data.startswith(b"%PDF")
    assert recorder.requests[0].url.path == "/bome/descargar/BOME-P-2026-4784.pdf"


def test_download_non_pdf_raises_parse_error(recorder: Recorder) -> None:
    recorder.fixture("/bome/descargar/BOME-A-2014-2.pdf", "art2014.html")
    with recorder.client() as bome, pytest.raises(BomeParseError):
        bome.download("BOME-A-2014-2")


# --------------------------------------------------------------------------- errors


def test_404_raises_not_found(recorder: Recorder) -> None:
    with recorder.client() as bome, pytest.raises(BomeNotFoundError) as info:
        bome.bulletin("BOME-B-2026-9999")
    assert info.value.status == 404
    assert info.value.url == f"{BASE}/bome/BOME-B-2026-9999"


def test_500_raises_http_error(recorder: Recorder) -> None:
    recorder.routes["/bome/BOME-B-2026-6416"] = lambda request: httpx.Response(500)
    with recorder.client() as bome, pytest.raises(BomeHTTPError) as info:
        bome.bulletin("BOME-B-2026-6416")
    assert not isinstance(info.value, BomeNotFoundError)
    assert info.value.status == 500
    assert isinstance(info.value, BomeError)


def test_empty_page_raises_not_found(recorder: Recorder) -> None:
    recorder.routes["/bome/BOME-B-2026-6416"] = lambda request: httpx.Response(200, text="  \n")
    with recorder.client() as bome, pytest.raises(BomeNotFoundError):
        bome.bulletin("BOME-B-2026-6416")


def test_transport_error_is_wrapped(recorder: Recorder) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    recorder.routes["/bome/BOME-B-2026-6416"] = boom
    with recorder.client() as bome, pytest.raises(BomeHTTPError) as info:
        bome.bulletin("BOME-B-2026-6416")
    assert info.value.status is None


def test_call_after_close_raises_bome_error(recorder: Recorder) -> None:
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    bome = recorder.client()
    bome.close()
    with pytest.raises(BomeError):
        bome.organismos(38)


def test_malformed_redirect_location_is_wrapped(recorder: Recorder) -> None:
    recorder.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "http://[::1"}
    )
    with recorder.client() as bome, pytest.raises(BomeHTTPError) as info:
        bome.resolve_cve("BOME-B-2026-6416")
    assert info.value.status is None


def test_invalid_url_is_wrapped(recorder: Recorder) -> None:
    # httpx.InvalidURL is not an httpx.HTTPError; the public API never builds
    # such a path, so exercise the private request wrapper directly.
    with recorder.client() as bome, pytest.raises(BomeHTTPError) as info:
        bome._request("/bome/\x00")
    assert info.value.status is None
    assert recorder.requests == []


def test_wrong_page_shape_raises_parse_error(recorder: Recorder) -> None:
    recorder.fixture("/bome/BOME-B-2026-6416", "y2005.html")
    with recorder.client() as bome, pytest.raises(BomeParseError):
        bome.bulletin("BOME-B-2026-6416")


# --------------------------------------------------------------------------- politeness


def test_polite_delay_between_consecutive_requests(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    sleeps: list[float] = []
    clock = iter([100.0, 100.1, 100.1, 100.2, 100.2, 100.3])
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(clock))
    with recorder.client(polite_delay=0.5) as bome:
        bome.organismos(38)
        bome.organismos(38)
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.4)


def test_no_delay_by_default(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    with recorder.client() as bome:
        bome.organismos(38)
        bome.organismos(38)
    assert sleeps == []
