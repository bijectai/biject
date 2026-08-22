#!/usr/bin/env bash
# =============================================================================
# verify_lockdown_aws.sh — THE acceptance test for the AWS network enforcement
# bound. Implements the spec §5.3 probe set. AWS port of
# infra/hetzner/firewall/verify_lockdown.sh (S4-D-21), same structure:
# negative probes that must be refused + a positive probe that proves the
# refusals are policy, not a dead network.
#
# RUN FROM THE AGENT HOST (EC2 #2, "biject-agent") — the probes only mean
# anything from the agent's network vantage point:
#
#   DEMO_DOMAIN=edc-demo.example.com \
#   ENFORCEMENT_PRIVATE_IP=10.42.1.x \
#   BIJECT_PROXY_API_KEY=... \
#   ./verify_lockdown_aws.sh
#
# ENFORCEMENT_PRIVATE_IP comes from `terraform output enforcement_private_ip`.
#
# The four probes (§5.3), and what each one proves:
#
#   [1] curl https://oc.<domain>/            -> expect HTTP 403
#       Traefik's ipAllowList admits only PRESENTER_IP; from here the edge
#       must answer 403. Any 2xx/3xx means the OC UI is reachable from the
#       agent — bound broken. (TLS must complete: the 403 is the allowlist
#       denial, not a connection failure.)
#   [2] curl http://<private-ip>:8080/       -> expect TIMEOUT
#       Direct Tomcat probe over the VPC. sg-enforcement has no :8080 rule,
#       and an SG denial silently drops the SYN — so the correct symptom is a
#       timeout (curl exit 28). "Connection refused" would mean the packet
#       REACHED the host and got an RST — SG not doing its job — and fails.
#   [3] nc :8080 and nc :5432                -> expect TIMEOUT (both)
#       Raw TCP, same reasoning; also covers Postgres. A fast failure
#       (refused) fails the probe for the same reason as [2].
#   [4] curl -H <auth> https://proxy.<domain>/healthz -> expect SUCCESS
#       The sanctioned path must work, or [1]-[3] prove nothing (an unplugged
#       cable would "pass" them). The key travels as `X-Biject-Proxy-Key` —
#       the proxy's auth middleware requires it on every route EXCEPT the two
#       health probes, so /healthz answers regardless; we send it anyway so
#       this invocation documents the exact header the tool calls use.
#
# Exit 0 ONLY if all four hold.
# =============================================================================
set -u  # NOT -e: we *expect* command failures in the negative probes.

DEMO_DOMAIN="${DEMO_DOMAIN:?set DEMO_DOMAIN (e.g. edc-demo.example.com)}"
ENFORCEMENT_PRIVATE_IP="${ENFORCEMENT_PRIVATE_IP:?set ENFORCEMENT_PRIVATE_IP (terraform output enforcement_private_ip)}"
BIJECT_PROXY_API_KEY="${BIJECT_PROXY_API_KEY:?set BIJECT_PROXY_API_KEY (same value as the enforcement host's PROXY_API_KEY)}"

OC_EDGE_URL="${OC_EDGE_URL:-https://oc.${DEMO_DOMAIN}/}"
OC_DIRECT_URL="${OC_DIRECT_URL:-http://${ENFORCEMENT_PRIVATE_IP}:8080/}"
PROXY_HEALTH_URL="${PROXY_HEALTH_URL:-https://proxy.${DEMO_DOMAIN}/healthz}"
PROXY_AUTH_HEADER="${PROXY_AUTH_HEADER:-X-Biject-Proxy-Key: ${BIJECT_PROXY_API_KEY}}"
CURL_TIMEOUT="${CURL_TIMEOUT:-8}"      # SG DROP manifests as a timeout; keep it short
NC_TIMEOUT="${NC_TIMEOUT:-8}"
CURL_EXTRA_ARGS="${CURL_EXTRA_ARGS:-}" # e.g. --resolve overrides while DNS propagates

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl required"; exit 2; }
command -v nc   >/dev/null 2>&1 || { echo "ERROR: nc required (netcat-openbsd; bootstrap installs it)"; exit 2; }

pass=0
fail=0

# ----------------------------------------------------------------------------
# [1] NEGATIVE probe: OC UI via the edge — must be 403 (allowlist denial).
# ----------------------------------------------------------------------------
echo "[1]  edge probe: ${OC_EDGE_URL} (expect HTTP 403)"
# shellcheck disable=SC2086  # CURL_EXTRA_ARGS is intentionally word-split
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "${CURL_TIMEOUT}" ${CURL_EXTRA_ARGS} "${OC_EDGE_URL}" 2>/dev/null)"
rc=$?
if [[ ${rc} -ne 0 ]]; then
    echo "     FAIL: request did not complete (curl exit ${rc})."
    echo "           INCONCLUSIVE — DNS/TLS must work for the 403 to mean"
    echo "           'denied by the edge allowlist' rather than 'network down'."
    fail=$((fail+1))
