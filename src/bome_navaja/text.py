"""Text normalisation and a small boolean matcher with the BOME site's semantics.

The site's search (verified live 2026-09-23) is a case- and accent-insensitive
literal substring match: word order matters, there is no stemming and no
synonyms ("destitución" finds nothing, the BOME says "cese"), and ``ñ`` is
folded into ``n`` ("año" and "ano" return the same 835 bulletins).
:func:`normalize` reproduces that folding so local matching agrees with the
site, and the local sumario index (task 5) must reuse it unchanged.

:func:`matches` extends the phrase match with the advanced-search collection
shape (``palabra=True`` additionally requires every phrase to start a word,
see :func:`phrase_starts`): a list of :class:`Term` (``operador`` "y"/"o", ``modo``
"contiene"/"no_contiene"). AND binds tighter than OR. Note the site itself
ignores OR (every term is ANDed); OR is only honoured locally.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

Operator = Literal["y", "o"]
Mode = Literal["contiene", "no_contiene"]

OPERATORS: tuple[str, ...] = ("y", "o")
MODES: tuple[str, ...] = ("contiene", "no_contiene")

_WHITESPACE = re.compile(r"\s+")
_WHITESPACE_CONTROLS = frozenset("\t\n\v\f\r")


def ignorable(char: str) -> bool:
    """True for characters :func:`normalize` drops after NFKD decomposition.

    That is combining marks (accents, the tilde of ``ñ``), control characters
    (Unicode ``Cc``: NUL, BEL, DEL, C1 controls, and also ``\x1c``-``\x1f``)
    and format characters (``Cf``: soft hyphen, zero-width space and joiners,
    BOM). The ASCII whitespace controls ``\t \n \v \f \r`` are kept and
    collapse into one space. SQLite's FTS5 trigram tokenizer skips NUL in
    stored text, so dropping controls keeps the index and :func:`matches`
    in agreement.
    """
    if unicodedata.combining(char):
        return True
    return unicodedata.category(char) in ("Cc", "Cf") and char not in _WHITESPACE_CONTROLS
_TERM_KEYS = frozenset({"texto", "operador", "modo"})


def normalize(text: str | None) -> str:
    """Fold ``text`` for comparison: no accents, casefolded, single spaces.

    Accents are removed by NFKD decomposition minus combining marks, which
    also turns ``ñ`` into ``n`` and ``ü`` into ``u`` (as the site does).
    Control and format characters are dropped too (see :func:`ignorable`).
    Pure and idempotent.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not ignorable(ch))
    return _WHITESPACE.sub(" ", stripped.casefold()).strip()


@dataclass(frozen=True, slots=True)
class Term:
    """One search term: a literal phrase, how it joins the previous term, and
    whether it must or must not appear."""

    text: str
    operator: Operator = "y"
    """Join with the previous term; ignored on the first term."""
    mode: Mode = "contiene"

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not normalize(self.text):
            raise ValueError("a search term needs a non-empty 'texto'")
        if self.operator not in OPERATORS:
            raise ValueError(f"'operador' must be one of {OPERATORS}, got {self.operator!r}")
        if self.mode not in MODES:
            raise ValueError(f"'modo' must be one of {MODES}, got {self.mode!r}")

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping with the tool-facing Spanish keys."""
        return {"texto": self.text, "operador": self.operator, "modo": self.mode}


def term_from_dict(item: Mapping[str, Any]) -> Term:
    """Build a :class:`Term` from ``{"texto", "operador"?, "modo"?}``; unknown keys fail."""
    if not isinstance(item, Mapping):
        raise ValueError(f"each term must be an object with 'texto', got {item!r}")
    unknown = set(item) - _TERM_KEYS
    if unknown:
        raise ValueError(f"unknown term keys {sorted(unknown)}; allowed: {sorted(_TERM_KEYS)}")
    if "texto" not in item:
        raise ValueError("each term needs 'texto'")
    return Term(
        text=item["texto"],
        operator=item.get("operador", "y"),
        mode=item.get("modo", "contiene"),
    )


def parse_terms(items: Iterable[Mapping[str, Any] | Term]) -> tuple[Term, ...]:
    """Validate a list of term dicts (or :class:`Term`) into a tuple of terms."""
    return tuple(item if isinstance(item, Term) else term_from_dict(item) for item in items)


def and_groups(terms: Sequence[Term]) -> list[list[Term]]:
    """Split terms into AND-groups at each ``operador="o"`` (first term's operator ignored)."""
    groups: list[list[Term]] = []
    for index, term in enumerate(terms):
        if index == 0 or term.operator == "o":
            groups.append([term])
        else:
            groups[-1].append(term)
    return groups


def phrase_starts(folded: str, phrase: str, *, palabra: bool = False) -> list[int]:
    """Start offsets of ``phrase`` in ``folded`` (both already normalized).

    Overlapping occurrences count. With ``palabra`` only occurrences at a
    word start are kept: the start of the text, or a previous character that
    is not alphanumeric (``str.isalnum``) after normalization. The phrase may
    end mid-word, so "cese" matches "ceses" but not "procese".
    """
    if not phrase:
        return []
    starts: list[int] = []
    index = folded.find(phrase)
    while index != -1:
        if not palabra or index == 0 or not folded[index - 1].isalnum():
            starts.append(index)
        index = folded.find(phrase, index + 1)
    return starts


def contains(haystack: str | None, phrase: str, *, palabra: bool = False) -> bool:
    """True when the normalized ``phrase`` occurs in the normalized ``haystack``."""
    folded_phrase = normalize(phrase)
    if not folded_phrase:
        return False
    return bool(phrase_starts(normalize(haystack), folded_phrase, palabra=palabra))


def _term_holds(folded: str, term: Term, palabra: bool) -> bool:
    found = bool(phrase_starts(folded, normalize(term.text), palabra=palabra))
    return found if term.mode == "contiene" else not found


def matches(
    haystack: str | None, query: str | Sequence[Term], *, palabra: bool = False
) -> bool:
    """True when ``haystack`` satisfies ``query`` with the site's semantics.

    ``query`` is a literal phrase or a list of :class:`Term`, evaluated as an
    OR of AND-groups. An empty query matches everything. A ``None`` or empty
    haystack (e.g. a 2014 article without sumario) never satisfies a
    non-empty query, not even a pure negation: unknown text is not evidence.
    ``palabra=True`` requires each phrase to start a word (the default is the
    site's plain substring match).
    """
    terms: Sequence[Term] = [Term(query)] if isinstance(query, str) else query
    if not terms:
        return True
    folded = normalize(haystack)
    if not folded:
        return False
    return any(
        all(_term_holds(folded, term, palabra) for term in group) for group in and_groups(terms)
    )
