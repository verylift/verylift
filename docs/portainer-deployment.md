# Deploying with Portainer

This repo's maintainers deploy via [Kamal](https://kamal-deploy.org) (see the
README's [Deployment](../README.md#deployment) section), but Kamal assumes SSH
access to a VPS. If you're self-hosting on a home server or NAS behind
[Portainer](https://www.portainer.io/), you're deploying `docker-compose.prod.yml`
directly as a Portainer **stack** instead — this page walks through that path
and the two things people most often trip on: missing environment variables,
and forgetting the one-time migration step.

Kamal's `.kamal/hooks/pre-deploy` runs `migrate`, `collectstatic`, and
`seed_all` automatically on every deploy, as the Postgres **owner** role.
Portainer has no equivalent hook — `docker compose up` only starts the `db`
and `app` services, and `app` deliberately connects as a **scoped, DML-only**
role that cannot create tables. If you only ever start the stack and never
run the admin step below, `app` boots successfully, `/healthz` reports
healthy (it only checks that Postgres is reachable, not that any tables
exist — see [Why `/healthz` won't catch this](#why-healthz-wont-catch-this)
below), and every page that touches the database throws a 500. **This is the
single most common cause of "it deployed but everything 500s" reports** —
check this first.

## 1. Deploy the stack

In Portainer: **Stacks → Add stack**, paste the contents of
[`docker-compose.prod.yml`](../docker-compose.prod.yml) into the web editor
(or point Portainer at this repo via its Git-repository stack option, using
that file as the compose path).

### Environment variables

`docker-compose.prod.yml` reads its secrets from `${VAR}` substitution and an
`env_file: .env` on the `app`/`app-admin` services. Portainer's stack editor
has an **Environment variables** section (below the web editor, or in the
Git-repo stack's "Environment variables" tab) — add every variable below
there rather than trying to bake a `.env` file into the stack yourself.
Portainer writes what you enter there into a `.env` file next to the
generated `docker-compose.yml` on the host, which satisfies both the
`${VAR}` substitution *and* the `env_file: .env` directive, since both
resolve relative to that same directory.

Use [`example.env`](../example.env) as the master reference — every variable
documented there (Django core, database, OIDC, email, FitnessVolt, etc.)
applies here unchanged. At minimum, set:

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | yes, no default | Django refuses to start without it. |
| `FIELD_ENCRYPTION_KEYS` | yes, no default | Generate with the command in `example.env`. Losing it makes stored Liftosaur API keys unrecoverable. |
| `ALLOWED_HOSTS` | yes, no default | Comma-separated hostname(s) you'll actually browse to, e.g. `verylift.home.example`. A mismatch here is a `DisallowedHost` 400, not a 500 — worth ruling out separately if you're chasing errors. |
| `POSTGRES_PASSWORD` | yes, no default | Owner-role Postgres password. Compose refuses to start the `db` service at all without it (`POSTGRES_PASSWORD must be set`) — this fails loudly at stack-deploy time, not as a runtime 500. |
| `POSTGRES_APP_PASSWORD` | yes, no default | Scoped app-role password. Must differ from `POSTGRES_PASSWORD`. Same fail-loud behavior as above. |
| `ADMIN_URL_PATH` | no (defaults to `the-rack/`) | Set your own to avoid the shared default being probed. |

Example minimal set to paste into Portainer's environment-variables panel:

```
SECRET_KEY=<generate a long random string>
FIELD_ENCRYPTION_KEYS=<generate with the Fernet command in example.env>
ALLOWED_HOSTS=verylift.home.example
POSTGRES_DB=verylift
POSTGRES_USER=verylift
POSTGRES_PASSWORD=<strong password>
POSTGRES_APP_USER=verylift_app
POSTGRES_APP_PASSWORD=<different strong password>
```

Deploy the stack. `db` and `app` come up; `app-admin` does **not** — it's
gated behind `profiles: [admin]` specifically so Portainer/`docker compose up`
never boots a long-running container holding owner DB credentials (mirrors
why Kamal's `release` role is exec-only too). That's expected — you invoke
`app-admin` on demand in the next step, not as part of normal stack startup.

## 2. Run migrations, collectstatic, and seed data (one-time, and after every image update)

This is the step Kamal automates and Portainer doesn't. Do it once after
first deploy, and again after every subsequent image pull that includes
migrations.

**If you have shell access to the Docker host** (SSH, or Portainer's own host
if you can reach it directly) — this is the straightforward path:

```bash
cd /path/to/the/stack   # wherever Portainer materialized docker-compose.yml + .env
docker compose -f docker-compose.prod.yml --profile admin run --rm app-admin \
  python manage.py migrate --noinput

docker compose -f docker-compose.prod.yml --profile admin run --rm app-admin \
  python manage.py collectstatic --noinput

docker compose -f docker-compose.prod.yml --profile admin run --rm app-admin \
  python manage.py seed_all
```

`--profile admin` is required — without it, compose won't start a service
whose only `profiles:` entry is `admin`. `run --rm` starts a fresh, one-off
`app-admin` container connected as the Postgres **owner** role, runs the
command, and removes the container afterward — nothing owner-credentialed is
left running.

**If you only have the Portainer web UI** (no host shell): Portainer doesn't
expose a "run one-off compose command" button, so use **Containers → select
the running `app` container → Duplicate/Edit**. In the advanced editor:

- keep the same image
- override **Command** to `python manage.py migrate --noinput`
- override the `DATABASE_URL` environment variable to use the **owner** role
  and password (`postgres://verylift:<POSTGRES_PASSWORD>@db:5432/verylift`,
  using the same `db` hostname and the `POSTGRES_PASSWORD` value you set in
  step 1) — the running `app` container's `DATABASE_URL` uses the scoped
  `verylift_app` role, which has no `CREATE TABLE` privilege and will fail
  with a permission error on `migrate`
- deploy it, let it run to completion, then delete the container

Repeat with `collectstatic --noinput` and `seed_all` as the command, then
delete those one-off containers too. Reusing "Duplicate/Edit" each time is
more clicks than the CLI path, but doesn't require host access.

`seed_all` loads the reference data (Liftosaur lift/alias fixtures,
FitnessVolt lift aliases) the app expects to exist; skipping it after an
image update that renamed or added fixtures means scoring can go quiet with
no error, matching what the `pre-deploy` Kamal hook's comment warns about.

## 3. Existing volume? Provision the app role manually

`scripts/db/init-app-role.sh` (mounted into `/docker-entrypoint-initdb.d/`)
only runs automatically on a **fresh** `postgres_data` volume. If you're
pointing this stack at a Postgres volume that already existed before you
added `POSTGRES_APP_USER`/`POSTGRES_APP_PASSWORD`, run it by hand once:

```bash
docker compose -f docker-compose.prod.yml exec db \
  /docker-entrypoint-initdb.d/init-app-role.sh
```

Without this, `app`'s scoped role doesn't exist yet and every DB connection
from `app` fails outright.

## Why `/healthz` won't catch this

`/healthz` (see [`core/middleware.py`](../core/middleware.py)) only runs
`SELECT 1` against Postgres — it confirms the database is *reachable*, not
that migrations have run. A container can report healthy in Portainer's UI
while every page that queries a real table 500s because the table doesn't
exist yet. If you're seeing 500s on a stack that looks healthy, step 2 above
is the first thing to check.

## Troubleshooting checklist

- **Every page 500s, healthcheck is green** → migrations never ran. See
  step 2.
- **Stack won't deploy / `db` container exits immediately** → check
  Portainer's logs for `db`; `POSTGRES_PASSWORD must be set` or
  `POSTGRES_APP_PASSWORD must be set` means one of those env vars is missing
  from the stack's environment-variables panel.
- **`app` container restarts in a loop, logs show a Django `ImproperlyConfigured`
  error at startup** → `SECRET_KEY` or `FIELD_ENCRYPTION_KEYS` is missing or
  empty.
- **400 Bad Request / "DisallowedHost"** → the hostname you're browsing to
  isn't in `ALLOWED_HOSTS`.
- **Login/password-reset emails never arrive, no error shown** → `EMAIL_HOST`
  is unset, which is a supported no-op state (see `example.env`) that falls
  back to printing the email to the `app` container's logs instead of
  sending it.
