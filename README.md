# very lift

[![CI](https://github.com/verylift/verylift/actions/workflows/ci.yml/badge.svg)](https://github.com/verylift/verylift/actions/workflows/ci.yml)
[![Security](https://github.com/verylift/verylift/actions/workflows/security.yml/badge.svg)](https://github.com/verylift/verylift/actions/workflows/security.yml)

A strength-challenge platform for friend groups. Connects to the workout tracker you already use — [Liftosaur](https://www.liftosaur.com/), [Hevy](https://www.hevyapp.com/), [Strong](https://www.strong.app/), or [wger](https://wger.de/) — and automatically scores your lifts against the goal chart you set for yourself, so everyone tracks progress against their own targets without double-entering data.

## What it does

- **Challenges** — create a time-boxed challenge, invite friends, pick the lifts everyone competes on
- **Automatic scoring** — pulls lift data from your tracker; awards points when you hit the rep-max targets on your own goal chart
- **Bring your own tracker** — participants in the same challenge can each be on a different app; everything lands in one pooled lift history
- **Fair by design** — each participant sets their own targets when they join, so points always measure progress against your own chart, not someone else's
- **Live leaderboard** — points chart, personal performance cards, and gap-to-next-point displayed per lift

## Supported trackers

| Tracker | Live API sync | CSV import | What you supply |
| --- | --- | --- | --- |
| [Liftosaur](https://www.liftosaur.com/) | ✅ | ✅ | API key, or a CSV export file |
| [Hevy](https://www.hevyapp.com/) | ✅ | ✅ | API key, or a CSV export file |
| [Strong](https://www.strong.app/) | — | ✅ | A CSV export file from Strong|
| [wger](https://wger.de/) | ✅ | — | Your instance URL, and an API token |
| No tracker | — | — | Log completed goal milestones manually |

Credentials are encrypted at rest and are never shared with other
participants. Using a tracker that isn't listed? Open a github issue or 
holler into our Discord.

## Documentation

User- and operator-facing docs on how the running system behaves live in
[`docs/`](docs/index.md) — scoring, sync & data freshness, challenges, and
units.

## Translations

The app is available in English and Spanish, with more languages welcome via
community contribution. See [`TRANSLATIONS.md`](TRANSLATIONS.md) for how to claim
a language, generate its `.po` file, and submit a translation.

## Requirements

- Python 3.13+, Docker, [uv](https://docs.astral.sh/uv/)
- [just](https://github.com/casey/just) task runner
- (Optional) An account with one of the [supported trackers](#supported-trackers)

## Development setup

Local dev runs Django **natively on the host** against Postgres in Docker. Only the
database is containerized; the app runs via `manage.py runserver`.

```bash
# Copy the env template and fill in values (SECRET_KEY and
# FIELD_ENCRYPTION_KEYS at minimum — both are required with no default)
cp example.env .env

# Install Python dependencies
uv sync

# Generate a field-encryption key for FIELD_ENCRYPTION_KEYS in .env
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Start the dockerized Postgres (db only — no app container)
docker compose up --wait -d db

# Run migrations and seed reference data (standards, lift aliases)
just migrate
just seed

# Create a local user
just manage create_local_user

# Run the dev server natively
just serve
```

The app will be available at http://localhost:8000. The `.env` file is read
automatically by `just` (`set dotenv-load`). `DATABASE_URL` in `example.env` points
at the Postgres container exposed on localhost:5432, so native tooling (`manage.py`,
`pytest`, `just ci`) connects with no override.

## Running tests

```bash
just ci          # lint + format check + tests (mirrors CI)
just test        # tests only
just fix         # auto-fix lint/format issues
```

## Deployment

Releases are published as Docker images to `ghcr.io/verylift/verylift`. Two
supported paths, depending on who's deploying:

### Kamal (this repo's maintainers)

`config/deploy.yml` and `.kamal/` drive a single-VPS deploy via
[Kamal](https://kamal-deploy.org): kamal-proxy in front for zero-downtime,
health-gated cutover, and a Postgres accessory alongside the app container.

Prerequisites:
- `gem install kamal` and Docker, on whatever machine you deploy from
- the Bitwarden CLI (`bw`)
- a GHCR PAT and the app's other secrets stored as Bitwarden vault items
  (`.kamal/secrets-common` is the source of truth for exactly which ones)
- an SSH config entry (`~/.ssh/config`, personal, never committed) that
  resolves a `verylift-prod` host alias to the VPS's real address and
  identity file:
  ```
  Host verylift-prod
    HostName <the VPS's real IP or hostname>
    IdentityFile <path to your admin SSH key>
  ```
  `config/deploy.yml` references this alias by name, not the real address —
  that's the one piece of this deployment's config that's genuinely
  sensitive and kept out of the (public) repo. Everything else Kamal needs
  (the SSH user, the public hostname, the GHCR org) is already public
  knowledge one way or another, so it's a plain literal in `config/deploy.yml`
  rather than indirected through anything.
- on the VPS itself: Docker, and 80/443 not published to the internet — this
  setup expects an inbound tunnel (e.g. Cloudflare Tunnel) forwarding to
  `localhost:80` in front of kamal-proxy, which binds loopback-only

The only thing to do before any `kamal` invocation is unlock Bitwarden, for
the secrets `.kamal/secrets-common` resolves at parse time — nothing else to
export or source:
```bash
export BW_SESSION="$(bw unlock --raw)"
```

One-time setup:
```bash
kamal setup   # first-time bootstrap: builds, pushes, boots everything
```

Thereafter, cutting and shipping a release are two separate steps on purpose —
tagging a release never automatically deploys it:
```bash
just release            # auto-suggests a YYYY.MM.PATCH version; builds + pushes the image, stamps CHANGELOG.md, tags
just deploy 2026.8.0     # pulls that exact image, migrates, zero-downtime cutover
```

`migrate`, `collectstatic`, and `seed_all` all run automatically via the
`pre-deploy` hook, as the Postgres owner role — reference-data fixtures
(workout tracker lifts/aliases, FitnessVolt lift aliases) ship inside the image, so
without reseeding every deploy the DB silently drifts from what the newly
deployed code expects.

### docker-compose.selfhost.yml (self-hosters)

`docker-compose.selfhost.yml` is provided as a working reference, not a
prescription: dropped capabilities, a read-only root filesystem, and resource
limits, with the reasoning for each choice inline in its comments. Feel free
to deviate from it.

It is a single `app` service with no bundled database, running against a
local SQLite file on a named volume — no other containers, no required config
beyond `SECRET_KEY`/`FIELD_ENCRYPTION_KEYS`/`ALLOWED_HOSTS` (see
`example.env`). The container runs `migrate`/`collectstatic`/`seed_all`
itself on every boot, so starting it is the whole install, and pulling a new
image applies its own schema changes.

For larger or multi-user deployments, point `DATABASE_URL` at a Postgres
instance — see [Database](#database) below for the tradeoff, and the guide
for a drop-in `db` service if you don't already run one. The app migrates
itself either way, so it connects with an account that can change the schema;
Kamal keeps a privilege split via its separate `release` role.

**[Self-hosting guide →](docs/self-hosting.md)** — step-by-step, assumes no
Python or Django knowledge, with Portainer-specific notes.

### Database

`DATABASE_URL` is optional — leaving it unset falls back to a local SQLite
file (`db.sqlite3`) instead of failing to start. This is the default for
`docker-compose.selfhost.yml`'s `app` service (see above) and is fine for a
single user or a small friend group. By default the SQLite connection runs in
its normal rollback-journal mode. Set `SQLITE_WAL=True` to opt into WAL mode
with a 5s busy timeout instead, so one writer works alongside concurrent
readers and the gunicorn workers share the file without tripping "database is
locked" — skip it if your storage can't support WAL (it needs working mmap,
which some network filesystems lack). Set `DATABASE_URL` to opt into Postgres
instead. This applies to both deployment paths above;
Kamal's `config/deploy.yml` always uses its Postgres accessory rather than
SQLite.

## Security

Dependencies and the production Docker image are scanned for known CVEs on every
PR, every push to `main`, and weekly. See [SECURITY.md](SECURITY.md) for the
scanners, failure thresholds, and triage process; run `just audit` to reproduce the
dependency scan locally.

## Project management

Bugs and feature requests are tracked as [GitHub Issues](../../issues).

Early development used [Backlog.md](https://github.com/MrLesk/Backlog.md), a file-based task
tracker — it was a great fit for that phase and is worth a look if you want something similar
for your own project.

## Cutting a release

```bash
just release            # extracts changelog, stamps CHANGELOG.md, tags, builds + pushes the image, pushes, creates a GitHub Release
```

Versions are `YYYY.MM.PATCH` (e.g. `2026.8.0`) — omit the argument to get an
auto-suggested one based on today's date, or pass one explicitly (e.g.
`just release 2026.8.1`). This also builds and pushes the production image
via `kamal build push` and creates a GitHub Release from that version's
changelog section, so `kamal`, Docker, and the `gh` CLI must be available on
whatever machine cuts the release. It still doesn't deploy anything — run
`just deploy <version>` as the follow-on step (see Deployment above).

