#!/usr/bin/env bash
# Collect "## Changelog" bullets from squash-merged PRs since the last tag and
# file them under "## Unreleased" in CHANGELOG.md, grouped into "### <Category>"
# subheadings. Idempotent: a bullet whose text already exists anywhere in
# CHANGELOG.md is skipped.
#
# Each bullet may start with a category tag, e.g.:
#   - [Security] Login endpoints are now rate limited.
#   - [Fixed] Editing a custom goal on a finished competition no longer re-scores it.
# Recognized tags (case-insensitive): Added, Changed, Fixed, Security, Removed.
# Untagged bullets default to Changed.
#
# Bullets that report no user-facing change (pure backlog/docs/chore
# housekeeping) are dropped entirely — match on the literal phrase
# "no user-facing change" anywhere in the bullet text. PRs with nothing but
# housekeeping to report should omit the "## Changelog" section entirely
# rather than including a placeholder bullet (see CLAUDE.md).
set -euo pipefail

CHANGELOG="${CHANGELOG:-CHANGELOG.md}"

if [ ! -f "$CHANGELOG" ]; then
    echo "error: $CHANGELOG not found" >&2
    exit 1
fi

# Canonical category order (Keep a Changelog style).
CATEGORIES="Added Changed Fixed Security Removed"

last_tag="$(git describe --tags --abbrev=0 2>/dev/null || true)"
if [ -n "$last_tag" ]; then
    range="${last_tag}..HEAD"
else
    range="HEAD"
fi

# PR numbers from squash-merge subjects: "... (#123)". Oldest first so
# changelog order matches merge order.
pr_numbers="$(git log "$range" --pretty=%s | grep -oE '\(#[0-9]+\)$' | tr -d '(#)' | tac || true)"

if [ -z "$pr_numbers" ]; then
    echo "No PRs found in range $range; nothing to add."
    exit 0
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# Seed per-category files with whatever bullets already exist under each
# "### Category" subheading of "## Unreleased" (so re-running is idempotent
# and preserves manually-added entries).
for cat in $CATEGORIES; do
    awk -v cat="### $cat" '
        /^## Unreleased[[:space:]]*$/ { inblock=1; next }
        inblock && /^## [^#]/ { exit }
        inblock && /^### / { grab = ($0 == cat); next }
        inblock && grab && /^[[:space:]]*-/ { print }
    ' "$CHANGELOG" > "$workdir/$cat.txt"
done
# Also capture any pre-existing bullets directly under Unreleased with no
# "### " subheading above them (legacy/manual entries) and file them as
# Changed so they are not lost when the block is rebuilt. Matches
# "^[[:space:]]*-" (not just column-0 "^-") since inserted bullets can carry
# leading indentation through from the source PR body.
awk '
    /^## Unreleased[[:space:]]*$/ { inblock=1; next }
    inblock && /^## [^#]/ { exit }
    inblock && /^### / { exit }
    inblock && /^[[:space:]]*-/ { print }
' "$CHANGELOG" >> "$workdir/Changed.txt"

added_any=0
for pr in $pr_numbers; do
    body="$(gh pr view "$pr" --json body -q '.body' 2>/dev/null || true)"
    [ -z "$body" ] && continue

    # Content between "## Changelog" and the next "## " heading (or EOF).
    section="$(printf '%s\n' "$body" | awk '
        /^## Changelog[[:space:]]*$/ { grab=1; next }
        grab && /^## / { exit }
        grab { print }
    ')"

    # Keep only bullet lines (starting with -). This filters out footers,
    # attributions, and blank lines.
    lines="$(printf '%s\n' "$section" | grep '^[[:space:]]*-' || true)"
    [ -z "$lines" ] && continue

    while IFS= read -r raw_line; do
        [ -z "$raw_line" ] && continue

        # Drop bullets that explicitly report no user-facing change.
        if printf '%s' "$raw_line" | grep -qi 'no user-facing change'; then
            continue
        fi

        # Extract an optional leading "[Category]" tag; default to Changed.
        # Anchored to "^[[:space:]]*-" to match the (possibly indented) bullet
        # lines collected above.
        category="Changed"
        text="$raw_line"
        tag_word="$(printf '%s' "$raw_line" | grep -oE '^[[:space:]]*-[[:space:]]*\[[A-Za-z]+\]' | grep -oE '[A-Za-z]+' || true)"
        if [ -n "$tag_word" ]; then
            tag_cap="$(printf '%s' "$tag_word" | awk '{print toupper(substr($0,1,1)) tolower(substr($0,2))}')"
            case " $CATEGORIES " in
                *" $tag_cap "*)
                    category="$tag_cap"
                    text="$(printf '%s' "$raw_line" | sed -E 's/^([[:space:]]*)-[[:space:]]*\[[A-Za-z]+\][[:space:]]*/\1- /')"
                    ;;
            esac
        fi

        # Idempotency: skip if this exact bullet text already exists anywhere
        # in the changelog, regardless of which category it lands under.
        # "--" stops grep from parsing a "- ..." bullet as an option string.
        if grep -Fxq -- "$text" "$CHANGELOG"; then
            continue
        fi

        echo "$text" >> "$workdir/$category.txt"
        added_any=1
        echo "Added (PR #$pr, $category): $text"
    done <<EOF
$lines
EOF
done

if [ "$added_any" -eq 0 ]; then
    echo "No new changelog entries to add."
    exit 0
fi

# Rebuild the Unreleased block from the per-category files, in canonical
# order, skipping empty categories.
new_block="$(mktemp)"
{
    echo "## Unreleased"
    echo
    for cat in $CATEGORIES; do
        if [ -s "$workdir/$cat.txt" ]; then
            echo "### $cat"
            cat "$workdir/$cat.txt"
            echo
        fi
    done
} > "$new_block"
# The loop above leaves one blank line trailing after the last category's
# bullets — keep it as the separator before the next "## vX.Y.Z" heading (or
# end of file), matching the existing spacing convention between sections.

# Splice: replace the old "## Unreleased" ... (next "## " or EOF) block with
# the rebuilt one.
awk -v newblock="$new_block" '
    BEGIN { while ((getline line < newblock) > 0) nb[++n] = line }
    /^## Unreleased[[:space:]]*$/ {
        for (i = 1; i <= n; i++) print nb[i]
        skipping = 1
        next
    }
    skipping && /^## [^#]/ { skipping = 0 }
    skipping { next }
    { print }
' "$CHANGELOG" > "$CHANGELOG.tmp"
mv "$CHANGELOG.tmp" "$CHANGELOG"
rm -f "$new_block"
