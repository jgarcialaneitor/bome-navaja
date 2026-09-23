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

## Constraints

- Tool names and parameters in Spanish; every tool returns a dict with `ok` and, on
  failure, `error` + `error_code`. stdout reserved for JSON-RPC (stdio).
- Tests run offline against saved fixtures (`httpx.MockTransport`); live tests gated by
  `BOME_NAVAJA_LIVE=1`. CI never hits the real site.
- Polite client: real browser UA, timeouts, small delay between bulk requests
  (index sync, drill-down).
- Python >= 3.12, deps: httpx, beautifulsoup4, lxml, mcp>=2,<3, pypdf.

## Tasks

- [ ] 1. Scaffold: pyproject (package `bome_navaja`, scripts `bome-navaja-mcp`), MIT license,
      pytest config, CI (Linux gating + Windows), README skeleton, fixtures.
- [ ] 2. Client + models + parsers: calendar, bulletin page tree, article page, sumario,
      section APIs, CVE resolution, search-results parsing and pagination.
- [ ] 3. Search: `buscar_bomes` (quick + advanced params) and `buscar_articulos` (drill-down
      that returns the matching articles, bounded).
- [ ] 4. Documents: PDF download with safe cross-platform paths; paginated PDF/HTML text reading.
- [ ] 5. Local sumario index: SQLite FTS5, incremental background sync with progress,
      index search tool, index status.
- [ ] 6. MCP server: every tool wired, uniform contract, `estado_servidor`, tests.
- [ ] 7. `.mcpb` bundle: manifest template, launcher, build scripts (Linux + Windows), parity test.
- [ ] 8. README (Spanish) with install paths (Claude Desktop .mcpb, Claude Code, Linux, Windows)
      and a live smoke run.

## Evidence

- Recon: /tmp/bome-recon (raw samples), Engram `bome-navaja/site-recon`.
- `e696d7e` chore: initialize repository (main).
