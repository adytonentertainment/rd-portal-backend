# Database schema

The schema this portal uses, as the models define it. Generated from
`app/models/` rather than written by hand, so it cannot drift from the code
without someone noticing.

## Read this first: the schema has two halves

This codebase also serves the Verax SaaS product, and both sets of tables live
in the same database. **The publisher portal uses only the lower-case tables.**

The CamelCase tables — `User`, `Client`, `Subscription`, `ACRCloudScan`,
`Agreement`, `Case`, `DocuSignPublishingAgreement`, `RevenueStatement`,
`RevenueTransaction`, `Songs`, `UserCatalog`, `Notification` and the rest — are
Verax features and carry no publisher data. The single exception is `User`,
which is the login record and **is** used: a portal login is a `User` joined to
a `contact`.

An incoming developer who starts from a table list and works inwards will
otherwise spend a day on tables this product never touches.

## The shape of it

```
publisher
  └── writer ──────────── writer_alias
        │                 writer_contact ─── contact ─── User (login)
        └── beneficiary_account
                  └── statement ─── statement_line
                        │
                        ├── statement_batch ─── statement_upload
                        │        └── validation_run ─── validation_finding
                        └── distribution  (what a writer can actually see)
```

**The one invariant worth knowing.** Ownership of a statement is resolved
through `beneficiary_account.writer_id` — the account's CURRENT owner — and
never through `distribution.writer_id`. The latter records who a statement was
published to at the time and is frozen at publish, deliberately, as an audit
fact. Accounts get re-pointed afterwards (a client-list import correcting who
an account belongs to) and nothing rewrites those old rows, so scoping a read
on `distribution.writer_id` serves the PREVIOUS owner their successor's
royalties. Every portal read joins through the account for this reason.

## Terminology

- **writer** — a client of the publisher; what RD calls a client. Not
  necessarily one person: a band, an estate or a company is one writer.
- **contact** — an email address with portal access. A writer can have several
  (manager, attorney, business manager), each authenticating separately.
- **beneficiary_account** — the account a statement is issued against. One
  writer can hold several, typically one per catalogue (YouTube, mechanical).
- **statement** — one period's royalties for one account: a PDF summary and an
  XLSX detail sheet.
- **distribution** — the act of publishing a statement to a writer's portal.
  A statement exists before it is distributed; only distributed statements are
  visible to the writer.

## Tables

### `publisher`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `name` | VARCHAR | required, unique |
| `default_fee_pct` | NUMERIC(5, 2) |  |
| `payout_threshold_usd` | NUMERIC(14, 6) |  |

### `writer`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `publisher_id` | INTEGER | FK -> `publisher.id`, required |
| `canonical_name` | VARCHAR | required |
| `status` | VARCHAR(10) | required |
| `contact_email` | VARCHAR |  |
| `portal_user_id` | INTEGER | FK -> `User.id` |
| `expected_catalogs` | JSON |  |
| `cadence` | VARCHAR(10) |  |
| `is_house_account` | BOOLEAN | required |
| `publisher_owned` | BOOLEAN | required |
| `awaiting_first_statement` | BOOLEAN | required |
| `is_client` | BOOLEAN | required |
| `is_commission_partner` | BOOLEAN | required |
| `kind` | VARCHAR(18) |  |
| `payee_name` | VARCHAR |  |
| `preferred_language` | VARCHAR(2) |  |

### `writer_alias`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `writer_id` | INTEGER | FK -> `writer.id`, required |
| `alias_name` | VARCHAR | required |
| `source` | VARCHAR(6) | required |
| `created_at` | DATETIME | required |

### `contact`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `email` | VARCHAR | required, unique |
| `display_name` | VARCHAR |  |
| `preferred_language` | VARCHAR(2) |  |
| `user_id` | INTEGER | FK -> `User.id` |
| `created_at` | DATETIME | required |

### `writer_contact`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `writer_id` | INTEGER | FK -> `writer.id`, required |
| `contact_id` | INTEGER | FK -> `contact.id`, required |
| `role` | VARCHAR(7) | required |
| `user_id` | INTEGER | FK -> `User.id` |

### `beneficiary_account`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `writer_id` | INTEGER | FK -> `writer.id`, required |
| `account_code` | VARCHAR | required, unique |
| `display_name` | VARCHAR |  |
| `catalog` | VARCHAR(4) |  |
| `cadence` | VARCHAR(10) |  |
| `status` | VARCHAR(10) | required |
| `superseded_by` | INTEGER | FK -> `beneficiary_account.id` |
| `opened_period` | VARCHAR |  |
| `closed_period` | VARCHAR |  |
| `pdf_only` | BOOLEAN | required |
| `xlsx_only` | BOOLEAN | required |

### `statement_upload`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `uploaded_by` | INTEGER | FK -> `User.id` |
| `uploaded_at` | DATETIME | required |
| `file_count` | INTEGER | required |
| `status` | VARCHAR(10) | required |
| `stats` | JSON |  |
| `claimed_at` | DATETIME |  |
| `claimed_by` | VARCHAR |  |

### `statement_batch`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `publisher_id` | INTEGER | FK -> `publisher.id` |
| `label` | VARCHAR | required |
| `period_code` | VARCHAR | required |
| `catalog` | VARCHAR(4) | required |
| `cadence` | VARCHAR(10) |  |
| `upload_id` | INTEGER | FK -> `statement_upload.id` |
| `uploaded_by` | INTEGER | FK -> `User.id` |
| `uploaded_at` | DATETIME | required |
| `status` | VARCHAR(12) | required |
| `stats` | JSON |  |
| `control_total` | NUMERIC(14, 6) |  |

