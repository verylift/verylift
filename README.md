# very lift

[![CI](https://github.com/verylift/verylift/actions/workflows/ci.yml/badge.svg)](https://github.com/verylift/verylift/actions/workflows/ci.yml)
[![Security](https://github.com/verylift/verylift/actions/workflows/security.yml/badge.svg)](https://github.com/verylift/verylift/actions/workflows/security.yml)

A strength-challenge platform for friend groups. Connects to your [Liftosaur](https://www.liftosaur.com/) workout log and automatically scores your lifts against the goal chart you set for yourself — so everyone tracks progress against their own targets without double-entering data.

## What it does

- **Challenges** — create a time-boxed challenge, invite friends, pick the lifts everyone competes on
- **Automatic scoring** — syncs lift data from Liftosaur; awards points when you hit the rep-max targets on your own goal chart
- **Fair by design** — each participant sets their own targets when they join, so points always measure progress against your own chart, not someone else's
- **Live leaderboard** — points chart, personal performance cards, and gap-to-next-point displayed per lift

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
- A [Liftosaur](https://www.liftosaur.com/) account and API key (for data sync)

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

Releases are published as Docker images to `ghcr.io/verylift/verylift`. How you run
that image in production is up to you — use Docker Compose, Kamal, Swarm, or
whatever fits your infrastructure.

`docker-compose.prod.yml` is provided as a working reference, not a prescription:
Postgres alongside the app, network isolation, dropped capabilities, a read-only
root filesystem, and resource limits, with the reasoning for each choice inline
in its comments. Feel free to deviate from it.

If a host can't run Postgres, `DATABASE_URL` is optional — leaving it unset falls
back to a local SQLite file (`db.sqlite3`) instead of failing to start. Set
`DATABASE_URL` to opt back into Postgres.

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
just release v1.2.3  # extracts changelog, stamps CHANGELOG.md, tags, pushes
```

This stamps `CHANGELOG.md` and creates a git tag — nothing automated builds or
publishes an image from it. Building and deploying is up to you (see
Deployment above).

## License

[PolyForm Shield 1.0.0](LICENSE) — source-available, not OSI-approved "open
source." You're free to self-host, modify, and redistribute this for any
purpose except offering a competing product or service. See the
[license text](LICENSE) for the exact terms.
