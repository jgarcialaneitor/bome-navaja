"""HTTP client for bomemelilla.es (Boletín Oficial de la Ciudad Autónoma de Melilla).

Endpoint map (read-only reconnaissance, 2026-09-23):

* ``GET /api/bomes/calendar?start&end``    JSON list of bulletins (400 without both).
* ``GET /api/bomes/{year}``                HTML month slider of one year.
* ``GET /api/section/consejerias/{id}``    JSON ``[{id, nombre}]`` of a departamento.
* ``GET /api/section/organismos/{id}``     JSON ``[{id, nombre}]`` of a consejería.
* ``GET /bome/{CVE}``                      bulletin page (article tree).
* ``GET /bome/{CVE}/sumario``              sumario web view.
* ``GET /bome/{CVE}/articulo/{n}``         full article text.
* ``GET /bome/descargar/{CVE}.pdf``        PDF of any CVE.
* ``GET /buscar-cve?cve=``                 302 to the canonical page of a CVE.
* ``GET /buscar`` / ``/buscador-avanzado`` search (bulletins only, 10 per page).
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import date
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

from .cve import (
    BASE_URL,
    Cve,
    CveKind,
    InvalidCveError,
    article_path,
    bulletin_path,
    parse_cve,
    pdf_path,
    sumario_path,
)
from .models import (
    Article,
    BomeDocumentTooLargeError,
    BomeError,
    BomeHTTPError,
    BomeNotFoundError,
    BomeParseError,
    Bulletin,
    BulletinRef,
    Entity,
    SearchPage,
    Sumario,
)
from .parsers import (
    parse_article_page,
    parse_bulletin_page,
    parse_calendar,
    parse_entities,
    parse_search_page,
    parse_sumario_page,
    parse_year_slider,
)

__all__ = [
    "BomeClient",
    "BomeError",
    "BomeHTTPError",
    "BomeNotFoundError",
    "BomeParseError",
    "SearchPath",
]

DEFAULT_TIMEOUT = 30.0

# First bulletin published on the site: BOME-B-2014-5092 (2014-01-03).
FIRST_YEAR = 2014

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

SearchPath = Literal["/buscar", "/buscador-avanzado"]
_SEARCH_PATHS: frozenset[str] = frozenset({"/buscar", "/buscador-avanzado"})
_GENERIC_CVE = re.compile(r"BOME-[A-Z]{1,2}-\d{4}-\d+")


def _today() -> date:
    """Today's date; a seam so tests can pin it."""
    return date.today()


def _raise_for_status(response: httpx.Response) -> None:
    """Map 404 to :class:`BomeNotFoundError` and other 4xx/5xx to :class:`BomeHTTPError`."""
    final_url = str(response.url)
    if response.status_code == 404:
        raise BomeNotFoundError(f"not found: {final_url}", status=404, url=final_url)
    if response.status_code >= 400:
        raise BomeHTTPError(
            f"HTTP {response.status_code} for {final_url}",
            status=response.status_code,
            url=final_url,
        )


