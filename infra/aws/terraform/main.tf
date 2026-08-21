# =============================================================================
# infra/aws/terraform/main.tf — AWS port of the Hetzner demo skeleton.
#
# Two EC2 instances in one VPC/public subnet:
#
#   EC2 #1 "biject-enforcement" (m6i.2xlarge, gp3 100GB, Elastic IP)
#     Runs the whole enforcement stack (../compose/enforcement/): Traefik,
#     biject-proxy, biject-api + redis, biject-trace, wall, OpenClinica +
#     Postgres. The three demo hostnames (proxy./wall./oc.<domain>) all
#     resolve to this instance's EIP.
#
#   EC2 #2 "biject-agent" (t3.large, Elastic IP)
#     Runs only the demo agent (../compose/agent/). It has NO route to
#     OpenClinica or Postgres: its SG has no egress rule for 8080/5432, and
#     the enforcement SG admits nothing but :443 (+ :22 admin, + :80 ACME).
#     Every tool call therefore traverses Traefik -> biject-proxy, which is
#     the enforcement hop.
#
# WHY THE AGENT GETS AN ELASTIC IP (documented choice per the port ticket):
#   sg-enforcement must admit :443 from the agent host. The obvious AWS-native
#   alternative — an SG-source rule (ingress from sg-agent) — does NOT work
#   here, because the agent dials `https://proxy.<domain>`, i.e. the
#   enforcement host's PUBLIC EIP. Same-VPC traffic addressed to an EIP
#   hairpins through the Internet Gateway and arrives carrying the agent's
#   PUBLIC source address, and SG-to-SG references only match traffic between
#   private addresses. So we allocate an EIP for the agent as well and allow
#   `<agent EIP>/32` on :443. (Routing the agent to the proxy by private IP
#   would restore the SG-source option, but then the agent would bypass
#   Traefik's TLS termination and certificate, which the demo relies on.)
#
# PREPARE ONLY: authored offline; never init/plan/applied from the sandbox.
# `terraform fmt`/`validate` could not be run here (no terraform binary on the
# authoring machine) — run both before the first plan. See ../APPLY-PLAN.md.
# =============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project_tag
      ManagedBy = "terraform"
    }
  }
}

# -----------------------------------------------------------------------------
# AMI — Ubuntu 24.04 LTS (noble), amd64, Canonical-owned.
# -----------------------------------------------------------------------------
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# -----------------------------------------------------------------------------
# VPC + one public subnet
# -----------------------------------------------------------------------------
resource "aws_vpc" "demo" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "biject-demo" }
}

resource "aws_internet_gateway" "demo" {
  vpc_id = aws_vpc.demo.id

  tags = { Name = "biject-demo" }
}

