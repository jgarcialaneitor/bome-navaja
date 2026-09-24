# Feature: old-portal-index (v0.0.4)

## Intent

Index the article sumarios of the old BOME portal on melilla.es in the local index, so
`buscar_en_indice` answers with AND / OR / NOT across 1991–2017 as well as bomemelilla.es
(2018 onwards). The old portal's own search is literal, unpaginated and single-phrase, and the
portal is a frozen legacy site that may disappear.

## Decisions

- 2026-09-24 (user): bulk-index the old portal ignoring its `robots.txt` (which disallows
  `ficha_bome.jsp` and `/mandar.php`). Mitigation kept: slow pace, per-run cap, the melilla.es
  site guard (3 errors per 10 min, 75 min cooldown), manual start only.
- Scope by default: catalog bulletins dated 1991-01-01..2017-12-31 not already indexed from
  bomemelilla.es. Evidence (one ficha each, 2026-09-24): 1986 ficha has no articles (only the
  whole-bulletin PDF); 1991 (30 articles) and 1999 (73) have full sumarios. Earlier bulletins
  only with an explicit `desde`. 2018 onwards stays with bomemelilla.es.
- Only ficha pages are fetched (one request per bulletin, ~100–140 KB); no PDFs.

## Tasks

- [x] 1. Index v4: origin and old-portal identity (dboid, keys for repeated pre-2014 ids,
      synthetic article keys), migration, search results carry `origen`. Across origins the
      better outcome wins (indexado > sin_sumarios > failure), bomemelilla.es on a tie.
      Verified: `748 passed, 18 skipped`. Commits `cffea83` (fixtures) + `421c00b`; native review
      `review-daa99a25c58fcdbc` approved and acknowledged.
- [x] 2. Old-portal sync: plan, pace, cap, own guard, shared lease, `roto`/`sin_sumarios`.
      Verified: `790 passed, 18 skipped`. Shared run loop extracted to `SincronizadorBase`
      (sync.py); `SincronizadorPortalAntiguo` in sync_antiguo.py; lease and last-sync summary
      record the origin. Commit `02a1082`; native review `review-957618e4a1569f4b` approved and
      acknowledged. Known limit: a bomemelilla.es `error`/`roto` key (only if that sync ran before
      2018) is re-planned by every old-portal run while the old ficha keeps failing (bounded by cap,
      guard and 5xx pause).
- [x] 3. Tools, README, manifest, version 0.0.4. Verified: `806 passed, 18 skipped`.
      `sincronizar_indice(origen="melilla.es")`; 19 tools unchanged in number. Commit `409be1f`;
      native review `review-89159e3cdf044087` approved (4 lenses) and acknowledged.
