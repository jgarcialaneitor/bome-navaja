# Feature: bome-mcp

## Intent

An MCP server ("bome-navaja", sibling of `navaja`) that lets an AI model use the
Boletín Oficial de la Ciudad Autónoma de Melilla (https://bomemelilla.es) as fully
as the site allows: list every BOME, open a bulletin and its articles, read whole
bulletins and single announcements in full, download any PDF, and search anything.
Example question that must be answerable: "todos los BOMEs donde hay nombramientos
y ceses de personal eventual".

Runs on Linux like `navaja`, and on Windows through a one-click `.mcpb` bundle for
Claude Desktop.

## Scope

In scope:
- Live access to every data path the site exposes (see "Site facts").
- Article-level search built on top of the site's BOME-level search (drill-down).
- Local SQLite FTS5 index of article sumarios, incrementally synchronised,
  giving instant article-level search with AND / OR / NOT / prefix, accent- and
  case-insensitive (user decision 2026-09-23: "en vivo + índice local de sumarios").
- PDF download (bulletin, sumario, article, single page) and PDF text reading,
  paginated so long bulletins never get truncated.
- Cross-platform paths (Linux XDG, Windows %LOCALAPPDATA%, macOS).
- `.mcpb` bundle (manifest v0.4, `server.type = "uv"`), same pattern as navaja.

Out of scope, deliberately:
- Full-text index of every article/PDF (user chose sumario index only; can be added later).
- BOMEs older than 2014: the site does not publish them.
- GitHub repository creation, push, PR, release: user decisions.

## Site facts (measured 2026-09-23, samples in tests/fixtures/)

- Symfony app, no robots/sitemap, no cookies/CSRF/captcha seen.
- `/api/bomes/calendar?start&end` JSON: 1935 bulletins (1240 `BOME-B`, 695 `BOME-BX`) since 2014-01-03.
- `/api/section/consejerias/{departamento}` and `/api/section/organismos/{consejeria}` JSON `[{id,nombre}]`.
- CVEs: `BOME-B`/`BOME-BX` bulletin, `BOME-A-YYYY-N` article, `BOME-S` sumario, `BOME-P-YYYY-N` page.
  `/buscar-cve?cve=` 302 → canonical page. PDFs: `/bome/descargar/{CVE}.pdf`.
- Pages: `/bome/{CVE}` (section → consejería → organismo → articles), `/bome/{CVE}/sumario`,
  `/bome/{CVE}/articulo/{n}` (full HTML text, `#pagina-N` anchors).
- Search `/buscar` and `/buscador-avanzado` (collections `contenido[i]`, `numero[i]`, and/or,
  like/nlike, departamento/consejeria/organismo), `page=N`, 10 per page. Returns BOMEs only.
- Search is case/accent-insensitive literal substring (word order matters, no synonyms:
  "destitución" = 0, the BOME says "cese").
- Article sumarios exist only from late 2016; 2014–2016 bulletins only have the whole PDF
  and page-content search.
- Learned in task 2 (live-verified 2026-09-23):
  - Extraordinary bulletins have their own article and sumario kinds: `BOME-AX`, `BOME-SX`
    (no `BOME-PX` seen yet).
  - Bulletin tree has 4 levels: departamento → consejería (id = section API id) → organismo →
    article. The sumario web view has no organismo level and no article CVEs.
  - Search results carry no snippet. `/buscar` only filters `contenido` when `from` and `tipo`
    are also sent; otherwise it silently lists the current-year bulletins (task 3 must enforce).
  - Calendar `end` is inclusive. `/buscar-cve` redirects even for non-existent CVEs (target
    then 404s), so resolution confirms the target. Year slider: `/api/bomes/{year}` (HTML).
- Learned in task 3 (live-verified 2026-09-23, `/buscador-avanzado`):
  - `from` is mandatory for text filtering; `to` is honoured. AND and "no contiene" apply
    within ONE article sumario; OR is ignored by the site (so `buscar_articulos` runs one
    site search per AND-group). Scopes can be mixed per term. "ñ" folds to "n".
  - Numeric criteria are exact (bulletin 6416, article 1051, page 4784, year 2025).
  - Counters use a thousands dot ("1.934").
  - Bulletin pages can OMIT articles (BOME-B-2025-6294 hides 745, a personal-eventual cese);
    the drill-down fetches numbering gaps from `/articulo/{n}`. Edge omissions are undetectable.
  - Some sumario pages (e.g. 2025-6294) are free-form `<p>` HTML and parse to 0 entries:
    task 5 must index from bulletin pages + gap fetch, not from the sumario view.
- Learned in task 4 (live-verified 2026-09-23):
  - 2014–2016 PDFs answer 404 (B-2014-5092, B-2015-5272, B-2016-5397, A-2014-2) although the
    page still shows "DESCARGAR BOME"; old articles have neither HTML text nor PDF.
  - Sizes: bulletin 6416 = 4.2 MB / 37 pages (~88k chars); sumario 253 KB / 2; article 1051
    265 KB / 4; page PDF 66 KB / 1. All recent PDFs have a text layer.
  - Article CVEs map to their bulletin only through `/buscar-cve` (302 to
    `/bome/{B}/articulo/{n}`); a page CVE resolves to the article URL + `#pagina-N`.
- Learned in task 5 (live-verified 2026-09-23):
  - Sync throughput ≈ 0.61 s per bulletin at `polite_delay=0.5` → full sync of ~1935
    bulletins ≈ 20–25 min. Index size ≈ 1.3 MB per 100 bulletins (≈ 25 MB total).
  - July 2025 (12 bulletins) syncs in 7.5 s with 14 GETs, including hidden article A-2025-745.
  - The index matches WHOLE WORDS (FTS5 unicode61 over `normalize(sumario)`), while the site
    and `buscar_articulos` match substrings: "cese" does not find "ceses"; use `cese*`.
    Every index result carries a note saying so.

## Constraints

- Tool names and parameters in Spanish; every tool returns a dict with `ok` and, on
  failure, `error` + `error_code`. stdout reserved for JSON-RPC (stdio).
- Tests run offline against saved fixtures (`httpx.MockTransport`); live tests gated by
  `BOME_NAVAJA_LIVE=1`. CI never hits the real site.
- Polite client: real browser UA, timeouts, small delay between bulk requests
  (index sync, drill-down).
- Python >= 3.12, deps: httpx, beautifulsoup4, lxml, mcp>=2,<3, pypdf.

## Decisions

- 2026-09-23: index sync runs ONLY when the model asks (`sincronizar_indice`); the server never
  crawls on its own. Empty or stale index is reported by `buscar_en_indice` / `estado_indice`.
- 2026-09-23: trigram index with a "palabra" mode (task 5b).

## Tasks

- [x] 1. Scaffold: pyproject (package `bome_navaja`, scripts `bome-navaja-mcp`), MIT license,
      pytest config, CI (Linux gating + Windows), README skeleton, fixtures.
      Verified: `15 passed` (`uv run pytest`). Commit `409df1f`.
      Native review not possible: `lens_context_budget_exceeded` (mostly fixtures + uv.lock).
      User decision 2026-09-23: leave it unreviewed rather than rewrite history.
- [x] 2. Client + models + parsers: calendar, bulletin page tree, article page, sumario,
      section APIs, CVE resolution, search-results parsing and pagination.
      Verified: `106 passed, 4 skipped` offline; `4 passed` live (`BOME_NAVAJA_LIVE=1`);
      independent verify PASS, its 2 medium + 3 low findings fixed. Commit `bed0423`;
      native review `review-6fa20ed52d3e2aa4` approved and acknowledged (1 lens, reliability).
      Follow-ups (advisory, non-blocking): the `resolve_cve` confirmation GET follows
      redirects, so only the first hop is host-checked (client.py:293); sumario article CVEs
      are derived from the bulletin kind, not read from the page (parsers.py:463).
- [x] 3. Search: `buscar_bomes` (quick + advanced params) and `buscar_articulos` (drill-down
      that returns the matching articles, bounded).
      Verified: `162 passed, 9 skipped` offline; `9 passed` live; independent verify PASS.
      Target question "personal eventual" AND "cese" → 12 articles in 12 BOMEs, no errors.
      Commit `0e20f5f`; native review `review-b147ae3b2618f346` approved and acknowledged.
      Follow-ups (advisory): gap detection (search.py:660) misses edge omissions and assumes
      consecutive numbering; skip gap fetches when a `consejeria` filter is absent from the
      bulletin; OR-merge order with dateless bulletins.
- [x] 4. Documents: PDF download with safe cross-platform paths; paginated PDF/HTML text reading.
      Verified: `222 passed, 13 skipped` offline; all 13 live tests passed; independent verify
      PASS (bulletin 6416 read in 5 chunks, pages 1..37 without gaps). Verify findings F1
      (non-positive budget in `paginar`) and F2 (temp file left on interrupt) fixed.
      Follow-up (perf): cache hits re-hash and re-parse the whole PDF on every read.
      Commit `b7f6e97`; native review `review-2aa297913d1a4077` approved and acknowledged.
      Follow-up (advisory, paths.py:51): `Path.home()` is evaluated even when an env override
      is set and raises a non-Bome `RuntimeError` without a resolvable home; relative env
      overrides depend on the client's cwd. Handle both when task 6 reports `estado_servidor`.
- [x] 5. Local sumario index: SQLite FTS5, incremental background sync with progress,
      index search tool, index status.
      Verified: `306 passed, 15 skipped` offline; 7 live passed; independent verify PASS
      (0 unexpected exceptions in a 400-string query fuzz; lease race and mid-save crash
      probes OK). Its medium finding (a raw sqlite error made the whole sync `fallido`)
      and 4 lows were fixed. Commit `aec4609`; Windows CI 306 passed (official CPython has
      FTS5); native review `review-003756d6afb01a15` approved and acknowledged.
      Follow-ups (advisory): a bulletin whose hidden-article fetch failed is stored as
      `indexado` with a note and is never retried outside the recent-days window
      (sync.py:310); the lease heartbeat is renewed only between bulletins (sync.py:277).
      Product option: FTS5 `trigram` over the normalised text would give the same substring
      semantics as the site (bigger index, terms under 3 characters need a fallback).
- [x] 5b. Trigram index (user decision 2026-09-23): FTS5 `trigram` over `normalize(sumario)` so
      the index matches substrings like the site; `coincidencia` = "fragmento" (default) |
      "palabra" (match only at a word start: "cese" → cese/ceses, not procese); terms under
      3 characters fall back to a direct scan; schema v2 rebuilds the FTS table locally.
      Verified: `372 passed, 15 skipped` offline; live index passed; two independent verifies.
      The first verify found a blocker: `orden="relevancia"` took 36–111 s on 20k articles,
      because bm25 was re-run per row. Fixed with a per-query temp score table: 28–185 ms on
      20k (1.2x of fecha). Other results: 0 mismatches vs `text.matches` over 240 random
      queries; exact pagination; the v1→v2 migration is atomic (survives a crash) and takes
      3.5 s at 20k; 0 raw exceptions in fuzzing; no concurrency cross-talk. Size ≈ 1.7x of
      v1. `normalize` now drops control/format characters (NUL made trigram produce false
      hits). Commit `0ac7ff3`; CI green (Linux + Windows, trigram available); native review
      `review-0a05f01dafa3410f` approved and acknowledged, with one advisory WARNING at
      index.py:690 (the v1→v2 FTS rebuild) to re-check in task 6.
- [x] 6. MCP server: every tool wired, uniform contract, `estado_servidor`, tests.
      17 Spanish tools on MCP v2 `MCPServer` with routing `instructions`; `ok`/`error`/
      `error_code` contract (11 BomeError subclasses mapped, `error_interno` without traceback).
      Site requests from tools are serialised through one locked client (MCP v2 runs sync
      tools in worker threads); the sync owns a second polite client. Nothing crawls or
      opens the index at start. Advisories fixed: lazy home + absolute env overrides
      (paths.py); v1→v2 migration normalises once, busy_timeout 15 s (index.py).
      Verified: `420 passed, 16 skipped` offline; all 16 live tests passed; independent verify
      PASS with a real stdio E2E through the `bome-navaja-mcp` script (17 tools, all stdout
      lines valid JSON-RPC, sync start/poll/search, closing stdin mid-sync exits in 0.5 s with
      a consistent index). Its medium finding (httpx INFO logs on stderr) and 3 lows fixed.
      Follow-ups (low): shutdown waits 10 s but a bulletin with many hidden articles can take
      longer (stale lease for up to 180 s); no size cap on `ver_bome`/`listar_bomes` payloads
      (~120 KB worst case); model-facing keys mix Spanish (search/index) and English (task-2
      models: `number`, `date`, `sections`); `buscar_en_indice` on an empty index returns
      before validating its arguments (review advisory, server.py:637).
      Commits `e15698e` + `f319653` (Windows CI fix: host-judged absolute paths, UTF-8 stdio
      test). Native reviews (high tier, 4 lenses) `review-7c4fcd6674915bb8` and
      `review-ca1e368edc1a0aa8` approved and acknowledged. CI green on Linux and Windows.
- [x] 7. `.mcpb` bundle: manifest template, launcher, build scripts (Linux + Windows), parity test.
      Mirrors navaja: manifest v0.4 (`server.type` uv, 17 tools matched against the server by
      test), launcher `bome_navaja_mcpb.py`, `scripts/build_mcpb.py` (safe staging) with
      `build_mcpb.sh` and `build_mcpb.ps1`. Optional `user_config.directorio_datos` maps to
      `BOME_NAVAJA_DATA_DIR`; its empty default is required (without it the literal
      `${user_config...}` would reach the server) and is enforced by a test.
      Verified: `437 passed, 16 skipped`; bundle 68.6 KB, 17 files (no tests/fixtures/caches);
      `mcpb validate` passes; the unpacked bundle launched like Claude Desktop answers
      initialize, lists 17 tools and runs `estado_servidor` with JSON-RPC-only stdout
      (3.7 s first run with a warm uv cache, 1.5 s warm). CI builds the bundle on Linux
      (artifact) and runs the PowerShell wrapper on Windows.
      Not verified: an install inside Claude Desktop itself; the bundle is unsigned.
      Commit `dc62e89`; CI green on all 4 jobs (the PowerShell wrapper ran on Windows);
      native review (high tier) `review-906b6781f322161e` approved and acknowledged.
      Follow-up (advisory, build_mcpb.py:75): a staging target INSIDE a staged source dir
      (e.g. `--out src/x`) is not refused.
- [x] 8. README (Spanish) with install paths (Claude Desktop .mcpb, Claude Code, Linux, Windows)
      and a live smoke run.
      README `60ceaa9` (353 lines, tuteo, navaja structure) with `tests/test_readme.py` (tools,
      env vars, repo paths and anchors kept in sync). Live smoke 2026-09-24, verdict READY:
      `uvx --from git+...@feat/bome-mcp bome-navaja-mcp` installed from the private repo in
      7.2 s cold; a stdio session ran all 17 tools (list, tree, article cursor over 4 pages,
      bulletin PDF 37 pages, PDF cache hit, README example → 12 bulletins / 12 articles,
      contenido search 141, empty-index aviso, July-2025 sync 12/12 in ~9 s, index hits incl.
      hidden BOME-A-2025-745 in both modes, error codes); every stdout line JSON-RPC, empty
      stderr, clean exit; ~80 GETs. Its 3 low findings (docstring keys, OR `total_bomes`,
      clamped `max_caracteres`) fixed in the README/docstrings.
      Not verified: an install inside Claude Desktop; unpinned install from main (after merge).

## Evidence

- Recon: /tmp/bome-recon (raw samples), Engram `bome-navaja/site-recon`.
- `be43b03` chore: initialize repository (main); `edeee29` docs(odd) (feat/bome-mcp).
- `bed0423` feat: add BOME client, CVE model and site parsers (task 2).
- `0e20f5f` feat: add BOME search and article drill-down (task 3).
- `b7f6e97` feat: add PDF cache and paginated document reading (task 4).
- `aec4609` feat: add local SQLite FTS5 index of article sumarios (task 5).
- `0ac7ff3` feat: switch the sumario index to trigrams with word-start mode (task 5b).
- `e15698e` feat: add the bome-navaja MCP server; `f319653` fix: Windows portability (task 6).
- `dc62e89` feat: add the Claude Desktop .mcpb bundle (task 7).
- `60ceaa9` docs: write the Spanish README (task 8).
- Checkpoint 2026-09-23 (user decision): private repo https://github.com/jgarcialaneitor/bome-navaja
  (HTTPS remote, like navaja; no SSH key on this host), `main` and `feat/bome-mcp` pushed,
  draft PR #1 that grows with the feature. CI green on Linux and Windows.
- Incident 2026-09-23: the first two commits (`e696d7e`, `7886b43`) landed in a pre-existing
  empty repository at `/home/ubuntu/.git` (created 2026-09-11) because `bome-navaja` had no
  `.git` of its own. Fixed by `git init -b main` inside the project and recreating both commits.
  User decided to delete `/home/ubuntu/.git` entirely; verified first that no project had
  history there (no commits before ours, no linked worktrees), then removed it.
