"""MCP server tests: tool registry, contract, error mapping, state and stdio."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest

from bome_navaja import __version__
from bome_navaja import server as srv
from bome_navaja.antiguo import FICHERO_CATALOGO, PortalAntiguo
from bome_navaja.client import BomeClient
from bome_navaja.guard import ENFRIAMIENTO_SEGUNDOS, FICHERO_ESTADO, FICHERO_ESTADO_MELILLA, GuardiaSitio
from bome_navaja.sync import SincronizadorIndice

FIXTURES = Path(__file__).parent / "fixtures"
ANTIGUO = FIXTURES / "antiguo"
BASE = "https://bomemelilla.es"
OLD_PDF = "https://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf"

EXPECTED_TOOLS = {
    "listar_bomes",
    "ver_bome",
    "ver_sumario",
    "leer_articulo",
    "leer_boletin",
    "leer_pdf",
    "descargar_pdf",
    "buscar_bomes",
    "buscar_articulos",
    "listar_consejerias",
    "listar_organismos",
    "resolver_cve",
    "buscar_en_indice",
    "estado_indice",
    "sincronizar_indice",
    "cancelar_sincronizacion",
    "estado_servidor",
    "buscar_bome_antiguo",
    "ver_bome_antiguo",
}


def results_html(cves: list[tuple[str, str]]) -> str:
    links = "".join(f'<li><a href="/bome/{cve}">BOME Nº {cve.rsplit("-", 1)[1]} del {day}</a></li>' for cve, day in cves)
    return (
        '<div class="page-search-result"><p class="lead">Página 1 de 1. Mostrando '
        f"{len(cves)} elementos de un total de {len(cves)} elementos.</p><ul>{links}</ul></div>"
    )


class Site:
    """MockTransport router over the captured fixtures."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.paces: list[dict[str, float]] = []
        self.guards: list[GuardiaSitio | None] = []
        self.lock = threading.Lock()
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        for path, name in {
            "/api/bomes/calendar": "cal.json",
            "/bome/BOME-B-2026-6416": "b6416.html",
            "/bome/BOME-BX-2026-41": "bx41.html",
            "/bome/BOME-B-2014-5092": "b5092.html",
            "/bome/BOME-B-2026-6416/sumario": "sum6416.html",
            "/bome/BOME-B-2026-6416/articulo/1051": "art1051.html",
            "/bome/BOME-B-2014-5092/articulo/2": "art2014.html",
            "/api/section/consejerias/1": "cons1.json",
            "/api/section/organismos/38": "org38.json",
            "/bome/descargar/BOME-P-2026-4784.pdf": "BOME-P-2026-4784.pdf",
            "/bome/descargar/BOME-A-2026-1050.pdf": "BOME-A-2026-1050.pdf",
        }.items():
            self.fixture(path, name)
        self.routes["/buscar-cve"] = lambda request: httpx.Response(
            302, headers={"location": "/bome/BOME-B-2026-6416/articulo/1051"}
        )

        def search(request: httpx.Request) -> httpx.Response:
            if "contenido[0][content]" in request.url.params and request.url.params.get(
                "contenido[0][content]"
            ) == "relacion provisional":
                html = results_html([("BOME-B-2026-6416", "martes, 22 de septiembre de 2026"),
                                     ("BOME-BX-2026-41", "viernes, 18 de septiembre de 2026")])
                return httpx.Response(200, text=html)
            return httpx.Response(200, content=(FIXTURES / "s_pe.html").read_bytes())

        self.routes["/buscador-avanzado"] = search

    def fixture(self, path: str, name: str) -> None:
        body = (FIXTURES / name).read_bytes()
        self.routes[path] = lambda request: httpx.Response(200, content=body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
        handler = self.routes.get(request.url.path)
        return handler(request) if handler else httpx.Response(404, text="Not found")

    def factory(self, *, guard: GuardiaSitio | None = None, **pace: float) -> BomeClient:
        """Records the requested pace (empty for interactive clients) but never waits."""
        self.paces.append(pace)
        self.guards.append(guard)
        return BomeClient(transport=httpx.MockTransport(self), polite_delay=0, guard=guard)


class FakeTime:
    """Wall clock of the server's guard; the sync's waits move it instead of sleeping."""

    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        self.now += seconds
        return False


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    target = tmp_path / "datos"
    monkeypatch.setenv("BOME_NAVAJA_DATA_DIR", str(target))
    monkeypatch.delenv("BOME_NAVAJA_PDF_DIR", raising=False)
    monkeypatch.delenv("BOME_NAVAJA_SYNC_DELAY", raising=False)
    monkeypatch.delenv("BOME_NAVAJA_SYNC_MAX_BOLETINES", raising=False)
    srv.close_shared_state()
    yield target
    srv.close_shared_state()


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    """The server's guard and sync run on a fake clock: no test ever really waits."""
    fake = FakeTime()
    monkeypatch.setattr(srv, "GuardiaSitio", functools.partial(GuardiaSitio, clock=fake.clock))
    monkeypatch.setattr(srv, "SincronizadorIndice", functools.partial(SincronizadorIndice, wait=fake.wait))
    return fake


@pytest.fixture
def site(data_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch) -> Site:
    fake = Site()
    monkeypatch.setattr(srv, "_client_factory", fake.factory)
    return fake


def ok(result: dict) -> dict:
    assert result["ok"] is True, result
    json.dumps(result)
    return result


def fail(result: dict, code: str) -> dict:
    assert result["ok"] is False, result
    assert result["error_code"] == code, result
    assert isinstance(result["error"], str) and result["error"]
    json.dumps(result)
    return result


# --------------------------------------------------------------------------- registry & descriptions


def tools_by_name() -> dict:
    return {tool.name: tool for tool in asyncio.run(srv.server.list_tools())}


def test_tools_are_registered_with_expected_names() -> None:
    assert set(tools_by_name()) == EXPECTED_TOOLS


def test_descriptions_guide_the_model() -> None:
    tools = tools_by_name()
    assert "ver_bome" in tools["ver_sumario"].description
    for name in ("leer_articulo", "leer_boletin", "leer_pdf"):
        assert "siguiente" in tools[name].description, name
    sync = tools["sincronizar_indice"].description
    assert "20" in sync and "estado_indice" in sync and "segundo plano" in sync
    assert "max_boletines" in sync and "varias" in sync
    assert "0,6 s" not in sync and "20-25" not in (srv.server.instructions or "")
    assert "sincronizar_indice" in tools["buscar_en_indice"].description
    assert "coincidencia" in tools["buscar_en_indice"].description
    assert all(tools[name].description for name in EXPECTED_TOOLS)


def test_server_instructions_route_the_model() -> None:
    text = srv.server.instructions or ""
    for needle in ("buscar_en_indice", "buscar_articulos", "leer_", "contenido", "cese", "CVE", "2016"):
        assert needle in text, needle
    assert srv.server.version == __version__


# --------------------------------------------------------------------------- success shapes


def test_listar_bomes(site: Site) -> None:
    result = ok(srv.listar_bomes("01/09/2026", "2026-09-30"))
    assert result["total"] == 8
    assert result["truncado"] is False
    assert result["bomes"][0]["cve"] == "BOME-B-2026-6416"  # newest first
    assert (result["desde"], result["hasta"]) == ("2026-09-01", "2026-09-30")
    params = site.requests[0].url.params
    assert (params["start"], params["end"]) == ("2026-09-01", "2026-09-30")


def test_listar_bomes_truncates(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "MAX_LISTADO", 3)
    result = ok(srv.listar_bomes("2026-09-01", "2026-09-30"))
    assert result["total"] == 8
    assert len(result["bomes"]) == 3
    assert result["truncado"] is True


def test_ver_bome(site: Site) -> None:
    result = ok(srv.ver_bome("bome-b-2026-6416"))
    assert result["cve"] == "BOME-B-2026-6416"
    assert result["number"] == 6416  # bulletin models keep their task-2 English keys
    assert result["total_articulos"] == 13
    assert result["sections"][0]["consejerias"][0]["name"] == "CONSEJO DE GOBIERNO"


def test_ver_bome_can_recover_hidden_articles(site: Site) -> None:
    result = ok(srv.ver_bome("BOME-B-2026-6416", recuperar_ocultos=True))
    assert result["articulos_ocultos"] == []
    assert result["errores_ocultos"] == []


def test_ver_sumario(site: Site) -> None:
    result = ok(srv.ver_sumario("BOME-S-2026-6416"))
    assert len(result["entries"]) == 13
    assert "aviso" not in result


def test_ver_sumario_free_form_page_warns(site: Site) -> None:
    html = (FIXTURES / "sum6416.html").read_text("utf-8").replace("sumario-articulo", "otro")
    site.routes["/bome/BOME-B-2026-6415/sumario"] = lambda request: httpx.Response(200, text=html)
    result = ok(srv.ver_sumario("BOME-B-2026-6415"))
    assert result["entries"] == []
    assert "ver_bome" in result["aviso"]


def test_leer_articulo_by_bulletin_and_number(site: Site) -> None:
    result = ok(srv.leer_articulo("BOME-B-2026-6416", 1051, max_caracteres=5000))
    assert result["cve"] == "BOME-A-2026-1051"
    assert result["fuente"] == "html"
    assert result["siguiente"] == {"desde_pagina": 2, "desde_caracter": 0}
    assert result["paginas"][0]["pagina_bome"] == 4784


def test_leer_articulo_by_article_cve(site: Site) -> None:
    result = ok(srv.leer_articulo("BOME-A-2026-1051"))
    assert result["metadatos"]["bome_cve"] == "BOME-B-2026-6416"


def test_leer_articulo_2014_stub(site: Site) -> None:
    result = ok(srv.leer_articulo("BOME-B-2014-5092", 2))
    assert result["fuente"] == "ninguna"
    assert result["aviso"]


def test_leer_pdf_and_descargar_pdf(site: Site, data_dir: Path) -> None:
    download = ok(srv.descargar_pdf("BOME-P-2026-4784"))
    assert download["ruta"] == str(data_dir / "pdfs" / "BOME-P-2026-4784.pdf")
    assert download["total_paginas"] == 1
    assert download["cache_hit"] is False
    reading = ok(srv.leer_pdf("BOME-P-2026-4784"))
    assert reading["fuente"] == "pdf"
    assert reading["completo"] is True
    assert reading["siguiente"] is None


def test_leer_boletin(site: Site) -> None:
    from test_documents import make_pdf

    pdf = make_pdf(["A" * 600, "B" * 600])
    site.routes["/bome/descargar/BOME-B-2026-6416.pdf"] = lambda request: httpx.Response(200, content=pdf)
    result = ok(srv.leer_boletin("BOME-B-2026-6416", max_caracteres=1000))
    assert result["metadatos"]["numero"] == 6416
    assert result["siguiente"] == {"desde_pagina": 2, "desde_caracter": 0}


def test_buscar_bomes(site: Site) -> None:
    result = ok(srv.buscar_bomes(texto="personal eventual", desde="01/01/2020"))
    assert result["total_bomes"] == 30
    assert result["consulta"]["desde"] == "2020-01-01"
    params = site.requests[0].url.params
    assert params["from"] == "2020-01-01"


def test_buscar_articulos(site: Site) -> None:
    result = ok(srv.buscar_articulos(texto="relacion provisional"))
    assert [a["numero"] for a in result["articulos"]] == [1055, 1056, 1057, 1058, 1059]
    assert result["bomes_revisados"] == 2


def test_listar_consejerias_y_organismos(site: Site) -> None:
    consejerias = ok(srv.listar_consejerias(1))
    assert consejerias["total"] == 128
    assert {"id": 18, "name": "CONSEJO DE GOBIERNO"} in consejerias["consejerias"]
    organismos = ok(srv.listar_organismos(38))
    assert organismos["organismos"] == [{"id": 91, "name": "CIUDAD AUTONOMA DE MELILLA"}]


def test_resolver_cve(site: Site) -> None:
    result = ok(srv.resolver_cve("BOME-A-2026-1051"))
    assert result == {"ok": True, "cve": "BOME-A-2026-1051", "url": f"{BASE}/bome/BOME-B-2026-6416/articulo/1051"}


def extraordinary_2019(site: Site) -> None:
    """The live resolver bug plus a 2019 calendar whose BX-24 holds AX-2019-103."""
    from test_documents import extraordinary_year, four_per_bulletin, serve_article, site_bug_resolver

    site_bug_resolver(site)  # type: ignore[arg-type]
    extraordinary_year(site, 2019, 30, four_per_bulletin)  # type: ignore[arg-type]
    serve_article(site, "BOME-AX-2019-103", "BOME-BX-2019-24")  # type: ignore[arg-type]


def index_bx24(data_dir: Path) -> None:
    """An index file where AX-2019-103 belongs to BX-2019-24."""
    from bome_navaja.index import SumarioIndex
    from bome_navaja.models import BulletinRef
    from bome_navaja.search import ArticuloEncontrado

    ref = BulletinRef("BOME-BX-2019-24", 24, date(2019, 6, 1), True, f"{BASE}/bome/BOME-BX-2019-24")
    article = ArticuloEncontrado(
        bome_cve=ref.cve, bome_numero=24, bome_fecha=ref.date, bome_extraordinario=True,
        cve="BOME-AX-2019-103", numero=103, sumario="Decreto", departamento="D", consejeria="C",
        organismo="O", url=f"{BASE}/bome/BOME-BX-2019-24/articulo/103", pdf_url=None,
    )
    index = SumarioIndex(data_dir / "sumarios.sqlite3")
    try:
        index.guardar_boletin(ref, [article], "indexado")
    finally:
        index.close()


def test_leer_articulo_ax_is_not_the_ordinary_article_of_the_resolver(site: Site) -> None:
    extraordinary_2019(site)
    result = ok(srv.leer_articulo("BOME-AX-2019-103"))
    assert result["cve"] == "BOME-AX-2019-103"
    assert result["metadatos"]["bome_cve"] == "BOME-BX-2019-24"
    assert "/bome/BOME-B-2019-5625/articulo/103" not in [r.url.path for r in site.requests]


def test_resolver_cve_ax_gives_the_extraordinary_article(site: Site) -> None:
    extraordinary_2019(site)
    result = ok(srv.resolver_cve("bome-ax-2019-103"))
    assert (result["cve"], result["url"]) == ("BOME-AX-2019-103", f"{BASE}/bome/BOME-BX-2019-24/articulo/103")
    assert "/bome/BOME-B-2019-5625/articulo/103" not in [r.url.path for r in site.requests]


def test_ax_tools_use_the_local_index_when_it_knows_the_article(site: Site, data_dir: Path) -> None:
    extraordinary_2019(site)
    index_bx24(data_dir)
    reading = ok(srv.leer_articulo("BOME-AX-2019-103"))
    resolved = ok(srv.resolver_cve("BOME-AX-2019-103"))
    assert reading["metadatos"]["bome_cve"] == "BOME-BX-2019-24"
    assert resolved["url"] == f"{BASE}/bome/BOME-BX-2019-24/articulo/103"
    assert [r.url.path for r in site.requests] == ["/bome/BOME-BX-2019-24/articulo/103"] * 2


def test_resolver_cve_px_gives_the_pdf_without_the_site_resolver(site: Site) -> None:
    result = ok(srv.resolver_cve("BOME-PX-2021-362"))
    assert result["cve"] == "BOME-PX-2021-362"
    assert result["url"] == f"{BASE}/bome/descargar/BOME-PX-2021-362.pdf"
    assert "extraordinari" in result["aviso"] and "ordinari" in result["aviso"]
    assert site.requests == []


def test_resolver_cve_never_returns_a_target_of_the_other_bulletin_kind(site: Site) -> None:
    site.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "/bome/BOME-B-2019-5625/sumario"}
    )
    result = fail(srv.resolver_cve("BOME-SX-2019-24"), "no_encontrado")
    assert "BOME-SX-2019-24" in result["error"]
    assert [r.url.path for r in site.requests] == ["/buscar-cve"]


