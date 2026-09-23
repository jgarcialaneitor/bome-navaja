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
