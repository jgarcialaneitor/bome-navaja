"""Live MCP tool calls for the target question (opt-in: BOME_NAVAJA_LIVE=1).

"Todos los BOMEs donde hay nombramientos y ceses de personal eventual."
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from bome_navaja import server as srv
from bome_navaja.text import normalize

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def isolated_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("BOME_NAVAJA_DATA_DIR", str(tmp_path / "datos"))
    monkeypatch.delenv("BOME_NAVAJA_PDF_DIR", raising=False)
    monkeypatch.setattr(srv, "_client_factory", srv._default_client_factory)
    srv.close_shared_state()
    yield
    srv.close_shared_state()


def test_live_personal_eventual_ceses_then_read_the_first() -> None:
    found = srv.buscar_articulos(texto="personal eventual", terminos=[{"texto": "cese"}])
    assert found["ok"] is True, found
    articles = found["articulos"]
    assert len(articles) >= 12
    for article in articles:
        folded = normalize(article["sumario"])
        assert "personal eventual" in folded and "cese" in folded
        assert article["cve"].startswith("BOME-A") and article["url"].startswith("https://")

    first = articles[0]
    text = srv.leer_articulo(first["bome_cve"], first["numero"])
    assert text["ok"] is True, text
    assert text["cve"] == first["cve"]
    assert text["fuente"] == "html"
    body = normalize(" ".join(page["texto"] for page in text["paginas"]))
    assert "eventual" in body
    status = srv.estado_servidor()
    assert status["ok"] is True and status["indice_existe"] is False
