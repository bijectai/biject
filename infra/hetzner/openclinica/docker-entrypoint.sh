#!/usr/bin/env bash
# =============================================================================
# S4-D-11 — OpenClinica container entrypoint
# =============================================================================
# Renders the committed datainfo.properties TEMPLATE (env-var placeholders)
# into the real config location of BOTH deployed webapps, waits for Postgres,
# then execs Tomcat. Runs on every container start, so config always tracks
# the current environment (rotating OC_DB_PASSWORD = restart the container).
# =============================================================================
set -euo pipefail

CATALINA_HOME="${CATALINA_HOME:-/usr/local/tomcat}"
TEMPLATE="/opt/oc/datainfo.properties.template"

# -----------------------------------------------------------------------------
# 1. Render datainfo.properties from the template.
#
# envsubst is given an EXPLICIT variable list so that only our ${OC_*} /
# ${DEMO_DOMAIN} placeholders are substituted — any literal `${...}` that OC
# itself understands (e.g. ${catalina.home}) passes through untouched.
# -----------------------------------------------------------------------------
: "${OC_DB_HOST:?OC_DB_HOST is required}"
: "${OC_DB_PORT:=5432}"
: "${OC_DB_USER:?OC_DB_USER is required}"
: "${OC_DB_PASSWORD:?OC_DB_PASSWORD is required}"
: "${OC_DB_NAME:?OC_DB_NAME is required}"
: "${DEMO_DOMAIN:?DEMO_DOMAIN is required}"
: "${OC_ADMIN_EMAIL:=ops@example.com}"
: "${OC_MAIL_HOST:=localhost}"
export OC_DB_HOST OC_DB_PORT OC_DB_USER OC_DB_PASSWORD OC_DB_NAME \
       DEMO_DOMAIN OC_ADMIN_EMAIL OC_MAIL_HOST

VARS='${OC_DB_HOST} ${OC_DB_PORT} ${OC_DB_USER} ${OC_DB_PASSWORD} ${OC_DB_NAME} ${DEMO_DOMAIN} ${OC_ADMIN_EMAIL} ${OC_MAIL_HOST}'

# OC 3.x reads WEB-INF/classes/datainfo.properties from inside each webapp.
# The UI and ws apps share one database and one filePath (they must — ws
# writes into the same studies the UI displays), so one rendered file serves
# both. If you rename a webapp context, update the paths here to match.
for APP in OpenClinica OpenClinica-ws; do
    TARGET="${CATALINA_HOME}/webapps/${APP}/WEB-INF/classes/datainfo.properties"
    if [[ ! -d "$(dirname "${TARGET}")" ]]; then
        echo "FATAL: ${APP} not exploded under webapps/ — was the WAR present in dist/ at build time?" >&2
        exit 1
    fi
    envsubst "${VARS}" < "${TEMPLATE}" > "${TARGET}"
    echo "entrypoint: rendered datainfo.properties -> ${APP}"
done

# ws also honors WEB-INF/classes/extract.properties etc. — defaults are fine
# for the demo; add rendering here if the spike ends up needing them.

# -----------------------------------------------------------------------------
# 2. Wait for Postgres (belt-and-braces on top of compose's service_healthy —
#    covers container restarts where compose ordering does not re-apply).
#    Uses bash's /dev/tcp so we need no pg client in the image.
# -----------------------------------------------------------------------------
echo "entrypoint: waiting for ${OC_DB_HOST}:${OC_DB_PORT} ..."
for i in $(seq 1 60); do
    if (exec 3<>"/dev/tcp/${OC_DB_HOST}/${OC_DB_PORT}") 2>/dev/null; then
        exec 3>&- 3<&- || true
        echo "entrypoint: database is accepting connections"
        break
    fi
    if [[ "${i}" == "60" ]]; then
        echo "FATAL: ${OC_DB_HOST}:${OC_DB_PORT} not reachable after 120s" >&2
        exit 1
    fi
    sleep 2
done

# -----------------------------------------------------------------------------
# 3. Hand off to Tomcat (CMD = catalina.sh run). exec so Tomcat is PID 1 and
#    receives SIGTERM from `docker stop` for a clean shutdown.
# -----------------------------------------------------------------------------
exec "$@"
