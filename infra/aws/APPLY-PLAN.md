# infra/aws — APPLY PLAN (human runbook)

The exact sequence for standing up the AWS demo deployment. Everything in this
file is run **by a human** — nothing here was executed by the agent that
prepared the tree (no `terraform init/plan/apply`, no AWS API call, no
credential was ever present in the sandbox). Track split per
`.claude/handoff/track2-human-gated.md` item 3.

Reality checks that could not be done offline, do them first:

- `terraform fmt -check` and `terraform validate` in `terraform/` — the
  authoring machine had no terraform binary, so the HCL has been reviewed but
  never parsed by terraform itself.
- Both compose files pass `docker compose config` offline; the **builds**
  (OpenClinica, wall placeholder) and **pulls** (see step 6) do not run until
  the hosts exist.

---

## 0. Prerequisites (once)

| Thing | Where |
| --- | --- |
| Terraform >= 1.5 + AWS credentials with EC2/VPC rights in `us-east-2` | admin workstation |
| An EC2 key pair in the region (`key_name` input) | AWS console / `aws ec2 create-key-pair` |
| Namecheap DNS access for the demo domain | browser |
| `OpenClinica.war` + `OpenClinica-ws.war` (3.17 CE, ~100MB, manual download) | admin workstation — handoff item 2 |
| The credential values from `.claude/handoff/track2-human-gated.md` item 1 | generated at step 5 |
| Public IPs: the presenter's and the admin's (`curl -4 ifconfig.me` from each) | — |

## 1. Terraform apply

```bash
cd infra/aws/terraform
terraform init
terraform fmt -check && terraform validate     # first-ever parse — see header
terraform plan \
  -var presenter_ip=<PRESENTER_IPV4> \
  -var admin_ip=<ADMIN_IPV4> \
  -var domain=<DEMO_DOMAIN> \
  -var key_name=<KEY_PAIR_NAME>
terraform apply <same -var flags>
```

Optional variables worth knowing about (see `variables.tf` for the full
rationale on each): `agent_strict_egress` / `agent_pinned_egress_cidrs`
(strict agent egress variant), `enable_acme_http01` (inbound :80 for Let's
Encrypt — default on), `bootstrap_http_egress` (egress :80 for apt — turn OFF
in step 9), `region` (default `us-east-2`).

**Outputs you will need downstream** (`terraform output`):

| Output | Used in step |
| --- | --- |
| `enforcement_eip` | 2 (DNS), 3 (ssh) |
| `agent_eip` | 3 (ssh) — also the /32 sg-enforcement admits on :443 |
| `enforcement_private_ip` | 8 (verify_lockdown_aws.sh) |
| `enforcement_instance_id` / `enforcement_root_volume_id` | 9 (snapshot) |
| `dns_records` | 2, ready to paste |

## 2. Namecheap A records

In Namecheap → Domain List → `<domain>` → Advanced DNS, create **three A
records**, all pointing at `enforcement_eip` (the `dns_records` output prints
exactly this):

| Host | Value | TTL |
| --- | --- | --- |
| `proxy` | `<enforcement_eip>` | 5 min while iterating |
| `wall` | `<enforcement_eip>` | 5 min |
| `oc` | `<enforcement_eip>` | 5 min |

Wait for propagation (`dig +short proxy.<domain>` must return the EIP) before
step 6 — Let's Encrypt HTTP-01 fails until it does.

## 3. Push the repo tree to both hosts

The bootstrap is deliberately clone-free (no repo credential ever lands on a
demo box). From the admin workstation, repo root:

```bash
# Enforcement host needs infra/ (both aws/ and hetzner/ — the OC image build
# context and init-db.sh are reused from the hetzner tree):
rsync -avz --exclude '.git' infra/ ubuntu@<enforcement_eip>:/opt/biject/infra/

# Agent host needs only the agent compose (adapters/ too if running the agent
# from source during the spike):
rsync -avz --exclude '.git' infra/ ubuntu@<agent_eip>:/opt/biject/infra/
rsync -avz --exclude '.git' adapters/ ubuntu@<agent_eip>:/opt/biject/adapters/
```

Confirm the bootstrap finished on each host: `docker --version && docker
compose version` (cloud-init can lag instance availability by a minute or two;
log: `sudo tail /var/log/cloud-init-output.log`).

## 4. Upload the OpenClinica WAR artifacts

The OC image build **fails fatally without both WARs** (same artifacts as the
Hetzner plan — handoff item 2). The AWS compose reuses the Hetzner build
context, so they go to the hetzner path on the enforcement host:

```bash
scp OpenClinica.war OpenClinica-ws.war \
    ubuntu@<enforcement_eip>:/opt/biject/infra/hetzner/openclinica/dist/
```

## 5. Populate the .env files

Credential list, generation recipes, and caveats:
`.claude/handoff/track2-human-gated.md` item 1. Cross-reference table:

