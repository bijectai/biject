#!/usr/bin/env bash
# =============================================================================
# verify_lockdown.sh — THE acceptance test for the network enforcement bound.
# Ticket S4-D-21.
#
# Sprint acceptance wording: the agent-to-OC refusal must be "tested and
# confirmed refused, not assumed". This script is that test. Run it FROM THE
# AGENT'S NETWORK VANTAGE POINT — i.e. inside the agent container/host:
#
#   docker compose exec agent /infra/hetzner/firewall/verify_lockdown.sh
#   # or, from the agent host itself:
#   ./verify_lockdown.sh
#
# It performs two probes and exits 0 ONLY if BOTH hold:
#
#   [1] NEGATIVE — a DIRECT call to OpenClinica from here MUST FAIL
#       (connection refused / dropped / timed out). Any HTTP response at all,
#       even 4xx/5xx, means a route to OC exists and the lockdown is BROKEN.
#   [2] POSITIVE — a PROXY-ROUTED health call MUST SUCCEED. Without this, an
#       unplugged cable would "pass" test [1]; the positive probe proves the
#       sanctioned path works, so the refusal in [1] is a real policy refusal,
#       not a dead network.
#
# Parameterization (env overrides; defaults match oc-ingress.nft /
# docker-user-rules.sh / the compose ipam config):
# =============================================================================
set -u  # NOT -e: we *expect* command failures in the negative probe.

OC_DIRECT_URL="${OC_DIRECT_URL:-http://172.28.100.30:8080/OpenClinica/}"  # OC on edc_internal — must be UNREACHABLE from here
PG_DIRECT_HOSTPORT="${PG_DIRECT_HOSTPORT:-172.28.100.40:5432}"            # Postgres on edc_internal — must be UNREACHABLE too
PROXY_HEALTH_URL="${PROXY_HEALTH_URL:-https://172.28.99.10:8443/health}" # proxy health — must be REACHABLE
CURL_TIMEOUT="${CURL_TIMEOUT:-5}"                                       # short: a DROP manifests as a timeout
CURL_EXTRA_ARGS="${CURL_EXTRA_ARGS:-}"                                  # e.g. --cacert /etc/biject/proxy-ca.pem

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl required"; exit 2; }

pass=0
fail=0

# ----------------------------------------------------------------------------
# [1a] NEGATIVE probe: direct HTTP to OpenClinica.
# ----------------------------------------------------------------------------
echo "[1a] direct agent -> OC HTTP probe: ${OC_DIRECT_URL} (timeout ${CURL_TIMEOUT}s)"
# -sS quiet, --max-time bounds the expected DROP-timeout, -o /dev/null discards
# any body. curl exit 0 == we got an HTTP response == a route to OC exists.
if curl -sS -o /dev/null --max-time "${CURL_TIMEOUT}" "${OC_DIRECT_URL}" 2>/dev/null; then
    echo "     FAIL: OC ANSWERED a direct call — the enforcement bound is broken."
    fail=$((fail+1))
else
    rc=$?
    # curl rc 7=connection refused, 28=timeout (typical for DROP). Either way:
    # no HTTP conversation happened, which is exactly what we require.
    echo "     ok: direct call REFUSED (curl exit ${rc}; 7=refused, 28=drop/timeout)"
    pass=$((pass+1))
fi

# ----------------------------------------------------------------------------
# [1b] NEGATIVE probe: direct TCP to Postgres (a second side-channel to the
#      EDC data would be just as fatal as reaching OC itself).
# ----------------------------------------------------------------------------
echo "[1b] direct agent -> Postgres TCP probe: ${PG_DIRECT_HOSTPORT}"
if curl -sS -o /dev/null --max-time "${CURL_TIMEOUT}" "telnet://${PG_DIRECT_HOSTPORT}" 2>/dev/null; then
    echo "     FAIL: Postgres port ACCEPTED a direct connection."
    fail=$((fail+1))
else
    rc=$?
    echo "     ok: direct Postgres connection REFUSED (curl exit ${rc})"
    pass=$((pass+1))
fi

# ----------------------------------------------------------------------------
# [2] POSITIVE probe: the sanctioned path (agent -> proxy) must work, or the
#     refusals above prove nothing (the network might simply be down).
# ----------------------------------------------------------------------------
echo "[2]  proxy-routed health probe: ${PROXY_HEALTH_URL}"
# shellcheck disable=SC2086  # CURL_EXTRA_ARGS is intentionally word-split
if curl -sS -o /dev/null --fail --max-time "${CURL_TIMEOUT}" ${CURL_EXTRA_ARGS} "${PROXY_HEALTH_URL}"; then
    echo "     ok: proxy health SUCCEEDED — sanctioned path is alive"
    pass=$((pass+1))
else
    rc=$?
    echo "     FAIL: proxy health call failed (curl exit ${rc})."
    echo "           The negative results above are INCONCLUSIVE without this."
    fail=$((fail+1))
fi

# ----------------------------------------------------------------------------
# Verdict. Exit 0 only if the direct calls were REFUSED *and* the proxy path
# SUCCEEDED — the exact sprint acceptance condition.
# ----------------------------------------------------------------------------
echo
if [[ ${fail} -eq 0 ]]; then
    echo "LOCKDOWN VERIFIED: ${pass}/3 probes correct."
    echo "  - direct agent->OC:       refused (confirmed, not assumed)"
    echo "  - direct agent->Postgres: refused (confirmed, not assumed)"
    echo "  - agent->proxy health:    succeeded"
    exit 0
else
    echo "LOCKDOWN NOT VERIFIED: ${fail} probe(s) wrong. Do NOT sign off the"
    echo "acceptance item. Check 'journalctl -k | grep biject-ddrop' and the"
    echo "subnet/IP parameters in docker-user-rules.sh / oc-ingress.nft."
    exit 1
fi
