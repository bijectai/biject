# biject

Formal-verification guardrail layer for AI-agent tool calls. An agent's proposed action
is turned into a Lean 4 conjecture, checked by a pre-warmed Lean kernel pool against
compiled policies, and the binary PROVED / REFUTED verdict is written to a
cryptographically signed, hash-chained audit ledger.

**This repository holds two things.** They arrived independently and both are live:

1. **The platform deployment topology** — the root [`docker-compose.yml`](docker-compose.yml),
   which names every biject service and pins each to an immutable image, plus the
   tooling in `scripts/` that enforces and moves those pins.
2. **The sprint v4 demo** — a self-contained clinical-data reconciliation demo:
   `PolicyEnv/`, `edc/`, `adapters/`, `infra/hetzner/`, `docs/DAY1-2-RUNBOOK.md`.

They share a directory tree and nothing else — no code path crosses between them. Two
names do collide, and it is worth knowing which is which before you open either:

| Name | Platform meaning | Sprint-demo meaning |
| --- | --- | --- |
| `docker-compose.yml` | Root file: the pinned platform topology | `infra/hetzner/docker-compose.yml`: the demo host stack (Traefik, OpenClinica, Postgres) |
| `contracts/` | Lives in [`biject-contracts`](https://github.com/bijectai/biject-contracts) and is vendored into each service | `contracts/tool_calls.json`: the sprint's **draft v0** tool-call contract, pending freeze |
| `scripts/` | `verify-pins.sh`, `pin-images.sh`, `smoke.sh` | `preflight.sh`, `audit_bound_harness.py`, `gen_audit_fixture.py` |

---

## Part 1 — The platform topology

The platform is split across seven repositories. This one owns how they are deployed
together; it publishes no image of its own.

| Repo | Role | Port |
| --- | --- | --- |
| [`biject-api`](https://github.com/bijectai/biject-api) | Verification core — FastAPI, Lean kernel pool, policy registry, signed ledger | 8002 |
| [`biject-proxy`](https://github.com/bijectai/biject-proxy) | Inline enforcement hop. Forwards only on a positive verdict | 8080 |
| [`biject-trace`](https://github.com/bijectai/biject-trace) | Read-only ledger query + chain verification | 8010 |
| [`biject-judge`](https://github.com/bijectai/biject-judge) | Advisory scoring. Never authorizes | 8020 |
| [`biject-console`](https://github.com/bijectai/biject-console) | Operator status board | 5173 |
| [`biject-contracts`](https://github.com/bijectai/biject-contracts) | The wire contracts. No runtime | — |

### Before first boot

```bash
cp .env.example .env
python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
# paste into AUDIT_SIGNING_KEY
```

`biject-api` raises at **import** time without `AUDIT_SIGNING_KEY` — there is no
ephemeral-key fallback — so the stack will not start until this is set. Rotating it
later invalidates every prior signature and breaks chain continuity for existing ledger
entries, so a rotation needs a migration plan rather than a re-roll.

### Running it

```bash
docker compose up -d
./scripts/smoke.sh
```

`smoke.sh` probes each service **and** compares the `image_sha` it reports against the
pin in `docker-compose.yml`. `docker compose ps` tells you a container is healthy; only
this tells you it is the build this repo claims to deploy.

### Pinning

Every image is pinned to something immutable: biject services to a full 40-character
git SHA, third-party images to a `sha256` digest. Each service repo's CI publishes
exactly one tag per merge to `main` — `ghcr.io/bijectai/<repo>:<git-sha>` — and no
`:latest`, so there is no floating tag to deploy by accident.

```bash
./scripts/pin-images.sh biject-proxy 68b73a0478ec7edf270e6eb5ae4402dc09459ff4
./scripts/verify-pins.sh
```

`pin-images.sh` moves the image tag and the matching `BIJECT_IMAGE_SHA` together — a
container that reports a different SHA than the one deployed is worse than one that
reports nothing. `verify-pins.sh` runs in CI and rejects floating tags, missing digests,
and any `build:` stanza that would let something other than the pinned image run under
the pinned name.

**A pin change is a deploy.** The diff is the deploy record.

### Two things that look like omissions and are not

**`biject-proxy` has no `depends_on: biject-api`, and its healthcheck probes `/healthz`
rather than `/readyz`.** The proxy is fail-closed: with the verifier unreachable it
comes up and denies every call, which is the specified behaviour. An ordering dependency
would stop it starting exactly when you most want it up and denying, and a readiness
healthcheck would get it restarted for doing its job.

**`biject-console` has no `depends_on` either.** nginx resolves upstreams per request, so
the console comes up and *reports* an outage instead of joining it.

### Current state

`biject-proxy` and `biject-console` are pinned to images that exist — their CI has run on
`main` and published them. The remaining pins name real commits whose images **have not
been published yet**, and `biject-api` has no publish workflow at all, so nothing produces
`ghcr.io/bijectai/biject-api:<sha>`. See [`CLAUDE.md`](CLAUDE.md) § Gate conditions.

---

## Part 2 — The sprint v4 demo

Formal action-gating demonstrated on a clinical-data reconciliation task: an OpenAI
Agents SDK agent resolves open queries in a self-hosted OpenClinica 3.17 CE EDC, and
**every write is gated by a Lean kernel-decided audit bound, enforced pre-forward by a
verification proxy** that is the only network route to the EDC.

What the demo proves: formal action-gating on a defined action surface, enforced
pre-commit, with a third-party-checkable proof artifact. What it does not prove: 21 CFR
Part 11 compliance, breach prevention, or coverage of any action outside the formalized
surface. PROVED means the predicate holds under kernel checking.

| Path | Role |
|---|---|
| `PolicyEnv/` | Lean 4 audit-bound predicate (`AuditEntryValid`), decidable, compile-time regression vectors |
| `contracts/tool_calls.json` | Tool-call JSON contract (draft, pending freeze) |
| `adapters/openai/` | Agents SDK function tools → proxy (reference integration; Bedrock/Foundry written post-sprint, not yet run) |
| `edc/` | OpenClinica 3 client (session ODM read, SOAP write), synthetic seed study |
| `infra/hetzner/` | Compose + Traefik + OpenClinica stack + network lockdown |
| `scripts/preflight.sh` | Host capacity/egress preflight |
| `docs/DAY1-2-RUNBOOK.md` | Host-side execution runbook |

---

Engineering rules and invariants for both halves: see [`CLAUDE.md`](CLAUDE.md).
Deviations from ticket text: [`.claude/deviations/`](.claude/deviations/).
