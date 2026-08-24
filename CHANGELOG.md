# Changelog

## Unreleased

## 2026.8.13 — 2026-08-24

### Added
- Free-tier Liftosaur users can now import their workout history via CSV export from Settings, alongside the existing Hevy CSV import
- Challenge owners can set an invite link to never expire, instead of it always eventually timing out
- Deleting your account now lets you optionally reassign ownership of any challenge you still run, defaulting to your longest-tenured co-participant
- Uploading a workout CSV now shows real upload progress plus a distinct message while the server processes it
- CI now fails a PR if a GPL/AGPL-licensed Python dependency is introduced via `uv.lock`/`pyproject.toml`
- scripts/import-liftosaur-to-wger.py seeds a wger instance with a Liftosaur CSV export via the wger REST API, for testing wger integrations
- Users can connect a self-hosted Wger instance from Settings (instance URL + API token) to sync workout history
- New `LiftSource.WGER` provenance for pooled lift history rows
- Onboarding lets a new user name an unsupported tracker app, recorded for backlog triage
- Onboarding now lets you connect Liftosaur, Wger, or Hevy directly (or tell us about another tracker you use), instead of a single generic "how do you track lifts" step
- Onboarding and Settings promote signing up for Liftosaur with our affiliate coupon code VERYLIFT, including a plain disclosure that we earn a commission, a copy-to-clipboard button for the code, and a dedicated suggestion step for users not using any tracker yet
- Challenge creators can choose a "Rep Target" scoring mode alongside Classic, suited to calisthenics like push-ups, dips, and pull-ups
- Rep Target participants get a Goals tab showing the reps needed at their target weight to earn each point value, and can open a co-participant's goal from the leaderboard
- Goal charts now show what each rep-max column is worth in points
- The rep target goal grid has a per-row clear button and a "Clear all", matching the classic goal grid
- The challenges list shows each challenge's mode (Classic or Rep Target)

### Changed
- Redesigned the site's social-preview image for link previews
- Onboarding's tracking-app step now supports Wger and Hevy alongside Liftosaur, with per-app API/CSV options instead of a single hardcoded Liftosaur choice
- The lift picker is now tabbed, opening on "All Lifts" with "Popular" and "Calisthenics" as curated views with their own "Select all"
- Suggested rep targets now sit above your best logged set, so a new goal starts at zero points instead of already being complete
- "Suggest targets" now fills only blank fields, keeping and visually distinguishing values you already typed

### Fixed
- Leaving a challenge and rejoining via invite link no longer resurrects your old goal chart — you're prompted to set a new one, and reusing the same goal name no longer errors
- Collapsing the desktop sidebar no longer hides Settings and the rest of the account menu — clicking the avatar expands it and opens the menu
- A never-expiring invite link to a challenge that's already ended now offers to help you start your own, instead of leading to a broken signup or a raw error page
- Compute could occasionally round an extrapolated cell to a value lighter than its own pinned anchor -- rounding now stays on the correct side of that anchor
- Deleted accounts now show consistently as their placeholder name with a "(deleted)" marker everywhere, instead of as "Former Participant" on some pages and a pseudonym on others
- Weight and repetition units are resolved live per Wger instance instead of assumed by default fixture ID, preventing silent weight mis-conversion or dropped sets on self-hosted instances with renumbered reference tables
- Workout log repetitions are now parsed correctly when the API returns them as a decimal-formatted string (e.g. `"7.00"`), which previously caused every synced set to be silently dropped
- A rep target goal is only complete at its full rep count; previously the last few reps were never required
- A slow Liftosaur sync no longer breaks the challenge page; it shows the history already synced
- Goal names are capped at 100 characters; over-long names no longer crash goal confirmation or leaving a challenge
- The "Final stretch" endgame nudge now fires in Rep Target mode when a lifter is within a few reps of their next point
- The challenge header shows a Rep Target participant's locked goal instead of "Not set"

## 2026.8.12 — 2026-08-18

### Added
- Pasting a JSON goal is now its own option when setting up your chart, instead of a hidden toggle inside manual entry
- The manual-entry goal grid has a Compute button: fill in a 1RM, a 5RM, or several known rep-maxes and it blends five rep-max formulas across every entered weight to fill in the rest
- New users are invited to join the current year's Very Open at the end of onboarding, when an operator has configured an invite link
- Users can permanently delete their own account from Settings → Danger Zone

