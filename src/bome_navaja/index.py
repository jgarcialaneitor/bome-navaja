"""Local SQLite FTS5 index of BOME article sumarios.

Storage and query side of the local index (task 5); :mod:`bome_navaja.sync`
fills it in the background. Only the stdlib ``sqlite3`` is used.

Design choices:

* **Connections**: one connection per thread (``threading.local``), all in
  WAL mode with a busy timeout, so the MCP thread can search while the sync
  thread writes. They are opened with ``check_same_thread=False`` only so
  :meth:`SumarioIndex.close` can close every one of them.
* **Matching** (schema v2, task 5b): the FTS5 column stores
  :func:`bome_navaja.text.normalize` (sumario) and uses the ``trigram``
  tokenizer with ``case_sensitive 1`` (normalization already happened in
  Python), so a phrase MATCH is an exact substring test on normalized text.
  ``coincidencia="fragmento"`` (default) is therefore the same as
  :func:`bome_navaja.text.matches` and the site: case/accent-insensitive,
  ``ñ``→``n``, substring ("cese" finds cese, ceses, procese).
  ``coincidencia="palabra"`` also requires each phrase to start a word
  (:func:`bome_navaja.text.phrase_starts`): the trigram MATCH narrows the
  rows and a deterministic SQL function ``bome_palabra`` registered on every
  connection does the exact check inside the same SQL statement, so
  ``COUNT``/``LIMIT``/``OFFSET`` stay exact and in SQL.
* **Terms**: ``texto`` + ``terminos`` form an OR of AND-groups like
  ``buscar_articulos``. Each term becomes its own SQL condition (an
  ``IN (… MATCH ?)`` subquery for phrases of 3+ characters; ``instr`` on the
  normalized text for shorter ones, which trigram cannot index), combined
  with ``AND``/``OR``/``NOT`` in SQL. Every user string is a bound
  parameter; the MATCH string is one double-quoted phrase with ``"``
  doubled, so it can never carry FTS5 syntax. A trailing ``*`` is accepted
  and ignored (substrings and word starts already imply a prefix). Groups
  made only of ``no_contiene`` terms are allowed: they scan the articles
  that have a sumario (~20k rows at most, cheap).
* **Relevance**: ``orden="relevancia"`` ranks by FTS5 bm25 over the OR of
  the positive 3+ character phrases (approximate with trigrams); without
  such a phrase it falls back to date order. The scores are computed once
  per query into a per-connection ``TEMP`` table (``bome_rank``, rowid
  primary key) inside the same read snapshot as the count, and the row
  query joins it by primary key, so ranking stays linear. Rows matched only
  through short terms have no score and rank last.
* **Highlight**: ``resaltado`` marks the matched substring (or word start)
  with ``**`` on the ORIGINAL
  sumario (accents kept). FTS5 ``snippet()`` is not used because it would
  return the normalized text; the highlight is computed in Python from a
  normalized→original character map.
* **Errors vs data**: saving a bulletin as ``error`` never discards articles
  indexed earlier for it; it only records the error.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import wraps
from dataclasses import dataclass, fields, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

from .models import (
    BomeError,
    BomeIndexUnavailableError,
    BomeIndexVersionError,
    BomeStorageError,
    BulletinRef,
    JsonModel,
)
from .paths import index_path
from .search import ArticuloEncontrado, BusquedaInvalidaError
from .text import Term, and_groups, ignorable, normalize, phrase_starts, term_from_dict

SCHEMA_VERSION = 2
"""v1 (task 5) used a word tokenizer; v2 uses trigram and is migrated in place."""

MIN_SQLITE_VERSION = (3, 34, 0)
"""First SQLite with the FTS5 ``trigram`` tokenizer."""

TRIGRAM_MIN_CHARS = 3

LEASE_STALE_SECONDS = 180.0
"""A sync lease whose heartbeat is older than this belongs to a dead process."""

BUSY_TIMEOUT_MS = 15_000
"""Wait up to 15 s for another connection's write lock. Normal writes (one
bulletin) take milliseconds; the one long writer is the v1→v2 FTS rebuild
(~2-4 s for ~20k articles), which must not make a concurrent opener or the
sync of another process fail with "database is locked"."""
DEFAULT_LIMITE = 20
MAX_LIMITE = 200
MAX_DESPLAZAMIENTO = 1_000_000
SNIPPET_CHARS = 220

EstadoBoletin = Literal["indexado", "sin_sumarios", "error"]
ESTADOS: tuple[str, ...] = ("indexado", "sin_sumarios", "error")
Orden = Literal["fecha", "relevancia"]
Coincidencia = Literal["fragmento", "palabra"]
COINCIDENCIAS: tuple[str, ...] = ("fragmento", "palabra")

_NOTAS = {
    "fragmento": (
        "coincidencia='fragmento': substring match on article sumarios, like the site "
        "(case/accent-insensitive; 'cese' also finds 'ceses' and 'procese'). Use "
        "coincidencia='palabra' to match only at the start of words."
    ),
    "palabra": (
        "coincidencia='palabra': each phrase must start a word (case/accent-insensitive; "
        "'cese' finds 'cese' and 'ceses' but not 'procese')."
    ),
}
_COVERAGE_NOTE = " Only indexed bulletins are searched: check 'cobertura'."

_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts "
    "USING fts5 (texto, tokenize = 'trigram case_sensitive 1')"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bulletins (
    cve TEXT PRIMARY KEY,
    number INTEGER NOT NULL,
    date TEXT,
    extraordinary INTEGER NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN ('indexado', 'sin_sumarios', 'error')),
    error_code TEXT,
    error_message TEXT,
    n_articulos INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bulletins_date ON bulletins (date);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    cve TEXT NOT NULL UNIQUE,
    bulletin_cve TEXT NOT NULL,
    number INTEGER NOT NULL,
    sumario TEXT,
    departamento TEXT NOT NULL,
    consejeria TEXT NOT NULL,
    organismo TEXT NOT NULL,
    consejeria_norm TEXT NOT NULL,
    url TEXT NOT NULL,
    pdf_url TEXT,
    listado_en_bome INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_bulletin ON articles (bulletin_cve);
CREATE TABLE IF NOT EXISTS calendar (
    cve TEXT PRIMARY KEY,
    number INTEGER NOT NULL,
    date TEXT,
    extraordinary INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT NOT NULL,
    heartbeat REAL NOT NULL,
    started REAL NOT NULL
);
"""


