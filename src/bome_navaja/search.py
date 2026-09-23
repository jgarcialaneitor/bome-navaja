"""Search layer: bulletin search on the site and article-level drill-down.

Public entry points, designed to be exposed 1:1 as MCP tools (task 6):

* :func:`buscar_bomes` — one results page of the site's advanced search.
* :func:`buscar_articulos` — pages through that search, opens each bulletin
  and keeps the articles whose sumario matches locally.

Site behaviour this module relies on (verified live on 2026-09-23 against
``/buscador-avanzado``):

* Every criterion travels in ``/buscador-avanzado``: ``from``, ``to``,
  ``departamento``, ``consejeria``, ``organismo``, the ``contenido[i][type|
  like|content|operator]`` collection (type ``sumario_articulo`` or
  ``pagina``; like ``like``/``nlike``) and the ``numero[i][type|like|number|
  operator]`` collection (type ``bome``/``articulo``/``pagina``/``year``).
  ``from`` must always be sent: without it the text is effectively ignored
  (1 result instead of 30 for "personal eventual"). ``to`` is honoured.
* Text terms are ANDed **within the same article sumario**: "personal
  eventual" AND "hacienda" gives 0 bulletins although many bulletins carry
  both words in different articles. ``nlike`` is per article too.
* The ``operator`` field is ignored: "or" behaves exactly like "and". This
  module therefore rejects OR in :func:`buscar_bomes` and implements it in
  :func:`buscar_articulos` by running one site search per AND-group.
* Numeric criteria are exact matches ("641" does not find bulletin 6416).
* Matching is case/accent-insensitive and folds ``ñ`` into ``n``, like
  :func:`bome_navaja.text.normalize`.
* Results are bulletins only, newest first, 10 per page.
* Bulletin pages can omit articles that the search matched (BOME-B-2025-6294
  lists 744 and 746-750; 745 lives at ``/articulo/745``). The drill-down
  fetches numbering gaps from their own article page. Limitation: only gaps
  BETWEEN the lowest and highest listed numbers are detected; an article
  omitted at either end of the bulletin cannot be inferred and is missed
  (such a bulletin then shows up in ``bomes_sin_coincidencia``).

Recommended production client: ``BomeClient(polite_delay=0.5)``; a
drill-down makes one request per result page plus one per bulletin.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from .client import BomeClient
from .models import (
    SEARCH_PAGE_SIZE,
    BomeError,
    BomeHTTPError,
    BomeNotFoundError,
    BomeParseError,
    Bulletin,
    BulletinRef,
    JsonModel,
    SearchPage,
)
from .cve import parse_cve
from .text import Term, and_groups, matches, normalize, term_from_dict

SEARCH_PATH = "/buscador-avanzado"

FIRST_DATE = date(2014, 1, 1)
"""The site publishes nothing older (first bulletin: 2014-01-03)."""

DEFAULT_MAX_BOMES = 20
MAX_BOMES_CAP = 100
DEFAULT_MAX_ARTICULOS = 100
MAX_ARTICULOS_CAP = 500

RECOMMENDED_POLITE_DELAY = 0.5
"""Seconds between requests recommended for drill-down in production."""

Ambito = Literal["sumario", "contenido"]

AMBITOS: dict[str, str] = {"sumario": "sumario_articulo", "contenido": "pagina"}
"""Tool-facing scope → site ``contenido[i][type]`` value."""

_SITE_LIKE = {"contiene": "like", "no_contiene": "nlike"}
_NUMERIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("numero_bome", "bome"),
    ("numero_articulo", "articulo"),
    ("numero_pagina", "pagina"),
    ("anio", "year"),
)
_TERM_KEYS_WITH_SCOPE = frozenset({"texto", "operador", "modo", "ambito"})


def _today() -> date:
    """Today's date; a seam so tests can pin it."""
    return date.today()


class BusquedaInvalidaError(BomeError, ValueError):
    """The search criteria are invalid or would silently list everything."""


# --------------------------------------------------------------------------- criteria


@dataclass(frozen=True, slots=True)
class _ScopedTerm:
    term: Term
    ambito: str

    def to_dict(self) -> dict[str, Any]:
        return {**self.term.to_dict(), "ambito": self.ambito}


