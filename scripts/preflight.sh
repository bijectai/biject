#!/usr/bin/env bash
# =============================================================================
# S4-D-00 — Runtime prereq + capacity preflight for the biject sprint demo
# =============================================================================
# Run this ON the Hetzner host (as the user that will run docker compose),
# BEFORE deploying the S4-D-10/S4-D-11 stack:
#
#     ./scripts/preflight.sh
#
# What it checks:
#   1. Free memory   — WARN if < 6 GB available (OC Tomcat + Postgres 9.5 need
#                      ~4 GB headroom on top of whatever is already running).
#   2. Free disk     — prints `df -h`, WARNs if the root filesystem is > 85% full.
#   3. Egress        — outbound HTTPS to api.openai.com. A bare (unauthenticated)
#                      curl returning ANY HTTP status (200/401/...) is a PASS:
#                      it proves the host can reach OpenAI. Only a transport-level
#                      failure (DNS, firewall, TLS) is a FAIL.
#   4. Responses API — if $OPENAI_API_KEY is exported, POSTs a trivial request to
#                      /v1/responses and reports the status. 2xx = PASS.
#                      Key not set = SKIP (warning only, not a hard fail).
#   5. Docker        — docker + docker compose (v2 plugin) present, daemon
#                      reachable; versions printed.
#
# Every run is appended to a timestamped log under ./preflight-logs/.
# Exit code: 0 if all HARD checks pass (warnings allowed), 1 otherwise.
#
# HARD checks: egress (3), Responses API when a key is present (4), docker (5).
# SOFT checks (warn only): memory (1), disk (2), Responses API when no key (4).
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Logging: mirror everything to a timestamped file under ./preflight-logs/
# (relative to the current working directory, per ticket).
# -----------------------------------------------------------------------------
LOG_DIR="./preflight-logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/preflight-$(date +%Y%m%d-%H%M%S).log"
# Process substitution requires bash (see shebang). All stdout/stderr from here
# on goes to both the terminal and the log file.
exec > >(tee -a "${LOG_FILE}") 2>&1

# -----------------------------------------------------------------------------
# Result bookkeeping.
#
# We run under `set -euo pipefail` for safety, but each individual check is
# invoked as `if ! check_x; then ...` so a failing check NEVER aborts the
# report — failures are collected and summarized at the end. Inside checks,
# commands that may legitimately fail are guarded with `|| true` / `|| echo`.
# -----------------------------------------------------------------------------
HARD_FAILS=0
WARNINGS=0
RESULTS=()   # human-readable one-liners for the final summary table

pass() { echo "  [PASS] $1"; RESULTS+=("PASS  $1"); }
warn() { echo "  [WARN] $1"; RESULTS+=("WARN  $1"); WARNINGS=$((WARNINGS + 1)); }
fail() { echo "  [FAIL] $1"; RESULTS+=("FAIL  $1"); HARD_FAILS=$((HARD_FAILS + 1)); }
skip() { echo "  [SKIP] $1"; RESULTS+=("SKIP  $1"); }

hr()      { printf '%.0s-' {1..78}; echo; }
section() { echo; hr; echo "## $1"; hr; }

echo "biject preflight — $(date -Is)"
echo "host:  $(hostname 2>/dev/null || echo '?')  |  kernel: $(uname -sr)"
echo "log:   ${LOG_FILE}"

# =============================================================================
# 1. Memory (SOFT check — warn under 6 GB available)
# =============================================================================
check_memory() {
    section "1/5 Memory (need ~4 GB headroom for OC Tomcat + Postgres; want >= 6 GB free)"
    # Print the human view first, as required by the ticket.
    free -g || { fail "memory: 'free' not available"; return 0; }

    # Use MiB for the actual comparison so a host with e.g. 5.6 GB free is not
    # rounded up to "6" by free -g. Column 7 of the "Mem:" row is "available",
    # which (unlike "free") accounts for reclaimable page cache.
    local avail_mib
    avail_mib=$(free -m | awk '/^Mem:/{print $7}' || echo 0)
    if [[ -z "${avail_mib}" || ! "${avail_mib}" =~ ^[0-9]+$ ]]; then
        warn "memory: could not parse 'free -m' output; check manually"
    elif (( avail_mib < 6144 )); then
        warn "memory: only ${avail_mib} MiB available (< 6 GiB). OC Tomcat+Postgres need ~4 GB headroom on top of the existing stack — expect swapping/OOM."
    else
        pass "memory: ${avail_mib} MiB available (>= 6 GiB)"
    fi
}

# =============================================================================
# 2. Disk (SOFT check — warn if / is > 85% used)
# =============================================================================
check_disk() {
    section "2/5 Disk"
    df -h || { fail "disk: 'df' not available"; return 0; }

    local used_pct
    used_pct=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9' || echo "")
    if [[ -z "${used_pct}" ]]; then
        warn "disk: could not parse usage of '/'; check df output above manually"
    elif (( used_pct > 85 )); then
        warn "disk: root filesystem ${used_pct}% full (> 85%). Docker images + OC data + Postgres will need several GB."
    else
        pass "disk: root filesystem ${used_pct}% full"
    fi
}

