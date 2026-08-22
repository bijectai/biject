#!/usr/bin/env bash
# =============================================================================
# docker-user-rules.sh — DOCKER-USER lockdown for EC2 #1 (biject-enforcement).
# AWS port of infra/hetzner/firewall/docker-user-rules.sh (S4-D-21).
#
# WHY DOCKER-USER (unchanged from the Hetzner original)
# -----------------------------------------------------
# Docker owns the iptables FORWARD chain and rewrites its own rules at daemon/
# container (re)start. DOCKER-USER is the one chain Docker guarantees to
# (a) create, (b) consult BEFORE any of its own forwarding rules, and
# (c) never flush — the reliable hook for constraining container-to-container
# traffic. This script owns a dedicated child chain (BIJECT-LOCKDOWN) jumped
# to from DOCKER-USER, so re-running it replaces only our rules.
#
# WHAT CHANGED IN THE AWS PORT
# ----------------------------
#   * Subnet/IP pins moved to the AWS compose ipam
#     (infra/aws/compose/enforcement/docker-compose.yml): edge 172.29.99.0/24,
#     kernel 172.29.98.0/24, edc 172.29.100.0/24; proxy .10 / traefik .20 /
#     OC .30 / Postgres .40 on edc. Keep these in sync with the compose file
#     and oc-ingress.nft — change them together or not at all.
#   * The Hetzner agent-egress rule groups are GONE: on AWS the agent is a
#     separate EC2 instance, and its egress is enforced by sg-agent
#     (terraform) — 443-only, no 8080/5432 anywhere. This host only enforces
#     the EDC ingress bound.
#   * Traefik gets a pinned allowance to OC :8080 ONLY — the documented
#     UI-exception route (oc.<domain>, presenter-only at the edge).
#   * The proxy's allowance is OC :8080 ONLY. The Hetzner original also
#     allowed the proxy to :5432; the proxy has no business talking to
#     Postgres, so the AWS port drops that (documented tightening).
#
# WHAT IT ENFORCES
# ----------------
#   (1) New flows into the edc subnet are permitted ONLY from the proxy
#       container IP (to OC :8080) and the traefik container IP (to OC :8080).
#       Intra-EDC (OC <-> Postgres) stays allowed. Everything else destined
#       for the subnet is logged and dropped.
#   (2) A LOG+DROP pair on every refusal, prefix "biject-ddrop-oc:", so
#       refused attempts land in the kernel log for the audit trail.
# =============================================================================
set -euo pipefail

# ----------------------------------------------------------------------------
# Parameters — single source of truth for this script. Override via env.
# Defaults match infra/aws/compose/enforcement/docker-compose.yml ipam.
# ----------------------------------------------------------------------------
EDGE_SUBNET="${EDGE_SUBNET:-172.29.99.0/24}"        # compose network "edge"
KERNEL_SUBNET="${KERNEL_SUBNET:-172.29.98.0/24}"    # compose network "kernel"
EDC_SUBNET="${EDC_SUBNET:-172.29.100.0/24}"         # compose network "edc" (internal)
PROXY_EDC_IP="${PROXY_EDC_IP:-172.29.100.10}"       # biject-proxy on edc (pinned)
TRAEFIK_EDC_IP="${TRAEFIK_EDC_IP:-172.29.100.20}"   # traefik on edc (pinned, UI exception)
OC_HTTP_PORT="${OC_HTTP_PORT:-8080}"                # OpenClinica (Tomcat)
PG_PORT="${PG_PORT:-5432}"                          # Postgres (referenced in comments/log only)

CHAIN="BIJECT-LOCKDOWN"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root." >&2
    exit 1
fi

# DOCKER-USER exists only once the Docker daemon has initialized networking.
if ! iptables -n -L DOCKER-USER >/dev/null 2>&1; then
    echo "ERROR: DOCKER-USER chain not found. Is the Docker daemon running?" >&2
    echo "       (Run this script After=docker.service; see apply.sh.)" >&2
    exit 1
fi

# ----------------------------------------------------------------------------
# Idempotent (re)build of our dedicated chain.
# ----------------------------------------------------------------------------
iptables -N "${CHAIN}" 2>/dev/null || true      # create if absent
iptables -F "${CHAIN}"                          # flush ONLY our chain

# Ensure exactly one jump from DOCKER-USER into our chain, at position 1 so we
# run before anything else a later tool might add there.
while iptables -C DOCKER-USER -j "${CHAIN}" 2>/dev/null; do
    iptables -D DOCKER-USER -j "${CHAIN}"
done
iptables -I DOCKER-USER 1 -j "${CHAIN}"

# ----------------------------------------------------------------------------
# Rule group A — established flows we already admitted keep flowing.
# ----------------------------------------------------------------------------
iptables -A "${CHAIN}" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

# ----------------------------------------------------------------------------
# Rule group B — ingress to the EDC subnet.
#   B1: proxy -> OC HTTP only (SOAP writes + ODM reads). NOT :5432.
#   B2: traefik -> OC HTTP only (the presenter-gated UI route).
#   B3: intra-EDC (OC <-> Postgres) stays allowed.
#   B4: everything else aimed at the subnet — edge, kernel, host, world —
#       is logged (rate-limited so a scan can't flood the journal) + dropped.
# ----------------------------------------------------------------------------
iptables -A "${CHAIN}" -s "${PROXY_EDC_IP}" -d "${EDC_SUBNET}" \
    -p tcp --dport "${OC_HTTP_PORT}" -j RETURN
iptables -A "${CHAIN}" -s "${TRAEFIK_EDC_IP}" -d "${EDC_SUBNET}" \
    -p tcp --dport "${OC_HTTP_PORT}" -j RETURN
iptables -A "${CHAIN}" -s "${EDC_SUBNET}" -d "${EDC_SUBNET}" -j RETURN
iptables -A "${CHAIN}" -d "${EDC_SUBNET}" \
    -m limit --limit 10/min --limit-burst 20 \
    -j LOG --log-prefix "biject-ddrop-oc: " --log-level 4
iptables -A "${CHAIN}" -d "${EDC_SUBNET}" -j DROP

# Everything we didn't match returns to DOCKER-USER / Docker's own rules.
iptables -A "${CHAIN}" -j RETURN

echo "[docker-user-rules] ${CHAIN} installed:"
iptables -n -v -L "${CHAIN}"
echo
echo "[docker-user-rules] refusals are logged with prefix 'biject-ddrop-oc:'"
echo "                    (journalctl -k | grep biject-ddrop)"
echo "[docker-user-rules] agent-host egress is enforced by sg-agent (terraform),"
echo "                    not on this host. Acceptance: run"
echo "                    verify_lockdown_aws.sh FROM THE AGENT HOST."
