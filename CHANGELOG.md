# Changelog

All notable changes to OpenFilings are documented in this file.

## Unreleased

### Added

- Added a monthly multi-issuer probe (`openfilings.probe`, scheduled in
  `.github/workflows/live-probe.yml`): a bounded, reviewed list of 2-3
  issuers per keyless market - none of them the smoke issuer - that
  resolves each company, lists its filings, extracts one filing, and
  reports verified / unverifiable / failed per issuer instead of
  pass/fail, so "unverifiable" stays visible rather than reading as
  success. Closes #9.

### Fixed

- Search results now deduplicate same-LEI company records to one record per
  legal entity (issue #12, decided option 1): one LEI filed in two ESEF
  jurisdictions previously produced two company IDs, and a bare query's
  ranking silently decided the jurisdiction (the "ESEF Portugal" smoke
  case was exercising Spain). The surviving record is chosen
  deterministically - alphabetically first by (market, country code,
  id) - and lists the other jurisdictions on the record's new
  `other_jurisdictions` field (also exposed in `company_summary`). Both
  per-jurisdiction IDs still resolve and each lists only its own
  market's filings; only the search-result layer merges. Distinct LEIs
  (e.g. the two Telenors) stay distinct. Closes #12.

## 0.22.0 - 2026-08-01

### Added

- Added a `PRAGMA user_version` schema-version check to the SQLite cache.
  Tables were created with `CREATE TABLE IF NOT EXISTS` and had no version
  tracking at all, so a future incompatible schema change would have
  silently met an old database with no way to detect the mismatch. A
  pre-versioning database (SQLite's own default, `user_version = 0`) is
  schema-compatible with version 1 by construction and is accepted and
  stamped forward; a cache from a *newer* schema version fails loudly with
  a clear message instead of operating on data it may not understand.
- Added proportional (+/-25%) jitter to retry backoff. `source="all"` fans
  out to 20+ adapters at once on a fixed exponential schedule, so a shared
  upstream hiccup had every client retrying in lockstep - impolite to a
  free public endpoint and self-defeating, since the retries collide again.
  A server's own `Retry-After` header is still respected exactly, without
  jitter, since it is an explicit instruction rather than a schedule to
  randomize.
- Added packaging metadata that was entirely absent: a `py.typed` marker
  (the public API was already fully annotated, but with no marker
  downstream type checkers ignored all of it - confirmed present in the
  built wheel), a real `__version__` resolved from installed package
  metadata, and PyPI classifiers/keywords/`[project.urls]` (license,
  Python versions, homepage, repository, issues, changelog). The version
  resolution lives in a new leaf module, `openfilings/_version.py`, with no
  internal imports of its own, since adapters are imported transitively by
  the top-level package and cannot import `openfilings.__version__`
  directly without risking a circular import.
- Ten adapters sent a hardcoded `User-Agent: openfilings/<version>` that
  had already drifted out of sync with the package before this release -
  ASX and SEDAR said 0.21, three said 0.20, and FCA NSM still said 0.2 from
  years earlier. All ten now read the real `__version__` at request time,
  so this cannot silently drift again.
- Fixed MCP tool documentation drift: 10 of 22 `@mcp.tool()`s were
  undocumented in the README, including `data_quality_report` - the tool an
  agent needs to decide whether to trust a figure - plus the fact-series
  tools (`financials_query`, `historical_facts_query`) research workflows
  depend on. Added a test comparing `server.py`'s tool names against the
  README's MCP tools section, so a new tool cannot ship silently
  undocumented again; verified the test actually fails when a tool is
  removed from the docs, not just that it passes today.

### Added (earlier this cycle)

- Added an optional `pdf-detect` extra using
  [pdf-inspector](https://github.com/firecrawl/pdf-inspector) for
  pre-extraction PDF classification. Evaluated as a replacement for
  `pymupdf4llm` and rejected for that role: three filings tested head-to-head
  (Unilever's 20-F, DBS's and Keppel's annual reports) produced identical
  figures from both engines, and the existing PDF-table heuristics do not
  parse pdf-inspector's markdown at all. What shipped instead is narrower and
  provably non-regressive - skip a doomed-to-fail native-extraction attempt
  when a PDF is confidently classified scanned/image-based (only when OCR is
  actually available, so the best-effort fast path is never lost), and
  surface detected font-encoding issues as a diagnostic warning without
  forcing OCR, since OCR has no page-subset support and generally degrades
  table structure relative to correct native extraction. Degrades to
  identical behavior when the extra is not installed.

### Fixed

- Indian PDF filings were understated by seven orders of magnitude. `_scale`
  recognized billion/million/thousand but not `crore` (10^7) or `lakh`
  (10^5), which is how essentially every SEBI-regulated annual report states
  its figures. Because India's XBRL path only covers filings from April 2025
  onward, the unscaled values landed in the same multi-period series as
  correctly scaled tagged ones - TCS returned total assets of 143651 for
  FY2024 next to 1596290000000 for FY2025. The suite's own NSE fixture
  carries a "(Rs in crore)" header and asserted the unscaled numbers, which
  is why nothing caught it; those expectations were wrong and are corrected.
- Equity totals dropped non-controlling interests wherever a statement
  tagged both `EquityAttributableToOwnersOfParent` and the real
  `ifrs-full:Equity`. Selection was by document order, and an IFRS balance
  sheet lists the parent line above the total, so the parent-only figure
  won and `assets = liabilities + equity` failed by exactly the NCI.
  Selection now follows the alias table's own priority. Confirmed live on
  BIM (Turkey), whose equity was understated by 1,817,555 TRY.
- Company search missed issuers whose registered name uses a letter NFKD
  cannot decompose - `ø æ œ ł đ ð þ ß ı` are independent letters rather
  than a base plus a combining mark, so "Orsted" was not a substring of
  "ørsted a s" once folded. Matching now transliterates them. This is
  applied only to search comparison, since `normalize_text` also builds
  KAP's company URLs.
- The live smoke suite's Colombia issuer went stale: SFC's registry now
  returns no "ACCION" (listed equity) records for Banco de Bogota at all,
  only bonds and certificates, so the case failed to resolve a company. It
  now uses Davivienda. This is the scheduled suite doing its job - the
  regulator's own universe changed, and nothing in this repo did.
- Peru's issuer universe was whatever appeared in a single year of SMV's
  summary dataset, and the registry stopped at the first year returning any
  rows. Early in a calendar year that is only the issuers who have already
  filed, so everyone else was unfindable. The registry now merges the two
  most recent years that carry data.
- ESEF company search sent the raw query to filings.xbrl.org as a byte-wise
  SQL `ilike`, so any query missing a diacritic returned nothing at all -
  "Jeronimo" could never match "JERÓNIMO MARTINS SGPS SA" across all 14
  ESEF markets. On an empty result it now retries on a short accent-free
  fragment and ranks the candidates locally, where folding applies.
- The live smoke suite reported `PASS ... not_applicable` for a third of its
  cases, which read as success but proved only that a filing was fetched. The
  balance-sheet identity is skipped whenever one of the three totals is
  derived rather than tagged, since checking `assets = liabilities + equity`
  against a total derived as `assets - equity` is circular. The run now ends
  with an explicit tally of verified / unverifiable / search-only cases, and
  `require_source_balance_sheet` - which existed but was enabled nowhere - is
  now set on the thirteen cases confirmed live to expose all three totals as
  source facts, so a silent degradation to derived values fails the run
  instead of passing it. Sweden (AB Volvo) and Singapore (Keppel), the two
  named regression guards, are among the cases that cannot verify: both
  filings leave a total to be derived, so their guard value is limited to
  extraction not crashing. Closing that needs pinned reference facts in
  `benchmarks.py`, which currently covers two issuers.
- The "ESEF Portugal" smoke case was exercising Spain. filings.xbrl.org
  indexes EDP Renovaveis under both `es_` and `pt_` company IDs for the same
  LEI, and the bare query "EDP" ranked the Spain-attributed record above the
  Portuguese parent. The case now names EDP, S.A. explicitly and resolves to
  `pt_lei_529900CLC3WDMGI9VH80`.

### Added

- Added six more keyless ESEF markets - Norway, Poland, Belgium, Austria,
  Luxembourg, and Portugal - reusing the existing generic ESEF connector
  against filings.xbrl.org. Verified live end-to-end (search, filings,
  financials, balance-sheet identity) against Equinor, Orlen, KBC,
  Verbund, ArcelorMittal, and EDP.
- Brazil now reads CVM's Open Data DFP/ITR datasets directly for financial
  statements - a standardized chart of accounts published as free bulk
  CSV/ZIP archives - instead of parsing the PDF filing. Falls back to the
  existing PDF-heuristic path when a company/year isn't in the dataset.
- Colombia's balance sheet is now read directly from SFC's CUIF supervisory
  dataset on datos.gov.co (assets/liabilities/equity accounts reconcile
  exactly across every regulated entity type) and merged with the PDF
  filing's other statements. The income statement still comes from the PDF,
  since CUIF reports income/expense accounts unclosed for supervisory
  purposes - even at year-end, revenue exactly equals expenses.
- India now reads NSE's "Integrated Filing - Financials" XBRL directly -
  the exclusive format for SEBI Regulation 33 financial results since April
  2025, when PDF submission was discontinued - instead of parsing the
  annual-report PDF. Reuses the existing tagged-XBRL statement pipeline
  entirely: the taxonomy's concept names already match the standard IFRS
  concepts recognized elsewhere. Falls back to the PDF annual report if no
  audited filing covers the exact fiscal year-end. Verified against
  Reliance Industries and Tata Consultancy Services.
- The `filing_financials` MCP tool now points the calling agent to
  `filing_search`/`filing_markdown` when structured extraction fails,
  instead of just a bare error - the filing's own converted text is still
  readable even when the heuristic statement parser can't make sense of it.
- The scheduled live smoke suite now fetches each checked filing's
  financials and verifies the balance-sheet identity holds, instead of
  only checking that a filing was found - this is the check that would
  have caught this session's Sweden and Singapore bugs automatically, on
  a schedule, instead of only when someone thinks to spot-check by hand.
  Coverage expanded to one issuer per ESEF jurisdiction (previously one
  for all of ESEF) plus company-search-only checks for Canada and Japan.
- Added a South Korea connector for the Financial Supervisory Service's
  OPENDART system: keyed company search over the official corp-code
  registry, annual/semiannual/quarterly filing listing, and financial
  statements read directly from `fnlttSinglAcntAll.json`'s IFRS-tagged
  account rows (mapped through the existing IFRS concept table, no
  DART-specific aliasing needed). `DART_API_KEY` is required for every
  operation - DART has no keyless surface at all, unlike EDINET. Built
  against DART's documented request/response shapes and verified with
  mocked-response tests only; no live API key was available in this
  session, so no live company or financial data has been verified.
- Added a keyless ASX listed-company adapter for Australia, covering company
  discovery only. Filing retrieval was built first against ASX's global
  announcements feed and then removed after measuring it: the feed accepts no
  issuer filter (`issuer_code`, `asx_code` and every variant tested are
  silently ignored), one uncached page costs 18-20 seconds and covers about
  six days, so a single company's last annual report sat roughly twelve
  minutes and forty requests away - and the adapter's `history_years=4`
  default was unreachable in practice, since its page budget only spanned
  about six months. The per-company `asx.api.markitdigital.com` endpoint is
  capped at five items regardless of parameters and is dominated by routine
  notices. ASIC's lodged reports are a paid-download product. Australia is
  therefore discovery-only, like Canada, and `list_filings`/`download_document`
  raise an error naming the public alternative. Switzerland was investigated
  and not added: SIX's data APIs are commercial, and ad hoc disclosures are
  pushed to each issuer's own website with no central free index or
  predictable document host to build against.
- Fixed ASX company discovery, which returned nothing: the
  `ASXListedCompanies.csv` endpoint no longer resolves. Discovery now reads
  the markitdigital listed-company directory and matches header columns
  case- and order-insensitively, since ASX has shipped both column orders
  and "GICS"/"GICs" casing.
- Added a keyless Turkey connector for KAP (the Public Disclosure Platform):
  company search and disclosure listing use KAP's own public website
  endpoints (not the paid, contract-gated Rest API data-distribution
  product). "Finansal Rapor" financial-report disclosures are read directly
  from KAP's rendered XBRL-viewer tables - each row carries the filer's
  literal IFRS-tagged concept next to its value, so the existing IFRS
  concept mapping applies with no Turkey-specific aliasing, and PDF parsing
  is skipped entirely. Live-verified end-to-end (search, filings,
  financials, balance-sheet identity) against Deniz Gayrimenkul GYO,
  Turkcell, and BIM.
- Added `category="insider"` (director/PDMR dealing notifications) and
  `category="major_holdings"` (substantial-shareholding notifications) to
  UK FCA NSM, India NSE, and Brazil CVM, alongside the existing
  `category="accounts"`. NSM maps these to its own `DSH`/`HOL` type codes
  on the same feed already used for accounts - no new adapter code. NSE
  reads SEBI's PIT (`/api/corporates-pit`) and shareholding-pattern
  (`/api/corporate-share-holdings-master`) endpoints directly, each
  returning a real downloadable XBRL document; `NseClient.document_url()`
  was widened to accept `/corporate/xbrl/*.xml` alongside the existing
  `/annual_reports/*.pdf|.zip`. CVM reads its own yearly VLMO Open Data
  archive (CVM Instrução 358 art. 11) - the same row shape as the IPE
  archive already used for `category="accounts"`, just a different yearly
  ZIP, and it combines insider trading and holdings into one filing so
  both are covered by `category="insider"` alone. Live-verified end-to-end
  on all three. Singapore SGX was investigated and not added: its general
  corporate-announcements API returned a genuine AWS API Gateway `403
  ForbiddenException`, not a missing route, and this project doesn't push
  past real access-control blocks.
- Added `search_disclosures()` - full-text keyword search across every
  issuer's disclosures for one source, not scoped to a company. FCA NSM's
  own top-level `keyword` search field is a no-op (confirmed live: it
  doesn't change result counts at all); this uses a `headline` criterion
  instead, which does filter. CVM's yearly IPE archive already covers
  every issuer in one file, so this filters it by subject/type instead of
  a new endpoint. Exposed via the CLI (`search-disclosures`) and MCP
  (`disclosures_search`).
- Added `get_company_facts()` - merges a company's most recent structured
  filings into one multi-period time series per line item, EdgarTools'
  `get_facts()` concept. Pure composition over `list_filings()`/
  `get_filing_financials()`, so it works for every market with structured
  or PDF-derived financials already, with zero adapter changes. Exposed
  via the CLI (`facts`) and MCP (`company_facts`).
- Added structured parsing for UK TR-1 major-shareholding notifications
  (`openfilings.ownership.extract_nsm_major_holder`): holder name, ISIN,
  reason, dates, and position, parsed from the filing's rendered HTML body
  by its fixed FCA-prescribed section order - verified against four real
  filings from different companies. `list_major_holders()` lists and
  parses one UK issuer's notifications; `search_major_holders()` is a
  bounded, 13F-style reverse lookup (what has a given holder disclosed a
  stake in, across UK issuers) - NSM's search index doesn't carry the
  holder's identity, only each filing's document body does, so this scans
  the `scan_limit` most recent TR-1 filings and parses each one, not the
  full historical record. Exposed via the CLI (`major-holders`,
  `search-major-holders`) and MCP (`major_holders_list`,
  `major_holders_search`).
- `major_holders_list`/`major_holders_search` now also cover Brazil: CVM's
  yearly Formulário de Referência "posição acionária" archive (the same
  `structured_archive`/`DADOS` yearly-ZIP convention already used for
  dfp/itr/vlmo) is parsed into the same `MajorHolderNotification` shape as
  the existing UK TR-1 path, routed automatically by the company id's
  market prefix. Live-verified against a real Petrobras (`br_cvm_009512`)
  shareholder list.
- Added structured parsing for UK MAR Article 19 PDMR/PCA dealing
  notifications (`openfilings.insider.extract_nsm_insider_dealings`),
  exposed as a new `insider_dealings_list` MCP tool - distinct from the
  existing `filings_list(category="insider")`, which only lists the raw
  DSH filing metadata; this parses each one's document body into person
  name, position, instrument/ISIN, transaction nature(s), price/volume
  lots, dates, and place(s). Verified against 8 real live FCA NSM DSH
  filings across different filing agents (RNS, EQS, PRN).
- Added `category="dividend"` to SGX, backed by the same
  `api.sgx.com/announcements` endpoint SGXNet's own frontend uses,
  including the CMS validator token exchange it requires. The token
  exchange itself succeeds live; the announcements endpoint currently
  returns an Akamai edge WAF 403 from this project's network - the same
  class of block already documented for ASX above, not a code defect.
  Additive and opt-in: the existing default `category="accounts"` path is
  unaffected.
- Added `category="material_event"` (KAP's ODA disclosure class) and
  `category="corporate_action"` (CA) to Turkey's KAP connector, alongside
  the existing accounts/disclosure split. Live-verified against real
  Turkcell filings.

### Fixed

- CVM major-holder data: the first implementation read `Pct_Total`/
  `Qtd_Total` from the FRE archive, which don't exist in the live CSV
  (its real columns are `Percentual_Total_Acoes_Circulacao`/
  `Quantidade_Total_Acoes_Circulacao`) - every result would have had
  silently empty `total_percent`/`total_voting_rights`. Found by
  downloading the real archive before committing, not from the mocked
  test suite alone.
- UK PDMR dealing extraction: the first implementation scored 1 of 8 on
  real live FCA NSM DSH filings despite passing its own hand-written
  fixture. Three real bugs, all found by testing against live filings:
  word-exported HTML sometimes splits one word across adjacent `<span>`
  elements, and the original per-node-stripped text join dropped
  legitimate spaces while merging fragments into garbage tokens;
  different filing agents word standard-form labels inconsistently
  around the article "the" ("Description of the financial instrument"
  vs. "Description of financial instrument"), so normalization now drops
  "the" as a stopword on both sides of every label comparison; and
  EQS-distributed filings prepend one cell containing the entire
  announcement as prose - itself quoting every field label - ahead of
  the real structured rows in the same table, which matched first and
  resolved every section to the wrong index. After these fixes, 7 of 8
  real filings parse correctly; the 8th (Foresight Enterprise VCT)
  correctly declines since it's a genuinely different notification
  format, not a MAR Art19 transaction table. One known gap remains:
  filings covering multiple lots/tranches for one person can still
  produce duplicated or misaligned transaction fields, since each
  dealing is parsed as a single flat record rather than split per lot.
- Found via a live end-to-end test pass across every supported market:
  CVM's balance sheet was missing `total_liabilities` (only
  `current_liabilities`/`noncurrent_liabilities` were present) - CVM's
  chart of accounts has no single "Passivo Total" line at all, so it's now
  derived as the sum of the two when not directly tagged, the same way
  every other market's balance sheet reconciles.
- PDF-derived financials (aligned-text path): a label repeating verbatim
  within one statement's page/continuation window (e.g. a cash-flow
  "changes in working capital" reconciliation note reusing a balance-sheet
  row's exact wording, such as "Trade and other receivables") no longer
  overwrites the real row with the note's period-on-period movement
  figure - found via a live ASX filing where this silently turned a
  positive receivables balance negative even though the top-line
  balance-sheet identity still happened to hold. Only applies to an exact
  repeated label; a different, more general label that resolves to the
  same concept (e.g. a segment's "Operating revenue" followed by the
  statement's real "net revenue" total) still prefers the later, more
  complete occurrence as before.
- FCA NSM has no generic "category" filter of its own, only disclosure type
  codes, so `category="accounts"` (the default) was a silent no-op -
  `filings()`/`get_filings()` could return the newest disclosure of any
  type (a director dealing, an admission notice) instead of a financial
  statement. Now maps to the "ACS" (accounts) type code automatically
  unless the caller passes their own `nsm_type_codes`.
- PDF-derived financials (aligned-text path): a page footer or a stray note
  reference carrying a real word alongside an embedded digit (e.g. a page
  number) is no longer stripped down to a false numeric value; the forward
  scan for a row's own numbers now stops after a run of unrelated prose
  instead of reading through it.
- PDF-derived financials: "Statement of Changes in Equity" is now detected
  as a statement boundary in more layouts (a missing "consolidated"-prefixed
  heading variant, and sentence-case headings beyond the first 12 lines),
  so its rows (equity components, not fiscal years) no longer overwrite a
  same-named row from a different statement.
- PDF-derived financials: a note reusing a grand-total label ("Total
  Assets") for a narrower scope (a subsidiary, a structured entity) no
  longer wins over the real consolidated total when both candidates tie on
  period count - the larger value is preferred, since a note's total can
  only be a subset of the entity's real total.
- Tagged-XBRL financials (any inline-XBRL market: UK-GAAP, ESEF, JP-GAAP,
  and now India): when a filer tags both a full total (e.g. "Equity") and a
  narrower component with identical period/context coverage (e.g.
  "EquityAttributableToOwnersOfParent", excluding non-controlling
  interests), the concept selection previously fell back to an arbitrary
  tie-break and could silently pick the narrower one, breaking the
  balance-sheet identity. The alias list's own order (the full total listed
  first) now breaks the tie.

- PDF-derived financials: a labeled row followed by an unlabeled sub-item
  breakdown (no repeated "Total X" line) no longer misattributes the first
  segment's value as the row's own total.
- PDF-derived financials: a combined subtotal label (e.g. "Passivo
  circulante e não circulante") no longer overwrites its own component's
  correct value.
- PDF-derived financials: Indian lakh/crore number grouping (e.g.
  "2,57,935") is now parsed correctly - previously failed silently on
  every NSE filing using this convention.
- PDF-derived financials: ratio-analysis disclosures, qualified variants
  (e.g. "X Under Development"), and grand totals restating a different
  total no longer falsely match unrelated line items.
- PDF-derived financials: a single-word alias (e.g. "revenue", "goodwill")
  no longer matches prose that merely starts with that word (e.g. "Revenue
  Reserves", "Goodwill is reviewed on an annual basis...").
- PDF-derived financials: a statement heading wrapped across separate
  extracted lines is now detected via a joined window instead of missed.
- Added missing common English aliases ("Net Revenue", "Income from
  Operations", "Net Income", "Total Current/Non-Current Assets/
  Liabilities") and a PDF ligature-extraction artifact ("Gross Proft" for
  "Gross Profit").

### Removed

- Removed Taiwan (TWSE/MOPS), Hong Kong (HKEX/HKEXnews), and mainland China
  (SSE/CNINFO) support.
- Removed a dead, unreferenced `_is_nsm_company_id` helper.

### Documentation

- Added a complete system architecture document covering components, data
  flows, adapter contracts, persistence, resource boundaries, trade-offs, and
  market-extension guidance.
- Added a project description defining the product scope, supported markets,
  capabilities, technology, maturity, and current limitations.

## 0.21.0 - 2026-07-23

- Added durable Canadian filing imports from allowlisted SEDAR+ generated URLs
  or browser-downloaded local PDFs.
- Routed imported filings through the existing CLI, Python, Markdown, section,
  structured-financial, and token-bounded MCP interfaces.
- Added compressed source-document storage, cache-budget accounting, redirect
  confinement, PDF validation, and explicit browser-verification recovery.

## 0.20.0 - 2026-07-23

- Added keyless listed-company and filing adapters for Mexico BMV, India NSE,
  mainland China CNINFO/SSE, Peru SMV, and Colombia SFC/SIMEV.
- Added official TSX/TSXV Canadian company discovery with an explicit SEDAR+
  browser-only filing limitation.
- Added retry-bounded shared HTTP handling, strict document-host validation,
  live smoke coverage, and Spanish and Simplified Chinese PDF table aliases.
- Added INR, MXN, CAD, CNY, PEN, and COP structured-statement currency support.
- Redesigned MCP responses for LLM use with compact metadata, progressive
  disclosure, section outlines, targeted reads, ranked excerpts, pagination,
  structured recovery guidance, and bounded financial-statement filters.
- Added native Markdown and structured-statement extraction for BMV quarterly
  IFRS JSON archives.
- Replaced Peru's 100,000-row SMV downloads with bounded official statement
  operations and limited request concurrency.

## 0.19.0 - 2026-07-23

- Added keyless listed-company adapters for seven ESEF markets, Brazil, Taiwan,
  Hong Kong, and Singapore while retaining the existing Japan connector.
- Added EdgarTools-style company, filing-collection, document-section, search,
  prefetch, and financial-statement APIs.
- Added high-confidence structured extraction from native and OCR-derived PDF
  statement text for CVM, TWSE, HKEX, and SGX reports.
- Removed Companies House so UK discovery contains exchange-listed issuers from
  the FCA path only.
- Added locked CI, Python compatibility, package smoke tests, CodeQL, dependency
  auditing, dependency updates, and scheduled keyless regulator smoke tests.
