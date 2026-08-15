#!/usr/bin/env bash
# Cut a release end to end: validate (or auto-suggest) a version, collect
# changelog, stamp the heading, build + push the production image via Kamal,
# commit, tag, push, and create a GitHub Release from that tag's changelog
# section. Building is no longer a separate, deployment-specific concern —
# it happens here so the pushed image matches the tagged commit exactly;
# `just deploy` only pulls and cuts over. Requires `kamal` (and Docker) and
# the `gh` CLI (already a dependency via extract_changelog.sh) available on
# whatever machine runs this script.
#
# Usage: scripts/release.sh [YYYY.MM.PATCH]
# Omit the version to get an auto-suggested one based on today's date and the
# highest existing patch already tagged this month.
#
# Fails fast (before any mutation) if:
#   - the version is not YYYY.MM.PATCH
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

if [ -z "$ver" ]; then
    git fetch --tags "$REMOTE" >/dev/null 2>&1 || true
    year="$(date +%Y)"
    month=$((10#$(date +%m)))   # strip any leading zero; portable (no GNU date -%-m)
    prefix="${year}.${month}."
    last_patch="$(git tag -l "${prefix}*" | sed -E "s/^${prefix}//" | sort -n | tail -1)"
    patch=$(( ${last_patch:-0} + $([ -n "$last_patch" ] && echo 1 || echo 0) ))
    suggested="${prefix}${patch}"
    printf 'No version given. Suggested version: %s\n' "$suggested"
    read -r -p "Use this version? [Y/n] " confirm
    case "$confirm" in
        [nN]*) die "aborted — pass an explicit version: just release YYYY.MM.PATCH" ;;
        *) ver="$suggested" ;;
    esac
fi

# --- Preconditions (all checked before we mutate anything) ---

echo "$ver" | grep -qE '^[0-9]{4}\.[0-9]{1,2}\.[0-9]+$' \
    || die "version must look like YYYY.MM.PATCH (got '$ver')"

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

# .kamal/secrets-common shells out to `bw get password ...` for every secret,
# which needs an unlocked vault session. Unlock up front, before any mutation,
# so the master-password prompt happens here instead of getting buried under
# `kamal build push`'s Docker build output later on.
if [ -z "${BW_SESSION:-}" ]; then
    echo ">> Unlocking Bitwarden vault..."
    export BW_SESSION="$(bw unlock --raw)"
fi
# Sync even with a session already exported: a vault item added/edited
# elsewhere isn't visible to this CLI's cache until synced, and `bw get
# password` on a stale cache either 404s on a brand-new item or silently
# resolves to a stale value -- Kamal doesn't treat that as fatal, so a secret
# can go out wrong with no failed command to point at.
echo ">> Syncing Bitwarden vault..."
bw sync

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

# --- Commit, tag, build + push image, push ---

git add "$CHANGELOG"
ALLOW_RELEASE_COMMIT=1 git commit -m "chore(release): $ver"
git tag "$ver"

echo ">> Building and pushing production image (version $ver)..."
kamal build push --version="$ver"

echo ">> Pushing main + tag $ver to $REMOTE..."
git push "$REMOTE" HEAD "$ver"

# --- Create a GitHub Release from this version's changelog section ---

echo ">> Creating GitHub Release $ver..."
# Matches the heading this script itself just stamped ("## $ver — $today"),
# anchored so a version that happens to be a prefix of another (unlikely
# under YYYY.MM.PATCH, but cheap to guard) can't match the wrong section.
release_notes="$(awk -v heading="## $ver " '
    index($0, heading) == 1 { grab=1; next }
    grab && /^## / { exit }
    grab { print }
' "$CHANGELOG")"
gh release create "$ver" --title "$ver" --notes "$release_notes"

echo ">> Released $ver."
