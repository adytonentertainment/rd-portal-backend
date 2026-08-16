# PRD — Royalty Statement Data Integration & Pre-Distribution Validation

**Status:** Draft for build
**Owner:** Steven Garcia
**Last updated:** 2026-06-11
**Predecessor:** [PRD.md](PRD.md) (demo vertical slice, 2026-05-19). This PRD replaces the demo's localStorage mocks with a real backend integration, driven by the full production dataset.
**Source dataset analyzed:** `/Users/stevengarcia/Downloads/wetransfer_h2-2025-and-h1-2026-statements_2026-06-10_1613/` — 6 statement batches from Regalias Digitales, LLC covering H2 2025 and H1 2026: **2,612 statements, 1,374 beneficiary accounts, ~929 distinct writers, $8,919,798.85 total payable.** Machine-readable extracts (`royalty_summary.csv`, `pdf_full.csv`) sit in that folder.

---

## 1. Problem

The publisher's ops flow today: a royalty processor produces, twice a year (plus quarterly for a subset of clients), one PDF statement + one XLSX detail file **per beneficiary account per catalog** — ~1,300 file pairs per semiannual run. The admin must get these into each writer's portal and trigger payout communication. There is currently:

- No ingestion path for these files (the demo upload modal fake-parses; the backend XLSX/PDF endpoints return 501).
- No check that a distribution run is **complete and internally consistent** before writers see it. Real risks found in the actual data: writers silently dropped between periods while still carrying a balance, duplicate accounts under drifted name spellings, statements whose payable is $0 because of recoupment (correct, but indistinguishable from a parse failure without validation).
- No canonical writer identity. The same human appears as `JN0261` (Mechanical) and `C00650` (YouTube), and names drift across periods (`Los Tucanes De Tijuana` vs `Los Tucanes DeTijuana`, `ameria bb` vs `amaria bb`).

