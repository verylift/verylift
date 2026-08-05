# Changelog

## Unreleased

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

