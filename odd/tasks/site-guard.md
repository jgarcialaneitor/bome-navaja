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

## Tasks

- [x] 1. `PX` CVE kind. Verified: `515 passed, 16 skipped`.
- [x] 2. Index v3: `http_status`, failure count, `roto` state, plan skips `roto`, migration.
      Verified: `536 passed, 16 skipped`. Migrated v2 errors with a stored 5xx become `roto` at once.
- [x] 3. Site guard: persisted error budget + cooldown, wired into client and sync.
      Verified: `593 passed, 16 skipped`. The guard replaces the v0.0.2 exponential block
      retries: a site block or 2 transport failures close the site for 75 min (or Retry-After).
- [ ] 4. Server tools, README, version 0.0.3.
