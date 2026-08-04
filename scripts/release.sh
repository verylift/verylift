#!/usr/bin/env bash
# Cut a release end to end: validate, collect changelog, stamp the heading,
# commit, tag, and push. Publishing/deploying from that tag is a separate,
# deployment-specific concern — see the README's Deployment section.
#
# Usage: scripts/release.sh vX.Y.Z
#
# Fails fast (before any mutation) if:
#   - the version is not vX.Y.Z
#   - you are not on main
#   - the working tree is dirty
#   - the tag already exists locally or on origin
#   - the ## Unreleased section is empty (nothing to release)
set -euo pipefail

CHANGELOG="${CHANGELOG:-CHANGELOG.md}"
REMOTE="${REMOTE:-origin}"

die() {
    echo "error: $*" >&2
    exit 1
}

ver="${1:-}"
[ -n "$ver" ] || die "usage: just release vX.Y.Z"

# --- Preconditions (all checked before we mutate anything) ---

echo "$ver" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    || die "version must look like vX.Y.Z (got '$ver')"

[ -f "$CHANGELOG" ] || die "$CHANGELOG not found (run from the repo root)"

branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || die "releases must be cut from main (on '$branch')"

git diff --quiet && git diff --cached --quiet \
    || die "working tree is dirty; commit or stash first"

if git rev-parse -q --verify "refs/tags/$ver" >/dev/null; then
    die "tag $ver already exists locally"
fi
if git ls-remote --exit-code --tags "$REMOTE" "$ver" >/dev/null 2>&1; then
    die "tag $ver already exists on $REMOTE"
fi

# --- Collect merged-PR changelog bullets into ## Unreleased ---

echo ">> Collecting changelog entries from merged PRs..."
./scripts/extract_changelog.sh

# Refuse to cut an empty release: the release notes come from this section, and
# an empty one both produces empty notes and means there's nothing to ship.
unreleased="$(sed -n '/^## Unreleased[[:space:]]*$/,/^## /p' "$CHANGELOG" \
    | grep -E '^[[:space:]]*-' || true)"
[ -n "$unreleased" ] \
    || die "## Unreleased is empty — nothing to release. Add PR '## Changelog' bullets first."

# --- Stamp the heading: new empty Unreleased + dated version section ---

today="$(date +%Y-%m-%d)"
echo ">> Stamping CHANGELOG: ## Unreleased -> ## $ver — $today"
# GNU sed; escape the em dash literally. Insert a fresh empty Unreleased above
# the stamped version heading so future releases collect cleanly.
sed -i "s/^## Unreleased\$/## Unreleased\n\n## $ver — $today/" "$CHANGELOG"

# --- Commit, tag, push ---

git add "$CHANGELOG"
ALLOW_RELEASE_COMMIT=1 git commit -m "chore(release): $ver"
git tag "$ver"
echo ">> Pushing main + tag $ver to $REMOTE..."
git push "$REMOTE" HEAD "$ver"

echo ">> Released $ver."
