# infra/hetzner/firewall — the enforcement boundary

Ticket S4-D-21. This directory implements and *proves* the single security
claim the demo rests on:

> The agent host has **no route to OpenClinica except the verification proxy**.
> Every tool call therefore passes the Rust proxy, which verifies a typed,
> signed audit entry against the Lean kernel **before** anything is forwarded
> to OC.

## Why the bound is at the network layer (not the protocol layer)

Protocol-layer guardrails (an MCP server, an SDK wrapper, a system prompt that
says "only use the tools") constrain a *well-behaved* agent. They do nothing
against a jailbroken agent, a buggy tool implementation, or any other process
on the agent host that opens a raw socket to OC. The enforcement here is
therefore placed where the agent's behavior is irrelevant: the kernel firewall
drops every packet from the agent toward the EDC network. If the packets
cannot arrive, the protocol above them does not matter. This is also why the
OpenAI adapter uses plain SDK function tools over HTTP rather than MCP — MCP
adds ergonomics, not enforcement, in this topology (see
`adapters/openai/tools.py`).

## Topology (matches the compose setup)

```
 networks:
   edge          172.28.99.0/24   agent (.20)  <->  proxy (.10)
   edc_internal  172.28.100.0/24   proxy (.10)  <->  OC (.30), Postgres (.40)
                 (internal: true — no host-published ports)
```

* `edc_internal` is a Docker `internal: true` network: OC and Postgres have no
  route out, and only containers attached to it (proxy, OC, Postgres) can be
  reached on it. That is **layer 1** of the isolation.
* The firewall rules in this directory are **layer 2**: they hold even if the
  compose file regresses (a port gets published, a container gets attached to
  the wrong network, someone uses `network_mode: host`).
* Agent egress is pinned to the proxy plus `api.openai.com:443`; all other
  private-address egress from the agent is dropped, closing LAN side-channels.

## Files

| File | Role |
| --- | --- |
| `oc-ingress.nft` | Host-level nftables table (`inet biject_lockdown`): input-hook protection for accidentally host-published OC/Postgres ports, plus a forward-hook copy of the policy at priority −5 (ahead of Docker's rules). Defense-in-depth around the Docker path. |
| `docker-user-rules.sh` | **The reliable hook for containerized traffic.** Docker owns FORWARD and rewrites its rules, but always consults the `DOCKER-USER` chain first and never flushes it. The script maintains a dedicated `BIJECT-LOCKDOWN` child chain there: proxy→OC allowed (service ports only), everything else to the EDC subnet log+DROP, agent egress restricted to proxy + public 443 (`OPENAI_STRICT=1` pins to api.openai.com's currently-resolved IPs). Subnets/IPs are variables at the top. Idempotent. |
| `apply.sh` | Orders the two layers: syntax-checks and loads the nft table, then installs the DOCKER-USER rules. Prints the persistence notes (nftables service + a systemd unit `After=docker.service` for the DOCKER-USER layer). |
| `verify_lockdown.sh` | **The acceptance test.** From the agent's vantage point: (1) direct curl to OC must FAIL, (2) direct TCP to Postgres must FAIL, (3) proxy health call must SUCCEED. Exits 0 only if all three hold — i.e. the refusal is *tested and confirmed refused, not assumed*, and confirmed against a live network rather than a dead one. |

## Operating notes

* Refused attempts are **logged** (kernel log prefixes `biject-ddrop-oc:`,
  `biject-ddrop-agent:`, `biject-oc-ingress-drop:`; rate-limited). Pull them
  with `journalctl -k | grep biject-ddrop` — they are part of the audit trail:
  a refused direct-to-OC attempt is a reportable event, not noise.
* Keep the subnet/IP parameters identical in `oc-ingress.nft`,
  `docker-user-rules.sh`, and the compose `ipam` blocks. They are declared
  once at the top of each file.
* `OPENAI_STRICT=1` pins agent egress to the IPs `api.openai.com` resolves to
  at apply time; those rotate, so re-run the script on a timer if you enable
  it. Default posture: agent may reach public 443 but zero private space.
* No secrets live in this directory, and none are needed by any script.

## Audit checklist (from the ticket)

Run through this before signing off the sprint acceptance item:

- [ ] `apply.sh` runs cleanly as root; `nft list table inet biject_lockdown`
      shows the table; `iptables -L BIJECT-LOCKDOWN -n -v` shows the chain
      jumped from `DOCKER-USER` at position 1.
- [ ] `verify_lockdown.sh` exits **0** from inside the agent container/host:
      direct OC call refused, direct Postgres connection refused, proxy health
      call succeeded. Attach the output to the acceptance record.
- [ ] The refusal appears in the kernel log
      (`journalctl -k | grep biject-ddrop-oc`) — proving the drop was the
      firewall, not a down service.
- [ ] `docker compose config` confirms `edc_internal` is `internal: true` and
      that **no** OC/Postgres port appears under any `ports:` mapping.
- [ ] Proxy is the only container attached to both `edge` and `edc_internal`;
      the agent container is attached to `edge` only.
- [ ] Rules survive a `docker compose restart` (Docker rewrote its chains;
      `BIJECT-LOCKDOWN` must still be first in `DOCKER-USER`) and a host
      reboot (persistence units in place per `apply.sh` notes).
- [ ] Re-run `verify_lockdown.sh` after the restart/reboot checks above.
