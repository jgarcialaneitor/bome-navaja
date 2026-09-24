"""MCP server tests: tool registry, contract, error mapping, state and stdio."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from bome_navaja import __version__
from bome_navaja import server as srv
from bome_navaja.client import BomeClient

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://bomemelilla.es"

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

    def factory(self, **pace: float) -> BomeClient:
        """Records the requested pace (empty for interactive clients) but never waits."""
        self.paces.append(pace)
        return BomeClient(transport=httpx.MockTransport(self), polite_delay=0)


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
def site(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
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
    def no_network() -> BomeClient:
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


def test_import_creates_no_client_and_no_index(data_dir: Path) -> None:
    import importlib

    importlib.reload(srv)
    try:
        assert srv._client is None
        assert srv._index is None
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
    assert result["reintentar_tras_segundos"] == 120.0
    assert "espera" in result["error"].lower()


def test_blocked_drill_down_is_sitio_bloqueando(site: Site) -> None:
    site.routes["/bome/BOME-BX-2026-41"] = lambda request: httpx.Response(403)
    result = fail(srv.buscar_articulos(texto="relacion provisional"), "sitio_bloqueando")
    assert result["estado_http"] == 403
    assert result["reintentar_tras_segundos"] is None


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
    def broken() -> BomeClient:
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

    def counting() -> BomeClient:
        client = site.factory()
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
