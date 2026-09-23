"""Pure parsers: bomemelilla.es responses (HTML / JSON text) → models.

Shapes verified against pages captured on 2026-09-23 (``tests/fixtures``).
Every returned string is plain text: HTML entities decoded, whitespace
collapsed, no markup. Old 2014–2016 bulletins have no article sumarios and
empty article pages; the parsers return ``None`` / empty tuples there.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .cve import (
    BASE_URL,
    Cve,
    InvalidCveError,
    article_url,
    bulletin_url,
    parse_cve,
    pdf_url,
    sumario_url,
)
from .models import (
    SEARCH_PAGE_SIZE,
    Article,
    ArticleRef,
    BomeParseError,
    Bulletin,
    BulletinRef,
    Consejeria,
    Entity,
    Organismo,
    Page,
    SearchPage,
    Section,
    Sumario,
    SumarioEntry,
)

_MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

_SPANISH_DATE = re.compile(r"(\d{1,2})\s+de\s+([a-záéíóúü]+)\s+de\s+(\d{4})", re.IGNORECASE)
_NUMBER = re.compile(r"N[º°o]\.?\s*(\d+)", re.IGNORECASE)
_CVE_IN_TEXT = re.compile(r"BOME-[A-Z]{1,2}-\d{4}-\d+", re.IGNORECASE)
_BULLETIN_HREF = re.compile(r"/bome/(BOME-BX?-\d{4}-\d+)/?$", re.IGNORECASE)
_INT = re.compile(r"\d+")
_SEARCH_COUNTER = re.compile(
    r"P[áa]gina\s+(\d+)\s+de\s+(\d+)\.\s*Mostrando\s+(\d+)\s+elementos?\s+"
    r"de\s+un\s+total\s+de\s+(\d+)",
    re.IGNORECASE,
)
_SUMARIO_NUMBER_PREFIX = re.compile(r"^\s*\d+\s*\.\s*")

_BLOCK_TAGS = frozenset(
    {
        "address", "article", "blockquote", "dd", "div", "dl", "dt", "figcaption",
        "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
        "li", "ol", "p", "pre", "section", "table", "tbody", "tfoot", "thead", "ul",
    }
)
_SKIP_TAGS = frozenset({"script", "style", "img", "svg", "noscript"})


# --------------------------------------------------------------------------- helpers


def _clean(text: str | None) -> str:
    """Collapse whitespace (including NBSP) into single spaces."""
    return re.sub(r"\s+", " ", text or "").strip()


def _text(tag: Tag | None) -> str:
    return _clean(tag.get_text(" ")) if tag is not None else ""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_spanish_date(text: str | None) -> date | None:
    """Parse the first ``"22 de septiembre de 2026"`` found in ``text``."""
    match = _SPANISH_DATE.search(text or "")
    if match is None:
        return None
    day, month_name, year = match.groups()
    month = _MESES.get(month_name.lower())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def _number_in(text: str | None) -> int | None:
    match = _NUMBER.search(text or "")
    return int(match.group(1)) if match else None


def _int_in(text: str | None) -> int | None:
    match = _INT.search(text or "")
    return int(match.group(0)) if match else None


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise BomeParseError(f"expected JSON, got: {text[:80]!r}") from exc


def _cve(text: str) -> Cve:
    """Parse a CVE printed by the site; an unknown shape is a parse error."""
    try:
        return parse_cve(text)
    except InvalidCveError as exc:
        raise BomeParseError(f"unexpected CVE on page: {exc}") from exc


def _canonical(text: str) -> str:
    return str(_cve(text))


def _html_to_text(node: Tag) -> str:
    """Plain text of ``node``: one line per block, table cells joined by ``" | "``."""
    parts: list[str] = []

    def walk(current: Tag) -> None:
        for child in current.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                parts.append(str(child))
                continue
            if not isinstance(child, Tag) or child.name in _SKIP_TAGS:
                continue
            if child.name == "br":
                parts.append("\n")
            elif child.name == "tr":
                cells = [
                    _clean(_html_to_text(cell).replace("\n", " "))
                    for cell in child.find_all(["td", "th"], recursive=False)
                ]
                parts.append("\n" + " | ".join(cell for cell in cells if cell) + "\n")
            elif child.name in _BLOCK_TAGS:
                parts.append("\n")
                walk(child)
                parts.append("\n")
            else:
                walk(child)

    walk(node)
    lines = (_clean(line) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


def _header(soup: BeautifulSoup) -> Tag:
    header = soup.select_one("section#bome-show")
    if header is None:
        raise BomeParseError("page has no BOME header (section#bome-show)")
    return header


def _title_line(header: Tag) -> str:
    return _text(header.find("h1"))


def _action_href(header: Tag, tipo: str, base_url: str) -> str | None:
    """Absolute href of the header action button ``data-bome-tipo=tipo``, if shown."""
    link = header.select_one(f'.bome-show-actions a[data-bome-tipo="{tipo}"]')
    return _absolute(link.get("href"), base_url) if link is not None else None


def _absolute(href: Any, base_url: str) -> str | None:
    if not href or not isinstance(href, str):
        return None
    href = href.strip()
    if href.startswith(("http://", "https://")):
        return href
    return base_url.rstrip("/") + "/" + href.lstrip("/")


def _counter(scope: Tag, title: str) -> int | None:
    """Read a ``<span title="Número de ...">`` counter directly under ``scope``."""
    span = scope.find("span", attrs={"title": title})
    return _int_in(span.get_text(" ")) if span is not None else None


# --------------------------------------------------------------------------- calendar & lists


def _bulletin_ref(
    cve_text: str, *, day: date | None, title: str | None, base_url: str
) -> BulletinRef | None:
    """Build a list item, or ``None`` for a malformed CVE (callers skip it)."""
    try:
        cve = parse_cve(cve_text)
    except InvalidCveError:
        return None
    return BulletinRef(
        cve=str(cve),
        number=cve.number,
        date=day,
        extraordinary=cve.kind.is_extraordinary,
        url=bulletin_url(cve, base_url=base_url),
        title=title or None,
    )


def parse_calendar(json_text: str, *, base_url: str = BASE_URL) -> list[BulletinRef]:
    """Parse ``/api/bomes/calendar`` (FullCalendar events) into bulletin refs.

    Items whose ``url`` does not carry a bulletin CVE are skipped.
    """
    data = _load_json(json_text)
    if not isinstance(data, list):
        raise BomeParseError("calendar payload is not a list")
    refs: list[BulletinRef] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        match = _BULLETIN_HREF.search(str(item.get("url") or ""))
        if match is None:
            continue
        try:
            day = date.fromisoformat(str(item.get("start") or "")[:10])
        except ValueError:
            day = None
        ref = _bulletin_ref(
            match.group(1),
            day=day,
            title=_clean(str(item.get("title") or "")),
            base_url=base_url,
        )
        if ref is not None:
            refs.append(ref)
    return refs


def _links_to_refs(links: list[Tag], base_url: str) -> list[BulletinRef]:
    refs: list[BulletinRef] = []
    for link in links:
        match = _BULLETIN_HREF.search(str(link.get("href") or ""))
        if match is None:
            continue
        title = _text(link)
        ref = _bulletin_ref(
            match.group(1), day=parse_spanish_date(title), title=title, base_url=base_url
        )
        if ref is not None:
            refs.append(ref)
    return refs


def parse_year_slider(html: str, *, base_url: str = BASE_URL) -> list[BulletinRef]:
    """Parse the month slider of a year (``/api/bomes/{year}`` or the home page).

    Bulletins come newest first, as the site lists them. An empty year
    (e.g. 2005) yields ``[]``.
    """
    soup = _soup(html)
    return _links_to_refs(soup.select(".swiper-slide a[href]"), base_url)


def parse_entities(json_text: str) -> list[Entity]:
    """Parse ``/api/section/consejerias/{id}`` or ``/organismos/{id}``."""
    data = _load_json(json_text)
    if not isinstance(data, list):
        raise BomeParseError("section payload is not a list")
    entities: list[Entity] = []
    for item in data:
        if not isinstance(item, dict) or "id" not in item:
            raise BomeParseError(f"unexpected section item: {item!r}")
        try:
            entity_id = int(item["id"])
        except (TypeError, ValueError) as exc:
            raise BomeParseError(f"non-numeric section id: {item!r}") from exc
        entities.append(Entity(id=entity_id, name=_clean(str(item.get("nombre") or ""))))
    return entities


# --------------------------------------------------------------------------- bulletin page


def _article_ref(item: Tag, bulletin_cve: str, base_url: str) -> ArticleRef | None:
    heading = item.find("h5")
    cve_match = _CVE_IN_TEXT.search(_text(heading))
    if cve_match is None:
        return None
    cve = _cve(cve_match.group(0))
    pdf_link = item.select_one('a[data-bome-tipo="Articulo"]')
    return ArticleRef(
        cve=str(cve),
        number=cve.number,
        sumario=_text(item.find("blockquote")) or None,
        url=article_url(bulletin_cve, cve.number, base_url=base_url),
        pdf_url=_absolute(pdf_link.get("href"), base_url) if pdf_link is not None else None,
    )


def _consejeria(block: Tag, bulletin_cve: str, base_url: str) -> Consejeria:
    button = block.select_one(".accordion-button")
    name_span = button.find("span") if button is not None else None
    id_match = re.search(r"(\d+)$", str(block.get("id") or ""))
    organismos: list[Organismo] = []
    for org in block.select("ul.organismo-list > li"):
        articles = [
            ref
            for item in org.select("ul.articulo-list > li")
            if (ref := _article_ref(item, bulletin_cve, base_url)) is not None
        ]
        organismos.append(Organismo(name=_text(org.find("h4")), articles=tuple(articles)))
    return Consejeria(
        name=_text(name_span),
        id=int(id_match.group(1)) if id_match else None,
        article_count=_counter(button, "Número de artículos") if button else None,
        page_count=_counter(button, "Número de páginas") if button else None,
        organismos=tuple(organismos),
    )


def _section(block: Tag, bulletin_cve: str, base_url: str) -> Section:
    heading = block.find("h3")
    name_span = heading.find("span") if heading is not None else None
    return Section(
        name=_text(name_span),
        page_count=_counter(heading, "Número de páginas") if heading else None,
        article_count=_counter(heading, "Número de artículos") if heading else None,
        consejerias=tuple(
            _consejeria(item, bulletin_cve, base_url)
            for item in block.select(".accordion-consejeria")
        ),
    )


def _check_page_cve(header: Tag, expected: str) -> None:
    found = _CVE_IN_TEXT.search(_text(header.find("h2")))
    if found is None:
        raise BomeParseError("page header carries no CVE")
    if _canonical(found.group(0)) != expected:
        raise BomeParseError(f"page is for {found.group(0)}, expected {expected}")


def parse_bulletin_page(html: str, cve: str, *, base_url: str = BASE_URL) -> Bulletin:
    """Parse ``/bome/{CVE}`` into the section → consejería → organismo tree."""
    bulletin_cve = parse_cve(cve).bulletin_cve()
    soup = _soup(html)
    header = _header(soup)
    _check_page_cve(header, str(bulletin_cve))
    title = _title_line(header)

    has_sumario = header.select_one('.bome-show-actions a[href$="/sumario"]') is not None
    content = soup.select_one("section#bome-show-content") or soup
    return Bulletin(
        cve=str(bulletin_cve),
        number=bulletin_cve.number,
        date=parse_spanish_date(title),
        extraordinary=bulletin_cve.kind.is_extraordinary,
        url=bulletin_url(bulletin_cve, base_url=base_url),
        pdf_url=pdf_url(bulletin_cve, base_url=base_url),
        sumario_pdf_url=_action_href(header, "Sumario", base_url),
        sumario_url=sumario_url(bulletin_cve, base_url=base_url) if has_sumario else None,
        sections=tuple(
            _section(block, str(bulletin_cve), base_url)
            for block in content.select(".bome-show-departamento")
        ),
    )


# --------------------------------------------------------------------------- article & sumario


def _pages(container: Tag | None, base_url: str) -> tuple[Page, ...]:
    if container is None:
        return ()
    pages: list[Page] = []
    for block in container.select("div.pagina[id^=pagina-]"):
        number = _int_in(str(block.get("id")))
        if number is None:
            continue
        pdf_link = block.select_one('.pagina-titulo a[data-bome-tipo="Pagina"]')
        page_cve = str(pdf_link.get("data-bome-name") or "") if pdf_link is not None else ""
        body = block.select_one(".pagina-texto")
        pages.append(
            Page(
                number=number,
                cve=_canonical(page_cve) if page_cve else None,
                pdf_url=_absolute(pdf_link.get("href"), base_url) if pdf_link is not None else None,
                text=_html_to_text(body) if body is not None else "",
            )
        )
    return tuple(pages)


def parse_article_page(html: str, *, base_url: str = BASE_URL) -> Article:
    """Parse ``/bome/{CVE}/articulo/{n}`` into the full article.

    The text is split per printed page on the ``#pagina-N`` blocks.
    """
    soup = _soup(html)
    header = _header(soup)
    article_match = _CVE_IN_TEXT.search(_text(header.find("h2")))
    if article_match is None:
        raise BomeParseError("article page header carries no article CVE")
    article_cve = _cve(article_match.group(0))

    breadcrumb = header.select_one(".breadcrumb a[href*='/bome/BOME-']")
    bulletin_match = _BULLETIN_HREF.search(str(breadcrumb.get("href"))) if breadcrumb else None
    if bulletin_match is None:
        raise BomeParseError("article page does not link its bulletin")
    bulletin_cve = _cve(bulletin_match.group(1))
    title = _title_line(header)

    content = soup.select_one("section#bome-show-articulo-content")
    pages = _pages(content.select_one(".paginas-sumario") if content else None, base_url)
    texts = [page.text for page in pages if page.text]
    intro = content.select_one(".intro-sumario") if content else None
    heading = content.find("h3") if content else None
    return Article(
        cve=str(article_cve),
        number=article_cve.number,
        bulletin_cve=str(bulletin_cve),
        bulletin_number=_number_in(title),
        bulletin_date=parse_spanish_date(title),
        heading=_text(heading) or None,
        sumario=_text(intro) or None,
        text="\n\n".join(texts) if texts else None,
        pages=pages,
        url=article_url(bulletin_cve, article_cve.number, base_url=base_url),
        pdf_url=_action_href(header, "Articulo", base_url),
    )


def parse_sumario_page(html: str, cve: str, *, base_url: str = BASE_URL) -> Sumario:
    """Parse ``/bome/{CVE}/sumario`` into a flat list of entries.

    Choice: the sumario is returned flat (``SumarioEntry`` carrying its
    departamento and consejería) rather than as the bulletin tree, because
    the sumario has no organismo level and its value is the first page of
    each article, which the bulletin page lacks. Article CVEs are derived
    from the bulletin kind (``B``→``A``, ``BX``→``AX``) since the sumario
    only prints article numbers.
    """
    bulletin_cve = parse_cve(cve).bulletin_cve()
    soup = _soup(html)
    header = _header(soup)
    title = _title_line(header)
    content = soup.select_one("section#bome-show-sumario-content") or soup

    entries: list[SumarioEntry] = []
    section: str | None = None
    consejeria: str | None = None
    for node in content.select(
        ".sumario-departamento h2, h3.sumario-consejeria, div.sumario-articulo"
    ):
        classes = node.get("class") or []
        if node.name == "h2":
            section = _text(node) or None
            consejeria = None
        elif "sumario-consejeria" in classes:
            consejeria = _text(node) or None
        else:
            number = _int_in(str(node.get("data-numero") or ""))
            if number is None:
                continue
            article_cve = bulletin_cve.article_cve(number)
            text = _SUMARIO_NUMBER_PREFIX.sub("", _text(node.select_one(".sumario-articulo__content")))
            entries.append(
                SumarioEntry(
                    section=section,
                    consejeria=consejeria,
                    article=ArticleRef(
                        cve=str(article_cve),
                        number=number,
                        sumario=text or None,
                        url=article_url(bulletin_cve, number, base_url=base_url),
                        pdf_url=pdf_url(article_cve, base_url=base_url),
                        first_page=_int_in(_text(node.select_one(".sumario-articulo__pagina"))),
                    ),
                )
            )

    return Sumario(
        bulletin_cve=str(bulletin_cve),
        cve=str(bulletin_cve.sumario_cve()),
        bulletin_number=_number_in(title),
        date=parse_spanish_date(title),
        url=sumario_url(bulletin_cve, base_url=base_url),
        pdf_url=_action_href(header, "Sumario", base_url),
        pages=tuple(page.number for page in _pages(content, base_url)),
        entries=tuple(entries),
    )


# --------------------------------------------------------------------------- search


def parse_search_page(html: str, *, base_url: str = BASE_URL) -> SearchPage:
    """Parse a ``/buscar`` or ``/buscador-avanzado`` results page.

    Reads the counter ``"Página 1 de 3. Mostrando 10 elementos de un total
    de 30 elementos."``. Without a counter the page is taken as having no
    pagination: 0 results when no hits are listed, otherwise one page.
    """
    soup = _soup(html)
    block = soup.select_one(".page-search-result")
    if block is None:
        return SearchPage(results=(), page=1, total_pages=0, total_results=0)
    results = tuple(_links_to_refs(block.select("a[href]"), base_url))
    counter = _SEARCH_COUNTER.search(_text(block))
    if counter is None:
        return SearchPage(
            results=results,
            page=1,
            total_pages=1 if results else 0,
            total_results=len(results),
        )
    page, total_pages, _shown, total = (int(value) for value in counter.groups())
    return SearchPage(
        results=results,
        # "Página 0 de 0" (no hits) is normalised to page 1 of 0.
        page=max(page, 1),
        total_pages=total_pages,
        total_results=total,
        per_page=SEARCH_PAGE_SIZE,
    )
