# Feature: polite-sync

## Intent

Make the index sync gentle enough that the BOME site's firewall does not block it, and make it
stop by itself when the site starts refusing requests. Incident 2026-09-24: a full sync
(~1,900 bulletins since 2014, 0.5 s between requests, several requests per bulletin) ran long
enough to trip the site's WAF, which then blocked the user's IP. The crawl kept going while
blocked, because a 403/429/503 was recorded as one more bulletin `error`.

## Scope

In scope (user decision 2026-09-24: "adelante con los cambios", points 1-4):
1. Circuit breaker: detect blocking answers (403, 429, 503, and a run of connection failures),
   back off (honouring `Retry-After`) and stop the sync with a clear state, without marking the
   blocked bulletins as `error`.
2. Slower pace for the sync only (~2 s + random jitter between requests); interactive tools
   keep their current pace.
3. Per-run cap on bulletins, so the history is filled over several runs.
4. Environment overrides `BOME_NAVAJA_SYNC_DELAY` and `BOME_NAVAJA_SYNC_MAX_BOLETINES`, with
   the prudent values as defaults.

Out of scope:
- Changing the User-Agent (point 5): pending user decision.
- Push, PR, merge: user decisions.

## Design

- `BomeBlockedError(BomeHTTPError)` for 403/429/503, carrying `retry_after` seconds when the
  site sends `Retry-After`. `search.py` re-raises it instead of recording it as a per-article
  or per-bulletin error, so drill-down stops too.
- The sync retries the same bulletin after a cancellable back-off; after the retries run out
  (or several bulletins in a row fail without any HTTP answer) it ends as `bloqueado`.
- Sync client: `polite_delay` 2.0 s plus uniform jitter up to 1.0 s; default cap of 250
  bulletins per run, reported in the state so the model knows more runs are needed.

## Tasks

- [x] 1. Block detection: `BomeBlockedError` in client/models, propagated by search.
      Verified: `461 passed, 16 skipped`. Commit `2c07e9a`; native review
      `review-1207b1da90397be0` approved (4 lenses) and acknowledged.
- [x] 2. Sync circuit breaker: cancellable back-off, retry, `bloqueado` final state, no
      `error` rows for blocked bulletins.
      Verified: `471 passed, 16 skipped`. Commit `cb5f1f6`; native review
      `review-6e461d0051ad38e0` approved (reliability) and acknowledged.
- [x] 3. Sync pace and budget: sync-only delay + jitter, per-run bulletin cap, env overrides
      wired through the server.
- [x] 4. Docs: README (timings, courtesy, variables, new state), task evidence.
      Tasks 3 and 4 share one commit: the README test requires every env var the code
      reads to be documented, so the docs travel with the behaviour.
      Verified: `510 passed, 16 skipped`.