elif [[ "${code}" == "403" ]]; then
    echo "     ok: HTTP 403 — denied by the presenter-only allowlist"
    pass=$((pass+1))
else
    echo "     FAIL: HTTP ${code} (expected 403). A 2xx/3xx means the OC UI"
    echo "           is reachable from the agent host — the bound is BROKEN."
    fail=$((fail+1))
fi

# ----------------------------------------------------------------------------
# [2] NEGATIVE probe: direct HTTP to Tomcat over the VPC private address.
#     An SG denial DROPS silently -> the only acceptable symptom is a timeout.
# ----------------------------------------------------------------------------
echo "[2]  direct probe: ${OC_DIRECT_URL} (expect timeout after ${CURL_TIMEOUT}s)"
if curl -sS -o /dev/null --max-time "${CURL_TIMEOUT}" "${OC_DIRECT_URL}" 2>/dev/null; then
    echo "     FAIL: Tomcat ANSWERED a direct call — the enforcement bound is BROKEN."
    fail=$((fail+1))
else
    rc=$?
    if [[ ${rc} -eq 28 ]]; then
        echo "     ok: timed out (curl exit 28) — SG dropped the SYN, as required"
        pass=$((pass+1))
    else
        echo "     FAIL: curl exit ${rc} (expected 28 = timeout). Exit 7 (refused)"
        echo "           means the packet REACHED the host and was RST — the SG"
        echo "           admitted it, which it must not. Investigate before signing off."
        fail=$((fail+1))
    fi
fi

# ----------------------------------------------------------------------------
# [3] NEGATIVE probes: raw TCP to 8080 and 5432 — both must time out.
#     nc's exit code can't distinguish drop from refused, so we time it: a
#     dropped SYN burns the whole timeout, a refused one fails immediately.
# ----------------------------------------------------------------------------
for port in 8080 5432; do
    echo "[3]  raw TCP probe: ${ENFORCEMENT_PRIVATE_IP}:${port} (expect timeout after ${NC_TIMEOUT}s)"
    start=${SECONDS}
    if nc -z -w "${NC_TIMEOUT}" "${ENFORCEMENT_PRIVATE_IP}" "${port}" 2>/dev/null; then
        echo "     FAIL: port ${port} ACCEPTED a connection — the bound is BROKEN."
        fail=$((fail+1))
    else
        elapsed=$((SECONDS - start))
        if [[ ${elapsed} -ge $((NC_TIMEOUT - 1)) ]]; then
            echo "     ok: no connection after ${elapsed}s — dropped, as required"
            pass=$((pass+1))
        else
            echo "     FAIL: refused after only ${elapsed}s — an RST came back, so"
            echo "           the SG admitted the packet. Expected a silent drop."
            fail=$((fail+1))
        fi
    fi
done

# ----------------------------------------------------------------------------
# [4] POSITIVE probe: the sanctioned path (agent -> proxy via Traefik), WITH
#     the API key. Must succeed, or the refusals above are inconclusive.
# ----------------------------------------------------------------------------
echo "[4]  proxy-routed health probe: ${PROXY_HEALTH_URL} (with API key; expect success)"
# shellcheck disable=SC2086  # CURL_EXTRA_ARGS is intentionally word-split
if curl -sS -o /dev/null --fail --max-time "${CURL_TIMEOUT}" \
        -H "${PROXY_AUTH_HEADER}" ${CURL_EXTRA_ARGS} "${PROXY_HEALTH_URL}"; then
    echo "     ok: proxy health SUCCEEDED — sanctioned path is alive"
    pass=$((pass+1))
else
    rc=$?
    echo "     FAIL: proxy health call failed (curl exit ${rc})."
    echo "           The negative results above are INCONCLUSIVE without this."
    fail=$((fail+1))
fi

# ----------------------------------------------------------------------------
# Verdict. Exit 0 only if every direct path was refused the RIGHT WAY *and*
# the sanctioned path succeeded — the spec §5.3 acceptance condition.
# ----------------------------------------------------------------------------
echo
if [[ ${fail} -eq 0 ]]; then
    echo "LOCKDOWN VERIFIED: ${pass}/5 probes correct."
    echo "  - agent->oc.<domain> (edge):   403, denied by allowlist (confirmed)"
    echo "  - agent->tomcat :8080 direct:  dropped (confirmed, not assumed)"
    echo "  - agent->tcp :8080 raw:        dropped (confirmed, not assumed)"
    echo "  - agent->tcp :5432 raw:        dropped (confirmed, not assumed)"
    echo "  - agent->proxy health w/ key:  succeeded"
    exit 0
else
    echo "LOCKDOWN NOT VERIFIED: ${fail} probe(s) wrong. Do NOT sign off the"
    echo "acceptance item. Check, on EC2 #1: 'journalctl -k | grep biject-'"
    echo "and the SG rules (terraform), then the pins in docker-user-rules.sh /"
    echo "oc-ingress.nft vs the compose ipam."
    exit 1
fi
