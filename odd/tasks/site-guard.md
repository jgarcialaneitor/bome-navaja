# Feature: site-guard (v0.0.3)

## Intent

Stop triggering the bomemelilla.es firewall. Field data (user, 2026-09-24, copy of the index):
the site bans the client IP right after the **fifth HTTP 500** (fail2ban-like, maxretry 5,
ban seen lasting ~1 h), regardless of the request rate. Three bans, each right after the fifth
500; the ban shows up as timeouts (dropped packets), never as 403. The 500s are
deterministic broken bulletin pages (`BOME-B-2018-5522` gave 500 in two runs), and new ones
keep appearing in never-requested pages (2017). v0.0.2 fed the ban itself: every run retried
every `error` bulletin, including the known-broken ones.

## Scope

1. `PX` CVE kind (extraordinary page, e.g. `BOME-PX-2021-362`): today a parse error.
2. Index schema v3: HTTP status and failure count per bulletin; a bulletin page that answers
   5xx twice becomes `roto` and normal syncs skip it (explicit `reintentar_rotos` retries it).
   Timeouts never make a page `roto` (they are the ban, not the page) and keep being retried.
   Migration fills the status from the stored v2 message (`HTTP 500 for ...`).
3. Site guard shared by every tool and the sync, persisted in the data folder so it survives
   restarts and is seen by other processes:
   - error budget: at most 3 HTTP error answers (>= 400) in a sliding 10-minute window; the sync
     waits for the oldest to leave the window instead of risking the fifth; after each error the
     sync also pauses 30-60 s;
   - cooldown: a block signal (403/429/503, or 2 transport failures in a row) closes the site for
     `Retry-After` or 75 min; while closed, every tool answers `sitio_bloqueando` with
     `reintentar_tras_segundos` without touching the network, and a new sync ends `bloqueado`
     at once.
4. Server, docs and version 0.0.3.

Out of scope: push, PR, merge, release (user decisions); User-Agent and `.mcpb` settings (user
decision 2026-09-24: leave as they are).

## Scope extension (user decision 2026-09-24: "todos los cambios nuevos a la 0.0.3")

Field + recon evidence (Engram `bome-navaja/old-portal-recon`): bomemelilla.es is an incomplete
migration for 2014-2017 (141 bulletins missing; the HTTP 500s concentrate there). The old portal
https://www.melilla.es/melillaPortal/contenedor.jsp?seccion=bome.jsp (city hosting 195.57.65.8,
https only: port 80 closed, its `http://` links must be rewritten) has the frozen catalog
1985-01-03..2021-03-12 (3,261 bulletins, numbering identical to bomemelilla CVEs), a text search
returning article-level sumarios with per-page PDF links, a bulletin "ficha" with the full
consejería tree and a whole-bulletin PDF, and PDFs served by `mandar.php`.

5. The sync starts at 2018-01-01 by default (earlier only with an explicit `desde`).
6. Old-portal client: catalog (cached locally, it is frozen), search, ficha, PDFs; own site guard.
7. Tools for the old portal, README, manifest.

robots.txt of melilla.es disallows `ficha_bome.jsp` and `/mandar.php`. User decision 2026-09-24:
ignore it for **on-demand** requests the user makes (ficha, PDF). Agent line, accepted by
proceeding: no bulk crawling of those paths (no index sync from the old portal) in this release.

## Tasks

- [x] 1. `PX` CVE kind. Verified: `515 passed, 16 skipped`. Commit `1294afc`; native review
      `review-7eb466b03d0875b0` approved and acknowledged.
- [x] 2. Index v3: `http_status`, failure count, `roto` state, plan skips `roto`, migration.
      Verified: `536 passed, 16 skipped`. Migrated v2 errors with a stored 5xx become `roto` at once.
      Commit `aaf93cc`; native review `review-f3bb95755a5b510b` approved (4 lenses) and acknowledged.
      Follow-up (advisory, index.py:877): a migrated `roto` (fallos 1) retried with
      `reintentar_rotos` that then times out or answers 4xx drops back to `error`.
- [x] 3. Site guard: persisted error budget + cooldown, wired into client and sync.
      Verified: `593 passed, 16 skipped`. The guard replaces the v0.0.2 exponential block
      retries: a site block or 2 transport failures close the site for 75 min (or Retry-After).
      Commit `67a8e53`; native review `review-0daa5029660912c5` approved (4 lenses) and
      acknowledged. Follow-ups (advisory): guard.py:285 state-file parsing, client.py:297.
      Known limits: the state file is read-merge-written without a cross-process lock, so two
      processes erring at the same instant can go 1-2 over the budget (still under 5).
- [x] 4. Server tools, README, version 0.0.3. Verified: `593 passed, 16 skipped`. Commit
      `3a06686`; native review `review-f3ca6064c6b4d09e` approved and acknowledged.
- [x] 5. Sync default start 2018-01-01. Verified: `599 passed, 16 skipped`. Pre-2018 gaps are
      reported apart as `pendientes_anteriores_2018`. Commit `51405e4`; native review
      `review-331770780bc8445c` approved (4 lenses) and acknowledged.
- [x] 6. Old-portal client + parsers + catalog cache + own guard. Verified: `680 passed, 18 skipped`
      (2 live tests not run). Full raw catalog: 3,260 unique bulletins 1985-01-03..2021-03-12,
      1,031 from 2014; 24 pre-2014 identifiers repeat (lookup by CVE returns a list).
      Found: 14 bulletins with the same number but a different date on each site
      (e.g. BOME-B-2015-5230: 2015-12-17 on bomemelilla.es vs 2015-05-01 on melilla.es).
      Commits `4ca8c62` (fixtures; unreviewed: over the reviewer context budget, captured data only)
      and `3094160` (native review `review-95e41b4b5d45c8c6` approved and acknowledged; advisory:
      antiguo.py:560-610 cached ficha validation).
- [x] 7. Old-portal tools, README, manifest. Verified: `723 passed, 18 skipped`. 19 tools:
      `buscar_bome_antiguo`, `ver_bome_antiguo`; `listar_bomes` merges both catalogs with `origen`;
      `leer_pdf`/`descargar_pdf` accept a validated old-portal `url` (cached as `melilla-<a>-<b>-<name>.pdf`). Commit `a5b6184`; native review
      `review-d5b56c436dfd0bfe` approved (4 lenses) and acknowledged.

## Delivery

- 2026-09-24 (user decision): [PR #3](https://github.com/jgarcialaneitor/bome-navaja/pull/3), CI green
  on all 4 jobs, merged into `main` with a merge commit (`7fb4b34`). Suite on `main`:
  `723 passed, 18 skipped`.
- Bundle built from `main`: `bome-navaja-0.0.3.mcpb` (115,550 bytes, sha256 `21f108b2…f8ab`);
  independent verify PASS (19 files byte-identical to `7fb4b34`, 19 tools, version 0.0.3, both site
  guards idle, no network). Release
  [v0.0.3](https://github.com/jgarcialaneitor/bome-navaja/releases/tag/v0.0.3) tagged on `7fb4b34`;
  downloaded asset sha256 verified identical.
- Not verified: install inside Claude Desktop; any tool against the real sites (live tests skipped).
- Follow-up noted by the verifier: `uv.lock` is not bundled, so dependency versions resolve fresh
  on each install.
