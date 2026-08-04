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

Releases are published as Docker images to `ghcr.io/verylift/verylift`. Two
supported paths, depending on who's deploying:

### Kamal (this repo's maintainers)

`config/deploy.yml` and `.kamal/` drive a single-VPS deploy via
[Kamal](https://kamal-deploy.org): kamal-proxy in front for zero-downtime,
health-gated cutover, and a Postgres accessory alongside the app container.

Prerequisites:
- `gem install kamal` and Docker, on whatever machine you deploy from
- the Bitwarden CLI (`bw`), with SSH access to the VPS
- a GHCR PAT and the app's other secrets stored as Bitwarden vault items (see
  the list below)
- on the VPS itself: Docker, and 80/443 not published to the internet — this
  setup expects an inbound tunnel (e.g. Cloudflare Tunnel) forwarding to
  `localhost:80` in front of kamal-proxy, which binds loopback-only

Deploy-time config lives entirely in the "very lift" Bitwarden folder — no
local env file to copy or fill in. Two kinds of value, handled differently:
- **Secrets** consumed by the running container (`SECRET_KEY`,
  `DATABASE_URL`, etc.) are read directly by Kamal itself via
  `.kamal/secrets-common`, which shells out to `bw get password <item>` —
  nothing to export for these.
- **`config/deploy.yml`'s own structure** (which host to SSH to, what
  hostname kamal-proxy expects) is resolved via ERB from the real shell
  environment at parse time, before secrets are loaded at all — Kamal has no
  first-class Bitwarden integration for this part, only for secrets. Source
  `.kamal/load-env.sh` before every `kamal` invocation to pull these from
  Bitwarden instead of relying on manually-exported (and easily stale)
  shell variables:
  ```bash
  export BW_SESSION="$(bw unlock --raw)"
  source .kamal/load-env.sh
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

`migrate` and `collectstatic` run automatically via the `pre-deploy` hook, as
the Postgres owner role. `seed_all` is not part of the automatic deploy path —
run it on demand when needed:
```bash
kamal app exec --roles=release "python manage.py seed_all"
```

Bitwarden vault items required, all in the "very lift" folder:
- `verylift-ghcr` (login) — `username`/`password` are the GHCR PAT's owner and the PAT itself
- `verylift-deploy-config` (secure note, 4 custom fields) — `deploy_host`, `deploy_ssh_user`, `deploy_host_name`, `allowed_hosts` (this one supports a leading-dot wildcard, e.g. `.verylift.com`, to match every subdomain — `deploy_host_name` does not, since kamal-proxy's Host-header routing has no wildcard support and needs the exact hostname(s) currently routed through the tunnel)
- `verylift-secret-key`, `verylift-field-encryption-keys`, `verylift-postgres-password`, `verylift-postgres-app-password`, `verylift-oidc-client-secret`, `verylift-email-host-password` (password-type, one value each)

### docker-compose.prod.yml (self-hosters)

`docker-compose.prod.yml` is provided as a working reference, not a prescription:
Postgres alongside the app, network isolation, dropped capabilities, a read-only
root filesystem, and resource limits, with the reasoning for each choice inline
in its comments. Feel free to deviate from it.

### Database

If a host can't run Postgres, `DATABASE_URL` is optional — leaving it unset falls
back to a local SQLite file (`db.sqlite3`) instead of failing to start. Set
`DATABASE_URL` to opt back into Postgres. This applies to both deployment paths
above.

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
just release            # extracts changelog, stamps CHANGELOG.md, tags, builds + pushes the image, pushes
```

Versions are `YYYY.MM.PATCH` (e.g. `2026.8.0`) — omit the argument to get an
auto-suggested one based on today's date, or pass one explicitly (e.g.
`just release 2026.8.1`). Unlike before, this now also builds and pushes the
production image via `kamal build push`, so `kamal` and Docker must be
available on whatever machine cuts the release. It still doesn't deploy
anything — run `just deploy <version>` as the follow-on step (see Deployment
above).

## License

[PolyForm Shield 1.0.0](LICENSE) — source-available, not OSI-approved "open
source." You're free to self-host, modify, and redistribute this for any
purpose except offering a competing product or service. See the
[license text](LICENSE) for the exact terms.
