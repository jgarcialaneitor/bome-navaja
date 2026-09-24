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

## Delivery

- Pending (user decisions): push, PR, merge, build and verify the `.mcpb`, release v0.0.5.
- Not verified: the settings screen inside Claude Desktop (decimals, `min`, the decimal comma).