def test_reading_tool_docs_explain_the_extraordinary_resolution() -> None:
    tools = tools_by_name()
    for name in ("leer_articulo", "resolver_cve"):
        assert "BOME-AX" in tools[name].description, name
    assert "BOME-PX" in tools["resolver_cve"].description


# --------------------------------------------------------------------------- index tools


def test_buscar_en_indice_without_index_gives_aviso(site: Site, data_dir: Path) -> None:
    result = ok(srv.buscar_en_indice("cese"))
    assert result["total"] == 0
    assert result["articulos"] == []
    assert "sincronizar_indice" in result["aviso"]
    assert "buscar_articulos" in result["aviso"]
    assert not (data_dir / "sumarios.sqlite3").exists()
    state = ok(srv.estado_indice())
    assert state["existe"] is False
    assert state["indice"] is None


def test_sync_through_the_tools(site: Site, data_dir: Path) -> None:
    started = ok(srv.sincronizar_indice(desde="01/09/2026", hasta="2026-09-30", reindexar_recientes_dias=0))
    assert started["estado"] == "en_curso"
    assert srv._get_sync().esperar(10)
    state = ok(srv.estado_indice())
    assert state["existe"] is True
    assert state["sincronizacion"]["estado"] == "completado"
    assert state["indice"]["boletines"]["indexado"] == 2  # 6416 and BX-41; the rest 404
    found = ok(srv.buscar_en_indice("relacion provisional"))
    assert found["total"] == 5
    assert found["cobertura"]["boletines_indexados"] == 2
    assert "vacío" not in found.get("aviso", "")
    assert "pendientes" in found["aviso"]  # 6 calendar bulletins answered 404
    word = ok(srv.buscar_en_indice("rden", coincidencia="palabra"))
    assert word["total"] == 0
    assert ok(srv.cancelar_sincronizacion())["estado"] == "completado"