class BomeClient:
    """Synchronous client over :class:`httpx.Client`.

    ``polite_delay`` seconds are enforced between consecutive requests (use a
    small value such as 0.5 for bulk work like index sync or drill-down).
    """

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        polite_delay: float = 0.0,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.polite_delay = polite_delay
        self._last_request: float | None = None
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "es-ES,es;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            },
        )

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has run."""
        return self._client.is_closed

    def __enter__(self) -> BomeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ transport

    def _wait_politely(self) -> None:
        if self.polite_delay <= 0 or self._last_request is None:
            return
        remaining = self.polite_delay - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def _request(
        self,
        path: str,
        *,
        params: Sequence[tuple[str, str]] | dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> httpx.Response:
        """GET ``path`` (site-relative or absolute) and map failures to ``BomeError``.

        Every failure without an HTTP answer (transport errors, malformed
        redirects, ``httpx.InvalidURL``, use after :meth:`close`) becomes
        :class:`BomeHTTPError` with ``status=None``, so callers can treat
        "the site could not be asked" uniformly.
        """
        url = path if path.startswith(("http://", "https://")) else self.base_url + path
        if self._client.is_closed:
            raise BomeHTTPError(f"client is closed; cannot request {url}", status=None, url=url)
        self._wait_politely()
        try:
            response = self._client.get(path, params=params, follow_redirects=follow_redirects)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise BomeHTTPError(f"request to {url!r} failed: {exc}", status=None, url=url) from exc
        finally:
            if self.polite_delay > 0:
                self._last_request = time.monotonic()
        _raise_for_status(response)
        return response

    def _get_text(
        self, path: str, *, params: Sequence[tuple[str, str]] | dict[str, str] | None = None
    ) -> str:
        response = self._request(path, params=params)
        text = response.text
        if not text.strip():
            url = str(response.url)
            raise BomeNotFoundError(f"empty page: {url}", status=response.status_code, url=url)
        return text

    # ------------------------------------------------------------------ listings

    def calendar(self, start: date, end: date) -> list[BulletinRef]:
        """Bulletins published from ``start`` to ``end``, both inclusive."""
        if end < start:
            raise ValueError(f"end {end} is before start {start}")
        text = self._get_text(
            "/api/bomes/calendar",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        return parse_calendar(text, base_url=self.base_url)

    def all_bulletins(self) -> list[BulletinRef]:
        """Every bulletin on the site, from 2014-01-01 to today, in one request.

        The site treats ``end`` as inclusive (verified live 2026-09-23), so
        today's bulletin is included.
        """
        return self.calendar(date(FIRST_YEAR, 1, 1), _today())

    def year_bulletins(self, year: int) -> list[BulletinRef]:
        """Bulletins of one year from the home-page slider, newest first."""
        text = self._get_text(f"/api/bomes/{int(year)}")
        return parse_year_slider(text, base_url=self.base_url)

    def consejerias(self, departamento_id: int) -> list[Entity]:
        """Consejerías of a departamento (ids from the advanced-search form)."""
        text = self._get_text(f"/api/section/consejerias/{int(departamento_id)}")
        return parse_entities(text)

    def organismos(self, consejeria_id: int) -> list[Entity]:
        """Organismos of a consejería."""
        text = self._get_text(f"/api/section/organismos/{int(consejeria_id)}")
        return parse_entities(text)

    # ------------------------------------------------------------------ documents

    def bulletin(self, cve: str | Cve) -> Bulletin:
        """A bulletin page with its section → consejería → organismo → article tree."""
        path = bulletin_path(cve)
        return parse_bulletin_page(self._get_text(path), str(parse_cve(cve)), base_url=self.base_url)

    def sumario(self, cve: str | Cve) -> Sumario:
        """The sumario web view; accepts the bulletin CVE or its sumario CVE."""
        bulletin_cve = parse_cve(cve).bulletin_cve()
        text = self._get_text(sumario_path(bulletin_cve))
        return parse_sumario_page(text, str(bulletin_cve), base_url=self.base_url)

    def article(self, bulletin_cve: str | Cve, n: int | str | Cve) -> Article:
        """Article ``n`` of a bulletin in full.

        ``n`` is the article number (its CVE number), or the article CVE
        itself (``BOME-A-2026-1051``), which must belong to the bulletin kind.
        """
        bulletin = parse_cve(bulletin_cve)
        number = self._article_number(bulletin, n)
        return parse_article_page(
            self._get_text(article_path(bulletin, number)), base_url=self.base_url
        )

    @staticmethod
    def _article_number(bulletin: Cve, n: int | str | Cve) -> int:
        if isinstance(n, int):
            if n <= 0:
                raise ValueError(f"article number must be positive: {n}")
            return n
        if isinstance(n, str) and n.strip().isdigit():
            return BomeClient._article_number(bulletin, int(n.strip()))
        article = parse_cve(n)
        expected = bulletin.article_cve(article.number)
        if article.kind not in (CveKind.ARTICLE, CveKind.EXTRA_ARTICLE) or article != expected:
            raise InvalidCveError(f"{article} is not an article CVE matching {bulletin}")
        return article.number

    def resolve_cve(self, cve: str | Cve, *, confirm: bool = True) -> str:
        """Canonical page URL of any CVE, read from the resolver's 302.

        The resolver redirects even for CVEs that do not exist (live:
        ``BOME-B-2099-1`` → ``/bome/BOME-B-2099-1`` → 404), so the target is
        confirmed with one GET and the final URL of that GET is returned.

        Raises :class:`BomeNotFoundError` when the site does not redirect,
        redirects off-site or to the home page, or the target page is missing.
        An off-site target is never requested.

        ``confirm=False`` skips the confirmation GET and returns the redirect
        target as is; use it when the caller fetches that page next anyway
        (it will raise :class:`BomeNotFoundError` itself if missing).
        """
        text = str(cve) if isinstance(cve, Cve) else re.sub(r"\s+", "", cve).upper()
        if _GENERIC_CVE.fullmatch(text) is None:
            raise InvalidCveError(f"not a BOME CVE (BOME-L-AAAA-NNNN): {cve!r}")
        response = self._request("/buscar-cve", params={"cve": text}, follow_redirects=False)
        location = response.headers.get("location")
        url = str(response.url)
        if not response.is_redirect or not location:
            raise BomeNotFoundError(f"CVE {text} did not resolve", status=response.status_code, url=url)
        target = urljoin(self.base_url + "/", location)
        if urlsplit(target).netloc != urlsplit(self.base_url).netloc:
            raise BomeNotFoundError(
                f"CVE {text} redirected off-site to {target}", status=response.status_code, url=url
            )
        if target.rstrip("/") == self.base_url:
            raise BomeNotFoundError(
                f"CVE {text} resolved to the home page", status=response.status_code, url=url
            )
        if not confirm:
            return target
        confirmation = self._request(target)
        return str(confirmation.url)

    def download(self, cve: str | Cve, *, max_bytes: int | None = None) -> bytes:
        """PDF bytes of any CVE (bulletin, sumario, article or page).

        The body is streamed; with ``max_bytes`` the download aborts with
        :class:`BomeDocumentTooLargeError` as soon as the announced
        ``Content-Length`` or the bytes received exceed it.
        """
        path = pdf_path(cve)
        url = self.base_url + path
        if self._client.is_closed:
            raise BomeHTTPError(f"client is closed; cannot request {url}", status=None, url=url)
        self._wait_politely()
        chunks: list[bytes] = []
        received = 0
        try:
            with self._client.stream("GET", path) as response:
                _raise_for_status(response)
                announced = response.headers.get("content-length", "")
                if max_bytes is not None and announced.isdigit() and int(announced) > max_bytes:
                    raise BomeDocumentTooLargeError(
                        f"{url} announces {announced} bytes, over the {max_bytes}-byte limit",
                        size=int(announced),
                        limit=max_bytes,
                    )
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if max_bytes is not None and received > max_bytes:
                        raise BomeDocumentTooLargeError(
                            f"{url} exceeded the {max_bytes}-byte limit while downloading",
                            size=received,
                            limit=max_bytes,
                        )
                    chunks.append(chunk)
                content_type = response.headers.get("content-type")
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise BomeHTTPError(f"request to {url!r} failed: {exc}", status=None, url=url) from exc
        finally:
            if self.polite_delay > 0:
                self._last_request = time.monotonic()
        content = b"".join(chunks)
        if not content.lstrip()[:4] == b"%PDF":
            raise BomeParseError(f"{url} did not return a PDF (content-type {content_type!r})")
        return content

    # ------------------------------------------------------------------ search

    def search_page(
        self,
        path: SearchPath,
        params: Sequence[tuple[str, str]],
        page: int = 1,
    ) -> SearchPage:
        """One results page of ``/buscar`` or ``/buscador-avanzado``.

        ``params`` is sent verbatim and in order (so repeated keys and the
        ``contenido[i][...]`` collections survive); any caller ``page`` entry is
        replaced by ``page``.

        Site quirk (verified live 2026-09-23): on ``/buscar`` the ``contenido``
        text is only applied when ``from`` and ``tipo`` are sent too. With
        ``contenido`` alone the site silently ignores it and returns the
        unfiltered list of current-year bulletins. This method does not guard
        against that; the search tool layer (task 3) must always send them.
        """
        if path not in _SEARCH_PATHS:
            raise ValueError(f"unsupported search path {path!r}; use /buscar or /buscador-avanzado")
        if page < 1:
            raise ValueError(f"page must be >= 1, got {page}")
        query = [(str(key), str(value)) for key, value in params if key != "page"]
        query.append(("page", str(page)))
        return parse_search_page(self._get_text(path, params=query), base_url=self.base_url)