### `statement`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `batch_id` | INTEGER | FK -> `statement_batch.id`, required |
| `account_id` | INTEGER | FK -> `beneficiary_account.id`, required |
| `period_code` | VARCHAR | required |
| `version` | INTEGER | required |
| `pdf_path` | VARCHAR(500) |  |
| `xlsx_path` | VARCHAR(500) |  |
| `statement_date` | DATE |  |
| `calculated` | NUMERIC(14, 6) |  |
| `recouped` | NUMERIC(14, 6) |  |
| `reserve_taken` | NUMERIC(14, 6) |  |
| `reserve_released` | NUMERIC(14, 6) |  |
| `carried_forward_in` | NUMERIC(14, 6) |  |
| `payable_prev` | NUMERIC(14, 6) |  |
| `settlement_paid` | NUMERIC(14, 6) |  |
| `payable` | NUMERIC(14, 6) |  |
| `before_tax` | NUMERIC(14, 6) |  |
| `payable_this` | NUMERIC(14, 6) |  |
| `carried_forward_out` | NUMERIC(14, 6) |  |
| `cheque_amount` | NUMERIC(14, 6) |  |
| `detail_sum` | NUMERIC(14, 6) |  |
| `embedded_total` | NUMERIC(14, 6) |  |
| `line_count` | INTEGER |  |
| `zero_pay_reason` | VARCHAR(19) |  |
| `parse_status` | VARCHAR(7) | required |
| `parse_error` | VARCHAR |  |

### `statement_line`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `statement_id` | INTEGER | FK -> `statement.id`, required |
| `row_no` | INTEGER | required |
| `song_code` | VARCHAR |  |
| `asset_id` | VARCHAR |  |
| `custom_id` | VARCHAR |  |
| `song_title` | VARCHAR |  |
| `country` | VARCHAR |  |
| `channel` | VARCHAR |  |
| `income_source` | VARCHAR |  |
| `income_type` | VARCHAR |  |
| `price` | NUMERIC(14, 6) |  |
| `commission_pct` | NUMERIC(9, 6) |  |
| `rbp` | NUMERIC(14, 6) |  |
| `rate_applied` | NUMERIC(14, 6) |  |
| `writer_split_pct` | NUMERIC(9, 6) |  |
| `ben_split_pct` | NUMERIC(9, 6) |  |
| `units` | NUMERIC(14, 4) |  |
| `earnings` | NUMERIC(14, 6) |  |

### `distribution`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `statement_id` | INTEGER | FK -> `statement.id`, required |
| `writer_id` | INTEGER | FK -> `writer.id`, required |
| `batch_id` | INTEGER | FK -> `statement_batch.id`, required |
| `period_code` | VARCHAR | required |
| `catalog` | VARCHAR(4) | required |
| `published_at` | DATETIME | required |
| `published_by` | INTEGER | FK -> `User.id` |
| `portal_visible` | BOOLEAN | required |
| `superseded_by` | INTEGER | FK -> `distribution.id` |
| `gate_snapshot` | JSON |  |

### `portal_invite`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `writer_id` | INTEGER | FK -> `writer.id`, required |
| `email` | VARCHAR | required |
| `token_hash` | VARCHAR(64) | required, unique |
| `role` | VARCHAR(7) | required |
| `invited_by_user_id` | INTEGER | FK -> `User.id` |
| `is_admin_invite` | BOOLEAN | required |
| `created_at` | DATETIME | required |
| `expires_at` | DATETIME | required |
| `accepted_at` | DATETIME |  |
| `revoked_at` | DATETIME |  |
| `delivery_status` | VARCHAR(16) | required |
| `delivery_error` | VARCHAR |  |
| `sent_at` | DATETIME |  |

### `validation_run`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `batch_id` | INTEGER | FK -> `statement_batch.id`, required |
| `started_at` | DATETIME | required |
| `finished_at` | DATETIME |  |
| `rules_version` | VARCHAR |  |
| `blockers` | INTEGER | required |
| `infos` | INTEGER | required |

### `validation_finding`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `run_id` | INTEGER | FK -> `validation_run.id`, required |
| `rule_id` | VARCHAR | required |
| `severity` | VARCHAR(7) | required |
| `scope` | VARCHAR(9) | required |
| `scope_ref` | VARCHAR |  |
| `message` | VARCHAR | required |
| `details` | JSON |  |
| `status` | VARCHAR(8) | required |
| `waived_by` | INTEGER | FK -> `User.id` |
| `waived_reason` | VARCHAR |  |
| `waived_at` | DATETIME |  |
| `acknowledged_by` | INTEGER | FK -> `User.id` |
| `acknowledged_at` | DATETIME |  |

### `client_import`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `publisher_id` | INTEGER | FK -> `publisher.id` |
| `filename` | VARCHAR |  |
| `sha256` | VARCHAR(64) |  |
| `uploaded_by` | INTEGER | FK -> `User.id` |
| `uploaded_at` | DATETIME | required |
| `status` | VARCHAR(14) | required |
| `row_count` | INTEGER | required |
| `diff` | JSON |  |
| `findings` | JSON |  |
| `stats` | JSON |  |
| `applied_at` | DATETIME |  |
| `applied_by` | INTEGER | FK -> `User.id` |

### `auth_throttle`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | PK |
| `scope` | VARCHAR(32) | required |
| `identifier` | VARCHAR(320) | required |
| `count` | INTEGER | required |
| `window_start` | DATETIME | required |
| `locked_until` | DATETIME |  |
| `updated_at` | DATETIME | required |