def test_the_default_sync_starts_in_2018_and_older_gaps_are_reported_apart(
    site: Site, data_dir: Path
) -> None:
    from bome_navaja.index import SumarioIndex
    from bome_navaja.models import BulletinRef

    ok(srv.sincronizar_indice(reindexar_recientes_dias=0))
    assert srv._get_sync().esperar(10)
    calendar = next(r for r in site.requests if r.url.path == "/api/bomes/calendar")
    assert calendar.url.params["start"] == "2018-01-01"
    before = ok(srv.estado_indice())["indice"]
    assert before["pendientes_anteriores_2018"] == 0
    # A calendar row recorded by an earlier sync from 2014 is not pending work.
    old = SumarioIndex(data_dir / "sumarios.sqlite3")
    try:
        old.registrar_calendario(
            [BulletinRef("BOME-B-2016-5300", 5300, date(2016, 5, 3), False, f"{BASE}/bome/BOME-B-2016-5300")]
        )
    finally:
        old.close()
    after = ok(srv.estado_indice())["indice"]
    assert after["pendientes"] == before["pendientes"]
    assert after["pendientes_anteriores_2018"] == 1
    cobertura = ok(srv.buscar_en_indice("relacion provisional"))["cobertura"]
    assert (cobertura["pendientes"], cobertura["pendientes_anteriores_2018"]) == (before["pendientes"], 1)


def test_the_tool_docs_explain_the_2018_default() -> None:
    tools = tools_by_name()
    sync = tools["sincronizar_indice"].description
    assert "2018-01-01" in sync and "portal antiguo" in sync and "2014-01-01" not in sync
    for name in ("estado_indice", "buscar_en_indice"):
        assert "pendientes_anteriores_2018" in tools[name].description, name
    text = srv.server.instructions or ""
    assert "2018-01-01" in text and "portal antiguo" in text


