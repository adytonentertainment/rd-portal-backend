# System architecture

Written for a developer who has not seen this system before and needs to
understand, run and change it without asking anyone.

---

## What it is

A white-label royalty portal for a music publisher, with two audiences:

- **Admin panel** — publisher staff ingest royalty statements, check them,
  resolve which client each one belongs to, and publish them.
- **Writer portal** — each client signs in and sees only their own royalties,
  with territory and source breakdowns, and downloads the original PDF and XLSX.

The unit of work is a **statement**: one reporting period's royalties for one
beneficiary account, arriving as a PDF (the payment summary) and an XLSX (the
line detail).

## The pieces

| | |
|---|---|
| **API** | FastAPI (Python), one Render web service |
| **Frontend** | React (CRA), served as a Render static site |
| **Database** | PostgreSQL on Render |
| **Statement files** | ~2 GB on a Render disk at `/var/data/statements` |
| **Email** | Resend, from a dedicated sending subdomain |
| **Errors** | Sentry |

The frontend is a separate repository (`rd-portal-frontend`) and talks to the
API over HTTPS with a bearer token. There is no server-side rendering and no
session cookie; the token lives in `localStorage`.

## The statement pipeline

This is the part worth understanding first, because most of the backend exists
to serve it. An admin drops a folder of files and the rest is automatic.

```
upload ──► sort ──► parse ──► validate ──► distribute
```

**upload** (`app/services/statement_ingest/upload_stream.py`)
Files stream to disk as bytes arrive rather than being buffered — a real drop
is ~5,200 files and ~2 GB, and holding that in memory would exhaust the
instance. The client declares a manifest up front and the server refuses to
finalize until every declared file is present, so a partial drop can never be
ingested as a complete period. Interrupted transfers resume.

**sort** (`sorter.py`)
Filenames carry the account and period (`Ben_PUB26H1_C00616_Name (YouTube
Publishing).xlsx`). Files are paired into statements, batched by period and
catalogue, and anything unparseable, unpaired or duplicate is recorded rather
than silently dropped.

**parse** (`xlsx_parser.py`, `pdf_parser.py`)
The XLSX gives line detail; the PDF gives the payment waterfall. Parsing runs
in parallel across worker processes on Postgres (`INGEST_CHILD_PERSIST`), and
serially on SQLite, which takes no concurrent writers. Mis-encoded text from
the source is repaired here — see `text_repair.py`.

**validate** (`app/services/validation/`)
A rules engine checks each batch: that Σ line earnings reconciles to the PDF's
stated total, that the waterfall adds up, that nothing is missing a half.
Findings are blockers, warnings or info, and keep a stable identity across
re-runs so a waived finding stays waived.

**distribute** (`app/services/distribution/`)
Publishing creates a `distribution` row and emails the writer. Until then a
statement exists but no writer can see it.

## Who can see what

Two separate authorisation models, deliberately not shared:

**Admin** — `require_admin`, backed by role plus an `ADMIN_EMAILS` allow-list.
Full access.

**Writer portal** — every `/me/*` route resolves a `contact` from the login and
then the writers that login has claimed. A `writer_id` in a path is only ever
honoured inside that scope.

> **The invariant that matters.** Ownership resolves through
> `beneficiary_account.writer_id`, never `distribution.writer_id`. The latter is
> frozen at publish as an audit fact; accounts get re-pointed afterwards and
> nothing rewrites history. Scoping on the frozen value served the previous
> owner their successor's royalties. See `app/routers/portal.py`.

**Admin preview** — `/admin/writers/{id}/portal-view` serves a client's own
portal data to an admin. It reuses the portal's aggregation functions rather
than reimplementing them, so it cannot drift from the page it previews. It is
not impersonation: no client session is minted and the route has no write
counterpart.

## How writers get in

Invite-only. `ALLOW_PUBLIC_REGISTRATION=false` makes `/auth/user` refuse.

An admin issues a `portal_invite`; the writer redeems it at
`/portal/accept-invite`, which creates the login itself. Several contacts can
hold access to one writer, each with their own credentials, and only the
primary contact can invite others — access should not spread sideways without
the person whose royalties they are.

## Email

Sent through Resend from `royalties@mail.regaliasdigitales.com`, on a sending
subdomain separate from the publisher's corporate mail so its SPF and DKIM
records never touch their apex. `EMAIL_REPLY_TO` points replies at a real
mailbox, because the sending address does not receive.

Every email in the system funnels through `EMail.send_email`, which is also
where `EMAIL_ALLOWLIST` is enforced — a safety catch for a deployment that is
reachable but not yet ready to contact clients.

Invites are sent in the recipient's language (EN/ES).

## Rate limiting and lockout

Login lockout counters live in Postgres (`auth_throttle`), not in process
memory, because an in-memory counter is erased by every deploy and Render
restarts routinely — the limit read as protection and was close to nothing.

It **fails open**: if the counter cannot be read the request proceeds. A
database blip removing brute-force protection is better than one locking every
client out of their royalties. Edge protection is the layer that should survive
a database fault, and there currently is none.

## Where things live

```
app/
  routers/        HTTP surface; portal.py and statements_admin.py are the big ones
  services/
    statement_ingest/   upload → sort → parse, and text repair
    validation/         the rules engine
    distribution/       publishing to portals
    portal/             invites and their delivery
    throttle.py         durable login throttling
  models/         SQLAlchemy models (see docs/DATABASE.md)
  emails/         templates and provider adapters
  monitoring/     Sentry init and scrubbing
migrations/       Alembic
scripts/          operational one-offs
```

## Things that will surprise you

- **The schema has two halves.** Lower-case tables are this product;
  CamelCase ones belong to the Verax SaaS product sharing this codebase and are
  unused here, except `User`. See `docs/DATABASE.md`.
- **`settings.mode` defaults to `"production"`**, and several guards in
  `auth.py` are written as `if settings.mode == "development"`. Those guards
  have therefore never run in production. The registration gate was added
  unconditionally for that reason; the others are worth reviewing.
- **The ingest worker runs in-process** (`INGEST_WORKER_IN_PROCESS=1`), so a
  large upload competes with API requests on one instance.
- **A Render disk attaches to one instance**, so the API cannot scale
  horizontally while statement files live on it.
- **Statement file paths are relative** to `STATEMENTS_STORAGE_ROOT`, so the
  storage root can move without rewriting rows.

## Running it locally

See `DEPLOY.md` for deployed configuration, and `README.md` for local setup.
