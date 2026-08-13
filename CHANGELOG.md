# Changelog

## Unreleased

## 2026.8.7 — 2026-08-12

### Added
- Settings page footer now shows the running app version
- /healthz response includes the running app version
- Draft challenges can now be deleted from the challenges page
- `SQLITE_WAL=True` enables WAL mode on the SQLite backend, letting concurrent writes from multiple gunicorn workers proceed without risking "database is locked" errors (off by default to avoid changing the journal mode of existing self-hosted databases)
- Log a completed set by hand from the Summary tab, for lifters who don't use a workout tracker — each summary card flips over to a target carousel with a date and confirm step
- Manually logged sets are recorded with their source, keeping self-reported work distinguishable from Liftosaur-synced work
- Upload a Hevy CSV export to log workouts, for free-tier Hevy users whose API access is Pro-gated
- Account settings shows a "Support ID" to quote when reporting an issue
- Reworked landing page including a newsletter signup and a "Join our Discord" card
- Terms of Service and Privacy Policy versions are now tracked with per-user consent records; existing users are grandfathered in automatically
- Signed-in users are prompted to re-consent if a tracked policy is updated with a new active version
- Production errors and logs can be forwarded to GlitchTip (or any Sentry-compatible endpoint) by setting `GLITCHTIP_DSN`

### Changed
- Liftosaur syncs for a given user and challenge can now run once a minute instead of once every 10 minutes
- The profile photo button in settings now reads "Save Photo" instead of "Upload Photo", matching the other settings sections
- Challenge detail page tab button now reads "Goals" instead of "Standards"
- Joining a challenge via invite link no longer requires a Liftosaur API key — manual self-report and Hevy CSV import work just as well
- Signup now offers a choice of tracking method (Liftosaur, manual entry, or CSV import) instead of a single optional API key field
- Privacy Policy and Terms of Service (v1.1) describe infrastructure and connected fitness-tracking services generically rather than naming specific vendors, and disclose the error-tracking/log-aggregation tooling used in production
- Privacy Policy updated to v1.2, naming the self-hosted GlitchTip instance explicitly; existing users will be prompted to re-consent.

### Fixed
- The goal-setup wizard's "suggested from history" method now recognizes pooled lift history from any source, not just a connected Liftosaur key

### Removed
- The dashboard no longer shows a banner urging keyless users to connect a Liftosaur API key

## 2026.8.5 — 2026-08-09

### Added
- Self-hosted deployments run migrations, collectstatic, and reference-data seeding automatically on container start, so no manual setup commands are needed.
- `HTTPS_ENABLED=False` lets a self-hosted install run over plain HTTP on a trusted network; it defaults to True, leaving HTTPS-only behaviour unchanged.
- `docs/self-hosting.md` is a step-by-step self-hosting guide covering key generation, first-run setup, admin account creation, Portainer, and troubleshooting.

### Changed
- `docker-compose.prod.yml` is renamed to `docker-compose.selfhost.yml` and is now a single service running on SQLite with no database configuration required; point `DATABASE_URL` at Postgres to use it instead.
- FitnessVolt strength standards are enabled by default (still requires a one-time `refresh_fitnessvolt_cache` run to populate the first snapshot); set `FITNESSVOLT_ENABLED=False` to opt out.
- Self-hosted installs now pull the FitnessVolt strength-standards data automatically on first boot, so the "standards" goal-setup method works with zero manual steps; set `FITNESSVOLT_ENABLED=False` to skip it.

### Removed
- The `app-admin` compose service and the bundled Postgres container are gone; the app performs its own migrations.

## 2026.8.4 — 2026-08-05

### Fixed
- Challenge detail page no longer 500s for participants with a 0-rep set logged against a lift in their goal

## 2026.8.3 — 2026-08-05

### Changed
- FitnessVolt strength standards are now enabled (requires a one-time `refresh_fitnessvolt_cache` run to populate the first snapshot).

## 2026.8.2 — 2026-08-04

### Fixed
- Production images no longer ship the local `.kamal/` and `config/` directories, which were never needed at runtime.

## 2026.8.1 — 2026-08-04

### Added
- `just release` now also creates a GitHub Release from the version's changelog section.

### Fixed
- Pushed production images now show up under this repo's GitHub Packages tab instead of only the org's general package listing.

## 2026.8.0 — 2026-08-04

### Added
- `config/deploy.yml` and `.kamal/` add Kamal-based single-VPS deployment support, alongside the existing `docker-compose.prod.yml` reference path.

### Changed
- `just release` now builds and pushes the production image in addition to stamping the changelog and tagging; a new `just deploy <version>` pulls that image and performs a zero-downtime cutover.
- Release versions are now date-based (`YYYY.MM.PATCH`, e.g. `2026.8.0`) instead of semver; `just release` with no argument auto-suggests the next version.
- Self-serve registration is now open.

### Fixed
- Terms of Service and Privacy Policy now correctly show as version 1.0 instead of 2.0.
- An empty `ADMIN_URL_PATH` now fails deployment loudly (a Django system check) instead of silently routing every URL to the Django admin.

