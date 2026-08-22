# infra/aws/firewall — the enforcement bound, AWS layout

AWS port of `infra/hetzner/firewall/` (S4-D-21). The bound — *nothing reaches
OpenClinica except through the verification proxy, plus the documented
presenter-only Traefik UI route* — is enforced in three layers here instead of
the Hetzner two:

| Layer | Where | What it constrains |
| --- | --- | --- |
| Security groups | `../terraform/main.tf` | Instance boundary. Agent host: egress 443 only, **no 8080/5432 rule exists anywhere**. Enforcement host: inbound 443 (presenter + agent EIP), 22 (admin), optional 80 (ACME). |
| DOCKER-USER | `docker-user-rules.sh` (run on EC2 #1) | Container-forwarded traffic. Only proxy (.10) and traefik (.20) may open flows into the edc subnet, `:8080` only. |
| nftables | `oc-ingress.nft` (run on EC2 #1 via `apply.sh`) | Host-namespace defense-in-depth around the Docker path. |

## Address pins

The subnets and container IPs are pinned in
`../compose/enforcement/docker-compose.yml` (ipam) and referenced by both
on-host scripts. **They are a set — change them together or not at all:**

| Thing | Value |
| --- | --- |
| edge subnet | 172.29.99.0/24 |
| kernel subnet | 172.29.98.0/24 |
| edc subnet (internal) | 172.29.100.0/24 |
| biject-proxy on edc | 172.29.100.10 |
| traefik on edc (UI exception) | 172.29.100.20 |
| openclinica | 172.29.100.30 |
| oc-db (Postgres) | 172.29.100.40 |

(Deliberately a different 172.29.x range from Hetzner's 172.28.x, so a config
that leaks across deployments is visibly wrong rather than silently plausible.)

## Order of operations on EC2 #1

```
sudo ./apply.sh        # nftables + DOCKER-USER (idempotent; re-run any time)
```

`apply.sh` prints the persistence note: nftables rules do not survive reboot
unless wired into `/etc/nftables.conf`, and `docker-user-rules.sh` needs a
systemd unit ordered `After=docker.service`.

## Acceptance — run from the AGENT host

`verify_lockdown_aws.sh` implements the spec §5.3 probe set **from EC2 #2**:

1. `curl https://oc.<domain>/` → expect **403** (presenter-only allowlist);
2. `curl http://<ec2-1-private-ip>:8080` → expect **timeout** (SG drops the
   SYN; a "refused" fails the probe — an RST means the packet got through);
3. raw TCP (`nc`) to **8080 and 5432** → expect **timeout**, same reasoning;
4. `curl` the proxy health endpoint **with** the API key → expect **success**
   (without this positive probe the refusals prove nothing).

Exit 0 only if all hold. Sprint acceptance wording still applies: the refusal
must be **tested and confirmed refused, not assumed**.