resource "aws_subnet" "public" {
  vpc_id     = aws_vpc.demo.id
  cidr_block = var.public_subnet_cidr
  # Public IPs are handled per-instance: a launch-time address for bootstrap
  # egress, replaced by the explicit EIP association moments later.
  map_public_ip_on_launch = false

  tags = { Name = "biject-demo-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.demo.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.demo.id
  }

  tags = { Name = "biject-demo-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# -----------------------------------------------------------------------------
# Elastic IPs — allocated standalone (no instance dependency) so the SG rules
# below can reference their addresses without a cycle. The enforcement EIP is
# what the three Namecheap A records point at; the agent EIP exists so
# sg-enforcement can name the agent's stable public source address (see file
# header for why an SG-source rule cannot do this job).
# -----------------------------------------------------------------------------
resource "aws_eip" "enforcement" {
  domain = "vpc"
  tags   = { Name = "biject-enforcement" }
}

resource "aws_eip" "agent" {
  domain = "vpc"
  tags   = { Name = "biject-agent" }
}

# -----------------------------------------------------------------------------
# sg-enforcement — EC2 #1.
#
# Inbound surface, complete list:
#   :443  from presenter_ip/32     (demo browser: wall + OC UI + proxy API)
#   :443  from <agent EIP>/32      (the agent's tool calls, via Traefik)
#   :22   from admin_ip/32         (rsync/scp + operations)
#   :80   from 0.0.0.0/0           ONLY when enable_acme_http01 (documented
#                                  deviation — Let's Encrypt HTTP-01; serves
#                                  the ACME responder + a 301, nothing else)
# Nothing else. In particular: no 8080, no 5432, no 8000/8010 — none of the
# container ports are published on the host at all (see the compose file), and
# the SG would drop them anyway.
#
# NOTE: Terraform removes AWS's default allow-all egress rule from managed
# SGs, so egress is exactly the rules declared below and nothing more.
# -----------------------------------------------------------------------------
resource "aws_security_group" "enforcement" {
  name        = "sg-enforcement"
  description = "biject enforcement host: 443 presenter+agent, 22 admin, optional 80 ACME"
  vpc_id      = aws_vpc.demo.id

  tags = { Name = "sg-enforcement" }
}

resource "aws_vpc_security_group_ingress_rule" "enf_443_presenter" {
  security_group_id = aws_security_group.enforcement.id
  description       = "HTTPS from the presenter workstation"
  cidr_ipv4         = "${var.presenter_ip}/32"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "enf_443_agent" {
  security_group_id = aws_security_group.enforcement.id
  description       = "HTTPS from the agent host's EIP (tool calls via Traefik -> biject-proxy)"
  cidr_ipv4         = "${aws_eip.agent.public_ip}/32"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "enf_22_admin" {
  security_group_id = aws_security_group.enforcement.id
  description       = "SSH from the admin workstation (rsync of repo tree + OC WARs)"
  cidr_ipv4         = "${var.admin_ip}/32"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

resource "aws_vpc_security_group_ingress_rule" "enf_80_acme" {
  count = var.enable_acme_http01 ? 1 : 0

  security_group_id = aws_security_group.enforcement.id
  description       = "ACME HTTP-01 only (LE validators dial from arbitrary IPs); disable via enable_acme_http01=false after switching to DNS-01"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "enf_out_443" {
  security_group_id = aws_security_group.enforcement.id
  description       = "TLS egress: ACME directory, ghcr.io image pulls, optional provider APIs"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "enf_out_80" {
  count = var.bootstrap_http_egress ? 1 : 0

  security_group_id = aws_security_group.enforcement.id
  description       = "Bootstrap only: Ubuntu apt mirrors are plain HTTP. Set bootstrap_http_egress=false after provisioning."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

# -----------------------------------------------------------------------------
# sg-agent — EC2 #2.
#
# Inbound: :22 from admin only. The agent exposes nothing to anyone.
#
# Egress, complete list:
#   :443 to <enforcement EIP>/32          — the sanctioned tool-call path
#   :443 to 0.0.0.0/0                     — ONLY when agent_strict_egress =
#                                           false (LLM provider APIs; see the
#                                           variable's doc for the tension and
#                                           the strict variant)
#   :443 to agent_pinned_egress_cidrs[*]  — the strict variant's allowlist
#   :80  to 0.0.0.0/0                     — bootstrap apt only, togglable
#
# EXPLICITLY ABSENT — and load-bearing: there is NO egress rule for 8080
# (OpenClinica/Tomcat) or 5432 (Postgres), to the enforcement host or to
# anywhere. Terraform strips the AWS default allow-all egress rule, so an
# absent rule is a denied port. This is the SG half of the enforcement bound;
# ../firewall/verify_lockdown_aws.sh is the test that CONFIRMS it (spec §5.3),
# rather than assuming it.
#
# DNS note: with egress restricted this tightly, name resolution still works
# because traffic to the Amazon-provided VPC resolver is not evaluated against
# security-group rules.
# -----------------------------------------------------------------------------
resource "aws_security_group" "agent" {
  name        = "sg-agent"
  description = "biject agent host: 22 admin in; 443-only out; no 8080/5432 anywhere"
  vpc_id      = aws_vpc.demo.id

  tags = { Name = "sg-agent" }
}

resource "aws_vpc_security_group_ingress_rule" "agent_22_admin" {
  security_group_id = aws_security_group.agent.id
  description       = "SSH from the admin workstation"
  cidr_ipv4         = "${var.admin_ip}/32"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

resource "aws_vpc_security_group_egress_rule" "agent_out_443_enforcement" {
  security_group_id = aws_security_group.agent.id
  description       = "The sanctioned path: HTTPS to the enforcement EIP (Traefik -> biject-proxy)"
  cidr_ipv4         = "${aws_eip.enforcement.public_ip}/32"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "agent_out_443_general" {
  count = var.agent_strict_egress ? 0 : 1

  security_group_id = aws_security_group.agent.id
  description       = "General TLS egress for LLM provider APIs (no stable published ranges). Strict variant: agent_strict_egress=true + agent_pinned_egress_cidrs."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "agent_out_443_pinned" {
  for_each = var.agent_strict_egress ? toset(var.agent_pinned_egress_cidrs) : toset([])

  security_group_id = aws_security_group.agent.id
  description       = "Strict-egress allowlist entry (e.g. currently-resolved LLM-provider address; rotates — expect to re-apply)"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "agent_out_80_bootstrap" {
  count = var.bootstrap_http_egress ? 1 : 0

  security_group_id = aws_security_group.agent.id
  description       = "Bootstrap only: Ubuntu apt mirrors are plain HTTP. Set bootstrap_http_egress=false after provisioning."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

# NOTE (deliberate absence): no egress rule for 8080 or 5432 exists on
# sg-agent, and none may be added. See the block comment above.

# -----------------------------------------------------------------------------
# EC2 #1 — biject-enforcement
# -----------------------------------------------------------------------------
resource "aws_instance" "enforcement" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.enforcement_instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.enforcement.id]
  key_name               = var.key_name

  # Launch-time public IP so cloud-init's apt bootstrap has egress from the
  # first second — the EIP association lands moments later and REPLACES this
  # address (there is no NAT gateway to fall back on). Without it, user_data
  # can race the EIP attach and die on `apt-get update`.
  associate_public_ip_address = true

  root_block_device {
    volume_type = "gp3"
    volume_size = var.enforcement_root_gb
    encrypted   = true
  }

  user_data = file("${path.module}/user_data/bootstrap.sh")

  lifecycle {
    # A new Canonical AMI release must not silently replace a host that holds
    # the audit ledger and the OC database. Move the AMI deliberately.
    ignore_changes = [ami]
  }

  tags = { Name = "biject-enforcement" }
}

resource "aws_eip_association" "enforcement" {
  instance_id   = aws_instance.enforcement.id
  allocation_id = aws_eip.enforcement.id
}

# -----------------------------------------------------------------------------
# EC2 #2 — biject-agent
# -----------------------------------------------------------------------------
resource "aws_instance" "agent" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.agent_instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.agent.id]
  key_name               = var.key_name

  # Same bootstrap-egress rationale as the enforcement instance above.
  associate_public_ip_address = true

  root_block_device {
    volume_type = "gp3"
    volume_size = var.agent_root_gb
    encrypted   = true
  }

  user_data = file("${path.module}/user_data/bootstrap.sh")

  lifecycle {
    ignore_changes = [ami]
  }

  tags = { Name = "biject-agent" }
}

resource "aws_eip_association" "agent" {
  instance_id   = aws_instance.agent.id
  allocation_id = aws_eip.agent.id
}
