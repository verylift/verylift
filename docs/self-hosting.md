# Self-hosting very lift

A step-by-step guide to running very lift on your own machine — a home
server, a NAS, or a small VPS. It assumes you can start a Docker container
and edit a text field. It does **not** assume you know Python, Django, or
Postgres.

There's a [Portainer](#portainer-notes) section near the bottom if that's how
you manage containers, but every step below works from a plain terminal too.

> This repo's maintainers deploy a different way (Kamal — see the README's
> [Deployment](../README.md#deployment) section). That path is for people with
> SSH access to a VPS and a Bitwarden vault. You almost certainly want this
> page instead.

## Before you start

**Docker**, with `docker compose`. Anything that runs containers works —
Docker Desktop, a NAS package, Portainer, plain Linux Docker. That's it.

Budget about five minutes of hands-on work, plus up to five more waiting for
the container's first boot on older or slower hardware (Step 3 explains why).

Decide one thing up front, because it changes a setting in Step 2: **will you
reach this over `https://` or plain `http://`?**

- **Over HTTPS** — because you're putting it on the internet, or you already
  run a reverse proxy. This is the default and needs no extra setting. Point
  your tunnel or proxy at the container's port `8000`, and make sure it
  forwards the `X-Forwarded-Proto` header (essentially all of them do).
  [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
  and [Pangolin](https://github.com/fosrl/pangolin) need no open ports;
  [Caddy](https://caddyserver.com/) and Nginx Proxy Manager handle
  certificates for you.
- **Over plain HTTP on your home network** — you'll browse to something like
  `http://192.168.1.50:8000` and no certificates are involved. Set
  `HTTPS_ENABLED=False` in Step 2. Without it the app redirects you to an
  `https://` address nothing is serving and refuses to send its login cookie,
  so nothing works.

If you're exposing this to the internet, use HTTPS. Over plain HTTP, anyone
on the same network can read the traffic, passwords included.

## Step 1 — Generate your two keys

very lift needs two secrets. Don't invent them by hand; generate them with
the commands below. These use the app's own Docker image, so **you don't need
Python installed** — Docker is enough.

Generate `SECRET_KEY`:

```bash
docker run --rm ghcr.io/verylift/verylift:latest \
  python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Generate `FIELD_ENCRYPTION_KEYS`:

```bash
docker run --rm ghcr.io/verylift/verylift:latest \
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Each prints one line. Copy both somewhere safe for the next step.

> **Keep `FIELD_ENCRYPTION_KEYS` backed up.** It encrypts everyone's stored
> Liftosaur API key. If you lose it, those keys are gone permanently and every
> user has to re-enter theirs. Losing `SECRET_KEY` is milder — it just logs
> everyone out.

## Step 2 — Create your configuration

Download [`docker-compose.selfhost.yml`](../docker-compose.selfhost.yml) from
this repo, and create a file named `.env` next to it:

```
SECRET_KEY=<the first key you generated>
FIELD_ENCRYPTION_KEYS=<the second key you generated>
ALLOWED_HOSTS=verylift.example.com,localhost
```

If you decided on **plain HTTP** in [Before you start](#before-you-start),
add one more line:

```
HTTPS_ENABLED=False
```

`ALLOWED_HOSTS` is the hostname you'll actually type into your browser — the
public name your tunnel or reverse proxy serves, not the container's internal
address. Separate multiple names with commas; `localhost` is included above so
you can reach the container directly at `http://localhost:8000` for a quick
sanity check before your tunnel or reverse proxy is wired up. Getting this
wrong gives you a `500 Internal Server Error` with `DisallowedHost` in the
container's logs — see [Troubleshooting](#troubleshooting).

That's the whole required configuration. There is no database to set up — the
app stores its data in a SQLite file on a Docker volume, which is fine for a
single user or a group of friends. (If you'd rather run Postgres, see
[Advanced: using Postgres](#advanced-using-postgres) at the bottom.)

Everything else is optional. [`example.env`](../example.env) documents every
available setting — email, single sign-on, and various tuning knobs — with
comments explaining what each one does.

## Step 3 — Start it

```bash
docker compose -f docker-compose.selfhost.yml up -d
```

That's it. The container sets itself up on first boot: it creates the
database tables, collects its static files, loads the reference data it
needs, and pulls the FitnessVolt strength-standards data it needs for the
"standards" goal-setup method. Watch it happen with:

```bash
docker compose -f docker-compose.selfhost.yml logs -f app
```

You'll see `applying migrations`, `collecting static files`, `seeding
reference data`, and `warming FitnessVolt strength standards cache` scroll
past, then gunicorn starting up. First boot takes longer than later ones —
budget up to five minutes on older or slower hardware (a well-worn NAS, say)
once you add up pulling the image itself, migrations and static-file
collection on a slower CPU, and the FitnessVolt pull. Later restarts are much
quicker: migrations and seeding become no-ops, and the FitnessVolt pull skips
straight past the expensive part once it's already cached. The FitnessVolt
pull is also best-effort: a failed or slow connection to FitnessVolt logs a
warning and moves on rather than blocking startup, so a fully offline LAN
still boots fine — it just won't have that goal-setup method available.
`FITNESSVOLT_ENABLED=False` in your `.env` does the same thing on purpose:
it skips the pull and removes "standards" from the goal-setup picker
entirely, rather than leaving the method there with no data behind it.

This setup step re-runs on every restart, which is intentional and safe: it's
how the app applies new database changes after you update the image. You never
need to run migration commands by hand — that's true whether you stay on
SQLite or switch to Postgres later.

## Step 4 — Create your account

Open your site in a browser and register through the normal sign-up page.
That gives you a regular user account — enough to create challenges, join
them, and connect Liftosaur.

If you also want access to the Django admin panel (for managing other users,
or poking at data directly), promote yourself to an administrator:

```bash
docker compose -f docker-compose.selfhost.yml exec app \
  python manage.py createsuperuser
```

Use the **same username** you just registered with to upgrade that account,
or a new one to create a separate admin login. The admin panel lives at
`/the-rack/` by default — a deliberately non-obvious path, since `/admin/` is
the first thing automated scanners try. Change it with `ADMIN_URL_PATH` in
your `.env` if you like.

## Updating to a new version

```bash
docker compose -f docker-compose.selfhost.yml pull
docker compose -f docker-compose.selfhost.yml up -d
```

Database changes in the new version are applied automatically as the
container restarts. Your data lives on Docker volumes and survives updates.

## Portainer notes

Everything above applies; only the mechanics differ.

- **Creating the stack:** *Stacks → Add stack*, then either paste the
  contents of `docker-compose.selfhost.yml` into the web editor, or use the
  *Repository* option pointing at this repo with
  `docker-compose.selfhost.yml` as the compose path.
- **Configuration:** use the **Environment variables** panel below the editor
  instead of creating a `.env` file by hand. Add `SECRET_KEY`,
  `FIELD_ENCRYPTION_KEYS`, and `ALLOWED_HOSTS` there — plus `HTTPS_ENABLED`
  set to `False` if you're on plain HTTP, per
  [Before you start](#before-you-start). Portainer writes them into a `.env`
  file beside the stack for you, which is exactly what the compose file
  expects.
- **Running the `createsuperuser` command from Step 4:** open *Containers*,
  click the `app` container, and use the **Console** button (`>_`) to get a
  shell inside it. Then run
  `python manage.py createsuperuser`.
- **Watching first boot:** the container's **Logs** view shows the same setup
  output described in Step 3.
- **Updating:** *Stacks → your stack → Update the stack*, with
  *Re-pull image* enabled.

## Troubleshooting

**The page redirects to an `https://` address that won't load, or you can log
in but get bounced straight back to the login page.**
You're browsing over plain HTTP without telling the app. Add
`HTTPS_ENABLED=False` to your `.env` and restart. If your browser keeps
redirecting even after that, it cached the old instruction — clear the site's
data for that hostname, or try a private window to confirm.

**A `500 Internal Server Error`, with `DisallowedHost` in the logs.**
The hostname in your browser isn't listed in `ALLOWED_HOSTS`. Check the logs
(`docker compose -f docker-compose.selfhost.yml logs app`) for a line like
`Invalid HTTP_HOST header: '<hostname>'. You may need to add '<hostname>' to
ALLOWED_HOSTS.` — add that hostname and restart the stack.

**The container starts then immediately stops, over and over.**
Check the logs (`docker compose -f docker-compose.selfhost.yml logs app`). An
`ImproperlyConfigured` error means `SECRET_KEY` or `FIELD_ENCRYPTION_KEYS` is
missing or empty — revisit Steps 1 and 2.

**Pages load but show a server error.**
Check the container's logs — the cause is almost always printed there. If
migrations, static-file collection, or seeding from Step 3 didn't complete,
the container stops rather than serving a half-configured database, so look
for an error in that section. The FitnessVolt warm-up is the one exception —
it's best-effort and never stops the container, so a stuck or failed pull
there isn't why pages are erroring.

**Password reset emails never arrive.**
Expected until you configure email. With no `EMAIL_HOST` set, reset links are
printed to the container's log instead of being sent — the flow still works,
but only someone with log access can complete it. See `example.env` for SMTP
settings.

**Occasional "database is locked" errors.**
The SQLite database runs in rollback-journal mode by default, which takes a
database-wide lock for every write — with several gunicorn workers sharing
one file, two writes landing at the same moment can collide. Set
`SQLITE_WAL=True` in your `.env` and restart to switch to WAL mode instead,
where one writer works alongside readers and a writer that does collide waits
up to 5 seconds rather than failing immediately. Under genuinely heavy
concurrent use, switching to Postgres (below) is the next step.

**The app can't open the database at all** (errors mentioning `disk I/O` or
`unable to open database file`) **and you have `SQLITE_WAL=True` set, with
your data volume living on a network share.**
WAL needs working shared memory (mmap) on the filesystem holding the database,
which NFS and SMB shares typically don't provide. Unset `SQLITE_WAL` (or set
it to `False`) in your `.env` and restart; the app converts the database back
to rollback-journal mode on the next start. Concurrent writes are then
slower, so prefer local storage or Postgres if you can.

## Advanced: using Postgres

SQLite is the default because it needs no setup and comfortably handles a
single user or small group. Postgres is worth the extra steps if you have
more users, want stronger concurrent-write behavior, or already run a
Postgres instance.

The compose file doesn't bundle a database — it stays a single service on
purpose. Switching is one line: point `DATABASE_URL` at a Postgres instance
in your `.env`, alongside the keys from Step 1.

```
DATABASE_URL=postgres://user:password@hostname:5432/verylift
```

Restart the stack and you're done. The app creates its own tables and seeds
its reference data on boot exactly as it does on SQLite — there are no extra
setup commands, and updates keep working as described in
[Updating to a new version](#updating-to-a-new-version).

Because the app performs its own migrations, the account in that URL needs
permission to create and alter tables. That's the tradeoff for a single
container doing everything: a compromised app could change the database
structure, not just its contents. If you'd rather keep those privileges
separated, `config/deploy.yml` in this repo shows how the maintainers' own
deployment splits them across two roles.

### Running Postgres alongside it

Already have Postgres somewhere? Use it — nothing more to do. If not, add a
new `db` service below your existing `app` service in
`docker-compose.selfhost.yml` — `db` and `postgres_data` are new, but `app`
and `volumes` already exist in your file, so add the two lines shown for each
into what's already there rather than pasting a second copy of either
section:

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: verylift
      POSTGRES_USER: verylift
      POSTGRES_PASSWORD: <a strong password>
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U verylift"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  app:
    # Add just this to the app service that's already there — wait for the
    # database before the app tries to migrate against it.
    depends_on:
      db:
        condition: service_healthy

volumes:
  # Add just this line inside the volumes section that's already there.
  postgres_data:
```

Then set `DATABASE_URL=postgres://verylift:<the password>@db:5432/verylift`
and bring the stack up as usual. `db` is the hostname because that's the
service name — Docker resolves it on the network Compose creates for you.
