# Deploying and operating the RD portal

**This system is deployed and live.** This file describes how it is wired, how
to operate it, and what is known to be outstanding. It is no longer a
from-scratch setup guide — the previous version described services and branches
that do not exist, which is worth knowing if you find an old copy.

---

## Where everything is

| | |
|---|---|
| Portal (writers + admin) | <https://royalties.regaliasdigitales.com> |
| API | <https://rd-portal-api.onrender.com> |
| Frontend repo | `adytonentertainment/rd-portal-frontend`, branch `main` |
| Backend repo | `adytonentertainment/rd-portal-backend`, branch `main` |

Render services: **rd-portal-api** (web), **rd-portal-web** (static site),
**rd-portal-db-2** (PostgreSQL 16, `basic_4gb`).

Both repos deploy on push to `main`. Local work is on `rd-portal-clean`, pushed
with:

```bash
git push rdportal rd-portal-clean:main
```

---

## How a deploy works

Render runs `python scripts/init_db.py` before the app starts.

It does **not** run `alembic upgrade head` directly, and that is deliberate: the
baseline revision `efa045b6d0ce` has an empty `upgrade()` (it was stamped onto a
database that already existed), so upgrading a *fresh* database dies two
revisions later with `relation "StatsCache" does not exist`. `init_db.py`
creates the schema from the models and stamps head when the database is empty,
and upgrades normally when it is not. It is idempotent.

Changing an environment variable does **not** redeploy on its own — trigger one,
or the new value is not picked up.

---

## Configuration that is easy to get wrong

**`EXTRA_CORS_ORIGINS` and `EXTRA_ALLOWED_HOSTS`** both fail confusingly. A
missing CORS origin appears only as a browser console error with nothing in the
server log; a missing allowed host makes every request return a bare 400. Both
must list the custom domain **and** the `onrender.com` hostname.

`EXTRA_ALLOWED_HOSTS` was `*` until recently, which disables host checking
entirely and allows host-header injection — including poisoned password-reset
links. Keep it pinned.

**Email.** Sending is Resend, from `royalties@mail.regaliasdigitales.com` on a
dedicated sending subdomain (`mail.regaliasdigitales.com`) so it is isolated
from RD's Google Workspace mail — none of its DNS records touch the apex, which
means a mistake here cannot break their business email.

| variable | purpose |
|---|---|
| `EMAIL_FROM` | visible sender; must match the verified Resend domain or sending fails outright |
| `EMAIL_REPLY_TO` | where replies go (`daniel@regaliasdigitales.com`) — the From address has no mailbox |
| `EMAIL_FROM_NAME`, `EMAIL_FOOTER_TEXT`, `EMAIL_SHOW_SOCIAL` | branding; unset falls back to the template's Verax defaults, which is wrong for a publisher |
| `EMAIL_ALLOWLIST` | **safety catch.** While set, mail goes only to these addresses and is dropped for everyone else. Use it on any deployment that is reachable but not ready to contact clients. Currently unset. |

**`ALLOW_PUBLIC_REGISTRATION=false`** makes the portal invite-only. Writers get
their login by redeeming a `PortalInvite` through `/portal/accept-invite`, which
creates the user itself and never touches registration.

> Note for anyone auditing `auth.py`: the beta passphrase and captcha checks are
> wrapped in `settings.mode == "development"`, and `mode` defaults to
> `"production"` — so those guards have never run in production. The
> registration gate was added unconditionally for that reason. **The same
> pattern still applies to the other checks and is worth reviewing.**

**`SENTRY_DSN`** enables error reporting. PII is off and request bodies, query
strings and secret-shaped keys are redacted before transmission — see
`app/monitoring/sentry.py`. Unset disables it entirely.

---

## Operating it

### Database access

External connections are **closed** (`ipAllowList: []`). The app reaches
Postgres over Render's internal network and is unaffected.

To run a script against production, either use the Render Shell on
**rd-portal-api**, or temporarily reopen:

```
ipAllowList: [{"cidrBlock": "0.0.0.0/0", "description": "temporary"}]
```

and close it again afterwards.

### Statement files

~2.0 GB at `/var/data/statements` on a 10 GB Render disk. Paths in the database
are stored **relative** to the storage root (`PUB26H1/YT/file.xlsx`), so
`STATEMENTS_STORAGE_ROOT` can move without rewriting rows.

A Render disk attaches to exactly one instance, so **rd-portal-api cannot scale
past one instance** while statement files live on it. Moving them to object
storage is the change that lifts that limit.

### Repairing mis-encoded titles

Source workbooks arrive with mis-encoded accents (`El CafÃ©`, `Adi¾s Amigo`) —
the corruption is in the cells as exported, not introduced here. The parser
repairs it on ingest. For data already stored:

```bash
python scripts/repair_statement_text.py --dry-run      # local / Render Shell
python scripts/repair_statement_text_remote.py --dry-run   # over a remote connection
```

Use the `_remote` variant over the network: the other loads every
`StatementLine` as an ORM object, which is 6.75M objects across the wire.
Both are idempotent and touch text only — never money or identifiers.

---

## Verifying a deploy

```bash
# 401 = route exists and requires auth. 404 or 400 means something is wrong.
curl -o /dev/null -w "%{http_code}\n" \
  https://rd-portal-api.onrender.com/admin/statements/reconcile
```

Then signed in as an admin at `/admin`:

- the ingestion audit reports `ok: true` with 0 violations
- total line earnings **$9,090,949.19** across **2,613 statements**
- open a writer's statement and confirm the PDF downloads — this is the check
  that catches a storage-root mistake

---

## Backups

`scripts/backup_db.sh` is SQLite-specific and does **not** cover Postgres.

- **Database:** Render's own backups. **Confirm the retention policy and test a
  restore.** This has not been done.
- **Statement files:** the disk is **not** backed up by Render. Snapshot it
  separately. No such snapshot currently exists.

---

## Known gaps

- **No edge protection.** No Cloudflare or WAF. Render provides basic DDoS
  absorption but no rules you control. Adding Cloudflare means moving the whole
  `regaliasdigitales.com` DNS zone — including Google Workspace MX — so it is an
  RD decision, not purely a technical one.
- **Rate limiting fails open.** Login lockout is durable (`auth_throttle` in
  Postgres, so it survives the restarts that used to erase it), but if the
  database is unreachable the check is skipped rather than blocking. The
  trade-off and its reasoning are documented in `app/services/throttle.py`.
- **`ENVIRONMENT=STAGING`** on the production service. Worth checking what that
  loosens.
- **DMARC is `p=none`** on `regaliasdigitales.com` — monitoring only, no
  reporting address. Worth tightening once sending has a few weeks of history;
  tightening it early will bounce legitimate invites.
- **Ingest worker runs in-process** (`INGEST_WORKER_IN_PROCESS=1`), so a large
  upload competes with API requests on one instance. Splitting it into its own
  Render service is the fix if it becomes a problem.
- **Python 3.11.9 on Render, 3.9.6 locally.** The test suite runs under 3.9.
- **Bulk distribution is lightly exercised.** Individual sends work; do one
  writer end-to-end before a bulk send.
