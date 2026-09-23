"""Live document checks against bomemelilla.es (opt-in: BOME_NAVAJA_LIVE=1)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from bome_navaja.client import BomeClient
from bome_navaja.documents import descargar_pdf, leer_articulo, leer_boletin

pytestmark = pytest.mark.live


@pytest.fixture
def bome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[BomeClient]:
    monkeypatch.setenv("BOME_NAVAJA_PDF_DIR", str(tmp_path / "pdfs"))
    with BomeClient(polite_delay=0.5) as client:
        yield client


def test_live_download_page_and_article(bome: BomeClient, tmp_path: Path) -> None:
    page = descargar_pdf(bome, "BOME-P-2026-4784")
    article = descargar_pdf(bome, "BOME-A-2026-1050")
    assert page.total_paginas == 1 and article.total_paginas == 1
    assert Path(page.ruta) == tmp_path / "pdfs" / "BOME-P-2026-4784.pdf"
    assert Path(article.ruta).read_bytes()[:4] == b"%PDF"
    again = descargar_pdf(bome, "BOME-P-2026-4784")
    assert again.cache_hit is True
    assert again.sha256 == page.sha256


def test_live_read_bulletin_in_two_chunks(bome: BomeClient) -> None:
    first = leer_boletin(bome, "BOME-B-2026-6416", max_caracteres=3000)
    assert first.fuente == "pdf"
    assert first.metadatos is not None and first.metadatos["numero"] == 6416
    assert first.total_paginas >= 30
    assert first.siguiente is not None
    second = leer_boletin(
        bome,
        "BOME-B-2026-6416",
        desde_pagina=first.siguiente.desde_pagina,
        desde_caracter=first.siguiente.desde_caracter,
        max_caracteres=3000,
    )
    assert second.metadatos is not None and second.metadatos["cache_hit"] is True
    last_first = first.paginas[-1]
    first_second = second.paginas[0]
    if last_first.cortada:
        assert first_second.numero == last_first.numero
        assert first_second.desde_caracter == last_first.desde_caracter + len(last_first.texto)
    else:
        assert first_second.numero == last_first.numero + 1
        assert first_second.desde_caracter == 0


def test_live_read_article_by_cve(bome: BomeClient) -> None:
    result = leer_articulo(bome, "BOME-A-2026-1051", max_caracteres=100_000)
    assert result.fuente == "html"
    assert result.metadatos is not None and result.metadatos["bome_cve"] == "BOME-B-2026-6416"
    assert [p.pagina_bome for p in result.paginas] == [4784, 4785, 4786, 4787]
    assert result.completo is True


def test_live_old_article_has_no_text(bome: BomeClient) -> None:
    result = leer_articulo(bome, "BOME-B-2014-5092", 2)
    assert result.fuente == "ninguna"
    assert result.paginas == ()
