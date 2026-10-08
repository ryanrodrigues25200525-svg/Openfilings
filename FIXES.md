# Fixes

A working checklist of known defects and gaps, each with the fix that closes
it. Items with a `#n` link have a GitHub issue carrying the full reproduction;
the rest were found while auditing this file into existence and are recorded
here first.

Nothing below is speculative. Every entry was reproduced against the live
source, observed in the code, or measured — this is not a wishlist.

Ordering is by what blocks real use, not by effort. P1–P2 change what the
library can answer; P3–P4 change whether you can trust and find it; P5–P10 are
the standing engineering backlog — packaging, performance, tests,
observability, hardening — that keeps the rest from rotting.

Keep it a living document: when a finding turns out to be correct behaviour,
move it to **Not defects** with the reason rather than deleting it, so the same
ground is not re-investigated later.

---

## P1 — Research blockers

The extraction layer is sound, but the normalized model stops one layer short
of the metrics valuation work actually uses. Today's 31 line items cover the
primary statements' totals and little else.

- [ ] **No depreciation & amortisation → EBITDA is not computable.**
  *Fix:* add a `depreciation_amortisation` entry to `LINE_ITEMS`
  (`DepreciationAndAmortisationExpense`,
  `DepreciationAmortisationAndImpairmentLoss…`), mapped on the cash-flow
  statement where it is most reliably tagged as the add-back.

- [ ] **No debt or borrowings → net debt, EV and leverage are not computable.**
  *Fix:* add `short_term_debt` (`ShorttermBorrowings`,
  `CurrentPortionOfLongtermBorrowings`) and `long_term_debt`
  (`LongtermBorrowings`), then derive `total_debt` with the existing
  `_sum_line_items` machinery so it carries `provenance: derived` honestly.

- [ ] **No non-controlling interests → the EV bridge is incomplete.**
  *Fix:* add `noncontrolling_interests` aliasing `NoncontrollingInterests`.
  Already present in source filings — it is how the equity/NCI defect was
  diagnosed — so this is purely a mapping addition.

- [ ] **No share counts → no market cap, no per-share metric, no dilution.**
  *Fix:* add `shares_outstanding` and `weighted_average_shares_diluted`.
  **Handle separately from the items above**: a share count is not money, and
  a currency scale multiplier applied to it reproduces the India `crore` bug
  exactly. `pdf_statements` already special-cases EPS with `item_scale = 1`;
  share counts need the same treatment plus a non-currency unit.

- [ ] **No trade payables or lease liabilities.**
  *Fix:* add `trade_payables` (`TradeAndOtherCurrentPayables`) and
  `lease_liabilities` (current + non-current). Unlocks working-capital and
  post-IFRS-16 adjusted leverage.

> **Constraint for all of the above.** Do **not** map these new sub-items into
> `validation.py`. That module documents the reason: finvariant treats a
> partially-supplied section as *not footing* rather than skipping it, so
> adding a few components without every sibling produces false validation
> failures. Extract and expose them; leave the identity mapping alone.

- [ ] **Cross-market comparison has no FX.** A Turkish issuer in TRY cannot be
  compared with a Finnish one in EUR.
  *Fix:* **deliberately do not build this.** Correct conversion needs
  period-end rates for the balance sheet, period-average for the P&L, and a
  rate source with history — a subsystem in its own right, and a second-rate
  version inside a filings library silently produces wrong comps. Guarantee
  `unit` is always populated and document the join to a dedicated rates
  source instead.

- [ ] **Segment and geography revenue is captured but not reachable.**
  *Fix:* this is a surfacing problem, not extraction — `dimensions` are
  already preserved on every `FinancialValue`, so the XBRL axes are in the
  data now. Add an `include_dimensions` flag to `filing_financials` and a
  group-by-axis helper. Expose raw axis/member pairs first; normalizing
  member names across taxonomies is hard and must not block access.

- [ ] **No screening — "every EU issuer under 8× EBITDA" is impossible.**
  *Fix:* do not build a query engine. `historical_backfill` already persists
  facts to SQLite; add (a) a bounded bulk backfill across a market's issuer
  list and (b) Parquet/CSV export (the `dataframe` extra already exists).
  Screening then happens in DuckDB or pandas.

---

## P2 — Correctness

