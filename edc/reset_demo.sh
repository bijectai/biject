#!/usr/bin/env bash
# =============================================================================
# reset_demo.sh — restore the OpenClinica demo DB to its pre-demo state (<60s)
# =============================================================================
# Run ON the Hetzner host (the machine running the compose stacks in
# infra/hetzner/). Two modes:
#
#   ./reset_demo.sh snapshot     take the golden snapshot. Run ONCE, right
#                                after seeding is complete and verified
#                                (edc/README.md step 8: seed.py --verify shows
#                                16 open queries). Refuses to overwrite an
#                                existing snapshot unless --force is given.
#
#   ./reset_demo.sh [restore]    restore the golden snapshot (default mode).
#                                Idempotent — safe to run repeatedly; each run
#                                ends in the identical post-seeding state.
#                                Refuses to run if the snapshot file is
#                                missing.
#
# WHY A DB RESTORE AND NOT A RE-IMPORT: OC 3.17's import-time rule runner
# consults rule_action_run_log and drops any DiscrepancyNoteAction that
# already ran for the same item + value + rule
# (ImportDataRuleRunner.populateToBeExpected, "findCountByRuleActionRunLogBean
# > 0 -> itr.remove()", verified at tag 3.17.2). Re-running seed.py against a
# played-through study therefore re-imports the values but does NOT re-create
# the discrepancy notes — the demo would start with resolved/corrected data
# and no open queries. pg_restore of the post-seeding snapshot rolls back the
# item values, the discrepancy notes, the audit rows AND rule_action_run_log
# in one shot, so every demo run starts from the identical state.
#
# WHAT IS (NOT) COVERED: the demo run writes only to Postgres (item values,
# discrepancy notes, audit, rule action log). The oc_data volume (uploaded
# CRF templates, extracts) is not touched by demo traffic, so a DB-only
# restore is sufficient. If you rebuild the study or re-upload CRFs, take a
# fresh snapshot.
#
# MECHANISM: pg_dump -Fc / pg_restore of the "openclinica" DB, executed with
# docker exec against the oc-db container of the
# infra/hetzner/openclinica/docker-compose.openclinica.yml stack (service
# names oc-db + openclinica; the container is resolved with `docker compose
# ps -q`, so the compose project name does not matter). During restore the
# openclinica (Tomcat) container is PAUSED — not restarted — so the reset
# stays well under 60s: OC 3.17's webapp startup alone takes minutes, while
# pause/unpause is instant. Remaining DB connections are terminated before
# the drop; the connection pool re-establishes on the next request after
# unpause. If the UI shows connection errors that persist past the first
# reload, `docker compose ... restart openclinica` is the (slower) fallback.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- configuration (override via environment) --------------------------------
# Compose file + env file of the OpenClinica stack (see the header of
# docker-compose.openclinica.yml: the shared .env lives one directory up).
OC_COMPOSE_FILE="${OC_COMPOSE_FILE:-$SCRIPT_DIR/../infra/hetzner/openclinica/docker-compose.openclinica.yml}"
OC_ENV_FILE="${OC_ENV_FILE:-$(dirname "$OC_COMPOSE_FILE")/../.env}"
OC_DB_NAME="${OC_DB_NAME:-openclinica}"
OC_DB_OWNER="${OC_DB_USER:-clinica}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-$SCRIPT_DIR/oc_demo_snapshot.dump}"

MODE="${1:-restore}"
FORCE="${2:-}"

compose() {
    docker compose --env-file "$OC_ENV_FILE" -f "$OC_COMPOSE_FILE" "$@"
}

die() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$OC_COMPOSE_FILE" ] || die "compose file not found: $OC_COMPOSE_FILE (set OC_COMPOSE_FILE)"
[ -f "$OC_ENV_FILE" ] || die "env file not found: $OC_ENV_FILE (set OC_ENV_FILE)"

DB_CID="$(compose ps -q oc-db)"
[ -n "$DB_CID" ] || die "oc-db container is not running (docker compose ps -q oc-db returned nothing)"
OC_CID="$(compose ps -q openclinica)"
[ -n "$OC_CID" ] || die "openclinica container is not running"

# psql/pg_dump/pg_restore run inside the oc-db container as the postgres
# superuser over the local socket — no password needed, nothing exposed.
pg() { docker exec -i "$DB_CID" "$@"; }

case "$MODE" in

snapshot)
    if [ -f "$SNAPSHOT_FILE" ] && [ "$FORCE" != "--force" ]; then
        die "snapshot already exists: $SNAPSHOT_FILE
Refusing to overwrite the golden snapshot. Re-run as
'./reset_demo.sh snapshot --force' only if you have re-seeded and really
want a new baseline."
    fi
    echo "Taking snapshot of database '$OC_DB_NAME' from container $DB_CID ..."
    TMP_FILE="$SNAPSHOT_FILE.tmp.$$"
    pg pg_dump -U postgres -Fc "$OC_DB_NAME" > "$TMP_FILE"
    [ -s "$TMP_FILE" ] || { rm -f "$TMP_FILE"; die "pg_dump produced an empty file"; }
    mv "$TMP_FILE" "$SNAPSHOT_FILE"
    echo "Snapshot written: $SNAPSHOT_FILE ($(du -h "$SNAPSHOT_FILE" | cut -f1))"
    echo "This is the pre-demo baseline every './reset_demo.sh' will restore."
    ;;

restore)
    [ -f "$SNAPSHOT_FILE" ] || die "snapshot file missing: $SNAPSHOT_FILE
Nothing to restore. Take it once, immediately after seeding is complete and
verified, with './reset_demo.sh snapshot' (see edc/README.md step 8)."
    START_TS=$(date +%s)

    echo "Pausing the openclinica (Tomcat) container ..."
    docker pause "$OC_CID" >/dev/null
    # From here on, always unpause — even if the restore fails midway.
    trap 'docker unpause "$OC_CID" >/dev/null 2>&1 || true' EXIT

    echo "Dropping and recreating database '$OC_DB_NAME' ..."
    pg psql -U postgres -v ON_ERROR_STOP=1 -d postgres \
        -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$OC_DB_NAME' AND pid <> pg_backend_pid();" \
        >/dev/null
    pg psql -U postgres -v ON_ERROR_STOP=1 -d postgres \
        -c "DROP DATABASE IF EXISTS \"$OC_DB_NAME\";" \
        -c "CREATE DATABASE \"$OC_DB_NAME\" OWNER \"$OC_DB_OWNER\";" \
        >/dev/null

    echo "Restoring snapshot ($(du -h "$SNAPSHOT_FILE" | cut -f1)) ..."
    # --single-transaction: the restored state becomes visible atomically.
    pg pg_restore -U postgres -d "$OC_DB_NAME" --single-transaction --no-owner --role="$OC_DB_OWNER" \
        < "$SNAPSHOT_FILE"

    echo "Unpausing the openclinica container ..."
    docker unpause "$OC_CID" >/dev/null
    trap - EXIT

    ELAPSED=$(( $(date +%s) - START_TS ))
    echo "Done in ${ELAPSED}s. Demo state restored to the post-seeding baseline"
    echo "(16 open queries; item values, notes, audit and rule_action_run_log"
    echo "all rolled back together)."
    echo "Quick check: python edc/seed.py --verify-only   # expect 16 open queries"
    ;;

*)
    die "unknown mode '$MODE' (use 'snapshot' or 'restore')"
    ;;
esac
