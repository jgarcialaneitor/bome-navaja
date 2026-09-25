# Feature: sync-settings (v0.0.5)

## Intent

Let the user tune, from the Claude Desktop settings of the `.mcpb` bundle, every setting that
governs how hard bome-navaja hits the sites (sync pace, per-run cap, interactive pace, site guard,
timeouts). Each setting explains what it does, the consequence of making it more aggressive, and
the recommended value to avoid a ban; the server warns when a value is riskier than recommended.
Also ship `uv.lock` inside the bundle so installs resolve the tested dependency versions.

## Decisions

- 2026-09-24 (user): expose every reasonable setting ("todos los que sean posibles"), always
  explaining the consequences and asking for responsible use.
- 2026-09-24 (user): one shared set of values for bomemelilla.es and the old melilla.es portal.
- 2026-09-24 (user): no hard limits. Any physically valid value is accepted (the current 1 s floor
  on the sync pause goes away); values riskier than recommended produce a warning instead.
  Nonsense values (negative, non-numeric, zero where it cannot work) fall back to the default with
  a warning, as today.
- 2026-09-24 (user): ship `uv.lock` in the bundle in the next release (v0.0.5).
- Baseline on `main` (`a6ba9d8`): `839 passed, 18 skipped`.

## Settings

| Setting | Default | Riskier when |
|---|---|---|
| Sync pause between requests | 2 s | below 2 s |
| Sync random jitter on top | 1 s | below 1 s |
| Max bulletins per sync run | 250 | above 250 |
| Interactive pause between requests (bomemelilla.es tools) | 0.5 s | below 0.5 s |
| Site errors tolerated before stopping | 3 | above 3 (5 = observed ban) |
| Window in which errors are counted | 10 min | below 10 min |
| Cooldown after a block signal | 75 min | below 75 min (ban lasts ~1 h) |
| Pause after a site error (min; max = 2 × min) | 30 s | below 30 s |
| Request timeout | 30 s | below 30 s (slow site mistaken for a block) |

Out of scope: internal robustness knobs unrelated to the site (storage failures, back-off slice,
lease staleness, Retry-After cap); the old portal's interactive pace (stays 1 s + 0.5 s).

## Tasks

- [x] 1. Settings module: read every setting from `BOME_NAVAJA_*` env vars (blank = unset,
      tolerant numbers such as `250.0`), no hard floors, risk warnings; wire into sync, both site
      guards, clients and error pause; `estado_servidor` reports values and warnings; tests.
      New module `ajustes.py`; new vars `BOME_NAVAJA_SYNC_JITTER`, `_QUERY_DELAY`,
      `_GUARD_MAX_ERRORS`, `_GUARD_WINDOW_MINUTES`, `_GUARD_COOLDOWN_MINUTES`,
      `_ERROR_PAUSE_SECONDS`, `_TIMEOUT`. The AX lookup cap follows the guard budget
      (`max(1, min(2, max_errores - 1))`). Verified: `988 passed, 18 skipped`. Commit `aafbc00`;
      native review `review-d190a663d5477e7a` approved (4 lenses) and acknowledged. Follow-up
      (advisory, ajustes.py:85-92): huge finite minute values overflow to infinity when turned
      into seconds.
- [x] 2. Bundle: one numeric `user_config` field per setting in the manifest (title, description
      with effect, consequence and recommendation), mapped to its env var; stage `uv.lock`; tests.
      Nine `number` fields, no `max`, `min` only for physical validity; responsible-use line in
      `long_description`. `mcpb validate` passes; mcpb renders defaults as `"2"`, `"0.5"`, `"250"`.
      `uv.lock` staged; no `--frozen` (a stale lock would then install silently). Verified:
      `992 passed, 18 skipped`. Commit `5f12f41`; native review `review-c7b65e4e98b89bc6` approved
      and acknowledged. Not verified: how the Claude Desktop settings UI handles decimals and `min`.
- [x] 3. README section on the settings and responsible use; version 0.0.5 (with the re-locked
      `uv.lock`); hardening: accept a decimal comma (`0,5`) and reject values that overflow to
      infinity once converted (advisory from task 1); tests.
      README `### Ajustes de ritmo y protección` with a responsible-use warning and one row per
      setting; fixed numbers elsewhere now say they are defaults. Parent decision: a time above one
      year is technically invalid (it crashed sockets, the guard deadline and `time.sleep`), so it
      falls back to the default; this is validity, not a safety limit. Verified:
      `1068 passed, 18 skipped`; `uv lock --check` OK. Commit `f48c07b`; native review
      `review-6524c4135285f9b7` approved and acknowledged.

## Post-review fix: the time ceiling was not portable

