#!/usr/bin/env bash
# Source this before running any `kamal` command against production:
#   source .kamal/load-env.sh
#
# Pulls the deploy-time values that config/deploy.yml interpolates via ERB
# (DEPLOY_HOST, DEPLOY_SSH_USER, DEPLOY_HOST_NAME, ALLOWED_HOSTS,
# GHCR_USERNAME) from Bitwarden. These are NOT handled by
# .kamal/secrets-common: that file is read by Kamal itself for values that
# end up in the container's runtime env, but deploy.yml's own ERB tags
# resolve from the real shell environment at YAML-parse time, before secrets
# are loaded at all — Kamal has no first-class Bitwarden integration for its
# own config structure, only for secrets. Re-run this every time before
# deploying, rather than relying on a long-lived shell's exported values —
# an exported DEPLOY_HOST from an earlier `-d local` test silently surviving
# into a later real deploy is exactly the mistake this exists to prevent.
#
# Deliberately does NOT `set -euo pipefail`: this is meant to be sourced into
# an interactive shell, and those options would persist there afterward
# rather than dying with a subprocess the way they would in an executed
# script — every command below is checked explicitly instead. Each failure
# check below is inlined at the top level (`... || return 1 2>/dev/null ||
# exit 1`) rather than wrapped in a helper function: a `return` inside a
# helper only unwinds that function, not the sourced script calling it, so
# wrapping this in a "die" function would print the error and then silently
# keep going — exactly the wrong thing for a precondition check.

if [ -z "${BW_SESSION:-}" ]; then
  echo "BW_SESSION not set. Run: export BW_SESSION=\"\$(bw unlock --raw)\"" >&2
  return 1 2>/dev/null || exit 1
fi

# A missing/mistyped item name doesn't necessarily fail loudly: `bw get`
# prints "Not found." and exits 0 for an unmatched item, so this checks the
# resolved value itself, not just each command's exit status.
_check_value() {
  local name="$1" value="$2"
  if [ -z "$value" ] || [ "$value" = "Not found." ]; then
    echo "Failed to load $name from Bitwarden (got: '${value:-<empty>}')" >&2
    return 1
  fi
}

_deploy_config_field() {
  # set -o pipefail here is scoped to the subshell each `$(_deploy_config_field
  # ...)` call site already creates via command substitution — it does not
  # leak into the calling shell the way a top-level `set -o pipefail` would.
  # Without it, a failed `bw get item` would be masked by jq's own exit
  # status (0, even fed empty/error input), silently yielding an empty value.
  set -o pipefail
  bw get item verylift-deploy-config | jq -r --arg name "$1" '.fields[] | select(.name == $name).value'
}

DEPLOY_HOST="$(_deploy_config_field deploy_host)"
_check_value DEPLOY_HOST "$DEPLOY_HOST" || return 1 2>/dev/null || exit 1

DEPLOY_SSH_USER="$(_deploy_config_field deploy_ssh_user)"
_check_value DEPLOY_SSH_USER "$DEPLOY_SSH_USER" || return 1 2>/dev/null || exit 1

DEPLOY_HOST_NAME="$(_deploy_config_field deploy_host_name)"
_check_value DEPLOY_HOST_NAME "$DEPLOY_HOST_NAME" || return 1 2>/dev/null || exit 1

ALLOWED_HOSTS="$(_deploy_config_field allowed_hosts)"
_check_value ALLOWED_HOSTS "$ALLOWED_HOSTS" || return 1 2>/dev/null || exit 1

GHCR_USERNAME="$(bw get username verylift-ghcr)"
_check_value GHCR_USERNAME "$GHCR_USERNAME" || return 1 2>/dev/null || exit 1

export DEPLOY_HOST DEPLOY_SSH_USER DEPLOY_HOST_NAME ALLOWED_HOSTS GHCR_USERNAME

echo "Loaded Kamal deploy env: DEPLOY_HOST=${DEPLOY_HOST}  DEPLOY_HOST_NAME=${DEPLOY_HOST_NAME}"

unset -f _check_value _deploy_config_field