- [ ] **[#6](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/6) Unilever's Form 20-F extracts a wrong currency (PEN for a UK issuer) and a ~1000× scale.**
  *Fix:* treat the currency misdetection as its own bug first — a filing
  retrieved from the FCA's UK storage mechanism should never resolve to PEN,
  and a source-plausibility check is cheap and independent of the table
  parsing. Do not rewrite the PDF heuristics wholesale to satisfy one
  document. Note the failure is already loud: `validation.ok` is `false`.

- [ ] **Partial failures are invisible on `source="all"`.** `_gather_available`
  returns whatever succeeded and discards every exception whenever at least
  one call worked. If 13 of 14 ESEF markets are down, the caller sees a short
  result list and no indication anything failed — which reads as "no such
  company" rather than "the data is incomplete." For research this is worse
  than an error.
  *Fix:* return the failures alongside the results and surface them in the
  MCP envelope (a `partial: true` plus the failing sources), so a caller can
  distinguish "not found" from "not checked."

- [x] **[#12](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/12) One LEI can produce two company IDs across jurisdictions.**
  *Fix:* decide deliberately between deduplicating search results by LEI
  (preferring the home country), making the LEI the company ID and moving
  country onto the filing, or documenting the duplication as intended. It is
  currently true by accident.
  *Fixed:* decided option 1 — search results collapse same-LEI records to one
  (deterministic survivor, `other_jurisdictions` listed on the record);
  per-jurisdiction IDs still resolve and list only their own filings.

- [x] **[#14](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/14) Some ESEF issuers return a years-old "latest" filing.**
  *Fix:* first establish which explanation applies — for one affected issuer,
  compare what filings.xbrl.org returns for the LEI *without* the country
  filter against what the adapter returns *with* it. That single comparison
  separates an upstream gap from a discovery bug, and only the second is ours.
  *Fixed:* it was a discovery bug — same-day filings broke ties on raw filing
  id. `_filing_sort_key` now prefers the newer reporting period before the id.

---

## P3 — Verification

- [x] **[#7](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/7) The Sweden and Singapore regression guards cannot fire.**
  *Fix:* pin reference facts in `benchmarks.py`, transcribed by hand from the
  published reports. That file's own rule applies — never populate it from
  extractor output.
  *Fixed:* Volvo FY2024 (ESEF) and Keppel FY2025 (SGX) benchmarks pinned
  from the published annual reports and verified against a live run.

- [ ] **[#8](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/8) Pinned benchmarks cover 2 issuers across 25 markets.**
  *Fix:* one reviewed issuer per distinct extraction path (CVM Open Data,
  SFC CUIF, NSE XBRL, KAP viewer, BMV, SMV, PDF-heuristic), prioritising
  paths where a scaling or concept-mapping error would still reconcile.

- [ ] **The 8 "extracted but unverifiable" markets prove nothing today.**
  Where a total is derived, the accounting identity is circular and correctly
  skipped.
  *Fix:* footing is **not** circular — `current + noncurrent liabilities`
  against the derived total uses independently tagged components, and
  finvariant already has the rule (`FOOT.bs.total_liabilities`, which is what
  fired on Unilever). Report the footing outcome instead of a bare
  `not_applicable`. Report rather than fail at first: legitimate presentation
  differences exist (Volvo books provisions separately), so observe the
  distribution before enforcing.

- [x] **[#9](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/9) The multi-issuer probe is not repeatable.**
  *Fix:* commit a bounded check — 2–3 reviewed issuers per market, none of
  them the smoke issuer — reporting verified/unverifiable/failed rather than
  pass/fail. Schedule monthly, not weekly. Keep it polite: these are free
  public endpoints.
  *Fixed:* `openfilings.probe` + `.github/workflows/live-probe.yml` (monthly).

- [ ] **[#11](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/11) South Korea has never run against a live DART key.**
  *Fix:* register a free key, resolve a KOSPI issuer, list filings, extract
  statements, check the identity. Then either drop the caveat or file what
  broke. Until then the caveat stays.

---

## P4 — Usability and operations

- [ ] **[#10](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/10) Brand and ticker names do not resolve (`PKO`, `Ford Otosan`).**
  *Fix:* two separable pieces. (1) `ranked_matches` requires the query to be
  a *substring* of a name field, so an extra token absent from the legal name
  kills an otherwise strong match — a token-subset or best-partial score
  fixes this class with no new data. (2) `PKO` appears nowhere in the legal
  name and needs an alias source per adapter. Do (1); treat (2) as
  per-source. Any change here needs near-miss tests, not just happy paths —
  loosening the matcher risks false positives across thousands of issuers.

- [x] **[#15](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/15) Cached facts and a running MCP server both survive a fix.**
  *Fix:* stamp cached financials with an extractor version and treat a
  mismatch as a cache miss. That makes corrections propagate on their own and
  subsumes the alternatives (a cache-invalidation command, changelog notes).
  *Fixed:* `FINANCIALS_EXTRACTOR_VERSION` stamps `filing_financials` rows;
  a mismatch (or NULL on pre-versioning rows) reads as a miss and the filing
  is re-extracted; `put_historical_facts` replaces per filing on backfill.

- [x] **The README documented 12 of 22 MCP tools.** Missing:
  `data_quality_report`, `financials_query`, `historical_facts_query`,
  `historical_backfill`, `companies_compare`, `company_research_brief`,
  `filings_diff`, `insider_dealings_list`, `watchlist_check`,
  `filing_sections`. `data_quality_report` is the tool an agent needs to know
  whether to trust a figure, and the fact-series tools are the ones research
  workflows depend on.
  *Fixed:* documented all 22, grouped by discovery/financials/ownership.
  Added a test comparing `server.py`'s `@mcp.tool()` names against the
  README's MCP tools section - the cheap version proposed here, not full
  doc generation - and verified it actually fails when a tool is removed
  from the docs, not just that it happens to pass today.

- [x] **[#13](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/13) Every Dependabot PR fails CI because `uv.lock` is not regenerated.**
  *Fix:* add a lockfile-refresh step so the PRs are testable, or restrict
  Dependabot to security updates and bump by hand with `uv lock --upgrade`.
  Do not drop `--locked` from CI — it is what makes the build reproducible.
  Separately, comment the deliberate `mcp<2` pin in `pyproject.toml` so it is
  not "helpfully" widened later.
  *Fixed:* `.github/workflows/dependabot-lock.yml` refreshes the lockfile.

---

## P5 — Packaging and distribution

The package cannot currently be installed by anyone who is not cloning the
repository, and ships less metadata than a PyPI page needs.

- [x] **No `py.typed` marker.** The public API is fully annotated, but with no
  marker downstream type checkers ignore all of it.
  *Fixed:* added `src/openfilings/py.typed`, confirmed present in the actual
  built wheel (not assumed) via `hatchling`'s default package inclusion.

- [x] **No `__version__` exported.** Callers cannot introspect the installed
  version, which also blocked the cache-stamping idea in
  [#15](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/15).
  *Fixed:* `__version__` resolved from installed package metadata, isolated
  in a leaf module (`openfilings/_version.py`) with no internal imports of
  its own, since adapters are imported transitively by the top-level
  package and importing `openfilings.__version__` directly from an adapter
  would risk a circular import. As a direct follow-on, ten adapters'
  hardcoded `User-Agent: openfilings/<version>` strings - which had already
  drifted (0.2 through 0.21 across different files) - now read the real
  version at request time.

- [x] **No PyPI metadata.** `pyproject.toml` had no `classifiers`, no
  `keywords`, no `[project.urls]`, and no license field, so a published page
  would be bare and unsearchable.
  *Fixed:* added classifiers (Python versions, licence, topic), keywords,
  and URLs for homepage, repository, issues and changelog.

- [x] **No release had been cut.** Version was pinned at `0.21.0` with a
  large `Unreleased` changelog and zero git tags.
  *Fixed:* cut `0.22.0` with a dated changelog section. A release workflow
  that builds and publishes on tag is still open - not attempted here since
  it needs a real decision on whether/where to publish (PyPI requires
  trusted-publisher or token setup this session cannot authorize).

---

## P6 — Robustness

- [x] **The SQLite cache has no schema version.** Tables were created with
  `CREATE TABLE IF NOT EXISTS` and there was no `user_version` or migration
  path, so a schema change would silently meet an old database.
  *Fixed:* `PRAGMA user_version`, checked on open. A pre-versioning database
  (SQLite's own default, `0`) is accepted and stamped forward as
  schema-compatible; a cache from a *newer* version than this build knows
  raises `ConfigurationError` instead of operating on data it may not
  understand. No migration engine was built, since no actual incompatible
  change exists yet to migrate - this is groundwork for the next one, not a
  framework built ahead of need. Pairs with the extractor-version
  stamp in [#15](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/15), which is now done (see P4).

- [x] **Retry backoff has no jitter.** `_common.py` backed off on a fixed
  `0.25 · 2^n` / `0.5 · 2^n` schedule. A `source="all"` search fans out to
  every adapter at once, so a shared upstream hiccup retried them all in
  lockstep.
  *Fixed:* proportional (+/-25%) jitter. A server's own `Retry-After` header
  is deliberately left un-jittered - it is an explicit instruction, not a
  schedule to randomize.

- [x] **OCR routing had no pre-extraction signal — evaluated `pdf-inspector`,
  scoped to what the evidence supports.** A wholesale engine swap was
  considered and rejected: three filings tested head-to-head (Unilever's
  20-F, DBS's annual report, Keppel's, chosen because each has a documented
  or suspected extraction problem) produced **identical figures** from
  `pdf-inspector` and the current `pymupdf4llm`, and the existing table
  parser (`xbrl/pdf_statements.py`) does not recognize pdf-inspector's
  markdown dialect at all — feeding its output in unmodified raises
  `FinancialsUnavailableError`. Tracing the Unilever bug ([#6](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/6))
  to its source confirmed the defect is in our own downstream
  currency-detection heuristic (it picks up "PEN" from an unrelated
  Peruvian-subsidiary footnote 300 pages later), not in extraction quality —
  a better engine would not have prevented it.
  *Shipped instead:* `extraction/pdf_detect.py`, an optional (`pdf-detect`
  extra) pre-extraction classification wired into `_extract_pdf`. Two uses,
  both provably non-regressive: (1) skip the native-extraction attempt when
  a PDF is confidently classified scanned/image-based, since
  `assess_markdown("")` already scores 0/unusable and routes to OCR
  regardless — this only removes a doomed-to-fail call, and is disabled
  whenever OCR itself is unavailable, so the best-effort fast path is never
  lost; (2) surface `has_encoding_issues` as a diagnostic warning only,
  deliberately not used to force OCR, since OCR has no page-subset support
  (`ocr_pdf_to_markdown` OCRs every page) and generally degrades table
  structure relative to correct native extraction, so forcing a
  whole-document OCR pass on a partial encoding fault could make a filing
  worse. Degrades to exactly today's behavior when the extra is not
  installed. 223 tests pass (4 new), and the full `uv sync --locked
  --all-extras --dev` / lint / test / build sequence was run locally exactly
  as CI runs it, with `uv.lock` regenerated for the new optional dependency.

---

## P7 — Performance

Measured, not guessed.

- [ ] **Issuer registries are re-downloaded on every process start.** Eight
  adapters (`asx`, `bmv`, `cvm`, `dart`, `kap`, `nse`, `sgx`, `smv`) hold the
  registry in `self._companies`, an instance attribute, so it survives exactly
  as long as the object. A second CLI invocation still costs a full round trip
  — measured at ~1.5s for a repeat `openfilings search --source asx`, which
  re-fetches the entire ASX directory CSV to answer a three-letter query. Peru
  is worse: its registry now spans two years, so that is four requests per
  process.
  *Fix:* persist registries in the `market_state` table that already exists,
  with a TTL per source. Cuts latency and, more importantly, stops hammering
  free regulator endpoints for data that changes daily at most.

- [ ] **Thirteen ESEF clients open thirteen connection pools to one host.**
  Every `EsefMarket` builds its own `EsefClient`, each constructing its own
  `httpx.AsyncClient`, and all thirteen talk to `filings.xbrl.org`. No
  connection reuse across markets, thirteen TLS handshakes where one pool
  would do.
  *Fix:* share a single injected `AsyncClient` across the ESEF market clients.
  The constructor already accepts one (`client=`), so this is wiring in
  `service.py`, not new machinery.

- [ ] **No conditional requests anywhere.** Nothing sends `If-None-Match` or
  `If-Modified-Since`, and no `ETag` is stored, so every registry fetch
  transfers the full body even when nothing changed.
  *Fix:* store the `ETag`/`Last-Modified` alongside the cached registry and
  revalidate. A `304` costs almost nothing and is the polite way to poll a
  public dataset.

- [ ] **`source="all"` fans out to every adapter with no concurrency cap.** One
  search dispatches to 20+ clients simultaneously; combined with jitter-free
  backoff (P6) a shared hiccup retries them all in lockstep.
  *Fix:* bound the fan-out with a semaphore and give the gather an overall
  deadline, so one slow source cannot hold the whole call.

---

## P8 — Tests

- [ ] **No coverage measurement at all.** Neither `pyproject.toml` nor CI
  mentions `pytest-cov` or `coverage`, so nobody knows which of the 219 tests'
  paths are exercised — or which adapters are effectively untested.
  *Fix:* add `pytest-cov`, print a report in CI, and publish the number.
  Do **not** add a hard threshold gate initially: an arbitrary percentage
  invites tests written to raise the number rather than to catch defects.

- [ ] **BMV and SMV have no adapter test file.** Mexico and Peru are both in
  the "statements verified" tier, and both are covered only incidentally
  through service-level tests. Every other adapter has its own file.
  *Fix:* add `tests/test_bmv.py` and `tests/test_smv.py` with recorded
  fixtures, matching the existing per-adapter pattern.

- [ ] **CI runs on `ubuntu-latest` only.** PDF extraction, OCR and font
  handling are exactly the areas where platform differences show up, and the
  project is developed on macOS — so the primary development platform is the
  one CI never exercises.
  *Fix:* add macOS to the matrix, at least for the Python version already
  pinned in the smoke job.

- [ ] **No test asserts the README documents every MCP tool.** Ten of
  twenty-two drifted undocumented before anyone noticed (P4).
  *Fix:* a test comparing `@mcp.tool()` names against the README list.

- [ ] **No property or fuzz test over number and scale parsing.** This is
  precisely where the two worst defects lived — the missing `crore`/`lakh`
  multiplier and the `"000"` marker false-triggering on figures with embedded
  zeros. Both were single-example bugs in a space that is enumerable.
  *Fix:* a table-driven test over scale markers × grouping conventions
  (Western thousands, Indian lakh/crore, European decimal comma), asserting
  round-trip magnitude. Cheap, and it targets the highest-defect-density code
  in the repo.

---

## P9 — Observability

- [ ] **The package contains no logging whatsoever.** `import logging` appears
  zero times across `src/`. For a library whose whole job is network I/O
  against twenty-odd flaky regulator endpoints, a failure in a scheduled job
  yields an exception and nothing else — no record of which source was tried,
  what it returned, or how long it took.
  *Fix:* stdlib `logging` with a `NullHandler` on the package logger (so the
  library stays silent by default), debug-level records for each outbound
  request, retry and cache hit or miss, and a `--verbose` flag on the CLI.
  This is the single change that makes every other bug on this list cheaper
  to diagnose.

---

## P10 — Security hardening

- [ ] **`xml.etree.ElementTree.fromstring` parses network data.**
  `adapters/dart.py` parses the DART corp-code XML with stdlib ElementTree,
  which Python's own documentation lists as vulnerable to entity-expansion
  ("billion laughs") and quadratic-blowup denial of service. External entity
  resolution is not the issue; expansion is.
  *Fix:* parse with `defusedxml`, or bound the decompressed input and reject
  documents declaring internal entities. Real-world risk here is low — DART is
  a keyed government API — so this is hardening, not an incident.

---

## Not defects

Recorded so they are not "rediscovered" later:

- **Bancolombia does not resolve.** It reorganised under Grupo Cibest S.A. in
  2025; the library correctly returns the current listed issuer.
- **Cementos Pacasmayo does not resolve.** Genuinely absent from SMV's summary
  dataset upstream.
- **Telenor Danmark is a separate record from Telenor ASA.** Different LEIs,
  correctly distinct — unlike [#12](https://github.com/ryanrodrigues25200525-svg/openfilings/issues/12), which is one LEI twice.
- **Germany is unavailable.** Blocked upstream: filings.xbrl.org lists German
  filings as unavailable for reliable discovery.
- **Zip handling is already bounded.** Both `edinet.py` and `bmv_json.py`
  check member counts *and* decompressed sizes before reading, so archive
  bombs are guarded. Checked while auditing; recorded so it is not "fixed"
  a second time.
- **The hardcoded key in `adapters/sfc.py`.** It is the public client
  identifier the SIMEV frontend ships to every browser, documented as such in
  a comment. Secret scanners will flag it; it is a false positive.