| Handoff credential | Lands in |
| --- | --- |
| `AUDIT_SIGNING_KEY` (Ed25519 seed; rotation breaks chain continuity — long-lived) | enforcement `.env` |
| `AUDIT_VERIFY_PUBKEY` (derived in biject-api's env) | enforcement `.env` |
| `AGENT_SIGNING_KEY` (agent host ONLY) | agent `.env` |
| `AGENT_VERIFY_PUBKEY` (public half of the above) | enforcement `.env` |
| OC service account + password (SOAP authorization checked; ws WAR wants the SHA-1 hex) | enforcement `.env` (`OPENCLINICA_*`) — account itself is created in step 6c |
| `PROXY_API_KEY` = `BIJECT_PROXY_API_KEY` (one value, two hosts) | both `.env`s |
| `TRACE_INGEST_TOKEN` | enforcement `.env` |
| LLM provider key + pinned model | agent `.env` (`OPENAI_API_KEY`/`OPENAI_MODEL`) |

On each host:

```bash
cd /opt/biject/infra/aws/compose/<enforcement|agent>
cp .env.example .env && chmod 600 .env
$EDITOR .env        # every variable is documented inline in the example
```

Also fill the non-credential values: `DEMO_DOMAIN`, `ACME_EMAIL`,
`PRESENTER_IP` (must equal the terraform `presenter_ip` input — SG and
Traefik allowlist gate the same person).

## 6. Bring-up order — enforcement host first

**a. Pin the images.** Every first-party `image:` in the enforcement compose
is a `PIN_ME_40CHAR_GIT_SHA` placeholder. Pin each to the SHA its repo's CI
published (convention: `scripts/pin-images.sh` in the meta repo — for this
file edit by hand, keeping `image:` tag and `BIJECT_IMAGE_SHA` identical per
service). Resolve the third-party digests (`traefik`, `postgres`) with
`docker buildx imagetools inspect <tag>`. Note (meta-repo CLAUDE.md gate
conditions): as of the last check only biject-proxy and biject-console images
exist; biject-api has no publish workflow yet — that is a hard blocker for
this stack and is tracked upstream, not here.

**b. Up:**

```bash
cd /opt/biject/infra/aws/compose/enforcement
docker compose up -d        # builds openclinica (WARs from step 4) + wall
docker compose ps           # wait: oc-db healthy, then openclinica up
```

First OC boot runs schema init — minutes, watch `docker compose logs -f
openclinica`.

**c. OC one-time setup** (browser, from the presenter/admin IP —
`https://oc.<domain>`): change the default admin password, create the
`ws-proxy` service account with **"Authorize SOAP web services in this
account" checked**, put its password (and SHA-1 hex) into `.env`
(`OPENCLINICA_USERNAME/_PASSWORD/_WS_PASSWORD`), then `docker compose up -d`
again to restart the proxy with real credentials. Seed the study per
`docs/DAY1-2-RUNBOOK.md`.

**d. Firewall (on-host layers):**

```bash
cd /opt/biject/infra/aws/firewall
sudo ./apply.sh
```

**e. S4-D-12 spike — first hour rule.** Per handoff item 4: with an instance
and credentials live, run the `docs/DAY1-2-RUNBOOK.md` §4 spike (ODM read
path, SOAP import path, WS-Security SHA1 behavior) BEFORE anything else in
Workstream D, and log the outcome to `.claude/deviations/S4-D-12.md` either
way.

**f. Agent host:**

```bash
cd /opt/biject/infra/aws/compose/agent
docker compose up -d        # placeholder image until the runner is published;
                            # for the spike, run adapters/openai on-host instead
```

## 7. Smoke checks

From the **presenter** workstation (the only IP that gets all three):

```bash
curl -s https://proxy.<domain>/healthz          # 200 JSON, image_sha matches the pin
curl -s https://wall.<domain>/                  # placeholder page (real wall: other track)
open https://oc.<domain>/OpenClinica            # OC login page (you are the allowed /32)
```

From the **enforcement host** (inside the stack):

```bash
docker compose exec biject-proxy biject-proxy --healthcheck   # liveness
curl -s http://localhost:443 >/dev/null || true               # traefik answering
docker compose logs --since 10m biject-api | tail             # started, no key errors
```

Expected posture notes, not failures: `/readyz` on the proxy reports degraded
until biject-api is healthy (fail-closed — the proxy denies every call in that
state); trace reports `signatures_checked: false` until `AUDIT_VERIFY_PUBKEY`
is set.

## 8. Acceptance — verify the bound from the agent host

```bash
ssh ubuntu@<agent_eip>
cd /opt/biject/infra/aws/firewall
DEMO_DOMAIN=<domain> \
ENFORCEMENT_PRIVATE_IP=<terraform output enforcement_private_ip> \
BIJECT_PROXY_API_KEY=<the shared key> \
./verify_lockdown_aws.sh
```

Spec §5.3 probe set; exit 0 required. The refusal must be **tested and
confirmed refused, not assumed** — do not sign off the acceptance item on
anything less than a clean run (all probes listed in the script header).

## 9. After green: EBS snapshot + tighten

Snapshot the enforcement root volume (holds the OC database, the audit
ledger, and the ACME account) so demo-day recovery is a restore, not a
rebuild:

```bash
aws ec2 create-snapshot \
  --volume-id "$(terraform output -raw enforcement_root_volume_id)" \
  --description "biject demo: post-bringup green state $(date -u +%Y%m%dT%H%M%SZ)" \
  --tag-specifications 'ResourceType=snapshot,Tags=[{Key=Project,Value=biject-demo}]'
aws ec2 describe-snapshots --snapshot-ids <id> --query 'Snapshots[0].State'  # wait: completed
```

Then close the bootstrap holes:

```bash
terraform apply <same -var flags> -var bootstrap_http_egress=false
```

(and, if you migrated Traefik to DNS-01 per the commented resolver block,
`-var enable_acme_http01=false` as well).

Re-run step 8 after any SG change — the acceptance test is cheap and the
alternative is assuming.
