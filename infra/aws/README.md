# infra/aws — AWS port of the demo skeleton

AWS port of `infra/hetzner/` per the target architecture: two EC2 instances,
the full enforcement stack on one, the agent alone on the other, and the
enforcement bound implemented at three network layers (security groups,
DOCKER-USER, nftables) instead of Hetzner's two. `infra/hetzner/` is untouched
and still describes the single-box Coolify deployment; the OpenClinica image
build context (`infra/hetzner/openclinica/`) is deliberately REUSED by this
port rather than duplicated.

| Path | What |
| --- | --- |
| `terraform/` | VPC + subnet, EC2 #1 `biject-enforcement` (m6i.2xlarge, gp3 100GB, EIP), EC2 #2 `biject-agent` (t3.large, EIP), `sg-enforcement` / `sg-agent`. Prepared offline — never applied; see APPLY-PLAN.md. |
| `compose/enforcement/` | The demo topology on EC2 #1: traefik (three :443 routers), biject-proxy (the only service on both kernel and edc), biject-api + redis, biject-trace, wall, OpenClinica + Postgres. Image pins are `PIN_ME` placeholders. |
| `compose/agent/` | The agent runner on EC2 #2. One service, no OpenClinica variables, ever. |
| `firewall/` | On-host lockdown for EC2 #1 + `verify_lockdown_aws.sh`, the spec §5.3 acceptance probes run FROM EC2 #2. |
| `wall/` | Build-verifiable placeholder for the wall (real dashboard lands from its own track). |
| `APPLY-PLAN.md` | **The human runbook.** Start here. |
