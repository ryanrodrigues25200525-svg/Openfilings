# Open Issues Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile the 8 open GitHub issues with the fixes already landed in HEAD, close what is proven, and leave narrow, honest follow-ups for what needs live data.

**Architecture:** Verify-first triage: run the existing regression tests as evidence, sync README/CHANGELOG/FIXES docs to the code, close GitHub issues with evidence links, then do only the small targeted code left (Unilever fixture scope, benchmark transcription scaffolding). No PDF-heuristic rewrites, no new subsystems.

**Tech Stack:** Python, pytest, SQLite cache, GitHub issues API via MCP

**Spec:** The 8 open issue bodies (#6, #7, #8, #9, #10, #11, #12, #15) plus HEAD commits 04b2184, 26c335d, cf46fff and README Known-issues section + FIXES.md P2/P3 + CHANGELOG Unreleased.

## Global Constraints

- Every production-code change needs a failing test first (TDD), per repo convention.
- `benchmarks.py` rule verbatim: "Add a benchmark only after an independent source-document review; never populate this list from the extractor output."
- Probe stays bounded and polite (2-3 issuers/market, monthly cadence); do not turn it into a crawl.
- PDF-heuristic changes for #6 must be targeted plus a fixture, not a rewrite.
- Matcher loosening for #10 needs a near-miss test, not just the happy path.

## Review Focus

- A same-LEI issuer filing in a third ESEF jurisdiction still collapses deterministically — expect one record with two `other_jurisdictions`.
- A cached filing extracted before a future extractor bump still reads as a miss after the bump — expect re-extraction, not stale values.
- "Ford Otosan" scores at least as well as "Ford", but "PKO" still resolves to nothing without alias data — expect empty, not a false positive.
- An FCA filing with an "S/ 000" scale marker never reports PEN — expect None/default, while the same marker on an SMV filing still reports PEN.
- A multi-issuer probe run with no network reports SKIP/offline, not red — expect `failed`-for-all to map to skipped in CI.

---

### Task 1: Establish verification baseline for already-landed fixes

**Files:**
- Test: `tests/test_multi_issuer_probe.py`, `tests/test_service.py`, `tests/test_storage.py`, `tests/test_company_matching.py`, `tests/test_pdf_financials.py`
- Modify: none

**Interfaces:**
- Consumes: HEAD commits 04b2184, 26c335d, cf46fff
- Produces: evidence log (test command + counts) used by Tasks 2-3 to justify closes

- [ ] **Step 1: Run the five targeted test files**

Run: `uv run pytest tests/test_multi_issuer_probe.py tests/test_service.py tests/test_storage.py tests/test_company_matching.py tests/test_pdf_financials.py -q`
Expected: PASS, ~all green (baseline on 2026-10-08 was 242 passed)

- [ ] **Step 2: Run the full suite once**

Run: `uv run pytest -q`
Expected: PASS with 0 failures; record the exact passed count for issue-close comments

- [ ] **Step 3: Record evidence, no commit (verification-only task)**

---

### Task 2: Close the five proven-fixed issues with evidence (#9, #12, #14, #15, #13)

**Files:**
- Modify: `README.md:70-110` (Known issues checkboxes), `FIXES.md` P2/P3 checkmarks, `CHANGELOG.md` Unreleased (already claims Closes #9, #12 — confirm #14/#15/#13 entries exist)
- Test: none (docs + tracker only)

**Interfaces:**
- Consumes: Task 1 evidence log
- Produces: GitHub issues closed as completed with test-evidence comments

- [ ] **Step 1: Write a failing check — confirm README still shows open boxes for fixed issues**

Run: `grep -n "issues/9\|issues/12\|issues/14\|issues/15\|issues/13" README.md`
Expected: FAIL-equivalent — lines show `- [ ]` (unchecked) for issues the code already fixes

- [ ] **Step 2: Update README Known-issues checkboxes to checked for #9, #12, #14, #15, #13**

In `README.md`, flip `- [ ]` to `- [x]` for issues 9, 12, 14, 15, 13 only. Leave #6, #7, #8, #10, #11 unchecked. Keep the one-line descriptions truthful (e.g. #15 now reads "stamped with extractor version; re-extracts on mismatch").

- [ ] **Step 3: Update FIXES.md P2/P3 entries for the same five to checked**

Same flip in `FIXES.md`. Do not rewrite fix descriptions.

- [ ] **Step 4: Confirm CHANGELOG Unreleased covers each close**

Check `CHANGELOG.md` Unreleased has entries for #9 (probe), #12 (LEI dedup), #14 (same-day sort), #15 (version stamp), #13 (dependabot lock). Add the missing one-line entries if any are absent, following the existing style.

- [ ] **Step 5: Comment + close each GitHub issue with evidence**

For each of #9, #12, #14, #15, #13: add an issue comment citing the HEAD commit hash, the test file + test name that pins it (e.g. #12 → `test_bare_query_for_dual_jurisdiction_lei_returns_one_home_record`, #15 → `test_stale_extractor_version_is_a_financials_cache_miss`, #14 → `test_list_filings_prefers_newer_period_on_same_filing_date`), and the full-suite passed count from Task 1. Then close with `state_reason: completed`.

- [ ] **Step 6: Commit docs**

```bash
git add README.md FIXES.md CHANGELOG.md
git commit -m "docs: mark #9 #12 #13 #14 #15 closed; sync READMEs to landed fixes"
```

---

### Task 3: Narrow #10 and #6 to their honest remainder

**Files:**
- Modify: `README.md` (reword #10 and #6 lines), `src/openfilings/xbrl/pdf_statements.py:441-451` (no change — reference only)
- Test: `tests/test_company_matching.py:48-74`, `tests/test_pdf_financials.py:192-236` (already pin part 1 / currency guard — reference only)

**Interfaces:**
- Consumes: `ranked_matches()` token-subset branch, `_NEVER_CURRENCY_BY_SOURCE` guard
- Produces: reworded issue tracker state (comment, keep open with narrowed scope)

- [ ] **Step 1: Verify part-1 and guard tests pass in isolation**

Run: `uv run pytest tests/test_company_matching.py::test_extra_query_token_does_not_lose_a_strong_partial_match tests/test_company_matching.py::test_extra_token_tolerance_still_rejects_near_misses tests/test_pdf_financials.py::test_fca_filing_never_reports_pen_currency -v`
Expected: PASS (3 passed)

- [ ] **Step 2: Comment on #10 — part 1 done, part 2 deferred per the issue's own split**

Post: token-subset fix + near-miss test landed in cf46fff; "PKO" still correctly returns [] because no scoring change can find a true alias — that half needs per-source ticker/alias data where the regulator publishes it. Keep #10 OPEN with the narrowed title scope (aliases/tickers), reword README line to "extra-token tolerance fixed; true aliases (PKO) need per-source alias data".

- [ ] **Step 3: Comment on #6 — currency guard done, scale/misalignment remains loud by design**

Post: never-PEN guard landed in cf46fff with `test_fca_filing_never_reports_pen_currency`; magnitudes ~1000x and identical totals are still a misaligned table read that `data_quality_report` flags (`ok: false`, 4 failed rules) rather than silently trusting. Per the issue ("targeted fix plus a fixture, not a rewrite"), keep #6 OPEN narrowed to scale/misalignment fixture, reword README line to "currency guard fixed; scale/misalignment still loudly flagged, not silently trusted". Do NOT touch PDF heuristics in this task.

- [ ] **Step 4: Commit docs**

```bash
git add README.md
git commit -m "docs: narrow #10 to alias half and #6 to scale half after guards landed"
```

---

### Task 4: Transcribe Volvo + Keppel benchmarks to unblock #7 (and seed #8)

**Files:**
- Modify: `src/openfilings/benchmarks.py:50-101`
- Test: `tests/test_benchmarks.py` (create if absent — assert the two new benchmarks exist and `_assert_reference_facts` accepts them against fixture financials)

**Interfaces:**
- Consumes: Volvo AB and Keppel Ltd published annual reports (independent source-document review)
- Produces: `ACCURACY_BENCHMARKS: tuple[AccuracyBenchmark, ...]` extended by 2 entries; `ReferenceFact(code, statement_type, period_end, value, unit, concept, provenance, confidence)`

- [ ] **Step 1: Write the failing test — benchmarks for Volvo and Keppel do not exist**

```python
def test_benchmarks_cover_volvo_and_keppel_regression_guards():
    labels = [b.label for b in ACCURACY_BENCHMARKS]
    assert any("Volvo" in label for label in labels)
    assert any("Keppel" in label for label in labels)
```

Run: `uv run pytest tests/test_benchmarks.py::test_benchmarks_cover_volvo_and_keppel_regression_guards -v`
Expected: FAIL (asserts on missing labels)

- [ ] **Step 2: Transcribe figures by hand from the published reports (never from extractor output)**

For Volvo AB (ESEF Sweden filing) and Keppel Ltd (SGX filing): download the published annual report package, read total_assets / total_liabilities / total_equity for the benchmarked period, record filing_id + filing_url + period_end + value + unit + concept. Note the source page in a code comment beside each fact.

- [ ] **Step 3: Implement the two `AccuracyBenchmark` entries in `src/openfilings/benchmarks.py:50-101`**

Follow the existing Tesco/ASML shape exactly. Confidence/provenance must reflect the source (tagged_xbrl where tagged, pdf_table/75 where PDF-derived).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_benchmarks.py -v`
Expected: PASS

- [ ] **Step 5: Attempt the live benchmark run (network-dependent, evidence only)**

Run: `uv run python -m openfilings.benchmarks` (or the repo's live-benchmark entrypoint)
Expected: either PASS with 4 benchmarks checked, or a documented live failure pasted into the #7 comment. Do not fabricate values to make it pass.

- [ ] **Step 6: Comment on #7 with transcription sources, keep open until live run is green**

- [ ] **Step 7: Commit**

```bash
git add src/openfilings/benchmarks.py tests/test_benchmarks.py
git commit -m "feat: pin Volvo and Keppel reference facts for #7 regression guards"
```

---

### Task 5: Seed #8 — one benchmark per uncovered extraction path (bounded)

**Files:**
- Modify: `src/openfilings/benchmarks.py`
- Test: `tests/test_benchmarks.py` (extend: one benchmark per path label)

**Interfaces:**
- Consumes: Task 4 pattern
- Produces: additional `AccuracyBenchmark` entries for CVM Open Data, SFC CUIF, NSE Integrated XBRL, KAP viewer tables, BMV, SMV, and one PDF-heuristic path

**Note:** This task is intentionally last and may span multiple commits — one issuer per path, prioritising scaling/concept-mapping risk. If transcription time exceeds one session, land Volvo+Keppel (Task 4) first and leave #8 open with a checklist comment of which paths remain.

- [ ] **Step 1: Write failing tests naming each missing path benchmark**
- [ ] **Step 2: Transcribe one issuer per path from published reports (same rule as Task 4)**
- [ ] **Step 3: Implement entries, run tests, run live benchmarks**
- [ ] **Step 4: Comment checklist on #8, commit per path**

---

### Task 6: Keep #11 open with its caveat intact (no code)

**Files:**
- Modify: none (verify `README.md:60,92,125` and CHANGELOG DART caveats still present)

**Interfaces:**
- Consumes: none
- Produces: confirmation comment on #11

- [ ] **Step 1: Verify caveats still say "never run against a live key"**

Run: `grep -n "never been run\|mocked" README.md CHANGELOG.md src/openfilings/adapters/dart.py | head`
Expected: caveats present

- [ ] **Step 2: Post the 4-step closing checklist on #11 (register key → resolve KOSPI issuer → list annuals → check identity) and leave OPEN**

No commit.