def test_buscar_en_indice_warns_while_a_sync_runs(site: Site, data_dir: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    body = (FIXTURES / "b6416.html").read_bytes()

    def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(10)
        return httpx.Response(200, content=body)

    site.routes["/bome/BOME-B-2026-6416"] = slow
    ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30", reindexar_recientes_dias=0))
    try:
        assert entered.wait(10)
        again = ok(srv.sincronizar_indice())
        assert again["estado"] == "en_curso"
        result = ok(srv.buscar_en_indice("cese"))
        assert "parcial" in result["aviso"]
        cancelled = ok(srv.cancelar_sincronizacion())
        assert cancelled["estado"] == "en_curso"
    finally:
        release.set()
    assert srv._get_sync().esperar(10)
    assert ok(srv.estado_indice())["sincronizacion"]["estado"] == "cancelado"


# --------------------------------------------------------------------------- estado_servidor


def test_estado_servidor_touches_neither_network_nor_index(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(**kwargs: object) -> BomeClient:
        raise AssertionError("estado_servidor must not create a client")

    monkeypatch.setattr(srv, "_client_factory", no_network)
    result = ok(srv.estado_servidor())
    assert result["version"] == __version__
    assert result["pid"] == os.getpid()
    assert result["rutas"]["datos"] == {"ruta": str(data_dir), "motivo": "BOME_NAVAJA_DATA_DIR"}
    assert result["rutas"]["pdfs"]["ruta"] == str(data_dir / "pdfs")
    assert result["rutas"]["indice"]["ruta"] == str(data_dir / "sumarios.sqlite3")
    assert result["indice_existe"] is False
    assert result["indice"] is None
    assert result["sqlite"]["fts5"] is True and result["sqlite"]["trigram"] is True
    assert result["sqlite"]["version"]
    assert result["cortesia_segundos"] == 0.5
    assert result["cortesia_sincronizacion"] == {
        "pausa_segundos": 2.0,
        "variacion_segundos": 1.0,
        "max_boletines_por_ejecucion": 250,
    }
    assert result["url_base"] == BASE
    assert result["guardia_sitio"] == {
        "enfriamiento_hasta": None,
        "segundos_restantes": 0,
        "motivo": None,
        "errores_en_ventana": 0,
        "max_errores": 3,
        "ventana_segundos": 600,
        "fichero": str(data_dir / FICHERO_ESTADO),
    }
    assert not data_dir.exists()


def test_estado_servidor_reports_sync_overrides_and_their_warnings(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOME_NAVAJA_SYNC_DELAY", "4.5")
    monkeypatch.setenv("BOME_NAVAJA_SYNC_MAX_BOLETINES", "40")
    pace = ok(srv.estado_servidor())["cortesia_sincronizacion"]
    assert pace == {"pausa_segundos": 4.5, "variacion_segundos": 1.0, "max_boletines_por_ejecucion": 40}

    monkeypatch.setenv("BOME_NAVAJA_SYNC_DELAY", "0.1")
    monkeypatch.setenv("BOME_NAVAJA_SYNC_MAX_BOLETINES", "muchos")
    pace = ok(srv.estado_servidor())["cortesia_sincronizacion"]
    assert (pace["pausa_segundos"], pace["max_boletines_por_ejecucion"]) == (1.0, 250)
    assert len(pace["avisos"]) == 2


# --------------------------------------------------------------------------- sync pace (polite-sync task 3)


def test_interactive_and_sync_clients_get_their_own_pace() -> None:
    interactive = srv._default_client_factory()
    sync = srv._default_client_factory(polite_delay=2.0, jitter=1.0)
    try:
        assert (interactive.polite_delay, interactive.jitter) == (0.5, 0.0)
        assert (sync.polite_delay, sync.jitter) == (2.0, 1.0)
    finally:
        interactive.close()
        sync.close()


def test_the_sync_client_uses_the_sync_pace(site: Site) -> None:
    srv.ver_bome("BOME-B-2026-6416")
    ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30", reindexar_recientes_dias=0))
    assert srv._get_sync().esperar(10)
    assert site.paces == [{}, {"polite_delay": 2.0, "jitter": 1.0}]  # interactive, then sync


def test_env_overrides_reach_the_sync(
    site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOME_NAVAJA_SYNC_DELAY", "3")
    monkeypatch.setenv("BOME_NAVAJA_SYNC_MAX_BOLETINES", "2")
    started = ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30", reindexar_recientes_dias=0))
    assert started["limite_boletines"] == 2
    assert srv._get_sync().esperar(10)
    assert site.paces == [{"polite_delay": 3.0, "jitter": 1.0}]
    state = ok(srv.estado_indice())["sincronizacion"]
    assert (state["total_planificado"], state["pendientes_tras_limite"]) == (2, 6)
    assert capsys.readouterr().err == ""


def test_bad_env_overrides_are_logged_on_stderr(
    site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOME_NAVAJA_SYNC_DELAY", "0.2")
    monkeypatch.setenv("BOME_NAVAJA_SYNC_MAX_BOLETINES", "todos")
    ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30", reindexar_recientes_dias=0))
    assert srv._get_sync().esperar(10)
    assert site.paces == [{"polite_delay": 1.0, "jitter": 1.0}]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "BOME_NAVAJA_SYNC_DELAY" in captured.err and "BOME_NAVAJA_SYNC_MAX_BOLETINES" in captured.err


def test_sincronizar_indice_passes_max_boletines(site: Site) -> None:
    started = ok(
        srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30", reindexar_recientes_dias=0, max_boletines=1)
    )
    assert started["limite_boletines"] == 1
    assert srv._get_sync().esperar(10)
    state = ok(srv.estado_indice())["sincronizacion"]
    assert state["estado"] == "completado"
    assert (state["total_planificado"], state["pendientes_tras_limite"]) == (1, 7)
    assert "sincronizar_indice" in state["mensaje"]
    assert [r.url.path for r in site.requests if r.url.path.startswith("/bome/")] == ["/bome/BOME-B-2026-6416"]


def test_broken_pages_become_rotos_and_are_skipped_unless_asked(site: Site) -> None:
    site.routes["/bome/BOME-B-2026-6416"] = lambda request: httpx.Response(500)

    def sync(**kwargs: object) -> None:
        ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30", reindexar_recientes_dias=0, **kwargs))
        assert srv._get_sync().esperar(10)

    def broken_requests() -> list[str]:
        return [r.url.path for r in site.requests if r.url.path == "/bome/BOME-B-2026-6416"]

    sync()
    sync()
    state = ok(srv.estado_indice())
    assert state["indice"]["boletines"]["roto"] == 1
    assert state["sincronizacion"]["rotos"] == 1
    found = ok(srv.buscar_en_indice("orden"))
    assert found["cobertura"]["rotos"] == 1
    assert "reintentar_rotos" in found["aviso"]
    site.requests.clear()
    sync()
    assert broken_requests() == []
    sync(reintentar_rotos=True)
    assert broken_requests() == ["/bome/BOME-B-2026-6416"]
    assert ok(srv.estado_indice())["sincronizacion"]["rotos"] == 1


def test_sincronizar_indice_documents_reintentar_rotos() -> None:
    description = tools_by_name()["sincronizar_indice"].description
    assert "reintentar_rotos" in description and "500" in description


def test_empty_index_cobertura_has_rotos(site: Site) -> None:
    assert ok(srv.buscar_en_indice("cese"))["cobertura"]["rotos"] is None


def test_import_creates_no_client_and_no_index(data_dir: Path) -> None:
    import importlib

    importlib.reload(srv)
    try:
        assert srv._client is None
        assert srv._index is None
        assert srv._portal is None and srv._portal_guard is None
        assert srv._sync_antiguo is None
        assert not data_dir.exists()
    finally:
        importlib.reload(srv)


# --------------------------------------------------------------------------- errors


def test_404_is_no_encontrado(site: Site) -> None:
    fail(srv.ver_bome("BOME-B-2026-9999"), "no_encontrado")


def test_500_is_error_http(site: Site) -> None:
    site.routes["/bome/BOME-B-2026-6416"] = lambda request: httpx.Response(500)
    result = fail(srv.ver_bome("BOME-B-2026-6416"), "error_http")
    assert result["estado_http"] == 500
    assert "reintentar_tras_segundos" not in result


def test_blocking_answer_is_sitio_bloqueando(site: Site) -> None:
    site.routes["/bome/BOME-B-2026-6416"] = lambda request: httpx.Response(
        429, headers={"retry-after": "120"}
    )
    result = fail(srv.ver_bome("BOME-B-2026-6416"), "sitio_bloqueando")
    assert result["estado_http"] == 429
    # Retry-After was 120 s, but the guard keeps the site closed for its whole cooldown.
    assert result["reintentar_tras_segundos"] == ENFRIAMIENTO_SEGUNDOS
    assert "espera" in result["error"].lower()


def test_blocked_drill_down_is_sitio_bloqueando(site: Site) -> None:
    site.routes["/bome/BOME-BX-2026-41"] = lambda request: httpx.Response(403)
    result = fail(srv.buscar_articulos(texto="relacion provisional"), "sitio_bloqueando")
    assert result["estado_http"] == 403
    assert result["reintentar_tras_segundos"] == ENFRIAMIENTO_SEGUNDOS


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda: srv.ver_bome("nope"), "cve_invalido"),
        (lambda: srv.leer_articulo("BOME-P-2026-4784"), "lectura_invalida"),
        (lambda: srv.leer_pdf("BOME-P-2026-4784", desde_pagina=0), "lectura_invalida"),
        (lambda: srv.buscar_bomes(), "busqueda_invalida"),
        (lambda: srv.buscar_bomes(texto="cese", desde="ayer"), "busqueda_invalida"),
        (lambda: srv.buscar_articulos(texto="cese", max_bomes=0), "busqueda_invalida"),
        (lambda: srv.listar_bomes("ayer"), "argumento_invalido"),
        (lambda: srv.listar_bomes("2026-09-30", "2026-09-01"), "argumento_invalido"),
        (lambda: srv.sincronizar_indice(reindexar_recientes_dias=-1), "busqueda_invalida"),
        (lambda: srv.sincronizar_indice(max_boletines=0), "busqueda_invalida"),
    ],
)
def test_validation_errors(site: Site, call: Callable[[], dict], code: str) -> None:
    fail(call(), code)


def test_index_query_errors(site: Site) -> None:
    ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30", reindexar_recientes_dias=0))
    assert srv._get_sync().esperar(10)
    fail(srv.buscar_en_indice("cese", coincidencia="exacta"), "busqueda_invalida")
    fail(srv.buscar_en_indice("cese", limite=0), "busqueda_invalida")


def test_unexpected_exception_is_error_interno(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(**kwargs: object) -> BomeClient:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(srv, "_client_factory", broken)
    result = fail(srv.ver_bome("BOME-B-2026-6416"), "error_interno")
    assert "secret internal detail" not in json.dumps(result)
    assert "Traceback" not in json.dumps(result)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" in captured.err and "secret internal detail" in captured.err


def test_nothing_is_printed_to_stdout(site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    srv.listar_bomes("2026-09-01", "2026-09-30")
    srv.ver_bome("BOME-B-2026-6416")
    srv.ver_bome("BOME-B-2026-9999")
    srv.leer_articulo("BOME-B-2026-6416", 1051)
    srv.buscar_articulos(texto="relacion provisional")
    srv.estado_servidor()
    srv.buscar_en_indice("cese")
    assert capsys.readouterr().out == ""


def test_shared_client_is_created_once(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[BomeClient] = []

    def counting(**kwargs: object) -> BomeClient:
        client = site.factory(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(srv, "_client_factory", counting)
    srv.ver_bome("BOME-B-2026-6416")
    srv.listar_consejerias(1)
    assert len(created) == 1
    srv.close_shared_state()
    assert created[0].closed


# --------------------------------------------------------------------------- stdio round trip


def test_stdio_initialize_and_list_tools(tmp_path: Path) -> None:
    import mcp.types

    import time

    env = {**os.environ, "BOME_NAVAJA_DATA_DIR": str(tmp_path / "datos")}
    # Binary pipes: the MCP stdio transport writes UTF-8 whatever the platform
    # (it re-wraps stdout's buffer), so each line is decoded strictly as UTF-8
    # here instead of with the locale encoding (cp1252 on Windows).
    process = subprocess.Popen(
        [sys.executable, "-m", "bome_navaja.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    lines: queue.Queue[str | BaseException] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                lines.put(raw_line.decode("utf-8", errors="strict"))
        except BaseException as exc:  # surface reader failures in the test thread
            lines.put(exc)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + 60

    def next_line() -> str:
        item = lines.get(timeout=max(0.0, deadline - time.monotonic()))
        if isinstance(item, BaseException):
            raise item
        return item

    def send(message: dict) -> None:
        assert process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()

    def receive(message_id: int) -> dict:
        while True:
            raw = next_line()
            message = json.loads(raw)  # every stdout line must be JSON-RPC
            assert message.get("jsonrpc") == "2.0", raw
            if message.get("id") == message_id:
                return message

    try:
        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": mcp.types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        })
        initialized = receive(1)
        assert initialized["result"]["serverInfo"]["name"] == "bome-navaja"
        instructions = initialized["result"].get("instructions", "")
        assert "buscar_en_indice" in instructions
        assert "Autónoma" in instructions  # non-ASCII survives the UTF-8 round trip
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = receive(2)
        assert {tool["name"] for tool in listed["result"]["tools"]} == EXPECTED_TOOLS
    finally:
        assert process.stdin is not None
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
    reader.join(timeout=5)
    while not lines.empty():
        leftover = lines.get_nowait()
        if isinstance(leftover, BaseException):
            raise leftover
        json.loads(leftover)  # anything left on stdout is JSON too
    assert not (tmp_path / "datos" / "sumarios.sqlite3").exists()


# --------------------------------------------------------------------------- concurrency & MCP layer


def test_site_requests_from_concurrent_tools_are_serialised(site: Site) -> None:
    entered = threading.Event()
    release = threading.Event()
    body = (FIXTURES / "b6416.html").read_bytes()

    def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(10)
        return httpx.Response(200, content=body)

    site.routes["/bome/BOME-B-2026-6416"] = slow
    results: dict[str, dict] = {}
    first = threading.Thread(target=lambda: results.update(a=srv.ver_bome("BOME-B-2026-6416")))
    first.start()
    assert entered.wait(10)
    second = threading.Thread(target=lambda: results.update(b=srv.listar_consejerias(1)))
    second.start()
    second.join(timeout=0.3)
    assert second.is_alive(), "the second tool must wait for the shared client"
    assert [r.url.path for r in site.requests] == ["/bome/BOME-B-2026-6416"]
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert results["a"]["ok"] and results["b"]["ok"]


def test_tools_work_through_the_mcp_call_layer(site: Site) -> None:
    def call(name: str, arguments: dict) -> dict:
        result = asyncio.run(srv.server.call_tool(name, arguments))
        assert result.is_error is False
        (content,) = result.content
        return json.loads(content.text)  # plain dict tools come back as JSON text

    good = call("ver_bome", {"cve": "BOME-B-2026-6416"})
    assert good["ok"] is True and good["total_articulos"] == 13
    bad = call("ver_bome", {"cve": "nope"})
    assert (bad["ok"], bad["error_code"]) == (False, "cve_invalido")
    assert call("estado_servidor", {})["ok"] is True


def test_server_does_not_log_each_request_on_stderr() -> None:
    # The MCP default (INFO) makes httpx log every request to stderr.
    assert srv.server.settings.log_level == "WARNING"


def test_tool_descriptions_are_accurate() -> None:
    names = {tool.name: tool.description or "" for tool in asyncio.run(srv.server.list_tools())}
    assert "sincronizacion_indice" not in names["estado_indice"]
    for tool in ("leer_articulo", "leer_boletin", "leer_pdf"):
        assert "cortada" in names[tool], tool


# --------------------------------------------------------------------------- site guard (site-guard task 3)


def test_one_persisted_guard_is_shared_by_the_tools_and_the_sync(site: Site, data_dir: Path) -> None:
    fail(srv.ver_bome("BOME-B-2026-9999"), "no_encontrado")
    guard = srv._get_guard()
    assert guard is srv._get_guard()
    assert guard.estado()["fichero"] == str(data_dir / FICHERO_ESTADO)
    stored = json.loads((data_dir / FICHERO_ESTADO).read_text("utf-8"))
    assert len(stored["errores"]) == 1  # the 404 was counted in the data folder
    ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30", reindexar_recientes_dias=0))
    assert srv._get_sync().esperar(10)
    assert srv._get_sync().guard is guard
    assert site.guards == [guard, guard]  # interactive client, then the sync client


def test_tools_refuse_without_the_network_during_a_cooldown(site: Site, fake_time: FakeTime) -> None:
    srv._get_guard().registrar(None)
    srv._get_guard().registrar(None)  # two requests in a row lost: the site is closed
    fake_time.now += 500
    for call in (
        lambda: srv.ver_bome("BOME-B-2026-6416"),
        lambda: srv.leer_articulo("BOME-B-2026-6416", 1051),
        lambda: srv.descargar_pdf("BOME-P-2026-4784"),
        lambda: srv.listar_consejerias(1),
    ):
        result = fail(call(), "sitio_bloqueando")
        assert result["reintentar_tras_segundos"] == pytest.approx(ENFRIAMIENTO_SEGUNDOS - 500)
        assert result["estado_http"] is None
        assert "bloque" in result["error"] and "UTC" in result["error"]
    assert site.requests == []
    ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30"))
    assert srv._get_sync().esperar(10)
    state = ok(srv.estado_indice())["sincronizacion"]
    assert state["estado"] == "bloqueado"
    assert state["reintentar_tras_segundos"] == pytest.approx(ENFRIAMIENTO_SEGUNDOS - 500)
    assert site.requests == []


def test_a_full_error_budget_is_pausa_preventiva(site: Site, fake_time: FakeTime) -> None:
    for number in (9997, 9998, 9999):
        fail(srv.ver_bome(f"BOME-B-2026-{number}"), "no_encontrado")
    fake_time.now += 100
    site.requests.clear()
    result = fail(srv.ver_bome("BOME-B-2026-6416"), "pausa_preventiva")
    assert result["reintentar_tras_segundos"] == pytest.approx(500)
    assert result["estado_http"] is None
    text = result["error"].lower()
    assert "pausa preventiva" in text and "cortafuegos" in text and "500 s" in text
    assert site.requests == []
    fake_time.now += 500
    ok(srv.ver_bome("BOME-B-2026-6416"))


def test_a_cooldown_left_by_another_process_is_honoured(
    site: Site, data_dir: Path, fake_time: FakeTime
) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / FICHERO_ESTADO).write_text(
        json.dumps({"errores": [], "enfriamiento_hasta": fake_time.now + 3000, "motivo": "HTTP 429"}), "utf-8"
    )
    result = fail(srv.ver_bome("BOME-B-2026-6416"), "sitio_bloqueando")
    assert result["reintentar_tras_segundos"] == pytest.approx(3000)
    assert site.requests == []
    state = ok(srv.estado_servidor())["guardia_sitio"]
    assert state["segundos_restantes"] == 3000
    assert state["motivo"] == "HTTP 429"
    assert state["enfriamiento_hasta"].endswith("+00:00")


def test_estado_servidor_reports_the_guard(site: Site, data_dir: Path) -> None:
    fail(srv.ver_bome("BOME-B-2026-9999"), "no_encontrado")
    state = ok(srv.estado_servidor())["guardia_sitio"]
    assert (state["errores_en_ventana"], state["max_errores"], state["ventana_segundos"]) == (1, 3, 600)
    assert state["enfriamiento_hasta"] is None
    assert state["fichero"] == str(data_dir / FICHERO_ESTADO)


def test_without_a_data_folder_the_guard_lives_in_memory(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bome_navaja.models import BomeError

    def unavailable(*args: object, **kwargs: object) -> object:
        raise BomeError("no data folder")

    monkeypatch.setattr(srv, "data_dir", unavailable)
    fail(srv.ver_bome("BOME-B-2026-9999"), "no_encontrado")
    guard = srv._get_guard()
    assert guard.estado()["fichero"] is None
    assert guard.errores_en_ventana() == 1


def test_close_shared_state_forgets_the_guard(site: Site) -> None:
    guard = srv._get_guard()
    srv.close_shared_state()
    assert srv._get_guard() is not guard



# --------------------------------------------------------------------------- old portal (site-guard task 7)


class OldPortal:
    """MockTransport router for the old portal on melilla.es (by ``seccion`` or path)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.guards: list[GuardiaSitio] = []
        self.paces: list[dict[str, float]] = []
        self.lock = threading.Lock()
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        for key, name in {
            "bome.jsp": "listado.html",
            "busqueda_bome.jsp": "busqueda_personal_eventual.html",
            "ficha_bome.jsp": "ficha_5302.html",
        }.items():
            body = (ANTIGUO / name).read_bytes()
            self.routes[key] = functools.partial(
                lambda content, request: httpx.Response(
                    200, content=content, headers={"content-type": "text/html;charset=ISO-8859-1"}
                ),
                body,
            )
        pdf = (ANTIGUO / "5302_73.pdf").read_bytes()
        self.routes["/mandar.php/n/9/4914/5302_73.pdf"] = lambda request: httpx.Response(
            200, content=pdf, headers={"content-type": "application/pdf"}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
        key = request.url.params.get("seccion") or request.url.path
        handler = self.routes.get(key)
        return handler(request) if handler else httpx.Response(404, text="no route")

    def seccions(self) -> list[str]:
        return [r.url.params.get("seccion") or r.url.path for r in self.requests]

    def factory(self, *, guard: GuardiaSitio, **pace: float) -> PortalAntiguo:
        """Records the requested pace (empty for the interactive portal) but never waits."""
        self.guards.append(guard)
        self.paces.append(pace)
        return PortalAntiguo(transport=httpx.MockTransport(self), guard=guard, polite_delay=0, jitter=0)


@pytest.fixture
def old(data_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch) -> OldPortal:
    fake = OldPortal()
    monkeypatch.setattr(srv, "_portal_factory", fake.factory)
    return fake


def calendar_json(*rows: tuple[str, str]) -> Callable[[httpx.Request], httpx.Response]:
    body = [
        {"title": f"Nº {cve.rsplit('-', 1)[1]}", "start": day, "url": f"/bome/{cve}"} for cve, day in rows
    ]
    return lambda request: httpx.Response(200, json=body)


def test_listar_bomes_after_the_old_portal_ends_never_asks_it(site: Site, old: OldPortal) -> None:
    result = ok(srv.listar_bomes("2026-09-01", "2026-09-30"))
    assert result["total"] == 8
    assert {b["origen"] for b in result["bomes"]} == {"bomemelilla.es"}
    assert "aviso" not in result
    assert old.requests == []


def test_listar_bomes_merges_the_old_catalog_before_2021_03_13(site: Site, old: OldPortal) -> None:
    site.routes["/api/bomes/calendar"] = calendar_json(
        ("BOME-B-2016-5302", "2016-01-08"), ("BOME-B-2016-5300", "2016-01-01")
    )
    result = ok(srv.listar_bomes("2016-01-01", "2016-01-08"))
    assert [b["cve"] for b in result["bomes"]] == ["BOME-B-2016-5302", "BOME-B-2016-5301", "BOME-B-2016-5300"]
    assert [b["origen"] for b in result["bomes"]] == ["bomemelilla.es", "melilla.es", "bomemelilla.es"]
    assert result["total"] == 3 and result["truncado"] is False
    assert result["solo_portal_antiguo"] == 1
    kept, only_old, _ = result["bomes"]
    assert "dboid" not in kept  # bomemelilla.es wins: its own entry, untouched
    assert only_old["dboid"] == 216769
    assert only_old["date"] == "2016-01-05" and only_old["number"] == 5301
    assert only_old["extraordinary"] is False
    assert "ver_bome_antiguo" in only_old["ver_con"] and "216769" in only_old["ver_con"]
    assert only_old["url"].startswith("https://www.melilla.es/melillaPortal/")
    assert old.seccions() == ["bome.jsp"]
    # The catalog is cached in the data folder and reused: no second GET.
    ok(srv.listar_bomes("2016-01-01", "2016-01-08"))
    assert old.seccions() == ["bome.jsp"]


def test_listar_bomes_before_2014_uses_only_the_old_catalog(site: Site, old: OldPortal) -> None:
    result = ok(srv.listar_bomes("1985-01-01", "1986-12-31"))
    assert result["total"] == 55
    assert {b["origen"] for b in result["bomes"]} == {"melilla.es"}
    assert all(b["cve_oficial"] is False for b in result["bomes"])
    assert site.requests == []  # bomemelilla.es starts in 2014: not asked
    extras = [b for b in result["bomes"] if b["cve"] == "BOME-BX-1986-1"]
    assert sorted(b["dboid"] for b in extras) == [280058, 280087]  # repeated identifiers both listed
    dates = [b["date"] for b in result["bomes"]]
    assert dates == sorted(dates, reverse=True)


def test_listar_bomes_across_2014_asks_bomemelilla_only_from_2014(site: Site, old: OldPortal) -> None:
    site.routes["/api/bomes/calendar"] = calendar_json()
    result = ok(srv.listar_bomes("2011-12-01", "2014-01-31"))
    calendar = next(r for r in site.requests if r.url.path == "/api/bomes/calendar")
    assert calendar.url.params["start"] == "2014-01-01"
    assert result["total"] > 0 and {b["origen"] for b in result["bomes"]} == {"melilla.es"}


def test_listar_bomes_keeps_max_listado_over_the_merge(
    site: Site, old: OldPortal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "MAX_LISTADO", 10)
    result = ok(srv.listar_bomes("2010-01-01", "2011-12-31"))
    assert result["total"] == 255
    assert len(result["bomes"]) == 10 and result["truncado"] is True
    assert result["bomes"][0]["date"].startswith("2011-12")


def test_listar_bomes_without_the_old_portal_still_lists_bomemelilla(site: Site, old: OldPortal) -> None:
    site.routes["/api/bomes/calendar"] = calendar_json(("BOME-B-2016-5302", "2016-01-08"))
    old.routes["bome.jsp"] = lambda request: httpx.Response(500)
    result = ok(srv.listar_bomes("2016-01-01", "2016-01-08"))
    assert [b["cve"] for b in result["bomes"]] == ["BOME-B-2016-5302"]
    assert "melilla.es" in result["aviso"] and "portal antiguo" in result["aviso"]
    assert result["portal_antiguo_error_code"] == "error_http"
    # The old portal's error counts in its own guard, never in bomemelilla.es's.
    assert srv._get_portal_guard().errores_en_ventana() == 1
    assert srv._get_guard().errores_en_ventana() == 0


def test_listar_bomes_with_the_old_portal_blocked_gives_an_aviso(site: Site, old: OldPortal) -> None:
    site.routes["/api/bomes/calendar"] = calendar_json(("BOME-B-2016-5302", "2016-01-08"))
    srv._get_portal_guard().registrar(403)
    result = ok(srv.listar_bomes("2016-01-01", "2016-01-08"))
    assert result["total"] == 1
    assert result["portal_antiguo_error_code"] == "sitio_bloqueando"
    assert result["portal_antiguo_reintentar_tras_segundos"] == pytest.approx(ENFRIAMIENTO_SEGUNDOS)
    assert old.requests == []


def test_listar_bomes_only_before_2014_fails_when_the_old_portal_does(site: Site, old: OldPortal) -> None:
    old.routes["bome.jsp"] = lambda request: httpx.Response(500)
    result = fail(srv.listar_bomes("1990-01-01", "1990-12-31"), "error_http")
    assert "melilla.es" in result["error"] and "bomemelilla.es" not in result["error"]


def test_buscar_bome_antiguo(site: Site, old: OldPortal) -> None:
    result = ok(srv.buscar_bome_antiguo("personal eventual"))
    assert result["total"] == 76 and result["truncado"] is False
    assert len(result["articulos"]) == 76
    first = result["articulos"][0]
    assert first["cve_boletin"] == "BOME-B-2020-5722" and first["fecha"] == "2020-01-17"
    assert first["sumario"].startswith("Decreto nº 20")
    assert first["ruta"][0] == "CIUDAD AUTÓNOMA DE MELILLA"
    assert first["paginas"][0]["url_pdf"] == "https://www.melilla.es/mandar.php/n/12/3334/5722_47.pdf"
    assert first["dboid_boletin"] == 257569 and first["origen"] == "melilla.es"
    (request,) = old.requests
    assert request.method == "POST" and request.content == b"textobome=personal+eventual"
    assert site.requests == []


def test_buscar_bome_antiguo_filters_by_date_and_limits(site: Site, old: OldPortal) -> None:
    result = ok(srv.buscar_bome_antiguo("personal eventual", desde="2019-01-01", hasta="31/12/2019", limite=2))
    assert (result["desde"], result["hasta"]) == ("2019-01-01", "2019-12-31")
    assert result["total"] > 2 and result["truncado"] is True
    assert len(result["articulos"]) == 2
    assert result["total_sin_filtrar"] == 76
    assert all(a["fecha"].startswith("2019") for a in result["articulos"])
    empty = ok(srv.buscar_bome_antiguo("personal eventual", desde="2021-01-01"))
    assert empty["total"] == 0 and empty["articulos"] == [] and empty["aviso"]


@pytest.mark.parametrize(
    "call",
    [
        lambda: srv.buscar_bome_antiguo("ab"),
        lambda: srv.buscar_bome_antiguo("personal eventual", limite=0),
        lambda: srv.buscar_bome_antiguo("personal eventual", limite=501),
        lambda: srv.buscar_bome_antiguo("personal eventual", desde="ayer"),
        lambda: srv.buscar_bome_antiguo("personal eventual", desde="2020-01-01", hasta="2019-01-01"),
        lambda: srv.buscar_bome_antiguo("euro €"),
    ],
)
def test_buscar_bome_antiguo_rejects_bad_arguments_without_a_request(
    old: OldPortal, call: Callable[[], dict]
) -> None:
    fail(call(), "busqueda_invalida")
    assert old.requests == []


def test_ver_bome_antiguo_by_cve_and_by_dboid(site: Site, old: OldPortal) -> None:
    by_cve = ok(srv.ver_bome_antiguo(cve="bome-b-2016-5302"))
    assert by_cve["cve"] == "BOME-B-2016-5302" and by_cve["dboid"] == 216808
    assert by_cve["url_pdf"] == "https://www.melilla.es/mandar.php/n/9/4913/5302.pdf"
    assert by_cve["total_articulos"] == 4
    article = by_cve["articulos"][0]
    assert article["numero"] == 27 and article["tipo"] == "Notificación"
    assert article["ruta"][-1] == "Personal Funcionario"
    assert article["paginas"] == [{"numero": 73, "url_pdf": OLD_PDF}]
    assert by_cve["origen"] == "melilla.es"
    assert old.seccions() == ["bome.jsp", "ficha_bome.jsp"]
    by_dboid = ok(srv.ver_bome_antiguo(dboid=216808))
    assert by_dboid["cve"] == "BOME-B-2016-5302"
    assert old.seccions() == ["bome.jsp", "ficha_bome.jsp", "ficha_bome.jsp"]
    assert site.requests == []


def test_ver_bome_antiguo_pre_2014_identifiers_are_flagged(site: Site, old: OldPortal) -> None:
    result = ok(srv.ver_bome_antiguo(dboid=280058))  # the ficha fixture answers any dboid
    assert "aviso" not in result or "bomemelilla.es" in result["aviso"]
    ambiguous = fail(srv.ver_bome_antiguo(cve="BOME-BX-1986-1"), "boletin_ambiguo")
    assert "280058" in ambiguous["error"] and "280087" in ambiguous["error"]
    assert "1986-05-20" in ambiguous["error"] and "1986-02-08" in ambiguous["error"]
    candidates = {(c["dboid"], c["fecha"]) for c in ambiguous["candidatos"]}
    assert candidates == {(280058, "1986-05-20"), (280087, "1986-02-08")}
    assert all("sufijo" in c for c in ambiguous["candidatos"])


def test_ver_bome_antiguo_errors(site: Site, old: OldPortal) -> None:
    missing = fail(srv.ver_bome_antiguo(cve="BOME-B-2099-1"), "no_encontrado")
    assert "melilla.es" in missing["error"] and "bomemelilla.es" not in missing["error"]
    fail(srv.ver_bome_antiguo(), "argumento_invalido")
    fail(srv.ver_bome_antiguo(cve="BOME-B-2016-5302", dboid=216808), "argumento_invalido")
    fail(srv.ver_bome_antiguo(dboid=0), "argumento_invalido")
    fail(srv.ver_bome_antiguo(cve="BOME-A-2016-1"), "cve_invalido")


def test_descargar_and_leer_pdf_of_an_old_portal_url(site: Site, old: OldPortal, data_dir: Path) -> None:
    download = ok(srv.descargar_pdf(url="http://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf"))
    assert download["ruta"] == str(data_dir / "pdfs" / "melilla-9-4914-5302_73.pdf")
    assert (data_dir / "pdfs" / "melilla-9-4914-5302_73.pdf").read_bytes() == (ANTIGUO / "5302_73.pdf").read_bytes()
    assert download["url"] == OLD_PDF
    assert download["cve"] is None and download["origen"] == "melilla.es"
    assert download["total_paginas"] == 1 and download["cache_hit"] is False
    reading = ok(srv.leer_pdf(url=OLD_PDF))
    assert reading["fuente"] == "pdf" and reading["origen"] == "melilla.es"
    assert reading["metadatos"]["cache_hit"] is True
    assert "4328" in reading["paginas"][0]["texto"]
    assert [str(r.url) for r in old.requests] == [OLD_PDF]
    assert site.requests == []
    again = ok(srv.descargar_pdf(url=OLD_PDF, refrescar=True))
    assert again["cache_hit"] is False and len(old.requests) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/mandar.php/n/9/4914/5302_73.pdf",
        "https://www.melilla.es/mandar.php/n/9/../4914/5302_73.pdf",
        "https://bomemelilla.es/bome/descargar/BOME-B-2016-5302.pdf",
        "file:///etc/passwd",
    ],
)
def test_old_pdf_tools_reject_foreign_urls(old: OldPortal, data_dir: Path, url: str) -> None:
    fail(srv.descargar_pdf(url=url), "url_pdf_invalida")
    fail(srv.leer_pdf(url=url), "url_pdf_invalida")
    assert old.requests == []
    assert not (data_dir / "pdfs").exists()


def test_pdf_tools_need_exactly_one_of_cve_and_url(site: Site, old: OldPortal) -> None:
    fail(srv.descargar_pdf(), "lectura_invalida")
    fail(srv.leer_pdf(), "lectura_invalida")
    fail(srv.descargar_pdf("BOME-P-2026-4784", url=OLD_PDF), "lectura_invalida")
    fail(srv.leer_pdf("BOME-P-2026-4784", url=OLD_PDF), "lectura_invalida")
    assert site.requests == [] and old.requests == []


def test_ver_bome_404_suggests_the_old_portal(site: Site) -> None:
    result = fail(srv.ver_bome("BOME-B-2016-5301"), "no_encontrado")
    assert "ver_bome_antiguo" in result["error"]
    recent = fail(srv.ver_bome("BOME-B-2026-9999"), "no_encontrado")
    assert "ver_bome_antiguo" not in recent["error"]


def test_old_portal_guard_refusals_name_melilla_es(site: Site, old: OldPortal, fake_time: FakeTime) -> None:
    guard = srv._get_portal_guard()
    guard.registrar(None)
    guard.registrar(None)  # two requests in a row lost: the old portal is closed
    fake_time.now += 100
    for call in (
        lambda: srv.buscar_bome_antiguo("personal eventual"),
        lambda: srv.ver_bome_antiguo(dboid=216808),
        lambda: srv.leer_pdf(url=OLD_PDF),
    ):
        result = fail(call(), "sitio_bloqueando")
        assert result["reintentar_tras_segundos"] == pytest.approx(ENFRIAMIENTO_SEGUNDOS - 100)
        assert "melilla.es" in result["error"] and "bomemelilla.es" not in result["error"]
    assert old.requests == []
    ok(srv.ver_bome("BOME-B-2026-6416"))  # bomemelilla.es is a different site: still open


def test_old_portal_error_budget_is_pausa_preventiva_for_melilla_es(
    site: Site, old: OldPortal, fake_time: FakeTime
) -> None:
    old.routes["ficha_bome.jsp"] = lambda request: httpx.Response(500)
    for _ in range(3):
        fail(srv.ver_bome_antiguo(dboid=216808), "error_http")
    result = fail(srv.ver_bome_antiguo(dboid=216808), "pausa_preventiva")
    assert "melilla.es" in result["error"] and "bomemelilla.es" not in result["error"]
    assert len(old.requests) == 3
    assert srv._get_guard().errores_en_ventana() == 0


def test_one_persisted_old_portal_guard_and_portal_per_process(
    site: Site, old: OldPortal, data_dir: Path
) -> None:
    ok(srv.buscar_bome_antiguo("personal eventual"))
    ok(srv.ver_bome_antiguo(dboid=216808))
    guard = srv._get_portal_guard()
    assert old.guards == [guard]  # one portal, built once with the process's old-portal guard
    assert guard is not srv._get_guard()
    assert guard.sitio == "melilla.es"
    assert guard.estado()["fichero"] == str(data_dir / FICHERO_ESTADO_MELILLA)
    portal = srv._portal
    assert portal is not None
    srv.close_shared_state()
    assert portal.closed and srv._portal is None
    assert srv._get_portal_guard() is not guard


def test_old_portal_guard_lives_in_memory_without_a_data_folder(
    old: OldPortal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bome_navaja.models import BomeError

    def unavailable(*args: object, **kwargs: object) -> object:
        raise BomeError("no data folder")

    monkeypatch.setattr(srv, "data_dir", unavailable)
    assert srv._get_portal_guard().estado()["fichero"] is None
    assert srv._get_portal_guard().sitio == "melilla.es"


def test_estado_servidor_reports_the_old_portal_without_network(
    data_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_portal(**kwargs: object) -> PortalAntiguo:
        raise AssertionError("estado_servidor must not create the old-portal client")

    monkeypatch.setattr(srv, "_portal_factory", no_portal)
    result = ok(srv.estado_servidor())
    assert result["guardia_portal_antiguo"] == {
        "enfriamiento_hasta": None,
        "segundos_restantes": 0,
        "motivo": None,
        "errores_en_ventana": 0,
        "max_errores": 3,
        "ventana_segundos": 600,
        "fichero": str(data_dir / FICHERO_ESTADO_MELILLA),
    }
    assert result["catalogo_portal_antiguo"] == {
        "ruta": str(data_dir / FICHERO_CATALOGO),
        "existe": False,
        "fetched_at": None,
        "boletines": None,
    }
    assert result["url_portal_antiguo"] == "https://www.melilla.es/melillaPortal"
    assert not data_dir.exists()


def test_estado_servidor_reports_the_cached_catalog(site: Site, old: OldPortal, data_dir: Path) -> None:
    ok(srv.listar_bomes("1985-01-01", "1985-12-31"))
    catalog = ok(srv.estado_servidor())["catalogo_portal_antiguo"]
    assert catalog["existe"] is True and catalog["boletines"] == 476
    assert catalog["fetched_at"] and catalog["ruta"] == str(data_dir / FICHERO_CATALOGO)
    (data_dir / FICHERO_CATALOGO).write_text("{broken", "utf-8")
    broken = ok(srv.estado_servidor())["catalogo_portal_antiguo"]
    assert broken["existe"] is True and broken["boletines"] is None and broken["error"]


def test_old_portal_tools_are_documented_for_the_model() -> None:
    tools = tools_by_name()
    search = tools["buscar_bome_antiguo"].description
    for needle in ("1985", "2021", "1991", "literal", "2018", "limite", "truncado"):
        assert needle in search, needle
    ficha = tools["ver_bome_antiguo"].description
    assert "dboid" in ficha and "cve" in ficha
    assert "url" in tools["leer_pdf"].description and "url" in tools["descargar_pdf"].description
    assert "origen" in tools["listar_bomes"].description
    text = srv.server.instructions or ""
    for needle in ("buscar_bome_antiguo", "ver_bome_antiguo", "melilla.es", "bomemelilla.es", "2018"):
        assert needle in text, needle


# --------------------------------------------------------------------------- old-portal index (old-portal-index task 3)


def old_catalog_page(years: dict[int, list[tuple[int, str]]]) -> bytes:
    """A catalog page shaped like the real one: one accordion set per year."""
    sets = "".join(
        f'<div class="set set{i}"><div class="title"><img src="resid/1/img/{year}.jpg"/></div>'
        '<div class="content"><div class="cBome"><div class="c45">'
        '<div class="listado1"><a href="">Mes</a></div><div class="listado2"><ul class="menu">'
        + "".join(
            '<li><a href="contenedor.jsp?seccion=ficha_bome.jsp&amp;dboidboletin='
            f'{dboid}&amp;codResi=1&amp;language=es&amp;codAdirecto=15">{text}</a></li>'
            for dboid, text in links
        )
        + "</ul></div></div></div></div></div>"
        for i, (year, links) in enumerate(years.items(), start=1)
    )
    html = (
        '<html><body><div class="bandaNo"><div id="accordion3" class="accordionWrapper">'
        f"{sets}</div></div></body></html>"
    )
    return html.encode("latin-1")


OLD_INDEX_CATALOG = old_catalog_page(
    {
        2018: [(300001, "nº 5520 / 02-01-2018")],
        1999: [(276000, "nº 3660 / 30-12-1999")],
        1991: [(278000, "nº 3175 / 26-12-1991")],
        1986: [(279997, "nº 2899 / 25-12-1986")],
    }
)
"""Four bulletins: 2018 and 1986 fall outside the old-portal sync's default range."""

OLD_INDEX_FICHAS = {
    300001: "ficha_5302.html",
    276000: "ficha_1999_3660.html",
    278000: "ficha_1991_3175.html",
    279997: "ficha_1986_2899.html",
}


def _latin1(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "text/html;charset=ISO-8859-1"})


@pytest.fixture
def old_index(old: OldPortal, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch) -> OldPortal:
    """The old portal with a small catalog and one ficha per dboid; its sync never sleeps."""
    from bome_navaja.sync_antiguo import SincronizadorPortalAntiguo

    monkeypatch.setattr(
        srv, "SincronizadorPortalAntiguo", functools.partial(SincronizadorPortalAntiguo, wait=fake_time.wait)
    )
    old.routes["bome.jsp"] = lambda request: _latin1(OLD_INDEX_CATALOG)
    fichas = {dboid: (ANTIGUO / name).read_bytes() for dboid, name in OLD_INDEX_FICHAS.items()}

    def ficha(request: httpx.Request) -> httpx.Response:
        body = fichas.get(int(request.url.params["dboidboletin"]))
        return _latin1(body) if body is not None else httpx.Response(404)

    old.routes["ficha_bome.jsp"] = ficha
    return old


def fichas_pedidas(old: OldPortal) -> list[int]:
    return [int(r.url.params["dboidboletin"]) for r in old.requests if r.url.params.get("seccion") == "ficha_bome.jsp"]


def test_sincronizar_indice_syncs_the_old_portal_with_its_defaults(site: Site, old_index: OldPortal) -> None:
    started = ok(srv.sincronizar_indice(origen="melilla.es"))
    assert (started["estado"], started["origen"]) == ("en_curso", "melilla.es")
    assert (started["desde"], started["hasta"]) == ("1991-01-01", "2017-12-31")
    assert started["limite_boletines"] == 250
    sync = srv._get_sync_antiguo()
    assert sync is srv._get_sync_antiguo()  # one per process
    assert sync.esperar(10)
    assert fichas_pedidas(old_index) == [276000, 278000]  # newest first; 2018 and 1986 out of range
    assert site.requests == []  # bomemelilla.es is not touched
    assert srv._sync is None
    guard = srv._get_portal_guard()
    assert sync.guard is guard
    assert old_index.guards == [guard]
    assert old_index.paces == [{"polite_delay": 2.0, "jitter": 1.0}]  # the sync pace, not the interactive one
    state = ok(srv.estado_indice())
    job = state["sincronizacion"]
    assert (job["estado"], job["origen"]) == ("completado", "melilla.es")
    assert (job["total_planificado"], job["indexados"]) == (2, 2)
    indice = state["indice"]
    assert indice["por_origen"]["melilla.es"]["boletines"]["indexado"] == 2
    assert indice["por_origen"]["bomemelilla.es"]["boletines"]["total"] == 0
    assert indice["ultimas_sincronizaciones"]["melilla.es"]["origen"] == "melilla.es"
    assert indice["ultimas_sincronizaciones"]["bomemelilla.es"] is None


def test_the_old_portal_sync_takes_the_tool_arguments_and_the_env_overrides(
    site: Site, old_index: OldPortal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOME_NAVAJA_SYNC_DELAY", "3")
    monkeypatch.setenv("BOME_NAVAJA_SYNC_MAX_BOLETINES", "1")
    started = ok(srv.sincronizar_indice(origen="melilla.es", reindexar_recientes_dias=3))  # ignored
    assert started["limite_boletines"] == 1
    assert srv._get_sync_antiguo().esperar(10)
    assert old_index.paces == [{"polite_delay": 3.0, "jitter": 1.0}]
    state = ok(srv.estado_indice())["sincronizacion"]
    assert (state["total_planificado"], state["pendientes_tras_limite"]) == (1, 1)
    assert 'sincronizar_indice(origen="melilla.es")' in state["mensaje"]
    assert fichas_pedidas(old_index) == [276000]
    old_index.requests.clear()
    ok(srv.sincronizar_indice(origen="melilla.es", desde="1986-01-01", hasta="1991-12-31", max_boletines=5))
    assert srv._get_sync_antiguo().esperar(10)
    assert fichas_pedidas(old_index) == [278000, 279997]


@pytest.mark.parametrize("origen", ["boe.es", "", "MELILLA", None, 1])
def test_sincronizar_indice_rejects_an_unknown_origen(site: Site, data_dir: Path, origen: object) -> None:
    result = fail(srv.sincronizar_indice(origen=origen), "argumento_invalido")  # type: ignore[arg-type]
    assert "bomemelilla.es" in result["error"] and "melilla.es" in result["error"]
    assert site.requests == []
    assert not (data_dir / "sumarios.sqlite3").exists()


def test_the_old_portal_sync_validates_its_arguments(site: Site, old_index: OldPortal) -> None:
    fail(srv.sincronizar_indice(origen="melilla.es", desde="ayer"), "argumento_invalido")
    fail(srv.sincronizar_indice(origen="melilla.es", max_boletines=0), "busqueda_invalida")
    fail(srv.sincronizar_indice(origen="melilla.es", desde="2000-01-01", hasta="1999-01-01"), "busqueda_invalida")
    assert old_index.requests == []


def test_a_running_bomemelilla_sync_keeps_the_old_portal_sync_out(site: Site, old_index: OldPortal) -> None:
    entered = threading.Event()
    release = threading.Event()
    body = (FIXTURES / "b6416.html").read_bytes()

    def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(10)
        return httpx.Response(200, content=body)

    site.routes["/bome/BOME-B-2026-6416"] = slow
    ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30", reindexar_recientes_dias=0))
    try:
        assert entered.wait(10)
        refused = ok(srv.sincronizar_indice(origen="melilla.es"))
        assert refused["estado"] == "en_curso_en_otro_proceso"
        assert refused["origen"] == "melilla.es" and refused["lease"]["origen"] == "bomemelilla.es"
        assert srv._get_sync_antiguo().owner != srv._get_sync().owner
        running = ok(srv.estado_indice())["sincronizacion"]
        assert (running["estado"], running["origen"]) == ("en_curso", "bomemelilla.es")
        cancelled = ok(srv.cancelar_sincronizacion())
        assert (cancelled["estado"], cancelled["origen"]) == ("en_curso", "bomemelilla.es")
    finally:
        release.set()
    assert srv._get_sync().esperar(10)
    assert old_index.requests == []
    last = ok(srv.estado_indice())["sincronizacion"]
    assert (last["estado"], last["origen"]) == ("cancelado", "bomemelilla.es")


def test_a_running_old_portal_sync_keeps_the_bomemelilla_sync_out(site: Site, old_index: OldPortal) -> None:
    entered = threading.Event()
    release = threading.Event()
    ficha = old_index.routes["ficha_bome.jsp"]

    def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(10)
        return ficha(request)

    old_index.routes["ficha_bome.jsp"] = slow
    ok(srv.sincronizar_indice(origen="melilla.es"))
    try:
        assert entered.wait(10)
        again = ok(srv.sincronizar_indice(origen="melilla.es"))
        assert (again["estado"], again["origen"]) == ("en_curso", "melilla.es")
        refused = ok(srv.sincronizar_indice())
        assert refused["estado"] == "en_curso_en_otro_proceso"
        assert refused["origen"] == "bomemelilla.es" and refused["lease"]["origen"] == "melilla.es"
        running = ok(srv.estado_indice())
        assert (running["sincronizacion"]["estado"], running["sincronizacion"]["origen"]) == ("en_curso", "melilla.es")
        assert running["indice"]["sincronizacion_en_curso"]["origen"] == "melilla.es"
        assert "parcial" in ok(srv.buscar_en_indice("suscripciones"))["aviso"]
        cancelled = ok(srv.cancelar_sincronizacion())
        assert (cancelled["estado"], cancelled["origen"]) == ("en_curso", "melilla.es")
    finally:
        release.set()
    assert srv._get_sync_antiguo().esperar(10)
    assert site.requests == []
    assert fichas_pedidas(old_index) == [276000]  # cancelled after the bulletin in flight
    last = ok(srv.estado_indice())["sincronizacion"]
    assert (last["estado"], last["origen"]) == ("cancelado", "melilla.es")
    # Once the lease is free, the other origin may sync.
    assert ok(srv.sincronizar_indice(desde="2026-09-01", hasta="2026-09-30"))["estado"] == "en_curso"
    assert srv._get_sync().esperar(10)
    assert ok(srv.estado_indice())["sincronizacion"]["origen"] == "bomemelilla.es"


def test_cancelar_sincronizacion_without_any_sync(site: Site) -> None:
    assert ok(srv.cancelar_sincronizacion())["estado"] == "inactivo"


def test_buscar_en_indice_finds_old_portal_articles(site: Site, old_index: OldPortal) -> None:
    ok(srv.sincronizar_indice(origen="melilla.es"))
    assert srv._get_sync_antiguo().esperar(10)
    found = ok(srv.buscar_en_indice("suscripciones"))
    assert found["total"] == 1
    (article,) = found["articulos"]
    assert article["origen"] == "melilla.es"
    assert article["bome_cve"] == "BOME-B-1999-3660" and article["bome_fecha"].startswith("1999-")
    assert article["url"].startswith("https://www.melilla.es/melillaPortal/") and "276000" in article["url"]
    assert article["pdf_url"].startswith("https://www.melilla.es/mandar.php/")
    cobertura = found["cobertura"]
    assert cobertura["por_origen"]["melilla.es"]["boletines_indexados"] == 2
    assert cobertura["fecha_min"].startswith("1991-")
    assert "vacío" not in (found.get("aviso") or "")


def test_close_shared_state_forgets_the_old_portal_sync(site: Site, old_index: OldPortal) -> None:
    ok(srv.sincronizar_indice(origen="melilla.es", max_boletines=1))
    sync = srv._get_sync_antiguo()
    srv.close_shared_state()
    assert srv._sync_antiguo is None
    assert sync.esperar(0)
    assert srv._get_sync_antiguo() is not sync


def test_estado_servidor_reports_the_old_portal_sync_pace(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = ok(srv.estado_servidor())
    assert result["cortesia_sincronizacion_portal_antiguo"] == {
        "pausa_segundos": 2.0,
        "variacion_segundos": 1.0,
        "max_boletines_por_ejecucion": 250,
    }
    monkeypatch.setenv("BOME_NAVAJA_SYNC_DELAY", "4")
    pace = ok(srv.estado_servidor())["cortesia_sincronizacion_portal_antiguo"]
    assert pace["pausa_segundos"] == 4.0
    assert not data_dir.exists()


def test_the_old_portal_index_is_documented_for_the_model() -> None:
    tools = tools_by_name()
    sync = tools["sincronizar_indice"].description
    for needle in ("origen", '"melilla.es"', "1991-01-01", "2017-12-31", "reindexar_recientes_dias", "una a la vez"):
        assert needle in sync, needle
    search = tools["buscar_en_indice"].description
    for needle in ("melilla.es", "1991", "2017", "origen", "pdf_url", "ficha"):
        assert needle in search, needle
    state = tools["estado_indice"].description
    for needle in ("por_origen", "ultimas_sincronizaciones", "origen"):
        assert needle in state, needle
    assert "cualquiera" in tools["cancelar_sincronizacion"].description
    assert "cortesia_sincronizacion_portal_antiguo" in tools["estado_servidor"].description
    text = srv.server.instructions or ""
    assert 'sincronizar_indice(origen="melilla.es")' in text and "1991" in text