# =============================================================================
# 3. Egress to api.openai.com (HARD check)
#    A bare curl with no auth: HTTP 401 (or 200) from OpenAI is a PASS — it
#    proves DNS + routing + TLS all work. Only "no HTTP response at all" fails.
# =============================================================================
check_egress() {
    section "3/5 Outbound egress to https://api.openai.com"
    if ! command -v curl >/dev/null 2>&1; then
        fail "egress: 'curl' is not installed (apt-get install -y curl)"
        return 0
    fi

    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
        https://api.openai.com/v1/models 2>/dev/null || echo "000")

    if [[ "${code}" == "000" ]]; then
        fail "egress: no HTTP response from api.openai.com (DNS/firewall/TLS problem — check outbound 443)"
    elif [[ "${code}" == "200" || "${code}" == "401" ]]; then
        pass "egress: api.openai.com reachable (HTTP ${code} on unauthenticated request — expected)"
    else
        # Reachable, but an unusual status (403 proxy, 5xx outage, ...). The
        # transport works, so egress itself passes — but flag it.
        warn "egress: api.openai.com reachable but returned unexpected HTTP ${code} (expected 401/200 unauthenticated)"
    fi
}

# =============================================================================
# 4. OpenAI Responses API (HARD if a key is set, SKIP/soft otherwise)
#    The agent (OpenAI Agents SDK) uses the Responses API, so we exercise the
#    real endpoint with a trivial 1-token-ish request rather than /v1/models.
# =============================================================================
check_responses_api() {
    section "4/5 OpenAI Responses API access"
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
        skip "responses-api: OPENAI_API_KEY not set — export it and re-run to verify API access (soft: not a hard fail)"
        warn "responses-api: unverified (no key in environment)"
        return 0
    fi

    # Trivial payload; OPENAI_MODEL is overridable in case the default is not
    # enabled for this key/org. Never echo the key itself.
    local model body http_code resp_file
    model="${OPENAI_MODEL:-gpt-4o-mini}"
    body=$(printf '{"model":"%s","input":"ping","max_output_tokens":16}' "${model}")
    resp_file=$(mktemp)

    http_code=$(curl -sS -o "${resp_file}" -w '%{http_code}' --max-time 30 \
        -X POST https://api.openai.com/v1/responses \
        -H "Authorization: Bearer ${OPENAI_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "${body}" 2>/dev/null || echo "000")

    if [[ "${http_code}" =~ ^2 ]]; then
        pass "responses-api: HTTP ${http_code} for model '${model}' — key works against /v1/responses"
    else
        # Include a short body snippet (OpenAI error messages are useful and
        # never contain the key).
        fail "responses-api: HTTP ${http_code} for model '${model}' — $(head -c 300 "${resp_file}" | tr '\n' ' ')"
    fi
    rm -f "${resp_file}"
}

# =============================================================================
# 5. Docker + docker compose (HARD check)
# =============================================================================
check_docker() {
    section "5/5 Docker & docker compose"

    if command -v docker >/dev/null 2>&1; then
        echo "  docker:         $(docker --version 2>&1 || true)"
        # Binary present is not enough — the daemon must answer, and this user
        # must be allowed to talk to it (docker group / rootless / sudo).
        if docker info >/dev/null 2>&1; then
            pass "docker: CLI present and daemon reachable"
        else
            fail "docker: CLI present but daemon unreachable (is dockerd running? is $(id -un) in the 'docker' group?)"
        fi
    else
        fail "docker: not installed"
    fi

    # Compose v2 plugin ('docker compose'), which is what Coolify uses.
    if docker compose version >/dev/null 2>&1; then
        echo "  docker compose: $(docker compose version 2>&1 || true)"
        pass "docker compose: v2 plugin present"
    elif command -v docker-compose >/dev/null 2>&1; then
        echo "  docker-compose: $(docker-compose --version 2>&1 || true)"
        warn "docker compose: only legacy standalone 'docker-compose' found — Coolify expects the v2 plugin ('docker compose')"
    else
        fail "docker compose: not installed (need the compose v2 plugin)"
    fi
}

# -----------------------------------------------------------------------------
# Run all checks. Each is wrapped so an internal failure can never abort the
# report (checks themselves always return 0 and record results via pass/warn/
# fail); the `if !` guard is belt-and-braces against unexpected errors under
# set -e.
# -----------------------------------------------------------------------------
if ! check_memory;        then fail "memory check crashed unexpectedly"; fi
if ! check_disk;          then fail "disk check crashed unexpectedly"; fi
if ! check_egress;        then fail "egress check crashed unexpectedly"; fi
if ! check_responses_api; then fail "responses-api check crashed unexpectedly"; fi
if ! check_docker;        then fail "docker check crashed unexpectedly"; fi

# =============================================================================
# Summary
# =============================================================================
section "Summary"
for line in "${RESULTS[@]}"; do echo "  ${line}"; done
echo
echo "  hard failures: ${HARD_FAILS}   warnings: ${WARNINGS}"
echo "  full log: ${LOG_FILE}"
echo

if (( HARD_FAILS > 0 )); then
    echo "RESULT: FAIL — fix the hard failures above before deploying the stack."
    exit 1
fi
echo "RESULT: PASS — host is ready for the S4-D-10 / S4-D-11 stack (review warnings above)."
exit 0
