@CLAUDE.local.md

## Search & exploration

Prefer `rg` (ripgrep) or `git ls-files`/`git grep` over raw `grep -r`/`find` for repo-wide searches — they respect `.gitignore` automatically, skipping `node_modules/`, `staticfiles/`, `__pycache__/`, etc. instead of dumping their contents into results.

## Branch Discipline

**Never commit directly to `main`.** All code changes must happen on a feature branch.

If you are on `main` when about to make code changes, create a branch first:
```
git checkout -b short-description
```

Prefer git worktrees for agent work so the main checkout stays clean:
```
git worktree add .claude/worktrees/short-description -b short-description
```
Name the worktree/branch after the work it holds, not the agent run that produced it.

## Quality Gate

**Before committing any code, run:**
```
docker compose up -d db
just ci
```

`just ci` runs lint + format check + tests — exactly what CI checks. If it fails locally, CI will fail. Fix all issues before committing. Use `just fix` to auto-resolve ruff errors.

## Release process

Versions are date-based (`YYYY.MM.PATCH`, e.g. `2026.8.0`), not semver — no `v`
prefix, and PATCH is a plain incrementing counter that resets each month, not
itself date-derived.

To cut a release, run `just release` (auto-suggests the next `YYYY.MM.PATCH`
and asks for confirmation) or `just release YYYY.MM.PATCH` with an explicit
version. Do not hand-tag or hand-stamp the changelog — always use
`just release`, or the release-notes step has no `## <version>` section to
read. This stamps `CHANGELOG.md`, builds the production image, pushes both
the image (to `ghcr.io`) and the git tag, and creates a GitHub Release from
that version's changelog section — it does not deploy. Run
`just deploy YYYY.MM.PATCH` separately to pull that image and cut over on the
VPS via Kamal (see the README's Deployment section).

### Changelog requirement

Every PR that changes user- or operator-visible behavior must include a `## Changelog` section with 1–3 bullets. **PRs with nothing but docs/chore housekeeping to report should omit the `## Changelog` section entirely** — do not add a placeholder bullet like "No user-facing change". `scripts/extract_changelog.sh` also drops any bullet containing that phrase as a safety net, but the correct move is to not write the section at all. PRs without a `## Changelog` section are silently skipped at release time and go undocumented, which is the intended behavior for pure housekeeping PRs.

Each bullet should start with a category tag so `just release` can group entries in `CHANGELOG.md`. Recognized tags (case-insensitive): `[Added]`, `[Changed]`, `[Fixed]`, `[Security]`, `[Removed]`. An untagged bullet defaults to `[Changed]`. Example:
```
## Changelog
- [Added] Users can now register without admin intervention via /accounts/register/
- [Changed] Post-registration settings page guides new users through nickname and theme setup
```

Do not include AI tool attributions (e.g. "Generated with Claude Code") in PR descriptions, commit messages, or any project content. The `## Changelog` section and PR body are user-facing; keep them clean.

## Logging

Every service module and view must include a module-level logger:

```python
import logging
logger = logging.getLogger(__name__)
```

### Log levels
- `logger.debug(...)` — internal flow, variable values, loop progress
- `logger.info(...)` — external API calls (Liftosaur, the OIDC provider), significant state changes
- `logger.warning(...)` — recoverable failures, unexpected-but-handled conditions
- `logger.exception(...)` — inside except blocks; automatically includes the stack trace

### Rules
- Always call `logger.exception(...)` or `logger.error(...)` before returning a failure
  response from a service function. Silent `return False` / `return None` on exception is not
  acceptable.
- Never log secrets: API keys, passwords, tokens, session IDs must be redacted or omitted.
- Log the *cause* not just the outcome: `logger.exception("Liftosaur weight fetch failed for user %s", user.id)`
  not `logger.error("API call failed")`.
- Django's LOGGING config in settings.py routes logs to console in development. Do not add
  file handlers or change the logging config unless the task explicitly requires it.

## Issue Tracking

Bugs and feature requests are tracked as GitHub Issues. Check open issues before starting
work, and reference the issue number in the PR that closes it.