### Changed
- The onboarding wizard no longer shows the app's nav sidebar or mobile menu, keeping focus on setup steps
- Deleted accounts are anonymized (pseudonymous name/email, photo removed) and deactivated rather than hard-deleted, preserving challenge/leaderboard history as "Former Participant"
- JSON-pasted goal charts with an unrecognized lift name no longer block the whole save — you can review and confirm to skip just that lift and keep the rest
- Sharing a challenge invite link now shows a proper link preview naming the challenge and participant count, with a correctly-sized image for Discord/Slack/iMessage

### Fixed
- The join-link preview page no longer shows a broken image for the inviter's avatar to signed-out visitors
- The invite-link preview page's content is now wrapped in a card, matching the rest of the app's visual style
- Invite links can no longer be created, regenerated, or edited for a challenge that has already ended, even in the window before its status formally flips
- Goal targets are now validated so a lift's weight can't increase as rep count goes up (e.g. a 10RM heavier than its 5RM), and Compute itself refuses to extrapolate from a lift's own contradictory pinned weights

### Removed
- `CHALLENGES_INVITE_LINK_TTL_DAYS` setting — no longer needed now that ended challenges reject invite-link mutations outright

## 2026.8.11 — 2026-08-17

### Added
- Desktop sidebar can now collapse to an icon-only rail, with the choice remembered across visits

### Changed
- The mobile navigation menu is now a single unified dropdown (profile, nav links, settings, logout) instead of a separate drawer and popover

## 2026.8.10 — 2026-08-17

### Added
- New accounts (including SSO) are asked for their kg/lb display preference during onboarding, defaulting to lb.
- `APP_LOG_LEVEL` env var lets the app-wide log level be raised for a live investigation via redeploy alone, no rebuild required

### Changed
- Signup now walks new users through a short setup: choosing how they'll track lifts, connecting Liftosaur if chosen, and picking kg/lb display units.
- Production logs are now emitted as structured JSON instead of plain text
- Gunicorn's own error log now renders as structured JSON in production, matching every other log source

### Security
- Bumped sqlparse to 0.6.0, fixing 4 known CVEs in this transitive dependency.
- Bumped Django to 6.0.8.
- Docker image now applies latest Debian security patches at build time, fixing a Trivy-flagged util-linux CVE baked into the base image.

### Removed
- Gunicorn's per-request access log, now redundant with kamal-proxy's structured request logs

## 2026.8.9 — 2026-08-14

### Added
- Challenges now automatically close within 30 minutes of their real end time in every creator timezone (including half-hour offsets), via a new in-container scheduler — no more challenges stuck at ACTIVE indefinitely.
- Following a challenge invite link now shows an accept/decline preview page (host, stats, lifts, leaderboard, points-over-time chart) before joining, instead of joining automatically

### Changed
- Leaderboard cards on the challenge detail and invite-accept pages show the full participant list in a scrollable panel instead of only whoever happened to already be scored
- `close_challenges` now fires 30 seconds past each half-hour tick instead of exactly on it, avoiding a boundary race for half-hour-offset timezones (configurable via `CLOSE_CHALLENGES_SCHEDULER_SKEW_SECONDS`)

### Fixed
- Local sign-in now honors a pending challenge invite, matching existing registration/SSO behavior.
- Leaderboards now show every accepted participant, including those with zero points (shown tied-last, or with "-" ranks if nobody has scored yet), instead of hiding unscored participants

## 2026.8.8 — 2026-08-13

### Added
- Invite links can be given a custom expiry and a max-uses cap, and edited in place without invalidating the shared URL

### Changed
- Settings page, profile photo preview, and dashboard profile card content are now centered
- Profile photo upload is now behind a pencil-icon toggle on the (larger) preview
- Support ID moved to its own settings card
- Invite link Regenerate/Copy buttons restyled as an icon + accent pair; landing page Challenges copy now highlights solo goal tracking
- Invite links default to expiring at the competition's own end date instead of a flat 7-day timer
- Competitions now close relative to the creator's own timezone instead of always UTC

### Security
- Invite-link tokens are shorter (8 characters) and the join page is now rate-limited per IP
- Inter and JetBrains Mono fonts are now self-hosted instead of loaded from Google Fonts, so visitors no longer send their IP/browser info to Google on page load

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

