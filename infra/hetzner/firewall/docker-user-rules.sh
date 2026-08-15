#!/usr/bin/env bash
# =============================================================================
# docker-user-rules.sh — DOCKER-USER lockdown for the OpenClinica demo.
# Ticket S4-D-21.
#
# WHY DOCKER-USER
# ---------------
# Docker owns the iptables FORWARD chain and rewrites its own rules at daemon/
# container (re)start, so rules placed directly in FORWARD can be reordered or
# bypassed. DOCKER-USER is the one chain Docker guarantees to (a) create,
# (b) consult BEFORE any of its own forwarding rules, and (c) never flush.
# That makes it the reliable hook for constraining container-to-container and
# container-to-world traffic. This script owns a dedicated child chain
# (BIJECT-LOCKDOWN) jumped to from DOCKER-USER, so re-running it replaces only
# our rules and never disturbs anything else in DOCKER-USER.
#
# WHAT IT ENFORCES
# ----------------
#   (1) DROP all traffic to the OC container subnet (edc_internal) except from
#       the proxy container IP — the network-layer enforcement bound.
#   (2) A LOG+DROP pair on every refusal, prefix "biject-ddrop:", so refused
#       attempts land in the kernel log for the audit trail.
#   (3) Agent egress: proxy:PROXY_PORT allowed; all other private (RFC1918)
#       destinations dropped; public TCP/443 allowed (api.openai.com) — or,
#       with OPENAI_STRICT=1, only the IPs api.openai.com resolves to right now.
#
# Keep the subnet/IP variables in sync with oc-ingress.nft and the compose
# file's ipam blocks (networks: edge, edc_internal).
# =============================================================================
set -euo pipefail

# ----------------------------------------------------------------------------
# Parameters — single source of truth for this script. Override via env.
# ----------------------------------------------------------------------------
EDGE_SUBNET="${EDGE_SUBNET:-172.28.99.0/24}"     # compose network "edge" (agent + proxy)
EDC_SUBNET="${EDC_SUBNET:-172.28.100.0/24}"       # compose network "edc_internal" (OC + Postgres + proxy)
PROXY_EDGE_IP="${PROXY_EDGE_IP:-172.28.99.10}"   # proxy static IP on edge
PROXY_EDC_IP="${PROXY_EDC_IP:-172.28.100.10}"     # proxy static IP on edc_internal
AGENT_IP="${AGENT_IP:-172.28.99.20}"             # agent container static IP on edge
OC_HTTP_PORT="${OC_HTTP_PORT:-8080}"            # OpenClinica (Tomcat)
PG_PORT="${PG_PORT:-5432}"                      # Postgres
PROXY_PORT="${PROXY_PORT:-8443}"                # verification proxy listener
OPENAI_STRICT="${OPENAI_STRICT:-0}"             # 1 = pin agent egress to api.openai.com's
                                                #     currently-resolved IPs (rotate; re-run to refresh)

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
iptables -N "${CHAIN}" 2>/dev/null || true     # create if absent
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
# Rule group B — ingress to the OC/EDC subnet.
#   Only the proxy may open connections into edc_internal, and only to the two
#   service ports. Intra-EDC (OC <-> Postgres) stays allowed. Everything else
#   destined for the subnet — the agent, the edge network, the world — is
#   logged and dropped.
# ----------------------------------------------------------------------------
iptables -A "${CHAIN}" -s "${PROXY_EDC_IP}" -d "${EDC_SUBNET}" \
    -p tcp -m multiport --dports "${OC_HTTP_PORT},${PG_PORT}" -j RETURN
iptables -A "${CHAIN}" -s "${EDC_SUBNET}" -d "${EDC_SUBNET}" -j RETURN
# Audit trail: log the refused attempt (rate-limited so a scan can't flood
# the journal), then drop it.
iptables -A "${CHAIN}" -d "${EDC_SUBNET}" \
    -m limit --limit 10/min --limit-burst 20 \
    -j LOG --log-prefix "biject-ddrop-oc: " --log-level 4
iptables -A "${CHAIN}" -d "${EDC_SUBNET}" -j DROP

# ----------------------------------------------------------------------------
# Rule group C — agent egress policy.
#   C1: agent -> proxy tool endpoint: the sanctioned path.
#   C2: agent -> any other private address: log + drop (closes every LAN
#       side-channel toward OC/Postgres, including future networks).
#   C3: agent -> public internet: 443 only. With OPENAI_STRICT=1, restrict to
#       the IPs api.openai.com resolves to *now* (they rotate — re-run this
#       script, e.g. from a timer, to refresh; default posture is any:443
#       because OpenAI publishes no stable ranges).
# ----------------------------------------------------------------------------
iptables -A "${CHAIN}" -s "${AGENT_IP}" -d "${PROXY_EDGE_IP}" \
    -p tcp --dport "${PROXY_PORT}" -j RETURN

for PRIVATE in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
    iptables -A "${CHAIN}" -s "${AGENT_IP}" -d "${PRIVATE}" \
        -m limit --limit 10/min --limit-burst 20 \
        -j LOG --log-prefix "biject-ddrop-agent: " --log-level 4
    iptables -A "${CHAIN}" -s "${AGENT_IP}" -d "${PRIVATE}" -j DROP
done

if [[ "${OPENAI_STRICT}" == "1" ]]; then
    # Resolve api.openai.com at apply time and allow exactly those IPs.
    OPENAI_IPS="$(getent ahostsv4 api.openai.com | awk '{print $1}' | sort -u)"
    if [[ -z "${OPENAI_IPS}" ]]; then
        echo "ERROR: OPENAI_STRICT=1 but api.openai.com did not resolve." >&2
        exit 1
    fi
    for IP in ${OPENAI_IPS}; do
        iptables -A "${CHAIN}" -s "${AGENT_IP}" -d "${IP}" -p tcp --dport 443 -j RETURN
    done
    # Anything else the agent tries to reach: log + drop.
    iptables -A "${CHAIN}" -s "${AGENT_IP}" \
        -m limit --limit 10/min --limit-burst 20 \
        -j LOG --log-prefix "biject-ddrop-agent: " --log-level 4
    iptables -A "${CHAIN}" -s "${AGENT_IP}" -j DROP
else
    # Default posture: public TLS only. (Private space already dropped above,
    # so this cannot reach OC; it just leaves DNS-rotation headaches out of
    # the demo.) Non-443 agent egress: log + drop.
    iptables -A "${CHAIN}" -s "${AGENT_IP}" -p tcp --dport 443 -j RETURN
    iptables -A "${CHAIN}" -s "${AGENT_IP}" \
        -m limit --limit 10/min --limit-burst 20 \
        -j LOG --log-prefix "biject-ddrop-agent: " --log-level 4
    iptables -A "${CHAIN}" -s "${AGENT_IP}" -j DROP
fi

# Everything we didn't match returns to DOCKER-USER / Docker's own rules.
iptables -A "${CHAIN}" -j RETURN

echo "[docker-user-rules] ${CHAIN} installed:"
iptables -n -v -L "${CHAIN}"
echo
echo "[docker-user-rules] refusals are logged with prefixes 'biject-ddrop-oc:'"
echo "                    and 'biject-ddrop-agent:' (journalctl -k | grep biject-ddrop)"
