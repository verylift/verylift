# Deploying with Portainer

This repo's maintainers deploy via [Kamal](https://kamal-deploy.org) (see the
README's [Deployment](../README.md#deployment) section), but Kamal assumes SSH
access to a VPS. If you're self-hosting on a home server or NAS behind
[Portainer](https://www.portainer.io/), you're deploying
[`docker-compose.selfhost.yml`](../docker-compose.selfhost.yml) directly as a
Portainer **stack** instead — this page walks through that path and the one
thing people most often trip on: forgetting the one-time migration step.

`docker-compose.selfhost.yml` starts a single `app` service by default,
running against a local SQLite file — no Postgres container required at all
for a single user or small friend group. Postgres is available as an opt-in
`postgres` profile for larger or multi-user deployments; see
[Advanced: Postgres instead of SQLite](#advanced-postgres-instead-of-sqlite)
below if you want it.

Kamal's `.kamal/hooks/pre-deploy` runs `migrate`, `collectstatic`, and
`seed_all` automatically on every deploy. Portainer has no equivalent hook —
`docker compose up` only starts the `app` service (and `db`, if you've opted
into the `postgres` profile), and nothing runs those three commands for you.
If you only ever start the stack and never run the step below, `app` boots
successfully, `/healthz` reports healthy (it only checks that the database is
reachable, not that any tables exist — see
[Why `/healthz` won't catch this](#why-healthz-wont-catch-this) below), and
every page that touches the database throws a 500. **This is the single most
common cause of "it deployed but everything 500s" reports** — check this
first, regardless of whether you're using SQLite or Postgres.

## 1. Deploy the stack

In Portainer: **Stacks → Add stack**, paste the contents of
[`docker-compose.selfhost.yml`](../docker-compose.selfhost.yml) into the web
editor (or point Portainer at this repo via its Git-repository stack option,
using that file as the compose path).

### Environment variables

`docker-compose.selfhost.yml` reads its secrets from `${VAR}` substitution and
an `env_file: .env` on the `app` service. Portainer's stack editor has an
**Environment variables** section (below the web editor, or in the Git-repo
stack's "Environment variables" tab) — add every variable below there rather
than trying to bake a `.env` file into the stack yourself. Portainer writes
what you enter there into a `.env` file next to the generated
`docker-compose.yml` on the host, which satisfies both the `${VAR}`
substitution *and* the `env_file: .env` directive, since both resolve
relative to that same directory.

Use [`example.env`](../example.env) as the master reference — every variable
documented there (Django core, database, OIDC, email, FitnessVolt, etc.)
applies here unchanged. For the default SQLite path, this is the entire
required list:

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | yes, no default | Django refuses to start without it. |
| `FIELD_ENCRYPTION_KEYS` | yes, no default | Generate with the command in `example.env`. Losing it makes stored Liftosaur API keys unrecoverable. |
| `ALLOWED_HOSTS` | yes, no default | Comma-separated hostname(s) you'll actually browse to, e.g. `verylift.home.example`. A mismatch here is a `DisallowedHost` 400, not a 500 — worth ruling out separately if you're chasing errors. |
| `ADMIN_URL_PATH` | no (defaults to `the-rack/`) | Set your own to avoid the shared default being probed. |

Example minimal set to paste into Portainer's environment-variables panel —
this alone is enough to deploy the whole stack, no database container needed:

```
SECRET_KEY=<generate a long random string>
FIELD_ENCRYPTION_KEYS=<generate with the Fernet command in example.env>
ALLOWED_HOSTS=verylift.home.example
```

Deploy the stack. `app` comes up on its own, storing its SQLite database file
on a named volume (`data`) so it survives container restarts/updates.

## 2. Run migrations, collectstatic, and seed data (one-time, and after every image update)

This is the step Kamal automates and Portainer doesn't. Do it once after
first deploy, and again after every subsequent image pull that includes
migrations.

**If you have shell access to the Docker host** (SSH, or Portainer's own host
if you can reach it directly) — this is the straightforward path:

```bash
cd /path/to/the/stack   # wherever Portainer materialized docker-compose.yml + .env
docker compose -f docker-compose.selfhost.yml exec app \
  python manage.py migrate --noinput

docker compose -f docker-compose.selfhost.yml exec app \
  python manage.py collectstatic --noinput

docker compose -f docker-compose.selfhost.yml exec app \
  python manage.py seed_all
```

`exec` runs the command inside the already-running `app` container — there's
no separate owner/scoped-role split to worry about on the SQLite path
(that split only exists for the Postgres profile; see below), so the
container you already have is the one that can run migrations.

**If you only have the Portainer web UI** (no host shell): open the stack's
`app` container in Portainer, use its **Console** feature (`>_` icon in the
container list) to get an interactive shell, and run the same three commands
manually:

```
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py seed_all
```

`seed_all` loads the reference data (Liftosaur lift/alias fixtures,
FitnessVolt lift aliases) the app expects to exist; skipping it after an
image update that renamed or added fixtures means scoring can go quiet with
no error, matching what the `pre-deploy` Kamal hook's comment warns about.

## Advanced: Postgres instead of SQLite

SQLite is fine for a single user or a small friend group, with one caveat:
it doesn't run in WAL mode, so concurrent writes from multiple gunicorn
workers can occasionally hit `database is locked` under real concurrent load
(tracked in [#16](https://github.com/verylift/verylift/issues/16)).
If that's a problem for you (or you'd simply rather run Postgres), the
`postgres` compose profile brings up a bundled Postgres container plus the
one-off `app-admin` service Kamal's owner role mirrors:

1. Add the Postgres variables from `example.env` to your stack's environment
   variables (`POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD`, etc. — see the
   comments there for what each controls).
2. Add `DATABASE_URL=postgres://verylift_app:<POSTGRES_APP_PASSWORD>@db:5432/verylift`
   to the same environment variables, using the `POSTGRES_APP_PASSWORD` value
   you just set. This is what switches `app` from SQLite to Postgres.
3. Redeploy the stack with the `postgres` profile active. Portainer's stack
   editor doesn't have a profile toggle, so this needs host shell access:
   ```bash
   docker compose -f docker-compose.selfhost.yml --profile postgres up -d db
   # wait for `db` to report healthy, then:
   docker compose -f docker-compose.selfhost.yml --profile postgres up -d app
   ```
4. Run the admin step (step 2 above) via `app-admin` instead of `exec`ing into
   `app` — `app-admin` connects as the Postgres **owner** role, which `app`'s
   scoped role deliberately cannot do (no `CREATE TABLE` privilege):
   ```bash
   docker compose -f docker-compose.selfhost.yml --profile postgres run --rm app-admin \
     python manage.py migrate --noinput
   docker compose -f docker-compose.selfhost.yml --profile postgres run --rm app-admin \
     python manage.py collectstatic --noinput
   docker compose -f docker-compose.selfhost.yml --profile postgres run --rm app-admin \
     python manage.py seed_all
   ```

### Existing volume? Provision the app role manually

`scripts/db/init-app-role.sh` (mounted into `/docker-entrypoint-initdb.d/`)
only runs automatically on a **fresh** `postgres_data` volume. If you're
pointing the `postgres` profile at a Postgres volume that already existed
before you added `POSTGRES_APP_USER`/`POSTGRES_APP_PASSWORD`, run it by hand
once:

```bash
docker compose -f docker-compose.selfhost.yml --profile postgres exec db \
  /docker-entrypoint-initdb.d/init-app-role.sh
```

Without this, `app`'s scoped role doesn't exist yet and every DB connection
from `app` fails outright.

## Why `/healthz` won't catch this

`/healthz` (see [`core/middleware.py`](../core/middleware.py)) only runs
`SELECT 1` against the database — it confirms the database is *reachable*,
not that migrations have run. A container can report healthy in Portainer's
UI while every page that queries a real table 500s because the table doesn't
exist yet. If you're seeing 500s on a stack that looks healthy, step 2 above
is the first thing to check.

## Troubleshooting checklist

- **Every page 500s, healthcheck is green** → migrations never ran. See
  step 2.
- **Using the `postgres` profile and the stack won't deploy / `db` exits
  immediately** → check Portainer's logs for `db`; an empty-password error
  from the Postgres image itself means `POSTGRES_PASSWORD` or
  `POSTGRES_APP_PASSWORD` is missing from the stack's environment-variables
  panel.
- **`app` container restarts in a loop, logs show a Django `ImproperlyConfigured`
  error at startup** → `SECRET_KEY` or `FIELD_ENCRYPTION_KEYS` is missing or
  empty.
- **400 Bad Request / "DisallowedHost"** → the hostname you're browsing to
  isn't in `ALLOWED_HOSTS`.
- **Login/password-reset emails never arrive, no error shown** → `EMAIL_HOST`
  is unset, which is a supported no-op state (see `example.env`) that falls
  back to printing the email to the `app` container's logs instead of
  sending it.
