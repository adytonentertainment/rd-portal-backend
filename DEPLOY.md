# Deploying VERAX to Render

Everything in this file has been tested locally against a real PostgreSQL 17
instance with the actual 6.75-million-row database, except the Render dashboard
steps themselves.

---

## 0. Before anything: push the code

Neither repository has been pushed. Render deploys from Git, so nothing here
works until it is on GitHub:

| repo | branch | state |
|---|---|---|
| `verax_backend` | `ralph/statement-data-integration` | 44+ commits ahead of `origin/main` |
| `verax_frontend RD` | `ralph/statement-admin-ui` | 123+ commits ahead of `origin/main` |

`render.yaml` targets `main` on both. Either merge these branches into `main`,
or change the `branch:` fields. Merging is cleaner — Render redeploys on every
push to the tracked branch, and you do not want that to be a working branch.

> This is also still the biggest risk to the work itself. The database is backed
> up twice daily; the code exists only on this laptop.

---

## 1. Create the services

Render Dashboard → **New** → **Blueprint** → point at the `verax_backend` repo.
It reads [`render.yaml`](render.yaml) and creates three things:

- **verax-db** — PostgreSQL 16, `basic-256mb`
- **verax-api** — the FastAPI service, with a 10 GB disk at `/var/data`
- **verax-frontend** — the React static site

The disk is what makes this survive a deploy. Render's normal filesystem is
wiped on every deploy; without it the statement PDFs vanish and every download
404s.

> A Render disk attaches to exactly one instance, so `verax-api` cannot scale
> past one instance while statement files live on it. That is fine at this size
> — moving files to S3/R2 is the change that lifts the limit.

## 2. Paste the secrets

`render.yaml` marks these `sync: false`, meaning Render asks you for them
instead of storing them in the repo. On **verax-api**:

| variable | value |
|---|---|
| `SECRET_KEY` | **generate a new one** — see below |
| `ADMIN_EMAILS` | `steven@adytonentertainment.com` |
| `EXTRA_CORS_ORIGINS` | the frontend URL, e.g. `https://verax-frontend.onrender.com` |
| `EXTRA_ALLOWED_HOSTS` | the API hostname, e.g. `verax-api.onrender.com` |
| `BASE_URL_FRONTEND` | `https://verax-frontend.onrender.com/` |
| `BASE_URL_BACKEND` | `https://verax-api.onrender.com/` |
| `EMAIL_API_KEY` | your Resend/SendGrid/Postmark key |
| `EMAIL_FROM` | e.g. `royalties@adytonentertainment.com` |

On **verax-frontend**: `REACT_APP_BACKEND_URL` = the API URL.

**Rotate `SECRET_KEY`.** The current one was written to plaintext logs (29,503
occurrences, since scrubbed). Every token ever signed with it should be treated
as compromised. Rotating logs everyone out once — trivial now, ugly after the
writers are onboarded.

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

`EXTRA_CORS_ORIGINS` and `EXTRA_ALLOWED_HOSTS` are easy to skip and fail
confusingly: a missing CORS origin shows up only as a browser console error with
nothing in the server log, and a missing allowed host makes every request return
a bare 400.

## 3. First deploy

The blueprint runs `python scripts/init_db.py` before the app starts.

It does **not** run `alembic upgrade head` directly, and that is deliberate: the
baseline revision `efa045b6d0ce` has an empty `upgrade()` (it was stamped onto a
database that already existed), so upgrading a *fresh* database dies two
revisions later with `relation "StatsCache" does not exist`. Verified locally.
`init_db.py` creates the schema from the models and stamps head when the
database is empty, and upgrades normally when it is not. It is idempotent —
Render runs it before every deploy.

At this point the app is up with an **empty** database.

## 4. Move the data across

Two pieces: the rows, then the files.

### 4a. Rows

Get the external connection string from the **verax-db** dashboard page, then
from this repo:

```bash
ENVIRONMENT=PRODUCTION python scripts/sqlite_to_postgres.py \
  --sqlite verax_livetest.db \
  --postgres "postgresql://...external connection string..."
```

Takes about **3.5 minutes** for 6.75M rows locally; over the network expect
longer. It prints per-table progress, resets the id sequences, and finishes by
comparing row counts on both sides. It exits non-zero if any table disagrees —
**do not point the app at a database where that check failed.**

Use `scripts/sqlite_to_postgres.py`, not the older
`scripts/migrate_sqlite_to_postgres.py`. The old one inserts one row at a time
(hours, for this data) and only converts booleans for three hardcoded tables, so
it dies partway through on `writer.is_client` with half the data loaded.

### 4b. Statement files

~2.0 GB under `storage/statements/`, which must end up at
`/var/data/statements` on the disk. Render has no direct file upload, so either:

- **rsync over SSH** — enable SSH on the service, then
  `rsync -avz storage/statements/ <ssh-target>:/var/data/statements/`
- **or** upload a tarball somewhere reachable and pull it from a Render Shell.

Paths in the database are stored **relative** to the storage root
(`PUB26H1/YT/file.xlsx`), so nothing needs rewriting — set
`STATEMENTS_STORAGE_ROOT=/var/data/statements` and the same rows resolve. This
was migrated by revision `x9y0z1a2b3c4`; all 2,613 statements were rewritten and
all 5,224 files verified to still resolve.

## 5. Verify before trusting it

```bash
# should be 401 (route exists, auth required) — not 404 or 400
curl -o /dev/null -w "%{http_code}\n" https://verax-api.onrender.com/admin/statements/reconcile
```

Then signed in as an admin, check `/admin`:

- **810 clients, 78 commission partners** (they overlap by 13 — never sum them)
- ingestion audit reports `ok: true` with 0 violations
- total line earnings **$9,090,949.19**
- open a writer's statement and confirm the PDF actually downloads — this is the
  check that catches a storage-root mistake

## 6. Backups

`scripts/backup_db.sh` is SQLite-specific and does **not** cover Postgres.
Render's own daily backups cover `basic-256mb`; confirm the retention on the
database page. The statement files on the disk are **not** backed up by Render —
snapshot them separately.

---

## Known gaps

- **Python 3.11 on Render vs 3.9.6 locally.** `render.yaml` pins `3.11.9`. The
  test suite has not been run under 3.11 — if the first build fails on a
  dependency, pin `PYTHON_VERSION` to `3.9.18` and revisit.
- **Ingest worker runs in-process** (`INGEST_WORKER_IN_PROCESS=1`), so a large
  upload competes with API requests on one 512 MB instance. Parsing is already
  parallel and streams uploads to disk, but a 5,200-file drop will be slower
  here than on the laptop. Splitting the worker into its own Render service is
  the fix if it becomes a problem.
- **No distributions have ever been run.** The send-to-writers path is untested
  against real data anywhere, deployed or not. Do one writer end-to-end before a
  bulk send.
