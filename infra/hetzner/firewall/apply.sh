#!/usr/bin/env bash
# =============================================================================
# apply.sh — apply the biject network lockdown on the Hetzner demo host.
# Ticket S4-D-21.
#
# Applies, in order:
#   1. oc-ingress.nft        — host-level nftables table (host namespace +
#                              pre-Docker forward hook; defense-in-depth).
#   2. docker-user-rules.sh  — DOCKER-USER iptables rules (the reliable hook
#                              for container-forwarded traffic; see that file).
#
# Idempotent: safe to re-run; each layer replaces its own previous rules and
# never touches Docker's or the distro's rules.
#
# After applying, ALWAYS run ./verify_lockdown.sh — sprint acceptance requires
# the direct-to-OC refusal to be tested and confirmed refused, not assumed.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------------------------------------------------------
# Preconditions
# ----------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root (loads kernel firewall rules)." >&2
    exit 1
fi

command -v nft >/dev/null 2>&1 || {
    echo "ERROR: nft not found. Install nftables (apt-get install nftables)." >&2
    exit 1
}

# ----------------------------------------------------------------------------
# 1. Host-level nftables table
# ----------------------------------------------------------------------------
echo "[apply] checking oc-ingress.nft syntax..."
nft --check --file "${SCRIPT_DIR}/oc-ingress.nft"

echo "[apply] loading oc-ingress.nft..."
nft --file "${SCRIPT_DIR}/oc-ingress.nft"
echo "[apply] nftables table 'inet biject_lockdown' loaded."

# ----------------------------------------------------------------------------
# 2. DOCKER-USER rules (only when Docker is present — it is on this host)
# ----------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1 && command -v iptables >/dev/null 2>&1; then
    echo "[apply] installing DOCKER-USER rules..."
    bash "${SCRIPT_DIR}/docker-user-rules.sh"
else
    echo "[apply] WARNING: docker/iptables not found — skipping DOCKER-USER" >&2
    echo "         layer. Container-forwarded traffic is then only covered by" >&2
    echo "         the nftables forward hook; verify with verify_lockdown.sh." >&2
fi

# ----------------------------------------------------------------------------
# Persistence note (not automated on purpose):
# nftables rules do not survive reboot unless saved. On this host, either
#   - include oc-ingress.nft from /etc/nftables.conf and enable nftables.service,
#   - and run docker-user-rules.sh from a systemd unit ordered After=docker.service
#     (DOCKER-USER only exists once the docker daemon has created it).
# ----------------------------------------------------------------------------
echo
echo "[apply] done. Now run:  ${SCRIPT_DIR}/verify_lockdown.sh"
echo "        (acceptance requires the refusal to be CONFIRMED, not assumed)"