@dataclass(frozen=True, slots=True)
class _Criteria:
    """Validated criteria shared by both searches."""

    terms: tuple[_ScopedTerm, ...]
    desde: date
    hasta: date
    filters: tuple[tuple[str, int], ...]
    """``(departamento|consejeria|organismo, id)`` in form order."""
    numbers: tuple[tuple[str, int], ...]
    """``(tool name, value)`` for the numeric criteria in form order."""
    explicit_dates: bool

    def site_params(self, terms: Sequence[_ScopedTerm] | None = None) -> list[tuple[str, str]]:
        """Ordered ``/buscador-avanzado`` params (without ``page``)."""
        params: list[tuple[str, str]] = [
            ("from", self.desde.isoformat()),
            ("to", self.hasta.isoformat()),
        ]
        params.extend((name, str(value)) for name, value in self.filters)
        for index, scoped in enumerate(self.terms if terms is None else terms):
            params += [
                (f"contenido[{index}][type]", AMBITOS[scoped.ambito]),
                (f"contenido[{index}][like]", _SITE_LIKE[scoped.term.mode]),
                (f"contenido[{index}][content]", scoped.term.text.strip()),
                # The site ignores the operator; AND is what it actually does.
                (f"contenido[{index}][operator]", "and"),
            ]
        site_types = dict(_NUMERIC_FIELDS)
        for index, (name, value) in enumerate(self.numbers):
            params += [
                (f"numero[{index}][type]", site_types[name]),
                (f"numero[{index}][like]", "like"),
                (f"numero[{index}][number]", str(value)),
                (f"numero[{index}][operator]", "and"),
            ]
        return params

    def echo(self) -> dict[str, Any]:
        """The effective query, JSON-safe, with the tool's Spanish names."""
        data: dict[str, Any] = {
            "terminos": [scoped.to_dict() for scoped in self.terms],
            "desde": self.desde.isoformat(),
            "hasta": self.hasta.isoformat(),
        }
        data.update(dict(self.filters))
        data.update(dict(self.numbers))
        return data


def _invalid(message: str) -> BusquedaInvalidaError:
    return BusquedaInvalidaError(message)