- PR #5 (`feat/sync-settings` → `main`, pushed 2026-09-25) failed CI job `test-windows` while `test`,
  `bundle` and `bundle-windows` were green:
  `tests/test_ajustes.py::test_the_longest_valid_times_still_work_at_runtime` raised
  `OverflowError: timeout doesn't fit into C timeval` at `sock.settimeout(31536000)`.
- Cause: `MAX_TIEMPO_SEGUNDOS` was one year. Linux accepts a timeout that large; Windows caps
  socket timeouts near `INT_MAX` milliseconds (about 24.8 days), so the "largest valid time"
  rule from task 3 was not portable, not wrong about validity.
- User decision 2026-09-25: lower the ceiling to 24 h (`86400` s), about 25 times below that
  limit and still far above any real value (the actual cooldown is 75 min). Still validity, not
  safety: a larger value falls back to the default with the same warning, now worded "más de un
  día". `src/bome_navaja/ajustes.py`, the boundary fixtures in `tests/test_ajustes.py`
  (`86400`/`86401`, `1440`/`1441`, `43200`) and the README sentence updated. Verified:
  `1068 passed, 18 skipped`; the runtime test still exercises `socket.settimeout`, which is what
  caught this. Commit `eb8d8b5`.
- Native review could not start for this candidate: five `review.start` attempts returned
  `consent-binding-stale` (`consent-binding-expired` at issue time) with four distinct bindings,
  `native_invocation_attempted: false` and `lineage_created: false` — a host-side consent-relay
  defect, nothing mutated. The user was told and said to continue, so this candidate is treated
  as left unreviewed; Windows CI on PR #5 is its verification.
- Review, second attempt (same day, after the fix was committed): the host resolved consent on its
  own UI and the review started, but the candidate was then the whole branch against `main`
  (20 files, 1750 lines, 156 KB materialized prompt, tier `high`) and the relay aborted a reviewer
  at 1,034,203 ms against a 1,034,180 ms bound (`pi-host-relay-timeout`, 0 reviewers prepared,
  0 submitted, nothing mutated); lineage `review-4ea50fde085826c1` stayed in `reviewing` and the
  provider stated a relaunch of the same slot reaches the same wall.
- User decision: narrow the candidate instead of raising the host relay timeout. New transaction
  `review-711515129835e773` over `046b710..0687561` (4 files, 84 lines, tier `medium`, single lens
  `review-reliability`): **approved and acknowledged**, authority burned
  (`gentle-ai.review-acknowledged/v1`). The other 18 files of the branch keep their per-commit
  approvals (`review-d190a663d5477e7a`, `review-c7b65e4e98b89bc6`, `review-6524c4135285f9b7`).
  `review-4ea50fde085826c1` remains an unfinished lineage with no review outcome.

## Delivery

- Done: branch pushed and PR #5 opened; CI `test`, `bundle`, `bundle-windows` green.
- Merged: PR #5 merged into `main` with a merge commit, `1338067cac1df3402352ac903e904a26453aeae1`
  (same convention as PR #3 and PR #4; branch not deleted). CI on `main`: 4/4 success. Local on
  `main`: `1068 passed, 18 skipped`.
- Released: **v0.0.5**, marked Latest, tagged on `1338067`. Bundle `bome-navaja-0.0.5.mcpb`
  (196,801 bytes, sha256 `5fbf2ff86857618ac14c4d55bdab1f3e6db2daf078b1b7b6f7e56f6a8289d379`), built from
  `main` and verified by an independent pass before publishing: 19 tools, version `0.0.5`, every
  shipped file byte-identical to the repository copy, `uv.lock` byte-identical to the repo's, and
  `mcpb validate` passing. The asset downloaded back from the release hashes identically to the
  local build.
- Correction to the wording above: the manifest declares **10** `user_config` entries — the nine
  new numeric settings plus the pre-existing `directorio_datos` (`directory`, `BOME_NAVAJA_DATA_DIR`,
  already present in v0.0.4). "Nine settings" always meant the nine numeric site-facing ones, and
  the README sentence is accurate as written; the release notes say "nueve campos nuevos" for that
  reason.
- Not verified: the settings screen inside Claude Desktop (decimals, `min`, the decimal comma) and
  how `uv run` cold-starts in a real install; also macOS/Windows behaviour, since verification ran
  on Linux only.
- Open, low priority: no `max` on any numeric field, so a user can still choose an aggressive value
  (mitigated by the warning in `estado_servidor` and the in-code site guard); the merged
  `feat/sync-settings` branch still exists locally and on origin; and the oversized-range lineage
  `review-4ea50fde085826c1` remains in `reviewing` with no outcome.
