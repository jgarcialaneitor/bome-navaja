"""PDF download/cache and paginated reading, offline (MockTransport + tmp_path)."""

from __future__ import annotations

import hashlib
import io
import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from bome_navaja import documents as documents_module
from bome_navaja.client import BomeClient
from bome_navaja.cve import InvalidCveError
from bome_navaja.documents import (
    MAX_PDF_BYTES,
    Cursor,
    LecturaInvalidaError,
    descargar_pdf,
    leer_articulo,
    leer_boletin,
    leer_pdf,
    paginar,
)
from bome_navaja.models import (
    BomeDocumentTooLargeError,
    BomeNotFoundError,
    BomeParseError,
    BomeStorageError,
)

BASE = "https://bomemelilla.es"
FIXTURES = Path(__file__).parent / "fixtures"
PAGE_PDF = (FIXTURES / "BOME-P-2026-4784.pdf").read_bytes()
ARTICLE_PDF = (FIXTURES / "BOME-A-2026-1050.pdf").read_bytes()


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF whose page i extracts exactly ``pages[i]`` (ASCII)."""
    count = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, text in enumerate(pages):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 8 Tf 20 800 Td ({escaped}) Tj ET".encode("latin-1") if text else b""
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources "
            f"<< /Font << /F1 3 0 R >> >> /Contents {5 + 2 * index} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


class Site:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def pdf(self, cve: str, body: bytes) -> None:
        self.routes[f"/bome/descargar/{cve}.pdf"] = lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/pdf"}
        )

    def page(self, path: str, fixture: str) -> None:
        body = (FIXTURES / fixture).read_bytes()
        self.routes[path] = lambda request: httpx.Response(200, content=body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        return handler(request) if handler else httpx.Response(404, text="Not found")

    def client(self) -> BomeClient:
        return BomeClient(transport=httpx.MockTransport(self))

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def pdfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "pdfs"
    monkeypatch.setenv("BOME_NAVAJA_PDF_DIR", str(target))
    return target


@pytest.fixture
def site() -> Site:
    return Site()


# --------------------------------------------------------------------------- descargar_pdf


def test_download_saves_under_canonical_name(site: Site, pdfs: Path) -> None:
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    with site.client() as client:
        result = descargar_pdf(client, "  bome-p-2026-4784 ")
    target = pdfs / "BOME-P-2026-4784.pdf"
    assert result.ruta == str(target)
    assert target.read_bytes() == PAGE_PDF
    assert result.cve == "BOME-P-2026-4784"
    assert result.tamano_bytes == len(PAGE_PDF) == 66314
    assert result.sha256 == hashlib.sha256(PAGE_PDF).hexdigest()
    assert result.total_paginas == 1
    assert result.cache_hit is False
    assert result.url == f"{BASE}/bome/descargar/BOME-P-2026-4784.pdf"
    assert result.motivo_directorio == "BOME_NAVAJA_PDF_DIR"
    # Nothing but the final file is left in the directory.
    assert sorted(p.name for p in pdfs.iterdir()) == ["BOME-P-2026-4784.pdf"]
    json.dumps(result.to_dict())


def test_download_extraordinary_page_cve(site: Site, pdfs: Path) -> None:
    site.pdf("BOME-PX-2021-362", PAGE_PDF)
    with site.client() as client:
        result = descargar_pdf(client, "bome-px-2021-362")
    assert result.cve == "BOME-PX-2021-362"
    assert result.ruta == str(pdfs / "BOME-PX-2021-362.pdf")
    assert result.url == f"{BASE}/bome/descargar/BOME-PX-2021-362.pdf"
    assert sorted(p.name for p in pdfs.iterdir()) == ["BOME-PX-2021-362.pdf"]


def test_download_reuses_cache_unless_refresh(site: Site, pdfs: Path) -> None:
    site.pdf("BOME-A-2026-1050", ARTICLE_PDF)
    with site.client() as client:
        first = descargar_pdf(client, "BOME-A-2026-1050")
        second = descargar_pdf(client, "BOME-A-2026-1050")
        third = descargar_pdf(client, "BOME-A-2026-1050", refrescar=True)
    assert (first.cache_hit, second.cache_hit, third.cache_hit) == (False, True, False)
    assert second.sha256 == first.sha256
    assert second.total_paginas == 1
    assert site.paths().count("/bome/descargar/BOME-A-2026-1050.pdf") == 2


def test_invalid_cached_file_is_downloaded_again(site: Site, pdfs: Path) -> None:
    pdfs.mkdir()
    (pdfs / "BOME-P-2026-4784.pdf").write_bytes(b"<html>truncated</html>")
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    with site.client() as client:
        result = descargar_pdf(client, "BOME-P-2026-4784")
    assert result.cache_hit is False
    assert (pdfs / "BOME-P-2026-4784.pdf").read_bytes() == PAGE_PDF


def test_write_is_atomic_in_the_same_directory(
    site: Site, pdfs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    replaced: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        replaced.append((Path(src), Path(dst)))
        assert Path(src).read_bytes() == PAGE_PDF
        real_replace(src, dst)

    monkeypatch.setattr(documents_module.os, "replace", spy)
    with site.client() as client:
        descargar_pdf(client, "BOME-P-2026-4784")
    ((src, dst),) = replaced
    assert src.parent == dst.parent == pdfs
    assert src != dst
    assert dst.name == "BOME-P-2026-4784.pdf"


def test_non_pdf_leaves_no_file_and_keeps_old_copy(site: Site, pdfs: Path) -> None:
    site.routes["/bome/descargar/BOME-P-2026-4784.pdf"] = lambda request: httpx.Response(
        200, text="<html>error</html>"
    )
    with site.client() as client:
        with pytest.raises(BomeParseError):
            descargar_pdf(client, "BOME-P-2026-4784")
        assert not pdfs.exists() or list(pdfs.iterdir()) == []
        # A valid cached copy survives a failed refresh.
        pdfs.mkdir(exist_ok=True)
        (pdfs / "BOME-P-2026-4784.pdf").write_bytes(PAGE_PDF)
        with pytest.raises(BomeParseError):
            descargar_pdf(client, "BOME-P-2026-4784", refrescar=True)
    assert (pdfs / "BOME-P-2026-4784.pdf").read_bytes() == PAGE_PDF
    assert sorted(p.name for p in pdfs.iterdir()) == ["BOME-P-2026-4784.pdf"]


def test_size_cap(site: Site, pdfs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_PDF_BYTES == 100 * 1024 * 1024
    monkeypatch.setattr(documents_module, "MAX_PDF_BYTES", 1000)
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    # Chunked answer without Content-Length: the cap must hold while streaming.
    site.routes["/bome/descargar/BOME-A-2026-1050.pdf"] = lambda request: httpx.Response(
        200, content=iter([ARTICLE_PDF[:600], ARTICLE_PDF[600:1200], ARTICLE_PDF[1200:]])
    )
    with site.client() as client:
        with pytest.raises(BomeDocumentTooLargeError) as info:
            descargar_pdf(client, "BOME-P-2026-4784")
        assert info.value.limit == 1000
        assert info.value.error_code == "documento_demasiado_grande"
        with pytest.raises(BomeDocumentTooLargeError):
            descargar_pdf(client, "BOME-A-2026-1050")
    assert not pdfs.exists() or list(pdfs.iterdir()) == []


def test_storage_errors_are_mapped(site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("BOME_NAVAJA_PDF_DIR", str(blocker / "pdfs"))
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    with site.client() as client, pytest.raises(BomeStorageError) as info:
        descargar_pdf(client, "BOME-P-2026-4784")
    assert info.value.error_code == "error_almacenamiento"
    assert str(blocker) in info.value.path


def test_replace_failure_is_mapped_and_cleans_temp(
    site: Site, pdfs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(src: str, dst: str) -> None:
        raise PermissionError("file is locked")

    monkeypatch.setattr(documents_module.os, "replace", fail)
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    with site.client() as client, pytest.raises(BomeStorageError, match="locked"):
        descargar_pdf(client, "BOME-P-2026-4784")
    assert list(pdfs.iterdir()) == []


def test_interrupted_write_cleans_temp_and_reraises(
    site: Site, pdfs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(fd: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(documents_module.os, "fsync", interrupt)
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    with site.client() as client, pytest.raises(KeyboardInterrupt):
        descargar_pdf(client, "BOME-P-2026-4784")
    assert list(pdfs.iterdir()) == []


@pytest.mark.parametrize("budget", [0, -5])
def test_paginar_rejects_a_budget_that_cannot_progress(budget: int) -> None:
    # A non-positive budget would return the same cursor forever.
    with pytest.raises(LecturaInvalidaError):
        paginar(["abc"], desde_pagina=1, desde_caracter=0, max_caracteres=budget)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "BOME-B-2026-6416/../x", "", "BOME-Z-2026-1"])
def test_unsafe_or_invalid_cves_never_touch_disk_or_network(site: Site, pdfs: Path, bad: str) -> None:
    with site.client() as client, pytest.raises(InvalidCveError):
        descargar_pdf(client, bad)
    assert site.requests == []
    assert not pdfs.exists()


def test_download_404_propagates(site: Site, pdfs: Path) -> None:
    # Live 2026-09-23: 2014-2016 PDFs (e.g. BOME-B-2014-5092) answer 404.
    with site.client() as client, pytest.raises(BomeNotFoundError):
        descargar_pdf(client, "BOME-B-2014-5092")


# --------------------------------------------------------------------------- paginar


def texts(result: tuple) -> list[str]:
    return [page.texto for page in result[0]]


def test_paginar_whole_pages_within_budget() -> None:
    pages = ["a" * 300, "b" * 300, "c" * 300, "d" * 300, "e" * 300]
    chunk, cursor = paginar(pages, desde_pagina=1, desde_caracter=0, max_caracteres=1000)
    assert [p.numero for p in chunk] == [1, 2, 3]
    assert cursor == Cursor(desde_pagina=4, desde_caracter=0)
    chunk, cursor = paginar(pages, desde_pagina=4, desde_caracter=0, max_caracteres=1000)
    assert [p.numero for p in chunk] == [4, 5]
    assert cursor is None


def test_paginar_always_returns_at_least_one_page() -> None:
    pages = ["a" * 900, "b" * 900]
    chunk, cursor = paginar(pages, desde_pagina=1, desde_caracter=0, max_caracteres=1000)
    assert [p.numero for p in chunk] == [1]
    assert cursor == Cursor(2, 0)


def test_paginar_splits_an_oversized_page_by_characters() -> None:
    big = "".join(chr(ord("a") + i % 26) for i in range(2500))
    pages = [big, "tail"]
    seen = []
    cursor: Cursor | None = Cursor(1, 0)
    while cursor is not None:
        chunk, cursor = paginar(
            pages,
            desde_pagina=cursor.desde_pagina,
            desde_caracter=cursor.desde_caracter,
            max_caracteres=1000,
        )
        seen.append((chunk, cursor))
    first_chunk, first_cursor = seen[0]
    assert first_chunk[0].texto == big[:1000]
    assert first_chunk[0].cortada is True
    assert first_chunk[0].desde_caracter == 0
    assert first_cursor == Cursor(1, 1000)
    assert seen[1][0][0].texto == big[1000:2000]
    assert seen[1][1] == Cursor(1, 2000)
    # The rest of the big page fits together with the next page.
    last_chunk, last_cursor = seen[2]
    assert [(p.numero, p.texto, p.cortada) for p in last_chunk] == [
        (1, big[2000:], False),
        (2, "tail", False),
    ]
    assert last_chunk[0].desde_caracter == 2000
    assert last_cursor is None
    # Nothing is lost or duplicated.
    assert "".join(p.texto for chunk, _ in seen for p in chunk if p.numero == 1) == big


def test_paginar_flags_pages_without_text() -> None:
    chunk, cursor = paginar(["x", "  \n ", "y"], desde_pagina=1, desde_caracter=0, max_caracteres=1000)
    assert [p.sin_texto for p in chunk] == [False, True, False]
    assert cursor is None


def test_paginar_carries_printed_page_numbers() -> None:
    chunk, _ = paginar(
        ["x", "y"], desde_pagina=1, desde_caracter=0, max_caracteres=1000, paginas_bome=[4784, 4785]
    )
    assert [p.pagina_bome for p in chunk] == [4784, 4785]


@pytest.mark.parametrize(
    ("desde_pagina", "desde_caracter"),
    [(0, 0), (3, 0), (1, -1), (1, 5), (True, 0), (1, "0")],
)
def test_paginar_rejects_bad_cursors(desde_pagina: object, desde_caracter: object) -> None:
    with pytest.raises(LecturaInvalidaError):
        paginar(["abcde", "x"], desde_pagina=desde_pagina, desde_caracter=desde_caracter, max_caracteres=1000)  # type: ignore[arg-type]


def test_paginar_empty_document() -> None:
    chunk, cursor = paginar([], desde_pagina=1, desde_caracter=0, max_caracteres=1000)
    assert chunk == ()
    assert cursor is None


# --------------------------------------------------------------------------- leer_pdf


def test_leer_pdf_real_page_fixture(site: Site, pdfs: Path) -> None:
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    with site.client() as client:
        result = leer_pdf(client, "BOME-P-2026-4784")
    assert result.cve == "BOME-P-2026-4784"
    assert result.fuente == "pdf"
    assert result.total_paginas == 1
    assert result.completo is True
    assert result.siguiente is None
    assert result.paginas[0].texto.startswith("CIUDAD AUTÓNOMA DE MELILLA")
    assert "ACUERDO DEL CONSEJO DE GOBIERNO" in result.paginas[0].texto
    assert result.aviso is None
    assert result.metadatos["ruta"] == str(pdfs / "BOME-P-2026-4784.pdf")
    data = result.to_dict()
    json.dumps(data)
    assert data["siguiente"] is None
    assert data["paginas"][0]["numero"] == 1


def test_leer_pdf_chunks_with_cursor(site: Site, pdfs: Path) -> None:
    pages = ["A" * 600, "B" * 600, "C" * 300, "D" * 2500]
    site.pdf("BOME-B-2026-6416", make_pdf(pages))
    collected: list[tuple[int, str]] = []
    with site.client() as client:
        first = leer_pdf(client, "BOME-B-2026-6416", max_caracteres=1000)
        assert [p.numero for p in first.paginas] == [1]
        assert first.siguiente == Cursor(2, 0)
        assert first.completo is False
        cursor = first.siguiente
        collected += [(p.numero, p.texto) for p in first.paginas]
        while cursor is not None:
            chunk = leer_pdf(
                client,
                "BOME-B-2026-6416",
                desde_pagina=cursor.desde_pagina,
                desde_caracter=cursor.desde_caracter,
                max_caracteres=1000,
            )
            collected += [(p.numero, p.texto) for p in chunk.paginas]
            cursor = chunk.siguiente
            assert chunk.completo is False
    # Downloaded once, then read from the cache.
    assert site.paths().count("/bome/descargar/BOME-B-2026-6416.pdf") == 1
    rebuilt = {n: "".join(t for m, t in collected if m == n) for n in range(1, 5)}
    assert [rebuilt[n] for n in range(1, 5)] == pages


def test_leer_pdf_whole_document_is_complete(site: Site, pdfs: Path) -> None:
    site.pdf("BOME-B-2026-6416", make_pdf(["A" * 600, "", "C" * 300]))
    with site.client() as client:
        result = leer_pdf(client, "BOME-B-2026-6416", max_caracteres=5000)
    assert result.completo is True
    assert result.siguiente is None
    assert [p.sin_texto for p in result.paginas] == [False, True, False]
    assert result.aviso is not None and "2" in result.aviso


def test_leer_pdf_budget_is_clamped(site: Site, pdfs: Path) -> None:
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    with site.client() as client:
        low = leer_pdf(client, "BOME-P-2026-4784", max_caracteres=10)
        high = leer_pdf(client, "BOME-P-2026-4784", max_caracteres=10**9)
    assert low.max_caracteres == 1000
    assert len(low.paginas[0].texto) == 1000
    assert low.siguiente == Cursor(1, 1000)
    assert high.max_caracteres == 100_000
    with site.client() as client, pytest.raises(LecturaInvalidaError):
        leer_pdf(client, "BOME-P-2026-4784", max_caracteres="lots")  # type: ignore[arg-type]
    with site.client() as client, pytest.raises(LecturaInvalidaError):
        leer_pdf(client, "BOME-P-2026-4784", desde_pagina=2)


# --------------------------------------------------------------------------- leer_articulo


def test_leer_articulo_by_bulletin_and_number(site: Site) -> None:
    site.page("/bome/BOME-B-2026-6416/articulo/1051", "art1051.html")
    with site.client() as client:
        result = leer_articulo(client, "BOME-B-2026-6416", 1051, max_caracteres=5000)
    assert result.cve == "BOME-A-2026-1051"
    assert result.fuente == "html"
    assert result.url == f"{BASE}/bome/BOME-B-2026-6416/articulo/1051"
    assert result.total_paginas == 4
    assert [p.pagina_bome for p in result.paginas][0] == 4784
    assert result.paginas[0].texto.startswith("El Consejo de Gobierno")
    assert result.siguiente is not None
    meta = result.metadatos
    assert meta["bome_cve"] == "BOME-B-2026-6416"
    assert meta["numero"] == 1051
    assert meta["bome_fecha"] == date(2026, 9, 22).isoformat()
    assert meta["sumario"].startswith("Acuerdo del Consejo de Gobierno")
    assert meta["pdf_url"] == f"{BASE}/bome/descargar/BOME-A-2026-1051.pdf"
    json.dumps(result.to_dict())


def test_leer_articulo_continues_with_the_same_cursor(site: Site) -> None:
    site.page("/bome/BOME-B-2026-6416/articulo/1051", "art1051.html")
    with site.client() as client:
        whole = leer_articulo(client, "BOME-B-2026-6416", 1051, max_caracteres=100_000)
        parts = []
        cursor: Cursor | None = Cursor(1, 0)
        while cursor is not None:
            chunk = leer_articulo(
                client,
                "BOME-B-2026-6416",
                1051,
                desde_pagina=cursor.desde_pagina,
                desde_caracter=cursor.desde_caracter,
                max_caracteres=1000,
            )
            parts.extend(chunk.paginas)
            cursor = chunk.siguiente
    assert whole.completo is True
    assert "".join(p.texto for p in parts) == "".join(p.texto for p in whole.paginas)


def test_leer_articulo_by_article_cve_resolves_without_double_fetch(site: Site) -> None:
    site.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "/bome/BOME-B-2026-6416/articulo/1051"}
    )
    site.page("/bome/BOME-B-2026-6416/articulo/1051", "art1051.html")
    with site.client() as client:
        result = leer_articulo(client, " bome-a-2026-1051 ")
    assert result.cve == "BOME-A-2026-1051"
    assert site.paths() == ["/buscar-cve", "/bome/BOME-B-2026-6416/articulo/1051"]
    assert site.requests[0].url.params["cve"] == "BOME-A-2026-1051"


# --------------------------------------------------------------------------- extraordinary articles


BX41 = (FIXTURES / "bx41.html").read_text("utf-8")
ART1051 = (FIXTURES / "art1051.html").read_text("utf-8")


def bx_page(year: int, number: int, first: int | None, last: int | None) -> str:
    """bx41.html as ``BOME-BX-{year}-{number}`` listing AX ``first`` and ``last`` (or nothing)."""
    html = BX41.replace("BOME-BX-2026-41", f"BOME-BX-{year}-{number}")
    if first is None or last is None:
        return html.replace("(CVE: BOME-AX-2026-102)", "").replace("(CVE: BOME-AX-2026-103)", "")
    for old, new in ((102, first), (103, last)):
        html = html.replace(f"BOME-AX-2026-{old}", f"BOME-AX-{year}-{new}")
        html = html.replace(f"/articulo/{old}'", f"/articulo/{new}'")
        html = html.replace(f"ARTÍCULO {old}", f"ARTÍCULO {new}")
    return html


def article_page(article_cve: str, bulletin_cve: str) -> str:
    """art1051.html re-labelled as ``article_cve`` of ``bulletin_cve``."""
    number = article_cve.rsplit("-", 1)[1]
    return (
        ART1051.replace("BOME-A-2026-1051", article_cve)
        .replace("ARTÍCULO 1051", f"ARTÍCULO {number}")
        .replace("BOME-B-2026-6416", bulletin_cve)
    )


def calendar_json(year: int, extraordinary: int, ordinary: int = 3) -> str:
    items = [
        {"title": f"Nº {5600 + n}", "start": f"{year}-01-{n + 1:02d}", "url": f"/bome/BOME-B-{year}-{5600 + n}"}
        for n in range(ordinary)
    ]
    # Deliberately out of order: the search sorts the bulletins itself.
    items += [
        {"title": f"Nº {n}", "start": f"{year}-{1 + n // 28:02d}-{1 + n % 28:02d}", "url": f"/bome/BOME-BX-{year}-{n}"}
        for n in reversed(range(1, extraordinary + 1))
    ]
    return json.dumps(items)


def extraordinary_year(
    site: Site, year: int, count: int, listed: Callable[[int], tuple[int, int] | None]
) -> None:
    """Calendar with ``count`` BX bulletins of ``year``; bulletin k lists ``listed(k)``."""
    body = calendar_json(year, count)
    site.routes["/api/bomes/calendar"] = lambda request: httpx.Response(200, text=body)
    for k in range(1, count + 1):
        span = listed(k)
        html = bx_page(year, k, *(span if span is not None else (None, None)))
        site.routes[f"/bome/BOME-BX-{year}-{k}"] = lambda request, html=html: httpx.Response(200, text=html)


def bulletin_fetches(site: Site) -> list[str]:
    return [p for p in site.paths() if p.startswith("/bome/BOME-BX-") and "/articulo/" not in p]


def site_bug_resolver(site: Site) -> None:
    """The live resolver bug: the X of an AX CVE is dropped (verified 2026-09-24)."""
    site.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "/bome/BOME-B-2019-5625/articulo/103"}
    )
    impostor = article_page("BOME-A-2019-103", "BOME-B-2019-5625")
    site.routes["/bome/BOME-B-2019-5625/articulo/103"] = lambda request: httpx.Response(200, text=impostor)


def serve_article(site: Site, article_cve: str, bulletin_cve: str) -> None:
    html = article_page(article_cve, bulletin_cve)
    number = article_cve.rsplit("-", 1)[1]
    site.routes[f"/bome/{bulletin_cve}/articulo/{number}"] = lambda request: httpx.Response(200, text=html)


def four_per_bulletin(k: int) -> tuple[int, int]:
    """BX k lists 4k+6..4k+9, so 103 is hidden inside BX-24 (102..105)."""
    return 4 * k + 6, 4 * k + 9


def test_ax_article_is_never_the_ordinary_one_the_resolver_points_to(site: Site) -> None:
    # Bug report 2026-09-24: BOME-AX-2019-103 came back as the ordinary A-2019-103.
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, four_per_bulletin)
    serve_article(site, "BOME-AX-2019-103", "BOME-BX-2019-24")
    with site.client() as client:
        result = leer_articulo(client, "BOME-AX-2019-103")
    assert result.cve == "BOME-AX-2019-103"
    assert result.metadatos["bome_cve"] == "BOME-BX-2019-24"
    assert result.url == f"{BASE}/bome/BOME-BX-2019-24/articulo/103"
    # The ordinary page the resolver points to is never even fetched.
    assert "/bome/BOME-B-2019-5625/articulo/103" not in site.paths()
    calendar = next(r for r in site.requests if r.url.path == "/api/bomes/calendar")
    assert (calendar.url.params["start"], calendar.url.params["end"]) == ("2019-01-01", "2019-12-31")
    # Binary search over 30 BX bulletins: at most ceil(log2(30)) + 2 pages.
    assert 1 <= len(bulletin_fetches(site)) <= 7
    assert site.paths()[-1] == "/bome/BOME-BX-2019-24/articulo/103"


def test_ax_article_uses_the_index_shortcut_without_resolver_or_calendar(site: Site) -> None:
    serve_article(site, "BOME-AX-2019-103", "BOME-BX-2019-24")
    asked: list[str] = []

    def indexed(cve: object) -> object:
        asked.append(str(cve))
        return documents_module.parse_cve("BOME-BX-2019-24")

    with site.client() as client:
        result = leer_articulo(client, "BOME-AX-2019-103", boletin_de_articulo=indexed)
    assert result.cve == "BOME-AX-2019-103"
    assert asked == ["BOME-AX-2019-103"]
    assert site.paths() == ["/bome/BOME-BX-2019-24/articulo/103"]


def test_ax_article_with_a_stale_index_hint_falls_back_to_the_search(site: Site) -> None:
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, four_per_bulletin)
    serve_article(site, "BOME-AX-2019-103", "BOME-BX-2019-24")
    wrong = article_page("BOME-AX-2019-99", "BOME-BX-2019-23")
    site.routes["/bome/BOME-BX-2019-23/articulo/103"] = lambda request: httpx.Response(200, text=wrong)
    with site.client() as client:
        result = leer_articulo(
            client,
            "BOME-AX-2019-103",
            boletin_de_articulo=lambda cve: documents_module.parse_cve("BOME-BX-2019-23"),
        )
    assert result.cve == "BOME-AX-2019-103"
    assert result.metadatos["bome_cve"] == "BOME-BX-2019-24"


def test_ax_index_hint_of_another_kind_or_year_is_ignored(site: Site) -> None:
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, four_per_bulletin)
    serve_article(site, "BOME-AX-2019-103", "BOME-BX-2019-24")
    for hint in ("BOME-B-2019-5625", "BOME-BX-2018-24"):
        site.requests.clear()
        with site.client() as client:
            result = leer_articulo(
                client,
                "BOME-AX-2019-103",
                boletin_de_articulo=lambda cve, hint=hint: documents_module.parse_cve(hint),
            )
        assert result.cve == "BOME-AX-2019-103"
        assert not any(p.startswith(("/bome/BOME-B-", "/bome/BOME-BX-2018")) for p in site.paths())


def test_ax_article_resolved_to_an_extraordinary_bulletin_is_used(site: Site) -> None:
    # Should the site fix its resolver, its (verified) answer is taken as is.
    site.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "/bome/BOME-BX-2019-24/articulo/103"}
    )
    serve_article(site, "BOME-AX-2019-103", "BOME-BX-2019-24")
    with site.client() as client:
        result = leer_articulo(client, "BOME-AX-2019-103")
    assert result.cve == "BOME-AX-2019-103"
    assert site.paths() == ["/buscar-cve", "/bome/BOME-BX-2019-24/articulo/103"]


def test_ax_article_hidden_between_two_bulletins_is_tried_in_both(site: Site) -> None:
    # BX k lists 4k+6..4k+8: 101 falls between BX-23 (98..100) and BX-24 (102..104).
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, lambda k: (4 * k + 6, 4 * k + 8))
    serve_article(site, "BOME-AX-2019-101", "BOME-BX-2019-24")
    site.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "/bome/BOME-B-2019-5625/articulo/101"}
    )
    with site.client() as client:
        result = leer_articulo(client, "BOME-AX-2019-101")
    assert result.cve == "BOME-AX-2019-101"
    assert result.metadatos["bome_cve"] == "BOME-BX-2019-24"
    articles = [p for p in site.paths() if "/articulo/" in p]
    assert articles == ["/bome/BOME-BX-2019-23/articulo/101", "/bome/BOME-BX-2019-24/articulo/101"]


def article_fetches(site: Site) -> list[str]:
    return [p for p in site.paths() if "/articulo/" in p]


def listed_except(overrides: dict[int, tuple[int, int] | None]) -> Callable[[int], tuple[int, int] | None]:
    """``four_per_bulletin`` except for the bulletins in ``overrides``."""
    return lambda k: overrides[k] if k in overrides else four_per_bulletin(k)


def first_three_unfetched(
    listed: dict[int, tuple[int, int] | None],
) -> Callable[[int], tuple[int, int] | None]:
    """8 BX bulletins, BX-4..BX-8 list nothing: the binary search spends its 5
    pages on them (ceil(log2(8)) + 2) and never fetches BX-1..BX-3."""
    return lambda k: listed.get(k)


def test_ax_article_in_a_bulletin_whose_page_lists_nothing_is_found(site: Site) -> None:
    # BX-24 lists nothing: 103 falls between BX-23 (98..101) and BX-25 (106..109).
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, listed_except({24: None}))
    serve_article(site, "BOME-AX-2019-103", "BOME-BX-2019-24")
    with site.client() as client:
        result = leer_articulo(client, "BOME-AX-2019-103")
    assert result.cve == "BOME-AX-2019-103"
    assert result.metadatos["bome_cve"] == "BOME-BX-2019-24"
    # The empty bulletin is tried before the listed neighbours.
    assert article_fetches(site) == ["/bome/BOME-BX-2019-24/articulo/103"]


def test_ax_article_in_the_second_of_two_empty_bulletins_is_found(site: Site) -> None:
    # BX-24 and BX-25 list nothing; 106 lives in BX-25.
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, listed_except({23: (98, 101), 24: None, 25: None}))
    serve_article(site, "BOME-AX-2019-106", "BOME-BX-2019-25")
    site.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "/bome/BOME-B-2019-5625/articulo/106"}
    )
    with site.client() as client:
        result = leer_articulo(client, "BOME-AX-2019-106")
    assert result.cve == "BOME-AX-2019-106"
    assert result.metadatos["bome_cve"] == "BOME-BX-2019-25"
    assert article_fetches(site) == [
        "/bome/BOME-BX-2019-24/articulo/106",
        "/bome/BOME-BX-2019-25/articulo/106",
    ]


def test_ax_article_hidden_at_the_lower_edge_is_still_found(site: Site) -> None:
    # BX-23 lists 98..100 and hides 101; BX-24 lists nothing.
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, listed_except({23: (98, 100), 24: None}))
    serve_article(site, "BOME-AX-2019-101", "BOME-BX-2019-23")
    with site.client() as client:
        result = leer_articulo(client, "BOME-AX-2019-101")
    assert result.metadatos["bome_cve"] == "BOME-BX-2019-23"
    assert article_fetches(site) == [
        "/bome/BOME-BX-2019-24/articulo/101",
        "/bome/BOME-BX-2019-23/articulo/101",
    ]


def test_ax_unfetched_gap_bulletin_listing_the_number_is_used_directly(site: Site) -> None:
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 8, first_three_unfetched({1: (1, 3), 2: (4, 6)}))
    serve_article(site, "BOME-AX-2019-5", "BOME-BX-2019-2")
    with site.client() as client:
        result = leer_articulo(client, "BOME-AX-2019-5")
    assert result.metadatos["bome_cve"] == "BOME-BX-2019-2"
    # Gap pages are read first (200s); no article guess, so no 404 for the site guard.
    assert article_fetches(site) == ["/bome/BOME-BX-2019-2/articulo/5"]
    assert bulletin_fetches(site)[-2:] == ["/bome/BOME-BX-2019-1", "/bome/BOME-BX-2019-2"]


def test_ax_listed_gap_page_without_the_number_is_dropped(site: Site) -> None:
    # BX-1 (1..2) is superseded by BX-2 (3..4) as the lower neighbour of 6;
    # BX-3 (7..8) is the upper one and hides 6 at its start.
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 8, first_three_unfetched({1: (1, 2), 2: (3, 4), 3: (7, 8)}))
    serve_article(site, "BOME-AX-2019-6", "BOME-BX-2019-3")
    with site.client() as client:
        result = leer_articulo(client, "BOME-AX-2019-6")
    assert result.metadatos["bome_cve"] == "BOME-BX-2019-3"
    assert article_fetches(site) == [
        "/bome/BOME-BX-2019-2/articulo/6",
        "/bome/BOME-BX-2019-3/articulo/6",
    ]


def test_ax_lookup_stops_after_two_article_pages_and_lists_the_rest(site: Site) -> None:
    # Candidates for 106: BX-24, BX-25 (list nothing), BX-23 (below), BX-26 (above).
    # Two wrong guesses (404s) leave one error of the site guard's 3-per-10-min
    # budget for the explicit call the error suggests.
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, listed_except({23: (98, 101), 24: None, 25: None}))
    with site.client() as client:
        with pytest.raises(documents_module.ArticuloNoLocalizadoError) as caught:
            leer_articulo(client, "BOME-AX-2019-106")
    assert documents_module.MAX_INTENTOS_ARTICULO_EXTRA == 2
    assert article_fetches(site) == [
        "/bome/BOME-BX-2019-24/articulo/106",
        "/bome/BOME-BX-2019-25/articulo/106",
    ]
    message = str(caught.value)
    assert "BOME-BX-2019-23" in message and "BOME-BX-2019-26" in message
    assert "cve='BOME-BX-2019-23'" in message and "numero=106" in message


def test_ax_shortcut_article_pages_count_toward_the_cap(site: Site) -> None:
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, listed_except({23: (98, 101), 24: None, 25: None}))
    with site.client() as client:
        with pytest.raises(documents_module.ArticuloNoLocalizadoError) as caught:
            leer_articulo(
                client,
                "BOME-AX-2019-106",
                boletin_de_articulo=lambda cve: documents_module.parse_cve("BOME-BX-2019-10"),
            )
    assert article_fetches(site) == [
        "/bome/BOME-BX-2019-10/articulo/106",
        "/bome/BOME-BX-2019-24/articulo/106",
    ]
    message = str(caught.value)
    assert all(f"BOME-BX-2019-{n}" in message for n in (25, 23, 26))


def test_ax_gap_page_budget_exhausted_is_a_clear_error(site: Site) -> None:
    # 16 BX bulletins listing nothing: 6 search pages, then 4 gap pages, 6 left unchecked.
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 16, lambda k: None)
    with site.client() as client:
        with pytest.raises(documents_module.ArticuloNoLocalizadoError) as caught:
            leer_articulo(client, "BOME-AX-2019-103")
    assert documents_module.BUSQUEDA_EXTRA_HUECO == 4
    assert len(bulletin_fetches(site)) == 6 + 4
    assert article_fetches(site) == []  # no blind guess spends the site guard's error budget
    message = str(caught.value)
    assert "6 bulletins" in message and "unchecked" in message
    assert "BOME-BX-2019-1 " in message and "BOME-BX-2019-16" in message
    assert "numero=103" in message


def test_ax_binary_search_is_bounded_and_ends_in_a_clear_error(site: Site) -> None:
    # Worst case: 64 BX bulletins whose pages list nothing.
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 64, lambda k: None)
    with site.client() as client:
        with pytest.raises(documents_module.ArticuloNoLocalizadoError) as caught:
            leer_articulo(client, "BOME-AX-2019-103")
    assert isinstance(caught.value, BomeNotFoundError)
    assert len(bulletin_fetches(site)) <= 8 + 4  # ceil(log2(64)) + 2, then the gap budget
    message = str(caught.value)
    assert "BOME-AX-2019-103" in message and "numero" in message and "BOME-BX-2019-" in message
    assert "/bome/BOME-B-2019-5625/articulo/103" not in site.paths()


def test_ax_number_beyond_every_listed_article_is_not_found(site: Site) -> None:
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 30, four_per_bulletin)
    with site.client() as client:
        with pytest.raises(documents_module.ArticuloNoLocalizadoError):
            leer_articulo(client, "BOME-AX-2019-900")
    assert len(bulletin_fetches(site)) <= 7


def test_ax_year_without_extraordinary_bulletins_is_not_found(site: Site) -> None:
    site_bug_resolver(site)
    extraordinary_year(site, 2019, 0, four_per_bulletin)
    with site.client() as client:
        with pytest.raises(documents_module.ArticuloNoLocalizadoError) as caught:
            leer_articulo(client, "BOME-AX-2019-103")
    assert "numero" in str(caught.value)
    assert bulletin_fetches(site) == []


def test_article_cve_whose_page_shows_another_article_is_an_error(site: Site) -> None:
    site.routes["/buscar-cve"] = lambda request: httpx.Response(
        302, headers={"location": "/bome/BOME-B-2026-6416/articulo/1050"}
    )
    site.page("/bome/BOME-B-2026-6416/articulo/1050", "art1051.html")  # shows BOME-A-2026-1051
    with site.client() as client:
        with pytest.raises(documents_module.ArticuloDistintoError) as caught:
            leer_articulo(client, "BOME-A-2026-1050")
    assert isinstance(caught.value, BomeNotFoundError)
    assert "BOME-A-2026-1050" in str(caught.value) and "BOME-A-2026-1051" in str(caught.value)


def test_leer_articulo_argument_validation(site: Site) -> None:
    with site.client() as client:
        with pytest.raises(LecturaInvalidaError):
            leer_articulo(client, "BOME-B-2026-6416")  # number missing
        with pytest.raises(LecturaInvalidaError):
            leer_articulo(client, "BOME-A-2026-1051", 1051)  # number is implied
        with pytest.raises(LecturaInvalidaError):
            leer_articulo(client, "BOME-P-2026-4784")
        with pytest.raises(LecturaInvalidaError):
            leer_articulo(client, "BOME-B-2026-6416", 0)
    assert site.requests == []


def test_leer_articulo_2014_stub_without_pdf(site: Site) -> None:
    # art2014.html has no HTML text and no PDF link; live the PDF answers 404.
    site.page("/bome/BOME-B-2014-5092/articulo/2", "art2014.html")
    with site.client() as client:
        result = leer_articulo(client, "BOME-B-2014-5092", 2)
    assert site.paths() == ["/bome/BOME-B-2014-5092/articulo/2"]
    assert result.fuente == "ninguna"
    assert result.paginas == ()
    assert result.total_paginas == 0
    assert result.siguiente is None
    assert result.completo is False
    assert result.aviso is not None
    assert f"{BASE}/bome/descargar/BOME-B-2014-5092.pdf" in result.aviso
    assert result.metadatos["encabezado"].endswith("Dirección General de Servicios Sociales")
    json.dumps(result.to_dict())


def test_leer_articulo_stub_with_pdf_link_falls_back_to_pdf(site: Site, pdfs: Path) -> None:
    html = (FIXTURES / "art2014.html").read_text("utf-8").replace(
        "VOLVER AL BOME\n                        </a>",
        "VOLVER AL BOME\n                        </a>"
        '<a data-bome-tipo="Articulo" href="/bome/descargar/BOME-A-2014-2.pdf">PDF</a>',
    )
    site.routes["/bome/BOME-B-2014-5092/articulo/2"] = lambda request: httpx.Response(200, text=html)
    site.pdf("BOME-A-2014-2", make_pdf(["Texto escaneado OCR"]))
    with site.client() as client:
        result = leer_articulo(client, "BOME-B-2014-5092", 2)
    assert result.fuente == "pdf"
    assert result.cve == "BOME-A-2014-2"
    assert result.paginas[0].texto == "Texto escaneado OCR"
    assert result.metadatos["bome_cve"] == "BOME-B-2014-5092"


def test_leer_articulo_stub_whose_pdf_is_missing(site: Site, pdfs: Path) -> None:
    html = (FIXTURES / "art2014.html").read_text("utf-8").replace(
        "VOLVER AL BOME\n                        </a>",
        "VOLVER AL BOME\n                        </a>"
        '<a data-bome-tipo="Articulo" href="/bome/descargar/BOME-A-2014-2.pdf">PDF</a>',
    )
    site.routes["/bome/BOME-B-2014-5092/articulo/2"] = lambda request: httpx.Response(200, text=html)
    with site.client() as client:
        result = leer_articulo(client, "BOME-B-2014-5092", 2)
    assert result.fuente == "ninguna"
    assert result.aviso is not None and "404" in result.aviso


# --------------------------------------------------------------------------- leer_boletin


def test_leer_boletin_reads_the_pdf_with_metadata(site: Site, pdfs: Path) -> None:
    site.page("/bome/BOME-B-2026-6416", "b6416.html")
    site.pdf("BOME-B-2026-6416", make_pdf(["A" * 600, "B" * 600]))
    with site.client() as client:
        result = leer_boletin(client, "bome-b-2026-6416", max_caracteres=1000)
    assert result.cve == "BOME-B-2026-6416"
    assert result.fuente == "pdf"
    assert result.total_paginas == 2
    assert result.siguiente == Cursor(2, 0)
    meta = result.metadatos
    assert meta["numero"] == 6416
    assert meta["fecha"] == "2026-09-22"
    assert meta["extraordinario"] is False
    assert meta["total_articulos"] == 13
    assert meta["ruta"] == str(pdfs / "BOME-B-2026-6416.pdf")
    json.dumps(result.to_dict())


def test_leer_boletin_without_pdf_returns_a_clear_result(site: Site, pdfs: Path) -> None:
    site.page("/bome/BOME-B-2014-5092", "b5092.html")
    with site.client() as client:
        result = leer_boletin(client, "BOME-B-2014-5092")
    assert result.fuente == "ninguna"
    assert result.paginas == ()
    assert result.aviso is not None and "404" in result.aviso
    assert result.metadatos["total_articulos"] == 4


def test_leer_boletin_rejects_non_bulletin_cves(site: Site) -> None:
    with site.client() as client, pytest.raises(LecturaInvalidaError):
        leer_boletin(client, "BOME-A-2026-1051")
    assert site.requests == []


# --------------------------------------------------------------------------- old portal PDFs (site-guard task 7)


OLD_PDF_URL = "https://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf"
OLD_PDF = (FIXTURES / "antiguo" / "5302_73.pdf").read_bytes()


class FakePortal:
    """Stands in for PortalAntiguo.pdf: records the URLs it was asked for."""

    def __init__(self, body: bytes = OLD_PDF) -> None:
        self.body = body
        self.urls: list[str] = []

    def pdf(self, url: str, *, max_bytes: int | None = None) -> bytes:
        self.urls.append(url)
        return self.body


@pytest.mark.parametrize(
    ("url", "name"),
    [
        (OLD_PDF_URL, "melilla-9-4914-5302_73.pdf"),
        ("http://www.melilla.es/mandar.php/n/9/4913/5302.pdf", "melilla-9-4913-5302.pdf"),
        (" https://www.melilla.es/mandar.php/n/0/1235/9_452.pdf ", "melilla-0-1235-9_452.pdf"),
    ],
)
def test_old_pdf_file_name_comes_only_from_the_validated_url(url: str, name: str) -> None:
    assert documents_module.nombre_pdf_antiguo(url) == name


def test_old_pdf_download_is_cached_under_the_derived_name(pdfs: Path) -> None:
    portal = FakePortal()
    first = documents_module.descargar_pdf_antiguo(portal, "http://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf")
    assert first.ruta == str(pdfs / "melilla-9-4914-5302_73.pdf")
    assert first.url == OLD_PDF_URL and first.cve is None and first.origen == "melilla.es"
    assert first.cache_hit is False and first.total_paginas == 1
    assert first.sha256 == hashlib.sha256(OLD_PDF).hexdigest()
    assert portal.urls == [OLD_PDF_URL]  # the portal receives the validated https URL
    again = documents_module.descargar_pdf_antiguo(portal, OLD_PDF_URL)
    assert again.cache_hit is True and portal.urls == [OLD_PDF_URL]
    documents_module.descargar_pdf_antiguo(portal, OLD_PDF_URL, refrescar=True)
    assert len(portal.urls) == 2
    assert sorted(p.name for p in pdfs.iterdir()) == ["melilla-9-4914-5302_73.pdf"]


def test_old_pdf_reading_follows_the_cursor_contract(pdfs: Path) -> None:
    reading = documents_module.leer_pdf_antiguo(FakePortal(), OLD_PDF_URL)
    assert reading.cve is None and reading.origen == "melilla.es"
    assert reading.fuente == "pdf" and reading.url == OLD_PDF_URL
    assert reading.completo is True and reading.siguiente is None
    assert "4328" in reading.paginas[0].texto
    with pytest.raises(LecturaInvalidaError):
        documents_module.leer_pdf_antiguo(FakePortal(), OLD_PDF_URL, desde_pagina=2)


def test_old_pdf_rejects_foreign_urls_before_touching_disk_or_network(pdfs: Path) -> None:
    from bome_navaja.antiguo import UrlPdfInvalidaError

    portal = FakePortal()
    for bad in ("https://evil.example/mandar.php/n/9/4914/5302_73.pdf", "../../etc/passwd", ""):
        with pytest.raises(UrlPdfInvalidaError):
            documents_module.descargar_pdf_antiguo(portal, bad)
        with pytest.raises(UrlPdfInvalidaError):
            documents_module.nombre_pdf_antiguo(bad)
    assert portal.urls == [] and not pdfs.exists()


def test_an_unreadable_old_pdf_is_not_cached(pdfs: Path) -> None:
    with pytest.raises(BomeParseError):
        documents_module.descargar_pdf_antiguo(FakePortal(b"%PDF-1.4 garbage"), OLD_PDF_URL)
    assert not (pdfs / "melilla-9-4914-5302_73.pdf").exists()


def test_bomemelilla_downloads_keep_their_origin(site: Site, pdfs: Path) -> None:
    site.pdf("BOME-P-2026-4784", PAGE_PDF)
    assert descargar_pdf(site.client(), "BOME-P-2026-4784").origen == "bomemelilla.es"
    assert leer_pdf(site.client(), "BOME-P-2026-4784").origen == "bomemelilla.es"