def _parse_date(value: date | str | None, name: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise _invalid(f"'{name}' must be an ISO date YYYY-MM-DD, got {value!r}")


def _positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid(f"'{name}' must be a positive integer, got {value!r}")
    return value


def _scoped_terms(
    texto: str | None,
    terminos: Sequence[Mapping[str, Any]] | None,
    ambito: str,
) -> tuple[_ScopedTerm, ...]:
    if ambito not in AMBITOS:
        raise _invalid(f"'ambito' must be one of {sorted(AMBITOS)}, got {ambito!r}")
    scoped: list[_ScopedTerm] = []
    if texto is not None:
        try:
            scoped.append(_ScopedTerm(Term(texto), ambito))
        except ValueError as exc:
            raise _invalid(f"'texto': {exc}") from None
    if terminos is None:
        return tuple(scoped)
    if isinstance(terminos, (str, bytes)) or not isinstance(terminos, Sequence):
        raise _invalid("'terminos' must be a list of objects {texto, operador?, modo?, ambito?}")
    for position, item in enumerate(terminos):
        if not isinstance(item, Mapping):
            raise _invalid(f"terminos[{position}] must be an object, got {item!r}")
        unknown = set(item) - _TERM_KEYS_WITH_SCOPE
        if unknown:
            raise _invalid(
                f"terminos[{position}]: unknown keys {sorted(unknown)}; "
                f"allowed: {sorted(_TERM_KEYS_WITH_SCOPE)}"
            )
        term_scope = item.get("ambito", ambito)
        if term_scope not in AMBITOS:
            raise _invalid(
                f"terminos[{position}].ambito must be one of {sorted(AMBITOS)}, got {term_scope!r}"
            )
        try:
            term = term_from_dict({k: v for k, v in item.items() if k != "ambito"})
        except ValueError as exc:
            raise _invalid(f"terminos[{position}]: {exc}") from None
        scoped.append(_ScopedTerm(term, term_scope))
    return tuple(scoped)


def _criteria(
    *,
    texto: str | None,
    ambito: str,
    terminos: Sequence[Mapping[str, Any]] | None,
    desde: date | str | None,
    hasta: date | str | None,
    departamento: int | None,
    consejeria: int | None,
    organismo: int | None,
    numbers: Mapping[str, Any],
) -> _Criteria:
    terms = _scoped_terms(texto, terminos, ambito)
    start = _parse_date(desde, "desde")
    end = _parse_date(hasta, "hasta")
    effective_start = start or FIRST_DATE
    effective_end = end or _today()
    if effective_end < effective_start:
        raise _invalid(f"'hasta' ({effective_end}) is before 'desde' ({effective_start})")
    filters = tuple(
        (name, checked)
        for name, value in (
            ("departamento", departamento),
            ("consejeria", consejeria),
            ("organismo", organismo),
        )
        if (checked := _positive_int(value, name)) is not None
    )
    checked_numbers = tuple(
        (name, checked)
        for name, _site in _NUMERIC_FIELDS
        if name in numbers and (checked := _positive_int(numbers[name], name)) is not None
    )
    explicit_dates = start is not None or end is not None
    if not (terms or filters or checked_numbers or explicit_dates):
        raise _invalid(
            "empty search: give 'texto'/'terminos', a date range, a departamento/"
            "consejeria/organismo or a number; otherwise the site lists every BOME"
        )
    return _Criteria(
        terms=terms,
        desde=effective_start,
        hasta=effective_end,
        filters=filters,
        numbers=checked_numbers,
        explicit_dates=explicit_dates,
    )


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class ResultadoBusquedaBomes:
    """One page of bulletins plus the effective query that produced it."""

    consulta: dict[str, Any]
    parametros_sitio: tuple[tuple[str, str], ...]
    """Exact ``/buscador-avanzado`` params sent, in order."""
    resultados: SearchPage

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping with the tool-facing Spanish keys."""
        page = self.resultados
        return {
            "consulta": dict(self.consulta),
            "parametros_sitio": [list(pair) for pair in self.parametros_sitio],
            "pagina": page.page,
            "total_paginas": page.total_pages,
            "total_bomes": page.total_results,
            "por_pagina": page.per_page,
            "hay_mas": page.has_next,
            "bomes": [ref.to_dict() for ref in page.results],
        }


@dataclass(frozen=True, slots=True)
class ArticuloEncontrado(JsonModel):
    """An article whose sumario matched, with its bulletin and place in it."""

    bome_cve: str
    bome_numero: int
    bome_fecha: date | None
    bome_extraordinario: bool
    cve: str
    numero: int
    sumario: str | None
    departamento: str
    consejeria: str
    organismo: str
    url: str
    pdf_url: str | None
    listado_en_bome: bool = True
    """False when the bulletin page omits the article and it was fetched from
    its own page (the site sometimes hides articles, e.g. BOME-A-2025-745)."""


@dataclass(frozen=True, slots=True)
class ErrorBusqueda(JsonModel):
    """A failure recorded during a drill-down that did not abort it."""

    etapa: Literal["bome", "articulo", "busqueda"]
    """``bome``: a bulletin page failed; ``articulo``: an article hidden from
    its bulletin page failed; ``busqueda``: a later results page failed."""
    cve: str | None
    error_code: str
    mensaje: str


MotivoTruncado = Literal["max_bomes", "max_articulos", "error_busqueda"]


@dataclass(frozen=True, slots=True)
class ResultadoBusquedaArticulos(JsonModel):
    """Articles found by :func:`buscar_articulos`, newest bulletin first."""

    consulta: dict[str, Any]
    parametros_sitio: tuple[tuple[tuple[str, str], ...], ...]
    """Params of each site search (one per AND-group of the query)."""
    articulos: tuple[ArticuloEncontrado, ...]
    bomes_revisados: int
    """Bulletins opened (including those whose fetch failed)."""
    total_bomes: int
    """Bulletins the site matched; a sum over searches when there is OR."""
    total_bomes_exacto: bool
    """False when ``total_bomes`` sums several searches (may double count)."""
    truncado: bool
    motivo_truncado: MotivoTruncado | None
    bomes_sin_coincidencia: tuple[BulletinRef, ...]
    """Bulletins the site matched but where no sumario matched locally (e.g.
    2014–2016 bulletins without sumarios, or local/site semantics drift)."""
    errores: tuple[ErrorBusqueda, ...] = field(default=())


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, BomeNotFoundError):
        return "no_encontrado"
    if isinstance(exc, BomeHTTPError):
        return "error_http"
    if isinstance(exc, BomeParseError):
        return "error_formato"
    return "error"


# --------------------------------------------------------------------------- buscar_bomes


def buscar_bomes(
    client: BomeClient,
    *,
    texto: str | None = None,
    ambito: Ambito = "sumario",
    terminos: Sequence[Mapping[str, Any]] | None = None,
    desde: date | str | None = None,
    hasta: date | str | None = None,
    departamento: int | None = None,
    consejeria: int | None = None,
    organismo: int | None = None,
    numero_bome: int | None = None,
    numero_articulo: int | None = None,
    numero_pagina: int | None = None,
    anio: int | None = None,
    pagina: int = 1,
) -> ResultadoBusquedaBomes:
    """One page (10 bulletins) of the site's advanced search.

    ``texto`` is a literal phrase (case/accent-insensitive substring).
    ``terminos`` adds more phrases: ``{"texto", "modo": "contiene"|
    "no_contiene", "ambito"?}``. Each term uses ``ambito`` ("sumario" = article
    sumarios, "contenido" = page text) unless it sets its own. All terms are
    ANDed and, on sumarios, must hold in the same article. ``operador: "o"``
    is rejected because the site silently ignores it; use
    :func:`buscar_articulos` (which honours OR) or one search per phrase.

    ``desde``/``hasta`` default to 2014-01-01 and today and are always sent.
    Numbers are exact matches. A search without any text, date, section or
    number is rejected because the site would list every bulletin.
    """
    criteria = _criteria(
        texto=texto,
        ambito=ambito,
        terminos=terminos,
        desde=desde,
        hasta=hasta,
        departamento=departamento,
        consejeria=consejeria,
        organismo=organismo,
        numbers={
            "numero_bome": numero_bome,
            "numero_articulo": numero_articulo,
            "numero_pagina": numero_pagina,
            "anio": anio,
        },
    )
    if any(index > 0 and s.term.operator == "o" for index, s in enumerate(criteria.terms)):
        raise _invalid(
            "the site ignores OR between terms (it ANDs them all); run one search per "
            "alternative, or use buscar_articulos, which applies OR at article level"
        )
    if isinstance(pagina, bool) or not isinstance(pagina, int) or pagina < 1:
        raise _invalid(f"'pagina' must be an integer >= 1, got {pagina!r}")
    params = criteria.site_params()
    page = client.search_page(SEARCH_PATH, params, page=pagina)
    return ResultadoBusquedaBomes(
        consulta={**criteria.echo(), "ambito": ambito, "pagina": pagina},
        parametros_sitio=tuple(params),
        resultados=page,
    )


# --------------------------------------------------------------------------- buscar_articulos


class _GroupStream:
    """Lazily pages one site search, yielding its bulletins newest first."""

    def __init__(self, client: BomeClient, params: list[tuple[str, str]], errors: list[ErrorBusqueda]):
        self.client = client
        self.params = params
        self.errors = errors
        self.total = 0
        self.failed = False

    def __iter__(self) -> Iterator[BulletinRef]:
        page_number = 1
        while True:
            try:
                page = self.client.search_page(SEARCH_PATH, self.params, page=page_number)
            except BomeError as exc:
                if page_number == 1:
                    raise
                self.failed = True
                self.errors.append(
                    ErrorBusqueda(
                        etapa="busqueda",
                        cve=None,
                        error_code=_error_code(exc),
                        mensaje=f"results page {page_number}: {exc}",
                    )
                )
                return
            if page_number == 1:
                self.total = page.total_results
            yield from page.results
            if not page.has_next or not page.results:
                return
            page_number += 1


def _newest_first(ref: BulletinRef) -> int:
    return -(ref.date.toordinal() if ref.date else 0)


def _bound(value: Any, name: str, cap: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid(f"'{name}' must be an integer >= 1, got {value!r}")
    return min(value, cap)


def buscar_articulos(
    client: BomeClient,
    *,
    texto: str | None = None,
    terminos: Sequence[Mapping[str, Any]] | None = None,
    desde: date | str | None = None,
    hasta: date | str | None = None,
    departamento: int | None = None,
    consejeria: int | None = None,
    organismo: int | None = None,
    numero_bome: int | None = None,
    numero_articulo: int | None = None,
    anio: int | None = None,
    max_bomes: int = DEFAULT_MAX_BOMES,
    max_articulos: int = DEFAULT_MAX_ARTICULOS,
) -> ResultadoBusquedaArticulos:
    """Articles whose sumario matches, found by drilling into matching bulletins.

    Terms work on article sumarios only (``ambito`` "contenido" is rejected).
    ``operador: "o"`` is honoured: the query is split into AND-groups, each
    group is one site search, and the bulletin lists are merged newest first
    without duplicates. Each bulletin page is opened and its articles are
    kept when their sumario satisfies the whole query locally
    (:func:`bome_navaja.text.matches`), when ``consejeria`` equals the
    article's consejería id and when ``numero_articulo`` equals its number.
    ``departamento`` and ``organismo`` only pre-filter at the site (the
    bulletin page carries no ids for them).

    Bounded by ``max_bomes`` (default 20, capped at 100) bulletins opened and
    ``max_articulos`` (default 100, capped at 500) articles returned. A failing
    bulletin is recorded in ``errores`` and the search goes on.
    """
    if terminos is not None and not isinstance(terminos, (str, bytes)) and isinstance(terminos, Sequence):
        for position, item in enumerate(terminos):
            if isinstance(item, Mapping) and item.get("ambito", "sumario") != "sumario":
                raise _invalid(
                    f"terminos[{position}]: buscar_articulos matches article sumarios only; "
                    "use buscar_bomes with ambito 'contenido' for page text"
                )
    criteria = _criteria(
        texto=texto,
        ambito="sumario",
        terminos=terminos,
        desde=desde,
        hasta=hasta,
        departamento=departamento,
        consejeria=consejeria,
        organismo=organismo,
        numbers={
            "numero_bome": numero_bome,
            "numero_articulo": numero_articulo,
            "anio": anio,
        },
    )
    bome_budget = _bound(max_bomes, "max_bomes", MAX_BOMES_CAP)
    article_budget = _bound(max_articulos, "max_articulos", MAX_ARTICULOS_CAP)
    terms = [scoped.term for scoped in criteria.terms]
    groups = _scoped_groups(criteria.terms)

    errors: list[ErrorBusqueda] = []
    group_params = [criteria.site_params(group) for group in groups]
    streams = [_GroupStream(client, params, errors) for params in group_params]
    merged: Iterator[BulletinRef] = (
        iter(streams[0]) if len(streams) == 1 else heapq.merge(*streams, key=_newest_first)
    )
    unique = _unique(merged)

    articles: list[ArticuloEncontrado] = []
    unmatched: list[BulletinRef] = []
    reviewed = 0
    reason: MotivoTruncado | None = None
    for ref in unique:
        if reviewed >= bome_budget:
            reason = "max_bomes"
            break
        if len(articles) >= article_budget:
            reason = "max_articulos"
            break
        reviewed += 1
        try:
            bulletin = client.bulletin(ref.cve)
        except BomeError as exc:
            errors.append(
                ErrorBusqueda(etapa="bome", cve=ref.cve, error_code=_error_code(exc), mensaje=str(exc))
            )
            continue
        found = 0
        for candidate in _bulletin_candidates(
            client, bulletin, errors, consejeria=consejeria, numero_articulo=numero_articulo
        ):
            if not matches(candidate.sumario, terms):
                continue
            found += 1
            if len(articles) >= article_budget:
                reason = "max_articulos"
                break
            articles.append(candidate)
        if found == 0:
            unmatched.append(ref)
        if reason:
            break
    if reason is None and any(stream.failed for stream in streams):
        reason = "error_busqueda"

    return ResultadoBusquedaArticulos(
        consulta={
            **criteria.echo(),
            "max_bomes": bome_budget,
            "max_articulos": article_budget,
        },
        parametros_sitio=tuple(tuple(params) for params in group_params),
        articulos=tuple(articles),
        bomes_revisados=reviewed,
        total_bomes=sum(stream.total for stream in streams),
        total_bomes_exacto=len(streams) == 1,
        truncado=reason is not None,
        motivo_truncado=reason,
        bomes_sin_coincidencia=tuple(unmatched),
        errores=tuple(errors),
    )


MAX_HIDDEN_ARTICLES_PER_BOME = 20
"""Upper bound of numbering gaps fetched per bulletin (one request each)."""


def _split_heading(heading: str | None) -> tuple[str, str, str]:
    """Split ``"DEPARTAMENTO - CONSEJER\u00cdA - ORGANISMO"`` from an article page."""
    parts = [part.strip() for part in (heading or "").split(" - ")]
    if len(parts) < 3:
        return (heading or "", "", "")
    return (parts[0], parts[1], " - ".join(parts[2:]))


def _bulletin_candidates(
    client: BomeClient,
    bulletin: Bulletin,
    errors: list[ErrorBusqueda],
    *,
    consejeria: int | None,
    numero_articulo: int | None,
) -> Iterator[ArticuloEncontrado]:
    """Articles of a bulletin in number order, passing the local id filters.

    Article numbers inside a bulletin are consecutive, so a gap in the listed
    numbers is an article the bulletin page does not render (verified live:
    BOME-B-2025-6294 lists 744 and 746-750; 745 exists at ``/articulo/745``).
    Gaps are fetched lazily from their own page, at most
    :data:`MAX_HIDDEN_ARTICLES_PER_BOME` per bulletin. With a ``consejeria``
    filter a hidden article is kept only if its consejer\u00eda name equals the
    name the bulletin gives to that id.
    """
    listed_numbers: set[int] = set()
    kept: dict[int, ArticuloEncontrado] = {}
    consejeria_names: set[str] = set()
    for section in bulletin.sections:
        for block in section.consejerias:
            wanted_block = consejeria is None or block.id == consejeria
            if consejeria is not None and wanted_block:
                consejeria_names.add(normalize(block.name))
            for organismo_block in block.organismos:
                for article in organismo_block.articles:
                    listed_numbers.add(article.number)
                    if wanted_block:
                        kept[article.number] = _found(
                            bulletin,
                            article.cve,
                            article.number,
                            article.sumario,
                            (section.name, block.name, organismo_block.name),
                            article.url,
                            article.pdf_url,
                            listed=True,
                        )
    numbers = sorted(listed_numbers)
    hidden = sorted(set(range(numbers[0], numbers[-1] + 1)) - listed_numbers) if numbers else []
    fetchable = set(hidden[:MAX_HIDDEN_ARTICLES_PER_BOME])
    if len(hidden) > MAX_HIDDEN_ARTICLES_PER_BOME:
        errors.append(
            ErrorBusqueda(
                etapa="articulo",
                cve=bulletin.cve,
                error_code="demasiados_huecos",
                mensaje=f"{len(hidden)} articles missing from the bulletin page; "
                f"only the first {MAX_HIDDEN_ARTICLES_PER_BOME} were fetched",
            )
        )
    for number in sorted(set(numbers) | fetchable):
        if numero_articulo is not None and number != numero_articulo:
            continue
        if number in listed_numbers:
            if number in kept:
                yield kept[number]
            continue
        article_cve = str(parse_cve(bulletin.cve).article_cve(number))
        try:
            page = client.article(bulletin.cve, number)
        except BomeError as exc:
            errors.append(
                ErrorBusqueda(
                    etapa="articulo", cve=article_cve, error_code=_error_code(exc), mensaje=str(exc)
                )
            )
            continue
        place = _split_heading(page.heading)
        if consejeria is not None and normalize(place[1]) not in consejeria_names:
            continue
        yield _found(
            bulletin,
            page.cve,
            page.number,
            page.sumario,
            place,
            page.url,
            page.pdf_url,
            listed=False,
        )


def _found(
    bulletin: Bulletin,
    cve: str,
    number: int,
    sumario: str | None,
    place: tuple[str, str, str],
    url: str,
    pdf_url: str | None,
    *,
    listed: bool,
) -> ArticuloEncontrado:
    return ArticuloEncontrado(
        bome_cve=bulletin.cve,
        bome_numero=bulletin.number,
        bome_fecha=bulletin.date,
        bome_extraordinario=bulletin.extraordinary,
        cve=cve,
        numero=number,
        sumario=sumario,
        departamento=place[0],
        consejeria=place[1],
        organismo=place[2],
        url=url,
        pdf_url=pdf_url,
        listado_en_bome=listed,
    )


def _scoped_groups(scoped: Sequence[_ScopedTerm]) -> list[list[_ScopedTerm]]:
    """AND-groups of scoped terms (same split as :func:`and_groups`); ``[[]]`` if none."""
    if not scoped:
        return [[]]
    by_term = iter(scoped)
    return [[next(by_term) for _ in group] for group in and_groups([s.term for s in scoped])]


def _unique(refs: Iterator[BulletinRef]) -> Iterator[BulletinRef]:
    seen: set[str] = set()
    for ref in refs:
        if ref.cve not in seen:
            seen.add(ref.cve)
            yield ref


__all__ = [
    "AMBITOS",
    "DEFAULT_MAX_ARTICULOS",
    "DEFAULT_MAX_BOMES",
    "FIRST_DATE",
    "MAX_ARTICULOS_CAP",
    "MAX_BOMES_CAP",
    "RECOMMENDED_POLITE_DELAY",
    "SEARCH_PAGE_SIZE",
    "ArticuloEncontrado",
    "BusquedaInvalidaError",
    "ErrorBusqueda",
    "ResultadoBusquedaArticulos",
    "ResultadoBusquedaBomes",
    "buscar_articulos",
    "buscar_bomes",
]