**Scope note — this system sits downstream of payment.** Payments are executed externally (cheques/transfers from the processor's run) *before* the files arrive here. The system never initiates, calculates, or gates money movement. Its job is purely informational: **sort the files, prove the set is complete and internally consistent, and deliver every writer's documents to their portal at once** — so that after a payout run, everyone has their data.

The product is **two surfaces driven by one action**:

- The **admin dashboard** is the control plane: the admin dumps the raw files — all of them, unsorted, in one upload — and the system sorts them by writer, period and statement type, parses and validates everything, shows exactly what is missing or inconsistent, and blocks publication until the file set is provably complete.
- The **writer portals** are the delivery target: the moment the admin hits Distribute, every writer's portal refreshes with their new statements (PDF + XLSX downloads, figures, breakdowns). Nothing reaches a portal except through a validated distribution.

---

## 2. Ground truth: the source data structure

Everything below was verified against all 2,612 real statements. The import pipeline and validation rules must encode these facts.

### 2.1 Batches and cadence

| Batch | Cadence | Accounts | Calculated | Payable |
|---|---|---|---|---|
| Mechanical 2025H2 | semiannual | 533 | $2,649,443 | $2,643,312 |
| Mechanical 2026H1 | semiannual | 580 | $2,747,187 | $2,676,192 |
| YouTube 2025H2 | semiannual | 696 | $1,085,535 | $1,129,321 |
| YouTube 2025Q4 | quarterly | 21 | $354,008 | $352,246 |
| YouTube 2026H1 | semiannual | 758 | $1,732,277 | $1,780,562 |
| YouTube 2026Q2 | quarterly | 24 | $340,930 | $338,166 |

- **Two catalogs** (royalty types): *Mechanical Royalties* (includes performance income) and *YouTube Publishing*. Statement periods are tagged `PUB25H2`, `PUB26H1`, `PUB25Q4`, `PUB26Q2`.
- **Two cadences.** Most accounts settle semiannually. A fixed set of ~21–24 accounts (the Luna Negra sub-publisher family `C00139`–`C00139r`, plus Gerencia, Ingrooves, Vim Music, Afinarte, Canserbero, Lupita Vega and the house account) settle **quarterly** and do *not* appear in the semiannual YouTube runs. Three writers (Canserbero, Gerencia, Lupita Vega) legitimately appear in **both** cadences because their mechanical account is semiannual while their YouTube account is quarterly.

### 2.2 Catalog membership (verified, name-matched across 929 writers)

- **430 writers (~46%)** have both Mechanical and YouTube accounts.
- **151 writers (~16%)** are Mechanical-only.
- **348 writers (~37%)** are YouTube-only.

⇒ "Required statement types per writer" is **not** publisher-wide (as the demo PRD §4 assumed). It is a **per-writer attribute** that the system must store and validate against.

### 2.3 File naming and pairing

```
Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf     ← statement of record
Ben_PUB25H2_CSJ002_Javier Solis (Mechanical Royalties).xlsx      ← line-item detail
```

- Pattern: `Ben_<PERIOD>_<ACCOUNT_CODE>[ -_]<Display Name> (<Royalty Type>).<ext>`. Account codes may carry a `-New` suffix (re-contracted writers get a second account, e.g. `C00739-New - Swifty Blue NEW`). Filename parsers must treat the code as `[A-Za-z0-9]+(?:-New)?` — a greedy or hyphen-naive regex mis-keys these (this bit us during analysis).
- Account code conventions: `CSJ###`/`JN####` = Mechanical; `C#####`(+letter suffix for sub-accounts) = YouTube; `CS####` = house/special; `CPJ###` = house performance.
- Every PDF should have a matching XLSX and vice versa. Known legitimate exceptions, both **house accounts**: `CPJ001` (Regalias Digitales performance royalties, PDF-only) and `CS0001` (Regalias Digitales YouTube, XLSX present, PDF missing in 2025H2).

### 2.4 XLSX detail schema (sheet `Blad1`)

Mechanical columns:
`Period, Beneficiary, Name, SongCode, SongTitle, Country, Channel, IncomeSource, IncomeType, Price, CommissionRate%, RBP, Rate_Applied, WrtierSplit% (sic), BenSplit%, Units, Earnings`

YouTube columns:
`Period, Beneficiary, Name, AssetID, CustomID Client, SongTitle, Country, Channel, IncomeSource, IncomeType, Price, CommissionRate%, RBP, Rate_Applied, ContPer, BenSplit%, Units, Earnings`

Parsing constraints (all observed in production files):

1. **The last data row is a grand-total row** — every cell null except `Earnings`. It must be excluded from line-item sums (or you double every writer's earnings) but **captured** as the embedded control total.
2. **Some files have a blank first row** before the header (2 of 2,612: `JN0080` 2026H1, `C00139` 2025Q4). Header detection must scan for the row starting with `Period`, not assume row 1.
3. `Earnings` is net of commission (`RBP = Price × (1 − CommissionRate%)`, `Earnings ≈ RBP × splits × Units`).
4. `SongTitle` can be numeric (e.g. `118`), `SongCode` can be int or string. Treat all identity fields as strings.
5. Typo `WrtierSplit%` is in the real header — map it, don't "fix" it.
6. Files range from a handful of rows to ~37k rows; a semiannual batch is ~1,100–1,500 files. Parsing must be an async background job, not a request-scoped operation.

### 2.5 PDF account summary (statement of record)

Page 2 carries an Account Summary. Two layout generations exist (older "Total payable amount" 4-line form; newer 9-line form). The full model:

```
payable = calculated
        − recouped                  (advance recoupment, shown negative)
        − reserve_taken + reserve_released
        + carried_forward            (below-threshold balance from prior period)
        + payable_prev               (prior payable not yet settled)
        − settlement_paid
```

Verified: **XLSX line-item sum == PDF "Royalties calculated" to the cent for 2,609/2,611 paired statements** (2 differ by ≤ $1.02 — rounding). The PDF is the *payment* of record; the XLSX is the *earnings* of record; the validation system's job is to prove they agree and that the cross-period ledger flows.

Statement letter (page 1) carries the cheque line: `For your payable amount you will find enclosed a cheque of USD X` — usable as a third cross-check.

PDF text extraction artifacts: `pdftotext -layout` inserts spaces inside words (`Payable am ount`, `pe riod`). Patterns must tolerate `\s*` inside keywords.

### 2.6 Real-world states a statement can legitimately be in

| State | Frequency in dataset | Meaning |
|---|---|---|
| Paid out | ~2,060 | payable > 0 |
| Below threshold, carried forward | 480 | calculated > 0 but payable 0; balance rolls to next period |
| Fully recouped against advance | 12 | e.g. OMB Peezy: $38,009 earned, 100% applied to advance, $20,509 advance still open |
| Zero earnings | 57 | active account, no income this period |

⇒ payable == 0 is **normal** and must never alone be an error; what matters is that the zero is *explained* (threshold, recoupment, or zero earnings) and the resulting balance is **tracked into the next period**.

### 2.7 Known inconsistencies in the historical data (validation must catch these going forward)

- **Dropped with balance**: 6 YouTube accounts present in 2025H2 with small unpaid carryforward balances (`chaka demus` $2.66, `martin finn purcell` $39.28, etc.) have **no 2026H1 statement**. Money is orphaned. Each was small this time; the rule must exist because next time it might not be.
- **Name drift / probable duplicates** across periods and catalogs: `Los Tucanes De Tijuana`↔`DeTijuana`, `ameria bb`↔`amaria bb` (dropped + "new" writer are the same person), `kerwin du bois`↔`kerwin dubois`, `sob x rbe`↔`sobxrbe`, `pimp tobi`↔`pimptobi`, `william luna`↔`williamluna`, `la furia oaxaquena`↔`oaxaqueña`, `jose (pepe) gutierrez`↔`jose pepe gutierrez`, `federico`↔`frederico yesan rojas`, `monk music group (che - dana - nate)`↔`(che-dana-nate)`, `pesa entertainment`↔`pesa entertainment inc`, `maikel de la calle`↔`maikel delacalle`, `r & beats`↔`r-beatz`. Note also real *near*-collisions that are **different people** (`Angel Martinez` CSJ005 vs `Angie Martinez` CSJ006) — fuzzy matching must propose, never auto-merge.
- **`-New` duplicate accounts** in 2026H1 (J Swey, Swifty Blue, AmpLive/Anthony Anderson, Payroll Giovanni): old and new accounts coexist in the same period; the writer portal must show them unified.
- **House accounts mixed into writer batches** (`CS0001`, `CPJ001`, accounts labeled "100% to Regalias Digitales") — must be excluded from writer-facing distribution and from "writers paid" metrics.

---

## 3. Goals / Non-goals

### Goals

1. **Ingest and sort** a raw distribution drop — the admin uploads all the loose PDF+XLSX files unsorted in one place; the system sorts them by writer, period and statement type into structured storage: batches → statements → line items, with original files retained and downloadable.
2. **Validate** every batch with a tiered rules engine and present findings in the admin panel as blockers/warnings with explanations.
3. **Gate distribution**: the "Distribute" action is disabled until the batch has zero unresolved blockers and all warnings are acknowledged or waived (with audit trail).
4. **Canonical writer identity**: one writer profile owning N beneficiary accounts across catalogs, cadences, and `-New` re-signings; alias-aware matching with human review.
5. **Cross-period continuity checks**: carryforward and advance figures parsed from consecutive statements must chain (period N+1's opening == period N's closing). This is not money management — it's the strongest available proof that no file is missing from the set.
6. **Writer portal**: each writer sees their distributed statements (all their accounts unified), with PDF/XLSX downloads and an earned-vs-paid explanation (threshold/recoupment).
7. **Backfill** the six existing batches as historical data and seed the continuity baseline.

### Non-goals (this phase)

- **Anything payment-related.** Payments are executed externally and *precede* ingestion. The system records what statements say was paid; it never calculates, initiates, schedules, or reconciles actual money movement against bank activity.
- **Email notifications — deferred.** Distribution emails ("your statement is available") will come in a later iteration; the client email list hasn't been delivered yet and the backend has no outbound email service. Design keeps the door open (nullable `contact_email` on `writer`, distribution events recorded so a later email feature can backfill notifications), but no email functionality ships in this PRD's scope. Writers learn of new statements by visiting their portal.
- Currency conversion (dataset is 100% USD).
- Multi-publisher tenancy (single publisher: Regalias Digitales; design tables with `publisher_id` for later, hardcode one row now).
- Statement *generation* (we ingest processor output; we don't compute royalties from DSP reports).
- Editing line items (statements are immutable records; corrections arrive as replacement files → new statement version).

---

## 4. Users

| Persona | Needs |
|---|---|
| **Publisher admin / ops** (Steven + client ops team) | Upload a drop in minutes, see a red/yellow/green readiness board, fix or waive issues, hit Distribute with confidence, answer writer queries ("why was I paid $0?") from the ledger. |
| **Writer** | See statements per period, download PDF/XLSX, understand carryforward/recoupment, get notified when a new period is distributed. |
| **Auditor / accountant** (future) | Trace any payable back to line items, see who waived which validation finding and when. |

---

## 5. Data model (new tables, FastAPI/SQLAlchemy + Alembic on existing Postgres)

Existing `RevenueStatement`/`RevenueTransaction` serve the self-serve writer upload flow; **do not overload them**. This is a parallel, publisher-side domain:

```
publisher                 id, name ("Regalias Digitales, LLC"), default_fee_pct, payout_threshold_usd

writer                    id, publisher_id, canonical_name, status(active|offboarded),
                          contact_email (nullable — unused for now, reserved for the
                          deferred email feature; client list pending),
                          portal_user_id (FK → users, nullable until invited),
                          expected_catalogs (set: MECH, YT), cadence (semiannual|quarterly|mixed),
                          is_house_account (bool)

writer_alias              id, writer_id, alias_name, source(import|manual), created_at
                          -- seeded with §2.7 drift pairs; matching checks aliases first

beneficiary_account       id, writer_id, account_code (e.g. JN0261, C00650, C00739-New) UNIQUE,
                          catalog (MECH|YT|PERF), cadence, status(active|closed|superseded),
                          superseded_by (FK self, for -New chains), opened_period, closed_period

statement_batch           id, publisher_id, label ("YouTube 2026H1"), period_code (PUB26H1),
                          catalog, cadence, upload_id (drop it came from — batches are
                          auto-derived from uploaded files, never hand-created), uploaded_by, uploaded_at,
                          status (uploaded|parsing|parsed|validating|needs_review|approved|distributed|archived),
                          stats jsonb (file counts, totals, error counts), control_total numeric NULL
                          -- control_total: optional admin-entered expected payout for V-BATCH-9

statement                 id, batch_id, account_id, period_code, version int DEFAULT 1,
                          pdf_path, xlsx_path, statement_date,
                          -- parsed from PDF account summary:
                          calculated, recouped, reserve_taken, reserve_released,
                          carried_forward_in, payable_prev, settlement_paid, payable,
                          -- computed from XLSX:
                          detail_sum, embedded_total, line_count,
                          zero_pay_reason (paid|threshold_carryover|recouped|zero_earnings) NULL,
                          parse_status, UNIQUE(account_id, period_code, catalog, version)

statement_line            id, statement_id, row_no,
                          song_code, asset_id, custom_id, song_title, country, channel,
                          income_source, income_type, price, commission_pct, rbp,
                          rate_applied, writer_split_pct, ben_split_pct, units, earnings
                          -- superset of both schemas; nullable where catalog-specific
                          -- ~2–4M rows per semiannual run pair: bulk COPY insert, indexed on statement_id

account_ledger            id, account_id, period_code, opening_balance, earned, recouped,
                          reserves_delta, paid, settlement, closing_balance,
                          advance_opening, advance_closing
                          -- derived entirely from parsed statements (incl. the PDF
                          -- "Recoupments against advances" table); exists only to power
                          -- continuity validation (V-LEDG-*) and the portal explainer.
                          -- closing(N) must equal opening(N+1): rule V-LEDG-1/2

validation_run            id, batch_id, started_at, finished_at, rules_version,
                          blockers int, warnings int, infos int
validation_finding        id, run_id, rule_id (e.g. V-LEDG-1), severity (blocker|warning|info),
                          scope (batch|statement|account|writer), scope_ref,
                          message, details jsonb,
                          status (open|resolved|waived), waived_by, waived_reason, waived_at

distribution              id, batch_id (or period-level grouping of batches), distributed_by,
                          distributed_at, writer_count, total_payable
                          -- per-writer distribution records are derivable from statements;
                          -- a later email feature can backfill notifications from these rows
```

**Identity resolution flow**: on import, each filename's `account_code` is looked up in `beneficiary_account`. Known code → attach. Unknown code → fuzzy-match display name against `writer.canonical_name` + `writer_alias` (normalized: lowercase, strip diacritics/spaces/punctuation — catches 12 of the 14 real drift pairs); candidates ≥ threshold go to a **review queue** (`needs_review` finding V-BATCH-5), no auto-merge. Admin resolves: "same writer" (creates account under existing writer + alias) or "new writer" (creates both).

---

## 6. Import pipeline

Async job chain (FastAPI `BackgroundTasks` is insufficient for 1,500-file batches — use a worker; simplest fit for current stack: a `statement_import` job table polled by a worker process, or RQ/Celery if Redis is acceptable):

**Upload model: the admin does zero pre-sorting.** They select/drag *all* the loose files — hundreds or thousands of PDFs and XLSXs, any mix of periods, catalogs and writers — into one drop zone. Sorting is entirely the system's job: it derives the batches from the filenames, groups the files per writer, and reports what it found. The admin never creates a "batch" by hand; batches materialize from what was uploaded.

```
1. UPLOAD     POST /admin/uploads (loose multi-file; chunked, resumable for 1,500+ files)
              → store raw files under storage/statements/incoming/{upload_id}/
2. SORT       Parse every filename → (period, account_code, display_name, royalty_type, ext).
              Auto-create or attach to statement_batch per (period, catalog) found in the
              drop. Group files per account: pair the detail XLSX with its one summarizing
              PDF by (period, account_code). Move files to
              storage/statements/{period}/{catalog}/. Findings: unpairable files (XLSX
              without its summary PDF or vice versa), unparseable names, duplicates of
              already-ingested statements. (status=parsing)
3. PARSE      Per pair, idempotent & resumable:
              a. XLSX: locate header row (scan for 'Period' cell), map columns by name
                 (incl. 'WrtierSplit%'), stream rows, exclude+capture total row,
                 bulk-insert statement_lines, compute detail_sum/line_count.
              b. PDF: pdftotext pages 1–3, extract all account-summary fields with
                 whitespace-tolerant patterns + cheque line.
4. RESOLVE    Account → writer resolution (§5). Unknowns → review queue.
5. LEDGER     Stage account_ledger rows for the period from parsed figures.
6. VALIDATE   Run the rules engine (§7); persist validation_run + findings.
              (status=needs_review or approved-eligible)
```

Progress is observable: batch detail page polls `GET /admin/batches/{id}` for stage + counts (files parsed / failed / remaining). Re-running import on the same batch is safe (upsert by `(account, period, version)`).

---

## 7. Validation system (the core deliverable)

### 7.1 Architecture

A **declarative rules registry** in backend code: each rule = `{id, level, default_severity, scope, check(ctx) → findings[]}`. Rules run server-side against parsed data (never in the UI — the demo's hardcoded UI checks in `AdminStatementDetail.jsx` get replaced by API-driven findings). A `validation_run` snapshots results; findings carry stable identities (`rule_id` + `scope_ref`) so waivers survive re-runs. Severities are configurable per publisher later; ship sensible defaults now.

### 7.2 Rules catalog

**Level 1 — File integrity (per file/pair)**

| ID | Check | Severity |
|---|---|---|
| V-FILE-1 | Every PDF has its XLSX and vice versa (exceptions: accounts flagged `pdf_only`/`xlsx_only`, i.e. CPJ001, CS0001) | blocker |
| V-FILE-2 | Filename parses to (period, account_code, type); file period == batch period | blocker |
| V-FILE-3 | XLSX opens, header row found, all required columns mapped | blocker |
| V-FILE-4 | XLSX contains exactly one grand-total row, at the end | warning |
| V-FILE-5 | PDF account summary extracted: `calculated` and `payable` both found | blocker |
| V-FILE-6 | Duplicate file for same (account, period) in one drop | blocker |

**Level 2 — Statement math (per statement)**

| ID | Check | Severity |
|---|---|---|
| V-STMT-1 | `detail_sum == embedded_total` (±$0.02) | blocker |
| V-STMT-2 | `detail_sum == pdf.calculated` (±$0.02; historical data shows ≤$1.02 rounding — anything over $0.02 warns, over $5 blocks) | warning/blocker |
| V-STMT-3 | Payable identity: `payable == calculated − recouped − reserve_taken + reserve_released + carried_forward_in + payable_prev − settlement_paid` (±$0.02) | blocker |
| V-STMT-4 | `payable >= 0` (0 negative payables exist in 2,612 real statements; a negative one is an upstream error) | blocker |
| V-STMT-5 | If `payable == 0`: classify `zero_pay_reason`; unexplainable zero (calculated>0, no recoup, no carryforward out) | blocker |
| V-STMT-6 | Cheque-line amount (page 1) == payable | warning |
| V-STMT-7 | Commission rate within expected band per catalog (YT ~13–15%, MECH 0% observed); out-of-band rates | warning |
| V-STMT-8 | Line-level sanity: no negative units; `Earnings ≈ RBP × splits × units` on sampled rows | info |

**Level 3 — Ledger continuity (per account, cross-period)**

| ID | Check | Severity |
|---|---|---|
| V-LEDG-1 | `carried_forward_in(N) + payable_prev(N) == closing unpaid balance(N−1)` | blocker |
| V-LEDG-2 | Advance continuity: advance closing balance printed on statement N == opening balance on statement N+1; within a statement, `closing == opening − recouped + new_advances ± adjustments` | blocker |
| V-LEDG-3 | Recouped amount ≤ advance opening balance stated on the same statement | warning |
| V-LEDG-4 | Threshold behavior: if `0 < before_tax < publisher.payout_threshold` then payable should be 0 with carryforward; if paid anyway → flag | warning |
| V-LEDG-5 | First-seen account with `carried_forward_in > 0` (history hole) | warning |

**Level 4 — Batch & roster completeness (the "is this drop complete?" gate)**

| ID | Check | Severity |
|---|---|---|
| V-BATCH-1 | **Roster coverage**: every `beneficiary_account` that is `active`, matches the batch's catalog+cadence, and appeared in the previous comparable period has a statement in this batch — or is explicitly `closed` with zero closing balance. This is the rule that catches the 2026H1 "dropped writers". | blocker |
| V-BATCH-2 | **Dropped with balance**: account absent from batch AND `closing_balance ≠ 0` or open advance ≠ 0 (catches chaka demus / martin finn purcell class of orphaned money) | blocker |
| V-BATCH-3 | **Catalog completeness per writer**: writer whose `expected_catalogs` includes both MECH and YT must have statements in both catalogs' batches for the period before period-level distribution (checked at period gate, not per batch) | blocker |
| V-BATCH-4 | **Cadence membership**: quarterly accounts must not appear in semiannual batches and vice versa, except writers flagged `cadence=mixed` (Canserbero, Gerencia, Lupita Vega) | warning |
| V-BATCH-5 | **Unknown account / new writer** appeared → entity-resolution review required | blocker until resolved |
| V-BATCH-6 | **Name drift**: statement display name ≉ canonical/alias names for the account (normalized fuzzy match) → propose alias | warning |
| V-BATCH-7 | **Probable duplicate writers** within batch: distinct accounts whose normalized names collide (AmpLive vs AmpLive (New)) and aren't linked via `superseded_by` | warning |
| V-BATCH-8 | **House accounts** present (CS*, CPJ*, "100% to Regalias") → auto-tag `is_house_account`, exclude from distribution + writer counts | info |
| V-BATCH-9 | **Control total**: if admin entered the processor's expected payout total, `Σ payable == control_total` (±$1) | blocker |
| V-BATCH-10 | **Plausibility vs prior period**: batch totals and account count within configurable % band of previous comparable period (2025H2→2026H1 real deltas: +3.7% mech, +60% YT — default band ±75%, warn only) | warning |

### 7.3 Severity & waiver semantics

- **Blocker**: distribution button disabled while any blocker is `open`. Resolved by fixing data (re-upload → new statement `version`) or **waiving** with required reason text; waivers are per-finding, logged (`waived_by`, `waived_at`, reason), and surface in the distribution record.
- **Warning**: must be individually or bulk **acknowledged** before distribution; acknowledgment logged.
- **Info**: visible, no gate.

### 7.4 The readiness gate (period-level)

Distribution operates on a **period** (e.g. "H1 2026"), which aggregates its batches (MECH + YT + any quarterly batch landing in the window). The readiness board is **per-writer and granular**: one row per writer, and within it one cell per statement type that writer is *supposed* to receive (from `expected_catalogs` + `cadence` on their profile) — each cell showing detail XLSX ✓/✗ and summary PDF ✓/✗ independently. A writer is **Ready** only when every expected statement type has both files present, parsed, and reconciled. Writers who don't receive a type show "n/a", never a false missing. This is the production version of the demo banner "H2 2025 · 3/5 writers have all statements in". The **Distribute** action requires:

1. All expected batches for the period are `approved` (0 open blockers, 0 unacknowledged warnings).
2. V-BATCH-3 cross-catalog check passes at period scope.
3. Admin confirmation modal restating: writer count, total payable (informational — already paid externally), and waived findings count.

Distribution then: marks statements `distributed`, exposes them to portal users, fires in-app notifications (existing `/notifications` infra), and freezes the batch (immutable; corrections = new versions with re-validation). Email notification is deferred (§3 non-goals).

---

## 8. Admin panel UX (maps to existing RD screens)

| Screen (existing file) | Change |
|---|---|
| `AdminStatements.jsx` | Becomes **Batches** list backed by `GET /admin/batches`; status chips follow batch lifecycle; row → batch detail. |
| `AdminStatementUpload.jsx` / `AdminUploadModal.jsx` | One drop zone for the whole unsorted dump: wire to `POST /admin/uploads` (loose multi-file, chunked). After upload, show the **sort result grouped by writer**: "1,338 files → 2 batches (Mechanical 2026H1, YouTube 2026H1) → 612 writers · 7 files couldn't be sorted (listed)". Keep filename auto-detect preview, but server-side parse is authoritative. Show async progress (sorted/parsed/failed/remaining). |
| `AdminStatementDetail.jsx` | Becomes **Batch detail**: validation findings grouped by rule level with severity badges, fix/waive/acknowledge actions, per-statement drill-down (account summary fields vs detail sum, line-item table paginated). Replace hardcoded issue list with `GET /admin/batches/{id}/findings`. |
| `AdminWriters.jsx` / `AdminWriterDetail.jsx` | Writer profile: canonical name, aliases, accounts (code, catalog, cadence, status, `-New` chains), expected catalogs, ledger timeline (per period: earned → recouped → carried → paid), advances. Entity-resolution **review queue** tab for V-BATCH-5/6/7 findings. |
| `AdminDistributions.jsx` / `AdminDistributionDetail.jsx` | Period-level readiness board + gated Distribute button; per-writer payout table fed by real statements; distribution log with waiver summary. |
Demo personas/localStorage (`rd_distribution_state_v1`, `distributionState.js`, `redZedRealData.js`) are retired once parity is reached; keep behind a `REACT_APP_DEMO_MODE` flag during transition.

### 8.1 Writer portal (the distribution target)

The portal is strictly a **read-only consumer of distributions**: a writer sees nothing for a period until the admin's Distribute action publishes it, and then sees everything for their accounts at once.

- **Earnings view** (`/earnings`, existing): period chips list only **distributed** periods; all aggregates (total revenue, top works, by-source, by-territory) computed from `statement_line` data across **all of the writer's accounts unified** — a writer with `JN0261` + `C00650` + a `-New` account sees one combined picture, with an optional per-catalog breakdown (Mechanical vs YouTube).
- **Statements view** (`WriterStatements.jsx`, existing): one row per statement (catalog + period + payable) with **PDF and XLSX download** (`GET /portal/statements/{id}/download?type=pdf|xlsx`, auth: owning writer only).
- **Earned vs paid explainer**: when payable ≠ calculated, the portal shows the account-summary waterfall (earned → recouped → carried forward → paid) so "why was I paid $0?" is self-serve — threshold carryovers (480 real cases) and recoupments (12 real cases) are the norm, not the exception.
- **Refresh on distribution**: publication flips visibility transactionally and fires an in-app notification. No portal caching may outlive a distribution event.
- **Mixed-cadence writers** (Canserbero, Gerencia, Lupita Vega) see quarterly and semiannual periods interleaved on the same timeline, newest first.

---

## 9. API surface (new FastAPI router `admin_statements.py`, admin-role gated)

```
POST   /admin/uploads                          dump loose files (chunked/resumable) → upload id (202)
GET    /admin/uploads/{id}                     sort result: derived batches, files per writer, unsortable files
GET    /admin/batches?period=&status=          list (batches are auto-derived from uploads)
GET    /admin/batches/{id}                     detail + pipeline progress + stats
POST   /admin/batches/{id}/revalidate          re-run rules
GET    /admin/batches/{id}/findings?severity=  findings list
POST   /admin/findings/{id}/waive              {reason}
POST   /admin/findings/{id}/acknowledge
GET    /admin/batches/{id}/statements?q=       statement list w/ key figures
GET    /admin/statements/{id}                  account summary + validation + ledger context
GET    /admin/statements/{id}/lines?page=      line items
GET    /admin/periods/{code}/readiness         per-writer readiness board
POST   /admin/periods/{code}/distribute        the gate (409 w/ blocking findings if not ready)
GET    /admin/writers / {id} / PATCH           roster, expected_catalogs, cadence, merge/alias ops
POST   /admin/writers/resolve                  entity-resolution decisions
GET    /portal/statements                      writer-facing, distributed only
GET    /portal/statements/{id}/download        file streaming (auth: owning writer)
```

Auth: replace the RD demo's hardcoded `ADMIN_EMAILS` with a real `role` on `User` (`admin` | `writer`); writer portal accounts linked via `writer.portal_user_id` (invite flow can be Phase 4).

---

## 10. Backfill plan (the six existing batches)

1. Seed `publisher`, run importer on all six folders in chronological order (2025H2 mech+YT → 2025Q4 → 2026H1 mech+YT → 2026Q2) so ledger continuity validates naturally.
2. First import auto-creates writers/accounts; run entity resolution with the §2.7 alias seed list preloaded so the 14 drift pairs merge correctly; manually resolve the review queue (expect ~60–80 items: 56 new mech + 72 new YT accounts in 2026H1, minus auto-matches).
3. Expected outcomes (acceptance fixtures, from verified analysis): 2,612 statements; Σ payable $8,919,798.85; V-STMT-2 warnings exactly 2 (No Vacation 26H1 $1.02, Kiboomers 25H2 $0.59); V-FILE-1 exceptions only CPJ001/CS0001; V-BATCH-2 fires on the 6 dropped-with-balance YouTube accounts; V-STMT-4 fires zero times.
4. Mark all six batches `distributed` retroactively (they were already paid) with a `historical_backfill` flag; portal immediately shows two periods of history per writer. The flag suppresses the notification step — backfills must not blast notifications for periods writers already know about.

---

## 11. Phasing

**Phase 1 — Ingestion & integrity (foundation)**
Models + migrations, file storage, loose-file upload + sorting, import pipeline stages 1–3, rules V-FILE-* and V-STMT-1/2/3/4, batch list/detail screens with findings. *Exit: drag the 1,516 loose 2026H1 YouTube files into the drop zone, watch them sort into 758 writer statements and reconcile to the cent.*

**Phase 2 — Identity & roster**
Writer/account/alias models, entity resolution + review queue, V-BATCH-1/2/5/6/7/8, writer admin screens. *Exit: full backfill runs clean; review queue empties; dropped-with-balance accounts flagged.*

**Phase 3 — Continuity & gate**
account_ledger (derived), V-LEDG-*, V-STMT-5/6, V-BATCH-3/4/9/10, period readiness board, waiver/acknowledge workflow, gated Distribute, batch freeze + versioning. *Exit: distribution of a period is impossible with a planted missing writer; possible after waiving with reason.*

**Phase 4 — Writer portal**
Portal statements + downloads + earned-vs-paid explainer, in-app notifications, writer invite/link flow, retire demo mocks. *Exit: the demo PRD §2 story runs end-to-end on real backend with the real RedZed files.*

**Later (out of this PRD)** — email notifications: client email list import + matching, statement-available emails on distribution, catch-up sends for past distributions. Blocked on the client delivering the email list and an outbound email provider decision.

---

## 12. Risks & open questions

1. **Worker infrastructure**: current backend has no job queue. Decision needed: simple DB-polled worker process (no new infra) vs Celery/RQ (+Redis). Recommendation: DB-polled worker now; the interface (§6) doesn't change if swapped later.
2. **Line-item volume**: ~4–6M rows per year at current roster size. Postgres is fine with bulk COPY + partitioning by period if needed; don't ORM-insert row-by-row.
3. **PDF layout drift**: the processor has already shipped two summary layouts; extraction must be pattern-bank-based with a "layout unrecognized" blocker (V-FILE-5) rather than silent misses.
4. **Threshold value**: payout threshold is observable (~$25–50?) but not explicitly stated in the data — confirm with the processor; V-LEDG-4 ships disabled until confirmed.
5. **Quarterly↔semiannual interaction**: do quarterly YouTube payouts for mixed-cadence writers (Canserbero) ever overlap amounts with semiannual mechanical? Data says no (separate accounts), but confirm the business rule for V-BATCH-4 exceptions.
6. **Performance-royalty catalog**: `CPJ001` hints at a third catalog (Performance) at house level; if writer-level performance statements appear later, `catalog` enum already allows it.
7. **Client email list is outstanding** (email feature deferred accordingly). When it lands and emails come back into scope: matching ~929 writer names against the client's spreadsheet will surface the same name-drift issues as §2.7 and needs human review time, and the backend has no outbound email service yet (SMTP/provider decision required). Distribution records are kept (§5 `distribution`) so the future email feature can backfill notifications for past distributions.

---

## Appendix A — Verified dataset facts cheat-sheet

- 6 batches, 2,612 statements, 1,374 accounts, ~929 writers, $8,909,380 calculated / $8,919,799 payable.
- Catalog split: 430 both / 151 mech-only / 348 YT-only. Cadence: 21 quarterly-only writers, 3 mixed.
- Reconciliation: 2,609/2,611 exact to the cent; 2 rounding diffs ≤ $1.02.
- Zero-pay: 480 threshold carryforwards, 12 full recoupments, 57 zero-earning.
- Churn 25H2→26H1: mech −9/+56, YT −11/+72; 6 dropped YT accounts left balances.
- Total recouped across dataset: $149,133. Negative payables: 0.
- Analysis artifacts: `royalty_summary.csv` (per-statement XLSX sums), `pdf_full.csv` (all PDF account-summary fields) in the data folder.
