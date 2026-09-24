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
- [x] 4. Bug (user report 2026-09-24): `leer_articulo("BOME-AX-2019-103")` returned the ORDINARY
      article 103 (a grant call instead of the decree). Root cause verified live: the site's resolver
      `/buscar-cve` drops the X for AX and PX — `BOME-AX-2019-103` → `/bome/BOME-B-2019-5625/articulo/103`,
      `BOME-PX-2021-362` → `/bome/BOME-B-2021-5839/articulo/170#pagina-362`; BX and SX resolve
      correctly. Fix: never trust the resolver for extraordinary article/page CVEs, resolve AX
      ourselves, and verify every fetched article's own CVE.
      Verified: `831 passed, 18 skipped`; live 2026-09-24: `leer_articulo("BOME-AX-2019-103")` →
      BOME-BX-2019-28 art. 103, Decreto nº 293 (personal eventual), ~4 s; `resolver_cve` AX → BX URL,
      PX → PDF URL with a note. Commit `87d1129`; native review `review-d8cd303051b078b4` approved
      (4 lenses) and acknowledged. Follow-up (advisory, documents.py:748): an extraordinary bulletin
      whose page lists no articles is skipped by the search, so an article hidden in it is not found.
- [ ] 5. Follow-up of task 4 (user request): find an AX article that sits in an extraordinary
      bulletin whose page lists no articles. Today the binary search only tries the two listed
      neighbours when the number falls between their ranges. Live 2026-09-24: an article number
      under the wrong BX bulletin answers 404 (not 500), which the site guard still counts.
