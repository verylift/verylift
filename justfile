set dotenv-load

# Run the Django dev server natively against the dockerized Postgres
serve *args:
    uv run python manage.py runserver {{args}}

# Run tests with coverage
test:
    uv run pytest

# Run the full CI quality gate: lint + format check + tests
# This is what CI checks — always run this before committing
ci: lint fmt-check test

# Run linter
lint:
    uv run ruff check .

# Run formatter check
fmt-check:
    uv run ruff format --check .

# Format code
fmt:
    uv run ruff format .

# Run lint + format fix
fix:
    uv run ruff check --fix . && uv run ruff format .

# Scan locked dependencies for known CVEs (same check as the CI security workflow).
# Kept out of `just ci` on purpose: the pre-commit gate must stay deterministic and
# offline-capable — a CVE published overnight should not block an unrelated commit.
audit:
    uv export --frozen --no-emit-project --output-file /tmp/requirements-audit.txt
    uvx pip-audit --strict --disable-pip -r /tmp/requirements-audit.txt

# Collect "## Changelog" bullets from merged PRs into CHANGELOG.md under Unreleased
changelog:
    ./scripts/extract_changelog.sh

# Cut a release end to end: validate, collect changelog, stamp the heading, commit, tag, and push (e.g. just release v1.2.3)
release ver:
    ./scripts/release.sh {{ver}}

# Run a given django management command
manage command +args="":
    uv run python manage.py {{command}} {{args}}

# Run every seed command (Liftosaur lifts, FitnessVolt lift aliases) (idempotent)
seed:
    uv run python manage.py seed_all

# Seed Liftosaur built-in lift / alias / lift-quality fixture data (idempotent)
seed-liftosaur-lifts:
    uv run python manage.py seed_liftosaur_lifts

# Seed FitnessVolt lift slug -> canonical lift name aliases (idempotent)
seed-fitnessvolt-lifts:
    uv run python manage.py seed_fitnessvolt_lifts

# Pull FitnessVolt strength standards into the versioned cache (idempotent; requires
# network access to fitnessvolt.com — not part of `just seed`, since seeding is
# static fixture data and this hits a live external API)
refresh-fitnessvolt-cache:
    uv run python manage.py refresh_fitnessvolt_cache

# Apply database migrations
migrate:
    uv run python manage.py migrate

# Create new migration
makemigrations *args:
    uv run python manage.py makemigrations {{ args }}

# Open Django shell
shell:
    uv run python manage.py shell

# Collect static files
collectstatic:
    uv run python manage.py collectstatic --noinput

# Install pre-commit hooks
install-hooks:
    uv run pre-commit install

# Run pre-commit on all files
pre-commit:
    uv run pre-commit run --all-files

# Extract translatable strings into locale/<lang>/LC_MESSAGES/django.po
# (requires the `gettext` system package: apt-get install gettext)
makemessages lang:
    uv run python manage.py makemessages -l {{ lang }} --no-obsolete

# Compile every locale/*/LC_MESSAGES/django.po into a .mo the app can load
compilemessages:
    uv run python manage.py compilemessages

# Build Tailwind CSS
tailwind-build:
    npx tailwindcss -i ./static/css/input.css -o ./static/css/output.css

# Watch Tailwind CSS
tailwind-watch:
    npx tailwindcss -i ./static/css/input.css -o ./static/css/output.css --watch

# Download and vendor a pinned Chart.js release (default v4.4.0)
update-chartjs version='4.4.0':
    curl -sL -o static/vendor/chart.js "https://cdn.jsdelivr.net/npm/chart.js@{{version}}/dist/chart.umd.min.js"
    printf '// Chart.js v%s\n' "{{version}}" | cat - static/vendor/chart.js > static/vendor/chart.js.tmp
    mv static/vendor/chart.js.tmp static/vendor/chart.js

# Download and vendor a pinned htmx release (default v2.0.4)
update-htmx version='2.0.4':
    curl -sL -o static/vendor/htmx.min.js "https://cdn.jsdelivr.net/npm/htmx.org@{{version}}/dist/htmx.min.js"
    printf '// htmx v%s\n' "{{version}}" | cat - static/vendor/htmx.min.js > static/vendor/htmx.min.js.tmp
    mv static/vendor/htmx.min.js.tmp static/vendor/htmx.min.js
