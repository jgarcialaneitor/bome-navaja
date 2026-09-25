"""BomeClient tests over httpx.MockTransport; no network."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from bome_navaja import client as client_module
from bome_navaja.client import BomeClient
from bome_navaja.cve import InvalidCveError
from bome_navaja.models import (
    BomeBlockedError,
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
        assert bome._client.timeout == httpx.Timeout(30.0)


def test_a_configured_timeout_reaches_every_request(recorder: Recorder) -> None:
    with recorder.client(timeout=4.5) as bome:
        assert bome.timeout == 4.5
        assert bome._client.timeout == httpx.Timeout(4.5)


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


@pytest.mark.parametrize(
    ("cve", "location"),
    [
        # Live 2026-09-24: the resolver drops the X of extraordinary article and page CVEs.
        ("BOME-AX-2019-103", "/bome/BOME-B-2019-5625/articulo/103"),
        ("BOME-PX-2021-362", "/bome/BOME-B-2021-5839/articulo/170#pagina-362"),
        # The symmetric contradiction is refused as well.
        ("BOME-A-2019-103", "/bome/BOME-BX-2019-24/articulo/103"),
    ],
)
@pytest.mark.parametrize("confirm", [True, False])
def test_resolve_cve_refuses_a_target_of_the_other_bulletin_kind(
    recorder: Recorder, cve: str, location: str, confirm: bool
) -> None:
    recorder.routes["/buscar-cve"] = lambda request: httpx.Response(302, headers={"location": location})
    with recorder.client() as bome, pytest.raises(client_module.ResolucionIncoherenteError) as info:
        bome.resolve_cve(cve, confirm=confirm)
    assert isinstance(info.value, BomeNotFoundError)
    assert cve in str(info.value)
    # The contradicting target is never requested.
    assert [r.url.path for r in recorder.requests] == ["/buscar-cve"]


@pytest.mark.parametrize(
    ("cve", "location"),
    [
        # Live 2026-09-24: extraordinary bulletins and sumarios resolve correctly.
        ("BOME-BX-2019-24", "/bome/BOME-BX-2019-24"),
        ("BOME-SX-2019-24", "/bome/BOME-SX-2019-24/sumario"),
        ("BOME-P-2026-4784", "/bome/BOME-B-2026-6416/articulo/1051#pagina-4784"),
    ],
)
def test_resolve_cve_accepts_targets_of_the_same_bulletin_kind(
    recorder: Recorder, cve: str, location: str
) -> None:
    recorder.routes["/buscar-cve"] = lambda request: httpx.Response(302, headers={"location": location})
    with recorder.client() as bome:
        assert bome.resolve_cve(cve, confirm=False) == BASE + location


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
    assert not isinstance(info.value, BomeBlockedError)
    assert info.value.status == 500
    assert isinstance(info.value, BomeError)


@pytest.mark.parametrize("status", [403, 429, 503])
def test_blocking_statuses_raise_blocked_error(recorder: Recorder, status: int) -> None:
    recorder.routes["/bome/BOME-B-2026-6416"] = lambda request: httpx.Response(status)
    with recorder.client() as bome, pytest.raises(BomeBlockedError) as info:
        bome.bulletin("BOME-B-2026-6416")
    assert isinstance(info.value, BomeHTTPError)
    assert not isinstance(info.value, BomeNotFoundError)
    assert info.value.status == status
    assert info.value.url == f"{BASE}/bome/BOME-B-2026-6416"
    assert info.value.retry_after is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120.0),
        (" 7 ", 7.0),
        ("-5", 0.0),
        ("Thu, 24 Sep 2026 10:01:30 GMT", 90.0),
        ("Thu, 24 Sep 2026 09:00:00 GMT", 0.0),  # already in the past
        ("soon", None),
        ("", None),
        ("nan", None),
    ],
)
def test_blocked_error_parses_retry_after(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch, header: str, expected: float | None
) -> None:
    monkeypatch.setattr(client_module, "_now", lambda: datetime(2026, 9, 24, 10, 0, 0, tzinfo=UTC))
    recorder.routes["/bome/BOME-B-2026-6416"] = lambda request: httpx.Response(
        429, headers={"retry-after": header}
    )
    with recorder.client() as bome, pytest.raises(BomeBlockedError) as info:
        bome.bulletin("BOME-B-2026-6416")
    if expected is None:
        assert info.value.retry_after is None
    else:
        assert info.value.retry_after == pytest.approx(expected)


def test_download_blocked_raises_blocked_error(recorder: Recorder) -> None:
    recorder.routes["/bome/descargar/BOME-P-2026-4784.pdf"] = lambda request: httpx.Response(
        503, headers={"retry-after": "30"}, content=b"<html>busy</html>"
    )
    with recorder.client() as bome, pytest.raises(BomeBlockedError) as info:
        bome.download("BOME-P-2026-4784")
    assert info.value.status == 503
    assert info.value.retry_after == 30.0
    assert info.value.url == f"{BASE}/bome/descargar/BOME-P-2026-4784.pdf"


def test_download_404_stays_not_found(recorder: Recorder) -> None:
    with recorder.client() as bome, pytest.raises(BomeNotFoundError) as info:
        bome.download("BOME-P-2026-4784")
    assert not isinstance(info.value, BomeBlockedError)


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


class FixedRng:
    """Injected randomness: ``uniform`` returns the queued fractions of its range."""

    def __init__(self, *fractions: float) -> None:
        self.fractions = list(fractions)
        self.calls: list[tuple[float, float]] = []

    def uniform(self, low: float, high: float) -> float:
        self.calls.append((low, high))
        return low + (high - low) * self.fractions.pop(0)


def test_jitter_is_added_to_the_polite_delay_per_request(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    sleeps: list[float] = []
    clock = iter([100.0, 100.5, 100.5, 104.0, 104.0])
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(clock))
    rng = FixedRng(0.5, 0.25)
    with recorder.client(polite_delay=2.0, jitter=1.0, rng=rng) as bome:
        bome.organismos(38)  # first request: no wait, no draw
        bome.organismos(38)  # 0.5 s elapsed of 2.0 + 0.5
        bome.organismos(38)  # 3.5 s elapsed of 2.0 + 0.25: no sleep
    assert rng.calls == [(0.0, 1.0), (0.0, 1.0)]
    assert sleeps == [pytest.approx(2.0)]


def test_jitter_alone_paces_requests(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    sleeps: list[float] = []
    clock = iter([10.0, 10.0, 10.0])
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(clock))
    with recorder.client(jitter=1.0, rng=FixedRng(0.75)) as bome:
        bome.organismos(38)
        bome.organismos(38)
    assert sleeps == [pytest.approx(0.75)]


def test_zero_jitter_never_draws_randomness(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    sleeps: list[float] = []
    clock = iter([100.0, 100.1, 100.1])
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(clock))
    rng = FixedRng()
    with recorder.client(polite_delay=0.5, rng=rng) as bome:
        assert bome.jitter == 0.0
        bome.organismos(38)
        bome.organismos(38)
    assert rng.calls == []
    assert sleeps == [pytest.approx(0.4)]


@pytest.mark.parametrize("kwargs", [{"polite_delay": -0.1}, {"jitter": -1.0}])
def test_negative_pace_is_rejected(recorder: Recorder, kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        recorder.client(**kwargs)


# --------------------------------------------------------------------------- site guard (site-guard task 3)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _guard():
    from bome_navaja.guard import GuardiaSitio

    return GuardiaSitio(None, clock=_Clock())


def test_a_guarded_client_records_every_answer(recorder: Recorder) -> None:
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    recorder.routes["/bome/BOME-B-2026-6416"] = lambda request: httpx.Response(500)
    guard = _guard()
    with recorder.client(guard=guard) as bome:
        bome.organismos(38)
        assert guard.errores_en_ventana() == 0
        with pytest.raises(BomeHTTPError):
            bome.bulletin("BOME-B-2026-6416")
        with pytest.raises(BomeNotFoundError):
            bome.bulletin("BOME-B-2026-9999")
        with pytest.raises(BomeNotFoundError):
            bome.download("BOME-P-2026-4784")
    assert guard.errores_en_ventana() == 3
    assert not guard.en_enfriamiento()


def test_a_guarded_client_refuses_without_the_network_when_the_budget_is_full(recorder: Recorder) -> None:
    from bome_navaja.models import BomePausaPreventivaError

    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    guard = _guard()
    for _ in range(3):
        guard.registrar(500)
    with recorder.client(guard=guard) as bome:
        with pytest.raises(BomePausaPreventivaError) as info:
            bome.organismos(38)
        assert info.value.retry_after == pytest.approx(600)
        assert info.value.url == f"{BASE}/api/section/organismos/38"
        with pytest.raises(BomePausaPreventivaError):
            bome.download("BOME-P-2026-4784")
    assert recorder.requests == []


def test_a_blocking_answer_closes_the_guard_and_later_calls_never_reach_the_site(
    recorder: Recorder,
) -> None:
    from bome_navaja.guard import ENFRIAMIENTO_SEGUNDOS
    from bome_navaja.models import BomePausaPreventivaError

    recorder.routes["/bome/BOME-B-2026-6416"] = lambda request: httpx.Response(
        429, headers={"retry-after": "120"}
    )
    recorder.fixture("/api/section/organismos/38", "org38.json", "application/json")
    guard = _guard()
    with recorder.client(guard=guard) as bome:
        with pytest.raises(BomeBlockedError) as info:
            bome.bulletin("BOME-B-2026-6416")
        assert info.value.status == 429
        # The caller learns how long the guard keeps the site closed, not only Retry-After.
        assert info.value.retry_after == pytest.approx(ENFRIAMIENTO_SEGUNDOS)
        assert guard.en_enfriamiento()
        for call in (lambda: bome.organismos(38), lambda: bome.download("BOME-P-2026-4784")):
            with pytest.raises(BomeBlockedError) as info:
                call()
            assert not isinstance(info.value, BomePausaPreventivaError)
            assert info.value.status is None
            assert info.value.retry_after == pytest.approx(ENFRIAMIENTO_SEGUNDOS)
    assert len(recorder.requests) == 1


def test_a_blocked_download_closes_the_guard(recorder: Recorder) -> None:
    recorder.routes["/bome/descargar/BOME-P-2026-4784.pdf"] = lambda request: httpx.Response(503)
    guard = _guard()
    with recorder.client(guard=guard) as bome, pytest.raises(BomeBlockedError):
        bome.download("BOME-P-2026-4784")
    assert guard.en_enfriamiento()


def test_transport_failures_close_the_guard(recorder: Recorder) -> None:
    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    recorder.routes["/bome/BOME-B-2026-6416"] = dropped
    recorder.routes["/bome/descargar/BOME-P-2026-4784.pdf"] = dropped
    guard = _guard()
    with recorder.client(guard=guard) as bome:
        with pytest.raises(BomeHTTPError) as info:
            bome.bulletin("BOME-B-2026-6416")
        assert info.value.status is None
        assert not guard.en_enfriamiento()
        with pytest.raises(BomeHTTPError) as info:
            bome.download("BOME-P-2026-4784")
        assert not isinstance(info.value, BomeBlockedError)
        assert guard.en_enfriamiento()
        with pytest.raises(BomeBlockedError):
            bome.bulletin("BOME-B-2026-6416")
    assert len(recorder.requests) == 2
    assert guard.errores_en_ventana() == 0


def test_an_invalid_url_is_not_a_transport_failure(recorder: Recorder) -> None:
    guard = _guard()
    with recorder.client(guard=guard) as bome:
        for _ in range(2):
            with pytest.raises(BomeHTTPError):
                bome._request("/bome/\x00")
    assert not guard.en_enfriamiento()


def test_a_successful_download_resets_the_streak(recorder: Recorder) -> None:
    recorder.fixture("/bome/descargar/BOME-P-2026-4784.pdf", "BOME-P-2026-4784.pdf", "application/pdf")
    guard = _guard()
    guard.registrar(None)
    with recorder.client(guard=guard) as bome:
        bome.download("BOME-P-2026-4784")
    guard.registrar(None)
    assert not guard.en_enfriamiento()


# --------------------------------------------------------------------------- fetch_bytes (shared with the old portal)


def test_fetch_bytes_posts_content_through_the_guard(recorder: Recorder) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"<html>ok</html>", headers={"content-type": "text/html"})

    recorder.routes["/form"] = answer
    guard = _guard()
    with recorder.client(guard=guard) as bome:
        content, response = bome.fetch_bytes(
            "POST",
            "/form",
            params={"a": "1"},
            content=b"q=x",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert content == b"<html>ok</html>" and response.status_code == 200
    (request,) = seen
    assert request.method == "POST" and request.content == b"q=x"
    assert request.url.params["a"] == "1"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"


def test_fetch_bytes_caps_the_size_and_records_errors(recorder: Recorder) -> None:
    from bome_navaja.models import BomeDocumentTooLargeError

    recorder.routes["/big"] = lambda request: httpx.Response(200, content=b"x" * 5000)
    recorder.routes["/broken"] = lambda request: httpx.Response(500)
    guard = _guard()
    with recorder.client(guard=guard) as bome:
        with pytest.raises(BomeDocumentTooLargeError) as info:
            bome.fetch_bytes("GET", "/big", max_bytes=1000)
        assert info.value.limit == 1000
        with pytest.raises(BomeHTTPError):
            bome.fetch_bytes("GET", "/broken")
    assert guard.errores_en_ventana() == 1
