#!/usr/bin/env bash
# =============================================================================
# user_data/bootstrap.sh — cloud-init bootstrap for BOTH demo instances
# (biject-enforcement and biject-agent). Runs once as root at first boot.
#
# Installs: Docker Engine + the compose plugin (official Docker apt repo),
# plus the small toolset the runbook and the firewall scripts rely on
# (rsync, netcat, nftables, jq, curl).
#
# DELIBERATELY CLONE-FREE: this script fetches NO repository and NO secrets.
# The repo tree and the OpenClinica WAR artifacts arrive by rsync/scp from the
# admin workstation (the only host sg-* admits on :22), e.g.:
#
#   rsync -avz --exclude '.git' ./infra/  ubuntu@<host-eip>:/opt/biject/infra/
#   scp OpenClinica.war OpenClinica-ws.war \
#       ubuntu@<enforcement-eip>:/opt/biject/infra/hetzner/openclinica/dist/
#
# Why not clone: the instances would need a repo credential (the repos are
# private) and outbound reach to a git host, and a credential parked on a demo
# box is exactly what the secret-material rule says not to do. rsync from the
# admin workstation needs neither.
# =============================================================================
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# ----------------------------------------------------------------------------
# Base packages. NOTE: Ubuntu's EC2 apt mirrors are plain HTTP — the SGs allow
# egress :80 only while bootstrap_http_egress=true (see variables.tf).
# ----------------------------------------------------------------------------
apt-get update -y
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    rsync \
    netcat-openbsd \
    nftables \
    jq

# ----------------------------------------------------------------------------
# Docker Engine + compose plugin, from Docker's own apt repo (HTTPS).
# ----------------------------------------------------------------------------
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y --no-install-recommends \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

systemctl enable --now docker

# Demo convenience: let the default user drive docker without sudo. Group
# membership takes effect on next login.
usermod -aG docker ubuntu

# ----------------------------------------------------------------------------
# Landing directory for the rsync'd tree (see header). Owned by ubuntu so the
# admin workstation can rsync without sudo gymnastics.
# ----------------------------------------------------------------------------
install -d -o ubuntu -g ubuntu /opt/biject

echo "biject bootstrap complete: $(docker --version) / $(docker compose version)"
