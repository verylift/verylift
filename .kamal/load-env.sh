#!/usr/bin/env bash
# Source this before running any `kamal` command against production:
#   source .kamal/load-env.sh
#
# Pulls the deploy-time values that config/deploy.yml interpolates via ERB
# (DEPLOY_HOST, DEPLOY_SSH_USER, DEPLOY_HOST_NAME, ALLOWED_HOSTS,
# GHCR_USERNAME) from the "very lift" Bitwarden folder. These are NOT handled
# by .kamal/secrets-common: that file is read by Kamal itself for values that
# end up in the container's runtime env, but deploy.yml's own ERB tags
# resolve from the real shell environment at YAML-parse time, before secrets
# are loaded at all — Kamal has no first-class Bitwarden integration for its
# own config structure, only for secrets. Re-run this every time before
# deploying, rather than relying on a long-lived shell's exported values —
# an exported DEPLOY_HOST from an earlier `-d local` test silently surviving
# into a later real deploy is exactly the mistake this exists to prevent.
set -euo pipefail

if [ -z "${BW_SESSION:-}" ]; then
  echo "BW_SESSION not set. Run: export BW_SESSION=\"\$(bw unlock --raw)\"" >&2
  return 1 2>/dev/null || exit 1
fi

export DEPLOY_HOST
export DEPLOY_SSH_USER
export DEPLOY_HOST_NAME
export ALLOWED_HOSTS
export GHCR_USERNAME

DEPLOY_HOST="$(bw get item verylift-deploy-config | jq -r '.fields[] | select(.name=="deploy_host").value')"
DEPLOY_SSH_USER="$(bw get item verylift-deploy-config | jq -r '.fields[] | select(.name=="deploy_ssh_user").value')"
DEPLOY_HOST_NAME="$(bw get item verylift-deploy-config | jq -r '.fields[] | select(.name=="deploy_host_name").value')"
ALLOWED_HOSTS="$(bw get item verylift-deploy-config | jq -r '.fields[] | select(.name=="allowed_hosts").value')"
GHCR_USERNAME="$(bw get username verylift-ghcr)"

echo "Loaded Kamal deploy env: DEPLOY_HOST=${DEPLOY_HOST}  DEPLOY_HOST_NAME=${DEPLOY_HOST_NAME}"
