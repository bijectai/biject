#!/usr/bin/env bash
# =============================================================================
# S4-D-11 — Postgres first-boot init for OpenClinica 3.17 CE
# =============================================================================
# Mounted into the postgres:9.5 container at
# /docker-entrypoint-initdb.d/10-init-db.sh. The official postgres image runs
# it EXACTLY ONCE, on first start with an empty data volume (biject-oc-pgdata).
# It never runs again on restarts/redeploys — to re-init from scratch you must
# remove that volume (which destroys all study data).
#
# Creates:
#   * role `clinica`   (login; password from OC_DB_PASSWORD in ../.env)
#   * db  `openclinica` owned by it (UTF8 — OC requires it)
# OC's own schema is then created by the OpenClinica webapp itself on ITS
# first start (Tomcat log shows the DDL run; first boot takes a few minutes).
#
# Env consumed (injected via the oc-db service environment):
#   POSTGRES_USER (default postgres), OC_DB_USER, OC_DB_PASSWORD, OC_DB_NAME
# =============================================================================
set -euo pipefail

: "${OC_DB_USER:=clinica}"
: "${OC_DB_NAME:=openclinica}"
: "${OC_DB_PASSWORD:?OC_DB_PASSWORD must be set (see ../.env.example)}"

echo "init-db: creating role '${OC_DB_USER}' and database '${OC_DB_NAME}'"

# Password is passed via a psql variable (:'ocpass') rather than interpolated
# into the SQL text, so special characters cannot break out of the literal.
psql -v ON_ERROR_STOP=1 \
     -v ocuser="${OC_DB_USER}" \
     -v ocpass="${OC_DB_PASSWORD}" \
     --username "${POSTGRES_USER:-postgres}" \
     --dbname postgres <<'EOSQL'
    -- Application role: plain LOGIN role, no superuser/createdb — OC only
    -- needs to own its objects inside its own database.
    CREATE ROLE :"ocuser" LOGIN PASSWORD :'ocpass';
EOSQL

# CREATE DATABASE cannot run inside a transaction block with other statements
# on 9.5, so issue it separately. Encoding must be UTF8 for OC.
psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER:-postgres}" \
     --dbname postgres \
     -c "CREATE DATABASE \"${OC_DB_NAME}\" OWNER \"${OC_DB_USER}\" ENCODING 'UTF8' TEMPLATE template0;"

echo "init-db: done — '${OC_DB_NAME}' ready for OpenClinica's first-boot schema install"
