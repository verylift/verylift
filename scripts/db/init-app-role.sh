#!/bin/sh
# Provision the least-privilege application database role.
#
# Used by the Kamal deployment only (config/deploy.yml mounts it into the `db`
# accessory). docker-compose.selfhost.yml deliberately does not: that setup is
# monolithic and its app container runs its own migrations, so it connects as
# the owner role and has no scoped role to provision.
#
# Two invocation paths, both idempotent and safe to re-run:
#   1. Fresh installs: bind-mounted into /docker-entrypoint-initdb.d and run
#      automatically by the postgres image entrypoint the first time the data
#      volume is initialized.
#   2. Existing volumes: run manually against a running db container, e.g.
#      `kamal accessory exec db /docker-entrypoint-initdb.d/init-app-role.sh`.
#      initdb.d scripts do NOT run on an already-initialized volume, so this is
#      the path for an existing prod database.
#
# It creates/updates a scoped role (LOGIN, no SUPERUSER/CREATEDB/CREATEROLE)
# that the app container connects as at runtime. The role can perform DML on
# application tables but cannot create tables, roles, or databases. The owner
# role (POSTGRES_USER) continues to run migrations and seeds via the
# `app-admin` compose service.
#
# Required environment (present in the db container):
#   POSTGRES_USER, POSTGRES_DB       - owner role and database
#   POSTGRES_APP_USER                - scoped role name
#   POSTGRES_APP_PASSWORD            - scoped role password
set -eu

: "${POSTGRES_USER:?POSTGRES_USER must be set}"
: "${POSTGRES_DB:?POSTGRES_DB must be set}"
: "${POSTGRES_APP_USER:?POSTGRES_APP_USER must be set}"
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD must be set}"

echo "Provisioning least-privilege app role '${POSTGRES_APP_USER}' on database '${POSTGRES_DB}'..."

# --set binds shell values to psql variables for the non-secret identifiers.
# The password is deliberately NOT passed via --set: --set args land in argv and
# are visible in `ps` for the life of the psql process. Instead the SQL below
# reads it straight from the process environment with `\getenv` (psql 9.6+; the
# image is postgres:16-alpine), so the password never appears in argv. :"app_user"
# (identifier quoting) and :'app_password' (literal quoting) keep values out of
# the SQL text and defend against injection.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set owner_user="$POSTGRES_USER" \
    --set db_name="$POSTGRES_DB" \
    --set app_user="$POSTGRES_APP_USER" <<'EOSQL'
-- Read the password from the environment (POSTGRES_APP_PASSWORD is exported into
-- the db container) rather than from an --set arg, keeping it out of argv/ps.
\getenv app_password POSTGRES_APP_PASSWORD

-- Create the role if it does not already exist (roles are cluster-level, so
-- guard with pg_roles). There is no CREATE ROLE IF NOT EXISTS, and psql does not
-- interpolate variables inside dollar-quoted DO blocks, so build the statement
-- at top level with format() (%I/%L quote the identifier/literal safely) and run
-- it via \gexec. The WHERE NOT EXISTS returns zero rows when the role is already
-- present, making this idempotent.
SELECT format(
    'CREATE ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION PASSWORD %L',
    :'app_user', :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec

-- Always sync the password (keeps it in step with .env; also the rotation path).
ALTER ROLE :"app_user" WITH LOGIN PASSWORD :'app_password';

-- Connect + schema usage. USAGE on public grants no CREATE, so the role cannot
-- add objects to the schema. On PG 15+/16 the public schema no longer grants
-- CREATE to PUBLIC by default, so this stays least-privilege.
GRANT CONNECT ON DATABASE :"db_name" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";

-- DML on all current tables and read/next on all current sequences. No UPDATE
-- on sequences (setval is only needed by fixture loading, which runs as owner).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";

-- The critical piece: objects CREATED BY the owner role in future (migrations)
-- automatically carry these grants, so new migrations need no re-run of this
-- script. FOR ROLE must name the owner because that is who `migrate` creates
-- objects as.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_user" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_user" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
EOSQL

echo "App role '${POSTGRES_APP_USER}' provisioned."
