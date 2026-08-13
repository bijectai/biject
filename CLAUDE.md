# biject (sprint v4 demo repo) — engineering notes for Claude

This repo hosts the **4-day sprint v4 demo**: an OpenAI Agents SDK agent that
resolves open data queries in a self-hosted OpenClinica 3.17 CE (OC3) EDC, with
every write forced through a verification proxy that checks a **Lean
kernel-decided audit bound before forwarding**. Ticket IDs `S4-D-##` (Dev) and
`S4-A-##` (Adeel). Deviations are logged in `.claude/deviations/` — read those
before assuming a ticket's original spec still describes the code.

## Invariants (do not weaken)

- **biject never receives natural language at verification time.** The proxy
  extracts a typed structured entry and sends only that to the kernel. The
  corrected value (`newValue`) is forwarded to OC but only its SHA-256 hash
  enters the kernel. `reasonCode` is an integer enum, never free text. Any
  change letting NL reach a kernel parameter is a product-claim violation.
- **Verify before forward, fail closed.** The proxy calls verify first and
  forwards only on an `allowed`/PROVED verdict; any verify timeout, error, or
  unknown verdict is a denial (`verdict != "allowed"` semantics, never a
  known-bad blocklist). Advisory lanes (heuristics, LLM judge) run outside the
  enforcement path and their output is never a kernel input or proxy decision.
- **The enforcement bound holds at the network layer.** OC and its Postgres
  live on an internal Docker network reachable only from the proxy; the agent
  host has no route to OC except the proxy (`infra/hetzner/firewall/`,
  verified by `verify_lockdown.sh` — tested, not assumed). This is why the
  agent uses SDK **function tools over plain HTTP, not MCP** — MCP is the
  post-sprint upgrade.
- **Two trusted inputs, both named:** `sigOk` (Ed25519 verdict from the
  signing pipeline) and `nowMs` (proxy-supplied clock). Documented in
  `PolicyEnv/PolicyEnv/Contract.lean`; keep the trust-boundary paragraph in
  sync with any change to either.
- **Lean house rules:** no `sorry`, no `axiom`, no `native_decide`, no
  `unsafe`, no `extern` anywhere under `PolicyEnv/`. Kernel checks stay Nat
  comparisons, String (in)equality, and enum bounds. `AuditEntryValid` is
  `Decidable` with zero axiom dependencies (verified via `#print axioms`).
- **Strict monotonicity:** backdating is rejected with strict `>` against the
  ledger head; an entry stamped exactly at the head is a replay. Known
  limitation: strict monotonicity assumes a single writer — fine for the
  single-agent demo, needs a sequencer for multi-writer.
- **OC3 auth is form-session (read) + WS-Security UsernameToken (write).**
  OC3 CE has SOAP + session ODM export only. **No OC4 REST. Never claim
  OAuth.** Seeded study data is 100% synthetic — no real PHI, ever.
- **SDK tracing is OFF** (`OPENAI_AGENTS_DISABLE_TRACING=1` /
  `set_tracing_disabled(True)`): PHI-adjacent payloads must not leave the
  host. Sprint decision #3.

## Public claim boundary

The demo proves: formal action-gating on a defined action surface, enforced
pre-commit, with a third-party-checkable proof artifact. It does **not** prove
21 CFR Part 11 compliance or coverage outside the formalized surface. PROVED
means the predicate holds under kernel checking — not that FDA would agree.
Adapter claim: "reference integration for OpenAI Agents SDK, validated end to
end; Bedrock and Foundry adapters written, not yet run."

## File-to-role map

- `PolicyEnv/` — standalone Lean 4 Lake project (toolchain
  `leanprover/lean4:v4.28.0`, same as the platform's `lean-worker/PolicyEnv`).
  - `PolicyEnv/Contract.lean` — typed kernel-side mirror of the tool-call
    contract (`AuditEntry`, `VerifyContext`, action/reason enums,
    `forwardSkewMs`), plus the trust-boundary documentation.
  - `PolicyEnv/AuditBound.lean` — `AuditEntryValid` (S4-D-13, the enforced
    audit bound) + derived `Decidable` instance + compile-time regression
    vectors (`lake build` is the regression suite).
- `contracts/tool_calls.json` — **DRAFT v0** of the tool-call contract;
  superseded by the S4-A-12 freeze (Adeel). The platform convention is that
  contracts live in `bijectai/biject-contracts` and are vendored — reconcile
  at freeze time.
- `edc/` — OC3 integration: `oc3_client.py` (session ODM read with
  `includeDNs=y&includeAudits=y`; SOAP `importData` write with `UpsertOn` +
  `TransactionType="Update"`), `study_def.xml` / `seed_data.xml` (synthetic
  study BJT-DEMO-01 with deliberately messy, *self-resolvable* data),
  `seed.py` (data import + `--verify` open-query count).
- `adapters/openai/` — Agents SDK function tools POSTing to the proxy.
  `adapters/` is **outside the core dependency graph**; core code never
  imports from it.
- `infra/hetzner/` — compose skeleton (Traefik TLS via `DEMO_DOMAIN`,
  networks `edge` + internal `edc_internal`), `openclinica/` (OC 3.17 CE +
  Postgres 9.5 + **OpenClinica-ws SOAP WAR** — the ws WAR is a separate
  artifact; without it there is nothing to write to), `firewall/`
  (DOCKER-USER chain lockdown + `verify_lockdown.sh` acceptance test).
- `scripts/preflight.sh` — S4-D-00 host checks (memory/disk headroom,
  egress to api.openai.com, Responses API access, docker versions). Run ON
  the Hetzner host before anything else.
- `docs/DAY1-2-RUNBOOK.md` — the manual, host-side steps that cannot be done
  from a code sandbox, in execution order, with the sprint's gates and
  fallback decision points.

## Deploy/build notes

- Coolify must use **Raw Compose Deployment** mode — Application mode
  silently strips `ipam` from compose networks (learned on the platform
  deploy; recorded in the knowledge graph as `CoolifyTraefikDeployment`).
- Lean build: `cd PolicyEnv && lake build` (that also runs the compile-time
  regression vectors). Toolchain pinned in `PolicyEnv/lean-toolchain`.
- Secrets only via untracked `.env` (see `infra/hetzner/.env.example`);
  nothing secret is committed. API keys live in env files on the host.

## Knowledge graph

Shared KG lives in the Supabase project `brain` (`entities`/`observations`/
`relations`, biject project namespace). Search it before touching a
component; record decisions and gotchas when you land a change.
