# =============================================================================
# infra/aws/terraform — input variables for the biject demo AWS deployment.
#
# PREPARE-ONLY NOTE: this configuration has never been `terraform init`-ed,
# planned, or applied from the sandbox that authored it. A human runs it —
# see ../APPLY-PLAN.md for the exact sequence and the values to supply.
# =============================================================================

variable "region" {
  description = "AWS region for the whole demo deployment."
  type        = string
  default     = "us-east-2"
}

variable "presenter_ip" {
  description = <<-EOT
    Public IPv4 address of the presenter's workstation (no /32 suffix — it is
    appended where used). This is the ONLY address allowed through Traefik's
    ipAllowList in front of the OpenClinica UI, and one of exactly two
    addresses allowed to reach the enforcement host on :443.
  EOT
  type        = string

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}$", var.presenter_ip))
    error_message = "presenter_ip must be a bare IPv4 address (e.g. 203.0.113.7), without a /32 suffix."
  }
}

variable "admin_ip" {
  description = <<-EOT
    Public IPv4 address of the operator/admin workstation (no /32 suffix).
    Grants SSH (:22) to BOTH instances — used for rsync of the repo tree and
    the OpenClinica WAR artifacts (there is deliberately no git clone in the
    bootstrap; see user_data/bootstrap.sh).
  EOT
  type        = string

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}$", var.admin_ip))
    error_message = "admin_ip must be a bare IPv4 address (e.g. 198.51.100.9), without a /32 suffix."
  }
}

variable "domain" {
  description = <<-EOT
    Demo base domain (no scheme, no trailing dot), e.g. edc-demo.example.com.
    Three A records — proxy.<domain>, wall.<domain>, oc.<domain> — must all
    point at the enforcement Elastic IP (Namecheap; manual step, see
    ../APPLY-PLAN.md). Compose-side this is the DEMO_DOMAIN env var.
  EOT
  type        = string
}

variable "key_name" {
  description = "Name of an EXISTING EC2 key pair in the region (created out of band; Terraform does not manage key material)."
  type        = string
}

# --- instance sizing (spec A.1) ----------------------------------------------

variable "enforcement_instance_type" {
  description = "EC2 #1 'biject-enforcement' instance type (spec A.1)."
  type        = string
  default     = "m6i.2xlarge"
}

variable "agent_instance_type" {
  description = "EC2 #2 'biject-agent' instance type (spec A.1)."
  type        = string
  default     = "t3.large"
}

variable "enforcement_root_gb" {
  description = "Root EBS volume size (gp3) for the enforcement host. Holds Docker images, the OC Postgres volume, and the audit ledger."
  type        = number
  default     = 100
}

variable "agent_root_gb" {
  description = "Root EBS volume size (gp3) for the agent host."
  type        = number
  default     = 30
}

# --- network addressing ------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR for the demo VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR for the single public subnet (both instances live here)."
  type        = string
  default     = "10.42.1.0/24"
}

# --- security-group toggles (each one is a documented tension) ---------------

variable "enable_acme_http01" {
  description = <<-EOT
    When true (default), opens inbound :80 on the enforcement SG to 0.0.0.0/0.

    WHY THIS EXISTS — documented deviation from "443 from two IPs and nothing
    else": the Traefik configuration (reused from infra/hetzner/traefik/) uses
    Let's Encrypt's HTTP-01 challenge, and LE's validation servers connect
    INBOUND on port 80 from arbitrary addresses. Port 80 serves only the ACME
    responder and a permanent 301 to HTTPS — no router content is reachable on
    it — so the exposed surface is the redirect, not the demo. The :443
    surface stays presenter+agent only.

    Strictest alternative (no inbound 80 at all): switch the cert resolver to
    the DNS-01 challenge with the Namecheap provider — see the commented block
    in ../compose/enforcement/traefik/traefik.yml — then set this to false and
    re-apply.
  EOT
  type        = bool
  default     = true
}

variable "agent_strict_egress" {
  description = <<-EOT
    Egress posture for the agent host's SG.

    false (default): agent may open TCP/443 to ANY public address. Needed in
    practice because the demo agent calls its LLM provider (api.openai.com),
    which publishes no stable IP ranges — the same tension the Hetzner
    firewall documents (infra/hetzner/firewall/docker-user-rules.sh,
    OPENAI_STRICT). The enforcement-relevant property still holds: there is NO
    egress rule for 8080 or 5432, so the agent cannot reach OpenClinica or
    Postgres on the enforcement host even by private IP — its only sanctioned
    path is :443 to the enforcement EIP (Traefik -> biject-proxy).

    true (strict variant): the general 443 rule is dropped. Egress 443 is then
    allowed ONLY to the enforcement EIP plus whatever you list in
    agent_pinned_egress_cidrs (e.g. the addresses api.openai.com resolves to
    right now — they rotate, so expect to re-apply).
  EOT
  type        = bool
  default     = false
}

variable "agent_pinned_egress_cidrs" {
  description = "Extra CIDRs the agent may reach on TCP/443 when agent_strict_egress = true (e.g. currently-resolved LLM-provider addresses). Ignored when strict egress is off."
  type        = list(string)
  default     = []
}

variable "bootstrap_http_egress" {
  description = <<-EOT
    When true (default), both SGs allow egress TCP/80. Ubuntu's default apt
    mirrors (…ec2.archive.ubuntu.com) are plain HTTP, so the user_data
    bootstrap (docker install) fails without it. After both hosts are
    provisioned and green, set this to false and `terraform apply` again to
    close the port.
  EOT
  type        = bool
  default     = true
}

variable "project_tag" {
  description = "Value for the Project tag on every resource."
  type        = string
  default     = "biject-demo"
}