def utc_iso(timestamp: float | None = None) -> str:
    moment = datetime.fromtimestamp(timestamp if timestamp is not None else time.time(), UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sqlite_version_info() -> tuple[int, ...]:
    """Version of the SQLite library (a seam so tests can simulate old builds)."""
    return tuple(sqlite3.sqlite_version_info)


def _sql_word_start(texto: str | None, phrase: str | None) -> int:
    """SQL ``bome_palabra(texto, frase)``: 1 when ``frase`` starts a word of ``texto``."""
    if not texto or not phrase:
        return 0
    return 1 if phrase_starts(texto, phrase, palabra=True) else 0


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """True when this SQLite build can create FTS5 tables."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._bome_fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._bome_fts5_probe")
    except sqlite3.OperationalError:
        return False
    return True


def _connect(path: Path) -> sqlite3.Connection:
    """Open one connection (a seam so tests can simulate open failures)."""
    return sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=False,
    )


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _reading(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Map raw ``sqlite3``/JSON failures of a read path to :class:`BomeIndexUnavailableError`.

    ``BomeError`` (including ``BusquedaInvalidaError``, a ``ValueError``)
    passes through unchanged.
    """

    @wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return method(*args, **kwargs)
        except BomeError:
            raise
        except (sqlite3.Error, ValueError) as exc:
            raise BomeIndexUnavailableError(f"cannot read the index: {exc}") from exc

    return wrapper


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class ArticuloIndexado(JsonModel):
    """An indexed article with its bulletin, as returned by :meth:`SumarioIndex.buscar`."""

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
    listado_en_bome: bool
    resaltado: str | None
    """The original sumario (windowed when long) with matches in ``**bold**``."""


@dataclass(frozen=True, slots=True)
class ResultadoIndice(JsonModel):
    """One page of index hits plus how complete the index is."""

    consulta: dict[str, Any]
    articulos: tuple[ArticuloIndexado, ...]
    total: int
    limite: int
    desplazamiento: int
    siguiente: int | None
    """Offset of the next page, or ``None`` when this is the last one."""
    cobertura: dict[str, Any]
    """Indexed range and count, last sync time and whether a sync is running:
    results only cover what has been indexed so far."""
    nota: str


@dataclass(frozen=True, slots=True)
class EstadoIndice(JsonModel):
    """Snapshot of the index contents."""

    ruta: str
    tamano_bytes: int
    version_esquema: int
    boletines: dict[str, int]
    articulos: int
    articulos_con_sumario: int
    fecha_min: date | None
    fecha_max: date | None
    calendario_conocidos: int
    """Bulletins known from the site calendar (recorded by the last sync)."""
    pendientes: int
    """Calendar bulletins not indexed yet, or whose last attempt failed."""
    ultima_sincronizacion: dict[str, Any] | None
    sincronizacion_en_curso: dict[str, Any] | None
    """Live sync lease (owner, heartbeat), or ``None``."""


# --------------------------------------------------------------------------- query building


@dataclass(frozen=True, slots=True)
class _Phrase:
    text: str
    """Normalized phrase (never empty)."""
    negated: bool

    @property
    def uses_trigram(self) -> bool:
        return len(self.text) >= TRIGRAM_MIN_CHARS and "\x00" not in self.text

    def fts(self) -> str:
        # One FTS5 string: the only special character inside it is '"', doubled.
        return '"' + self.text.replace('"', '""') + '"'

    def sql(self, palabra: bool) -> tuple[str, list[Any]]:
        """SQL condition on ``f`` (the FTS row) and its bound parameters."""
        parts: list[str] = []
        params: list[Any] = []
        if self.uses_trigram:
            parts.append("f.rowid IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)")
            params.append(self.fts())
        elif not palabra:
            parts.append("instr(f.texto, ?) > 0")
            params.append(self.text)
        if palabra:
            parts.append("bome_palabra(f.texto, ?)")
            params.append(self.text)
        condition = "(" + " AND ".join(parts) + ")"
        return (f"NOT {condition}" if self.negated else condition), params


def _invalid(message: str) -> BusquedaInvalidaError:
    return BusquedaInvalidaError(message)


def _terms(texto: str | None, terminos: Sequence[Mapping[str, Any]] | None) -> list[Term]:
    terms: list[Term] = []
    if texto is not None:
        try:
            terms.append(Term(texto))
        except ValueError as exc:
            raise _invalid(f"'texto': {exc}") from None
    if terminos is None:
        return terms
    if isinstance(terminos, (str, bytes)) or not isinstance(terminos, Sequence):
        raise _invalid("'terminos' must be a list of objects {texto, operador?, modo?}")
    for position, item in enumerate(terminos):
        try:
            terms.append(term_from_dict(item))
        except ValueError as exc:
            raise _invalid(f"terminos[{position}]: {exc}") from None
    return terms


def _phrase(term: Term) -> _Phrase:
    _require_utf8(term.text, "texto")
    folded = normalize(term.text.strip().rstrip("*"))
    if not folded:
        raise _invalid(f"term {term.text!r} has nothing to search for")
    return _Phrase(text=folded, negated=term.mode == "no_contiene")


def _terms_condition(
    terms: Sequence[Term], palabra: bool
) -> tuple[str | None, list[Any], list[_Phrase]]:
    """SQL for an OR of AND-groups, its parameters, and the positive phrases."""
    if not terms:
        return None, [], []
    groups: list[str] = []
    params: list[Any] = []
    positives: list[_Phrase] = []
    for group in and_groups(terms):
        conditions: list[str] = []
        for term in group:
            phrase = _phrase(term)
            condition, condition_params = phrase.sql(palabra)
            conditions.append(condition)
            params.extend(condition_params)
            if not phrase.negated:
                positives.append(phrase)
        groups.append("(" + " AND ".join(conditions) + ")")
    return "(" + " OR ".join(groups) + ")", params, positives


# --------------------------------------------------------------------------- highlight


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """``text.normalize(text)`` plus, per output char, the index of its source char."""
    out: list[str] = []
    source: list[int] = []
    pending_space = False
    for index, char in enumerate(text):
        decomposed = unicodedata.normalize("NFKD", char)
        piece = "".join(c for c in decomposed if not ignorable(c)).casefold()
        for folded in piece:
            if folded.isspace():
                pending_space = bool(out)
                continue
            if pending_space:
                out.append(" ")
                source.append(index)
                pending_space = False
            out.append(folded)
            source.append(index)
    return "".join(out), source


def _highlight(sumario: str | None, phrases: Sequence[_Phrase], palabra: bool) -> str | None:
    if not sumario or not phrases:
        return None
    folded, source = _normalize_with_map(sumario)
    if folded != normalize(sumario):  # safety net: never mis-place a highlight
        return None
    spans: list[tuple[int, int]] = []
    for phrase in phrases:
        size = len(phrase.text)
        for start in phrase_starts(folded, phrase.text, palabra=palabra):
            spans.append((source[start], source[start + size - 1] + 1))
    if not spans:
        return None
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    low, high = 0, len(sumario)
    if high > SNIPPET_CHARS + 20:
        low = max(0, merged[0][0] - 80)
        high = min(len(sumario), low + SNIPPET_CHARS)
        low = max(0, high - SNIPPET_CHARS)
    pieces: list[str] = []
    cursor = low
    for start, end in merged:
        if start < low or end > high:
            continue
        pieces += [sumario[cursor:start], "**", sumario[start:end], "**"]
        cursor = end
    pieces.append(sumario[cursor:high])
    body = "".join(pieces).strip()
    return ("…" if low > 0 else "") + body + ("…" if high < len(sumario) else "")


# --------------------------------------------------------------------------- index


def _date_arg(value: date | str | None, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):  # a datetime counts as its calendar day
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise _invalid(f"'{name}' must be an ISO date YYYY-MM-DD, got {value!r}")


def _int_arg(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise _invalid(f"'{name}' must be an integer {bounds}, got {value!r}")
    return value


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utf8(text: str) -> str:
    """``text`` made storable: lone surrogates (not UTF-8 encodable) become ``?``."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _require_utf8(text: str, name: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(f"'{name}' contains characters that are not valid text") from None


def _storable(article: ArticuloEncontrado) -> ArticuloEncontrado:
    """Copy of ``article`` with every text field made UTF-8 encodable."""
    changes = {
        field.name: _utf8(value)
        for field in fields(article)
        if isinstance(value := getattr(article, field.name), str)
    }
    return replace(article, **changes)


def _error_info(error: BaseException | str | None) -> tuple[str | None, str | None]:
    if error is None:
        return None, None
    if isinstance(error, BaseException):
        code = getattr(error, "error_code", None) or type(error).__name__
        return _utf8(str(code)), _utf8(str(error))
    return "error", _utf8(str(error))


class SumarioIndex:
    """The local sumario index stored in one SQLite file."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else index_path()[0]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BomeStorageError(f"cannot create {self.path.parent}: {exc}", path=str(self.path)) from exc
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        if _sqlite_version_info() < MIN_SQLITE_VERSION:
            raise BomeIndexUnavailableError(
                f"SQLite {'.'.join(map(str, _sqlite_version_info()))} is too old for the local "
                "sumario index: the FTS5 trigram tokenizer needs SQLite 3.34 or newer "
                "(live searches still work)"
            )
        try:
            conn = self._conn()
            if not _fts5_available(conn):
                raise BomeIndexUnavailableError(
                    "this Python's SQLite has no FTS5 module; the local sumario index is "
                    "unavailable (live searches still work)"
                )
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                self._migrate(conn)
            except BomeStorageError as exc:
                raise BomeIndexUnavailableError(
                    f"cannot prepare the index {self.path} (schema creation or migration "
                    f"failed and was rolled back): {exc}"
                ) from exc
        except sqlite3.Error as exc:
            self.close()
            raise BomeIndexUnavailableError(f"cannot open the index {self.path}: {exc}") from exc
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------------ plumbing

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = _connect(self.path)
                conn.row_factory = sqlite3.Row
                conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                conn.create_function("bome_palabra", 2, _sql_word_start, deterministic=True)
            except sqlite3.Error as exc:
                raise BomeIndexUnavailableError(f"cannot open the index {self.path}: {exc}") from exc
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def conexiones_abiertas(self) -> int:
        """Number of open per-thread connections (diagnostics and tests)."""
        with self._lock:
            return len(self._connections)

    def cerrar_conexion_hilo(self) -> None:
        """Close the calling thread's connection (call it before a worker thread ends)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)
        try:
            conn.close()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        """Close every connection opened by this index (from any thread)."""
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise BomeStorageError(f"index busy or unwritable: {exc}", path=str(self.path)) from exc
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException as exc:
            self._rollback(conn)
            if isinstance(exc, (sqlite3.Error, UnicodeError)):
                raise BomeStorageError(
                    f"cannot write the index: {exc}", path=str(self.path)
                ) from exc
            raise

    @contextmanager
    def _read_snapshot(self, conn: sqlite3.Connection) -> Iterator[None]:
        """Deferred read transaction: count, ranking and rows see one snapshot."""
        conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._rollback(conn)
            raise
        conn.execute("COMMIT")

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        """Roll back if a transaction is still open; never mask the original error."""
        if not conn.in_transaction:
            return
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    @staticmethod
    def _schema_exists(conn: sqlite3.Connection) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
            ).fetchone()
            is not None
        )

    @staticmethod
    def _stored_version(conn: sqlite3.Connection) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return row[0] if row else None

    def _check_version(self, conn: sqlite3.Connection) -> None:
        stored = self._stored_version(conn)
        if stored != str(SCHEMA_VERSION):
            raise BomeIndexVersionError(
                f"index {self.path} has schema version {stored!r}; this bome-navaja "
                f"understands version {SCHEMA_VERSION}. Delete the file to rebuild it."
            )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Create the schema if needed and check its version.

        Idempotent under a first-open race between processes: every statement
        is ``IF NOT EXISTS`` / ``INSERT OR IGNORE`` and the version is re-read
        inside the same write transaction.
        """
        if self._schema_exists(conn):
            if self._stored_version(conn) == "1":
                self._upgrade_from_v1()
            self._check_version(conn)
            return
        with self._tx() as tx:
            # executescript() would COMMIT first; run the statements one by one
            # so the whole schema is created atomically.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    tx.execute(statement)
            tx.execute(_FTS_DDL)
            tx.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._check_version(tx)

    def _upgrade_from_v1(self) -> None:
        """v1 → v2: rebuild the FTS table with trigrams from ``articles``, locally.

        One transaction; bulletin and article rows are kept, nothing is
        crawled. The version is re-read under the write lock, so a concurrent
        opener that already migrated makes this a no-op. Each sumario is
        normalized exactly once, in Python (a SQL function in both the SELECT
        list and the WHERE clause ran twice per row). Failures roll back and
        surface from ``__init__`` as :class:`BomeIndexUnavailableError`.
        """
        with self._tx() as tx:
            if self._stored_version(tx) != "1":
                return
            tx.execute("DROP TABLE IF EXISTS articles_fts")
            tx.execute(_FTS_DDL)
            rows = tx.execute("SELECT id, sumario FROM articles").fetchall()
            folded = ((row[0], normalize(row[1])) for row in rows)
            tx.executemany(
                "INSERT INTO articles_fts (rowid, texto) VALUES (?, ?)",
                ((article_id, text) for article_id, text in folded if text),
            )
            tx.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
            )

    @_reading
    def version_esquema(self) -> int:
        """Schema version stored in the file."""
        row = self._conn().execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row[0])

    def _meta(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ writes

    def guardar_boletin(
        self,
        ref: BulletinRef,
        articles: Sequence[ArticuloEncontrado],
        estado: EstadoBoletin,
        error: BaseException | str | None = None,
    ) -> None:
        """Store one bulletin and its articles in a single transaction.

        Idempotent: the bulletin's previous articles are replaced; duplicate
        article CVEs are collapsed, the last one wins. Any SQLite failure rolls
        the transaction back and raises :class:`BomeStorageError`. With
        ``estado="error"`` nothing indexed earlier is discarded: an already
        indexed bulletin keeps its articles and state, and only the error is
        recorded.
        """
        if estado not in ESTADOS:
            raise ValueError(f"estado must be one of {ESTADOS}, got {estado!r}")
        # One row per article CVE: a page listing an article twice keeps the
        # last occurrence (at the position of the first one).
        # Lone surrogates (not UTF-8 encodable) are replaced by "?" before storing.
        articles = [
            _storable(article)
            for article in {article.cve: article for article in articles}.values()
        ]
        code, message = _error_info(error)
        now = utc_iso()
        day = ref.date.isoformat() if ref.date else None
        with self._tx() as conn:
            if estado == "error":
                previous = conn.execute(
                    "SELECT estado FROM bulletins WHERE cve = ?", (ref.cve,)
                ).fetchone()
                if previous is not None and previous[0] != "error":
                    conn.execute(
                        "UPDATE bulletins SET error_code = ?, error_message = ? WHERE cve = ?",
                        (code, message, ref.cve),
                    )
                    return
            stale = [
                row[0]
                for row in conn.execute("SELECT id FROM articles WHERE bulletin_cve = ?", (ref.cve,))
            ]
            for article in articles:
                row = conn.execute("SELECT id FROM articles WHERE cve = ?", (article.cve,)).fetchone()
                if row is not None:
                    stale.append(row[0])
            for article_id in set(stale):
                conn.execute("DELETE FROM articles_fts WHERE rowid = ?", (article_id,))
                conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
            conn.execute(
                "INSERT OR REPLACE INTO bulletins (cve, number, date, extraordinary, estado, "
                "error_code, error_message, n_articulos, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ref.cve, ref.number, day, int(ref.extraordinary), estado, code, message,
                 0 if estado == "error" else len(articles), now),
            )
            if estado == "error":
                return
            for article in articles:
                cursor = conn.execute(
                    "INSERT INTO articles (cve, bulletin_cve, number, sumario, departamento, "
                    "consejeria, organismo, consejeria_norm, url, pdf_url, listado_en_bome) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (article.cve, ref.cve, article.numero, article.sumario, article.departamento,
                     article.consejeria, article.organismo, normalize(article.consejeria),
                     article.url, article.pdf_url, int(article.listado_en_bome)),
                )
                folded = normalize(article.sumario)
                if folded:
                    conn.execute(
                        "INSERT INTO articles_fts (rowid, texto) VALUES (?, ?)",
                        (cursor.lastrowid, folded),
                    )

    def registrar_calendario(self, refs: Sequence[BulletinRef]) -> None:
        """Remember the bulletins the site calendar lists (for ``pendientes``)."""
        with self._tx() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO calendar (cve, number, date, extraordinary) VALUES (?, ?, ?, ?)",
                [
                    (r.cve, r.number, r.date.isoformat() if r.date else None, int(r.extraordinary))
                    for r in refs
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('calendar_updated', ?)",
                (utc_iso(),),
            )

    def guardar_resumen_sincronizacion(self, resumen: Mapping[str, Any]) -> None:
        """Persist the summary of the last sync (JSON) for :meth:`estado`."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                (json.dumps(dict(resumen), ensure_ascii=True),),
            )

    # ------------------------------------------------------------------ reads

    @_reading
    def estado_boletin(self, cve: str) -> str | None:
        """Stored ``estado`` of a bulletin, or ``None`` when never processed."""
        row = self._conn().execute("SELECT estado FROM bulletins WHERE cve = ?", (cve,)).fetchone()
        return row[0] if row else None

    @_reading
    def estados_boletines(self) -> dict[str, str]:
        """``cve → estado`` for every processed bulletin."""
        return {row[0]: row[1] for row in self._conn().execute("SELECT cve, estado FROM bulletins")}

    def _last_sync(self) -> dict[str, Any] | None:
        raw = self._meta("last_sync")
        return json.loads(raw) if raw else None

    def _coverage(self) -> dict[str, Any]:
        row = self._conn().execute(
            "SELECT count(*), min(date), max(date) FROM bulletins "
            "WHERE estado IN ('indexado', 'sin_sumarios')"
        ).fetchone()
        pending = self._pending()
        last = self._last_sync()
        return {
            "boletines_indexados": row[0],
            "fecha_min": row[1],
            "fecha_max": row[2],
            "pendientes": pending,
            "ultima_sincronizacion": (last or {}).get("finalizado"),
            "sincronizacion_en_curso": self.lease() is not None,
        }

    def _pending(self) -> int:
        return self._conn().execute(
            "SELECT count(*) FROM calendar c LEFT JOIN bulletins b ON b.cve = c.cve "
            "WHERE b.cve IS NULL OR b.estado = 'error'"
        ).fetchone()[0]

    @_reading
    def buscar(
        self,
        texto: str | None = None,
        terminos: Sequence[Mapping[str, Any]] | None = None,
        *,
        desde: date | str | None = None,
        hasta: date | str | None = None,
        extraordinario: bool | None = None,
        consejeria: str | None = None,
        orden: Orden = "fecha",
        limite: int = DEFAULT_LIMITE,
        desplazamiento: int = 0,
        coincidencia: Coincidencia = "fragmento",
    ) -> ResultadoIndice:
        """Search indexed sumarios; see the module docstring for the semantics.

        ``coincidencia`` is ``"fragmento"`` (substring, like the site) or
        ``"palabra"`` (each phrase starts a word). ``consejeria`` is a
        case/accent-insensitive substring of the consejería name. ``orden`` is
        ``"fecha"`` (newest bulletin first, then article number) or
        ``"relevancia"`` (approximate trigram bm25; date order when no phrase
        of 3+ characters is searched). ``limite`` is capped at 200;
        ``siguiente`` is the next offset. Articles without a sumario only
        appear when no term is given.
        """
        if coincidencia not in COINCIDENCIAS:
            raise _invalid(f"'coincidencia' must be one of {COINCIDENCIAS}, got {coincidencia!r}")
        palabra = coincidencia == "palabra"
        terms = _terms(texto, terminos)
        condition, condition_params, positives = _terms_condition(terms, palabra)
        start = _date_arg(desde, "desde")
        end = _date_arg(hasta, "hasta")
        if extraordinario is not None and not isinstance(extraordinario, bool):
            raise _invalid(f"'extraordinario' must be true, false or null, got {extraordinario!r}")
        if orden not in ("fecha", "relevancia"):
            raise _invalid(f"'orden' must be 'fecha' or 'relevancia', got {orden!r}")
        limit = min(_int_arg(limite, "limite", 1), MAX_LIMITE)
        offset = _int_arg(desplazamiento, "desplazamiento", 0, MAX_DESPLAZAMIENTO)

        sources = "FROM articles a JOIN bulletins b ON b.cve = a.bulletin_cve"
        where: list[str] = []
        params: list[Any] = []
        if condition is not None:
            # Driving from the FTS rows keeps sumario-less articles out and
            # exposes the normalized text as f.texto for instr/bome_palabra.
            sources = (
                "FROM articles_fts f JOIN articles a ON a.id = f.rowid "
                "JOIN bulletins b ON b.cve = a.bulletin_cve"
            )
            where.append(condition)
            params.extend(condition_params)
        if start is not None:
            where.append("b.date >= ?")
            params.append(start)
        if end is not None:
            where.append("b.date <= ?")
            params.append(end)
        if extraordinario is not None:
            where.append("b.extraordinary = ?")
            params.append(int(extraordinario))
        if consejeria is not None:
            if not isinstance(consejeria, str) or not normalize(consejeria):
                raise _invalid("'consejeria' must be a non-empty text")
            _require_utf8(consejeria, "consejeria")
            where.append("a.consejeria_norm LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(normalize(consejeria))}%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        order = "b.date DESC, b.number DESC, a.number ASC"
        ranking = [p for p in positives if p.uses_trigram]
        rank_join = ""
        if orden == "relevancia" and ranking:
            # bm25 is computed ONCE per query into a per-connection TEMP table
            # keyed by rowid; the row query then joins it through its primary
            # key. A CTE joined to the result made SQLite re-run the MATCH for
            # every row (quadratic: 111 s for 14k hits on 20k articles).
            rank_join = " LEFT JOIN temp.bome_rank r ON r.rid = f.rowid"
            order = "coalesce(r.score, 0), " + order
        conn = self._conn()
        try:
            with self._read_snapshot(conn):
                total = conn.execute(f"SELECT count(*) {sources}{clause}", params).fetchone()[0]
                if rank_join:
                    conn.execute(
                        "CREATE TEMP TABLE IF NOT EXISTS bome_rank "
                        "(rid INTEGER PRIMARY KEY, score REAL NOT NULL)"
                    )
                    conn.execute("DELETE FROM temp.bome_rank")
                    conn.execute(
                        "INSERT INTO temp.bome_rank (rid, score) "
                        "SELECT rowid, rank FROM articles_fts WHERE articles_fts MATCH ?",
                        (" OR ".join(p.fts() for p in ranking),),
                    )
                rows = conn.execute(
                    "SELECT a.cve, a.number, a.sumario, a.departamento, a.consejeria, "
                    "a.organismo, a.url, a.pdf_url, a.listado_en_bome, b.cve AS bcve, "
                    f"b.number AS bnumber, b.date AS bdate, b.extraordinary {sources}{rank_join}"
                    f"{clause} ORDER BY {order} LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
        except sqlite3.Error as exc:
            raise BomeStorageError(f"index query failed: {exc}", path=str(self.path)) from exc
        articles = tuple(
            ArticuloIndexado(
                bome_cve=row["bcve"],
                bome_numero=row["bnumber"],
                bome_fecha=date.fromisoformat(row["bdate"]) if row["bdate"] else None,
                bome_extraordinario=bool(row["extraordinary"]),
                cve=row["cve"],
                numero=row["number"],
                sumario=row["sumario"],
                departamento=row["departamento"],
                consejeria=row["consejeria"],
                organismo=row["organismo"],
                url=row["url"],
                pdf_url=row["pdf_url"],
                listado_en_bome=bool(row["listado_en_bome"]),
                resaltado=_highlight(row["sumario"], positives, palabra),
            )
            for row in rows
        )
        return ResultadoIndice(
            consulta={
                "terminos": [term.to_dict() for term in terms],
                "desde": start,
                "hasta": end,
                "extraordinario": extraordinario,
                "consejeria": consejeria,
                "orden": orden,
                "coincidencia": coincidencia,
            },
            articulos=articles,
            total=total,
            limite=limit,
            desplazamiento=offset,
            siguiente=offset + limit if offset + limit < total else None,
            cobertura=self._coverage(),
            nota=_NOTAS[coincidencia] + _COVERAGE_NOTE,
        )

    @_reading
    def estado(self) -> EstadoIndice:
        """Counts, date range, calendar coverage, file size and last sync."""
        conn = self._conn()
        counts = {name: 0 for name in ESTADOS}
        for row in conn.execute("SELECT estado, count(*) FROM bulletins GROUP BY estado"):
            counts[row[0]] = row[1]
        counts["total"] = sum(counts[name] for name in ESTADOS)
        articles, with_text = conn.execute(
            "SELECT count(*), sum(CASE WHEN trim(coalesce(sumario, '')) <> '' THEN 1 ELSE 0 END) "
            "FROM articles"
        ).fetchone()
        low, high = conn.execute(
            "SELECT min(date), max(date) FROM bulletins WHERE estado IN ('indexado', 'sin_sumarios')"
        ).fetchone()
        known = conn.execute("SELECT count(*) FROM calendar").fetchone()[0]
        size = sum(
            candidate.stat().st_size
            for candidate in (self.path, Path(f"{self.path}-wal"))
            if candidate.exists()
        )
        return EstadoIndice(
            ruta=str(self.path),
            tamano_bytes=size,
            version_esquema=self.version_esquema(),
            boletines=counts,
            articulos=articles,
            articulos_con_sumario=with_text or 0,
            fecha_min=date.fromisoformat(low) if low else None,
            fecha_max=date.fromisoformat(high) if high else None,
            calendario_conocidos=known,
            pendientes=self._pending(),
            ultima_sincronizacion=self._last_sync(),
            sincronizacion_en_curso=self.lease(),
        )

    # ------------------------------------------------------------------ cross-process lease

    @staticmethod
    def _lease_info(row: sqlite3.Row, now: float) -> dict[str, Any]:
        return {
            "propietario": row["owner"],
            "latido": utc_iso(row["heartbeat"]),
            "iniciado": utc_iso(row["started"]),
            "segundos_desde_latido": round(now - row["heartbeat"], 1),
        }

    def adquirir_lease(
        self, owner: str, *, now: float | None = None, stale_after: float = LEASE_STALE_SECONDS
    ) -> dict[str, Any] | None:
        """Take the sync lease; ``None`` on success, else the live holder's info.

        A lease whose heartbeat is older than ``stale_after`` seconds is taken
        over (its process died without releasing it).
        """
        moment = time.time() if now is None else now
        with self._tx() as conn:
            row = conn.execute("SELECT owner, heartbeat, started FROM sync_lease WHERE id = 1").fetchone()
            if row is not None and row["owner"] != owner and moment - row["heartbeat"] <= stale_after:
                return self._lease_info(row, moment)
            started = row["started"] if row is not None and row["owner"] == owner else moment
            conn.execute(
                "INSERT OR REPLACE INTO sync_lease (id, owner, heartbeat, started) VALUES (1, ?, ?, ?)",
                (owner, moment, started),
            )
        return None

    def renovar_lease(self, owner: str, *, now: float | None = None) -> bool:
        """Refresh the heartbeat; ``False`` when ``owner`` no longer holds the lease."""
        moment = time.time() if now is None else now
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE sync_lease SET heartbeat = ? WHERE id = 1 AND owner = ?", (moment, owner)
            )
            return cursor.rowcount == 1

    def liberar_lease(self, owner: str) -> None:
        """Drop the lease if ``owner`` holds it."""
        with self._tx() as conn:
            conn.execute("DELETE FROM sync_lease WHERE id = 1 AND owner = ?", (owner,))

    @_reading
    def lease(
        self, *, now: float | None = None, stale_after: float = LEASE_STALE_SECONDS
    ) -> dict[str, Any] | None:
        """Info on the live lease, or ``None`` when free or stale."""
        moment = time.time() if now is None else now
        row = self._conn().execute(
            "SELECT owner, heartbeat, started FROM sync_lease WHERE id = 1"
        ).fetchone()
        if row is None or moment - row["heartbeat"] > stale_after:
            return None
        return self._lease_info(row, moment)


__all__ = [
    "LEASE_STALE_SECONDS",
    "MAX_DESPLAZAMIENTO",
    "utc_iso",
    "MAX_LIMITE",
    "SCHEMA_VERSION",
    "ArticuloIndexado",
    "EstadoIndice",
    "ResultadoIndice",
    "SumarioIndex",
]
