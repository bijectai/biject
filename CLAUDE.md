# biject — engineering notes for Claude

Project-level guidance and durable facts for this repository. Keep entries factual and
current; when a change touches one of these areas, update the relevant section in the
same PR.

## 1. Context *(platform)*

**biject** is a formal-verification guardrail layer for AI-agent tool calls. An agent's
proposed action is turned into a Lean 4 conjecture, checked by a pre-warmed Lean kernel
pool against compiled policies, and the binary PROVED / REFUTED verdict is written to a
cryptographically signed, hash-chained audit ledger.

The platform is split across seven repositories:

| Repo | Role |
| --- | --- |
| `biject` | Meta repo. Owns the deployment topology: `docker-compose.yml` pinning every service image by commit SHA. |
| `biject-api` | The verification core — FastAPI, Lean kernel pool, policy registry, signed audit ledger. Reference implementation for repo structure. |
| `biject-proxy` | Inline enforcement hop. Forwards a tool call to its downstream target only after `biject-api` returns `allowed`. |
| `biject-trace` | Read-only ledger query and chain-verification surface. |
| `biject-judge` | Advisory lane — heuristic and model-based scoring. Never authorizes. |
| `biject-console` | Operator UI. |
| `biject-contracts` | The wire contracts every other repo speaks. Ships no runtime. |

**This repo holds two independent things.** They arrived separately and both are
live:

1. **The platform deployment topology.** The root `docker-compose.yml` names every
   service and pins each one to an immutable image; no image is published from here.
   Sections marked *(platform)* below govern it.
2. **The sprint v4 demo** — `PolicyEnv/`, `edc/`, `adapters/`, `infra/hetzner/`,
   `docs/DAY1-2-RUNBOOK.md`. Sections marked *(sprint v4)* govern it.

No code path crosses between the two. Two names do collide and are worth fixing in
your head before reading further: the root `docker-compose.yml` is the platform
topology while `infra/hetzner/docker-compose.yml` is the demo host stack, and
`contracts/tool_calls.json` is the sprint's draft contract — the platform's contracts
live in `bijectai/biject-contracts` and are vendored into each service.

An earlier version of this file said this repo owned the deployment topology "and
nothing else". That was true when it was written and is not true now; the sprint work
landed on `main` afterwards.

A pin is a fact about what is running. Changing one is a deploy, and it shows up as a
one-line diff with a commit SHA you can look up. Most of the rules below exist to keep
that property true.

## 2. Ownership *(platform)*

| Area | Owner |
| --- | --- |
| Everything in this repo | `@bijectai/eng` |
| `docker-compose.yml` | `@bijectai/eng` — every change here is a deploy |
| `scripts/` | `@bijectai/eng` |
| `.github/workflows/` | `@bijectai/eng` |

`.github/CODEOWNERS` is the enforced version of this table; keep the two in step. The
handles are a placeholder team until the org's real teams exist — replace them, do not
delete the file.

## 2B. Review rules

Three rules govern every repo in the platform. They are stated identically in every
repo's `CLAUDE.md` so that a change reviewed in isolation is still reviewed against
them. `AGENTS.md` carries the reviewer-facing procedure and points here for the rules
themselves, so the two cannot drift.

### 2B.1 — Verification inputs

A policy or authorization decision must be made only on typed, enumerated, or hashed
values. Free-form natural language — from a caller, an agent, or a model — must never
become an input to a decision, and must never reach a proof kernel parameter. This is
the core product claim; a violation invalidates it.

*Safe path:* extract a typed struct at the request boundary and pass enum indices and
content hashes. Advisory lanes (heuristics, LLM judging, scoring) may read prose, but
their output must never feed a decision.

*In this repo:* there is no decision path to protect — this file starts containers, it
does not authorize calls. The rule shows up as a deployment default instead:
`PROXY_TARGETS` is empty, so `biject-proxy` comes up unable to reach anything until
someone writes down what it may reach. Callers name a target; nobody supplies a URL.

### 2B.2 — Enforcement ordering

No request may reach a downstream system before verification has returned a positive
verdict, and failure to obtain a verdict must deny rather than pass. Blocking has to be
structural — before the call is made — not a flag checked after the fact.

*Safe path:* block on the verdict; treat any non-positive result, including a timeout,
an error, or a missing policy, as a denial. Forward exactly the parameters that were
verified, so nothing can change between the check and the use.

*In this repo:* the topology must not undermine the ordering the services implement.
Two decisions carry that:

- **`biject-proxy` has no `depends_on` for `biject-api`.** The proxy is fail-closed:
  with the verifier unreachable it comes up and denies every call, which is the
  specified behaviour. An ordering dependency would stop it starting exactly when you
  most want it up and denying.
- **Its healthcheck probes `/healthz`, not `/readyz`.** A proxy denying every call is
  working, not unhealthy, and an orchestrator must not restart it for that.

`biject-console` likewise has no `depends_on` — nginx resolves upstreams per request, so
the console comes up and reports an outage rather than joining it.

### 2B.3 — Secret material

Signing keys, key seeds, and loaded private-key objects must never reach an output
surface: log lines, stdout, exception messages, API response models, test fixtures, or
files committed to the repository. The audit ledger's non-repudiation guarantee rests
entirely on the signing key staying secret.

*Safe path:* emit a key identifier or a fingerprint instead of the material, and read key
material from the environment rather than from a tracked file.

*In this repo:* `AUDIT_SIGNING_KEY` is referenced but never held. It is read from `.env`,
which is gitignored, and `.env.example` carries an empty placeholder and the command to
generate a real one. No key, seed, or token may be written into `docker-compose.yml`,
where it would be committed. `biject-trace` receives only `AUDIT_VERIFY_PUBKEY`, the
public half — a read-only observer holding the signing key would void the ledger's
non-repudiation guarantee.

## The sprint v4 demo

`main` carries the **4-day sprint v4 demo**: an OpenAI Agents SDK agent that resolves
open data queries in a self-hosted OpenClinica 3.17 CE (OC3) EDC, with every write
forced through a verification proxy that checks a **Lean kernel-decided audit bound
before forwarding**. Ticket IDs `S4-D-##` (Dev) and `S4-A-##` (Adeel). Deviations are
logged in `.claude/deviations/` — read those before assuming a ticket's original spec
still describes the code.

The invariants below are the sprint's own statement of §2B.1 and §2B.2 in its own
terms. They agree with the rules above; where the sprint is more specific, the sprint
text is the operative one for the demo's files.

## Invariants *(sprint v4)* — do not weaken

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
  signing pipeline — since S4-D-30, verified over the signed digest
  recomputed from the ledger-stored canonical params, so proven bytes =
  recorded bytes) and `nowMs` (proxy-supplied clock). Documented in
  `PolicyEnv/PolicyEnv/Contract.lean`; keep the trust-boundary paragraph in
  sync with any change to either.
- **Lean house rules:** no `sorry`, no `axiom`, no `native_decide`, no
  `unsafe`, no `extern` anywhere under `PolicyEnv/`. Kernel checks stay Nat
  comparisons, String (in)equality, String byte length (`utf8ByteSize` — NOT
  `String.length`, which drags in classical axioms on this toolchain), and
  enum bounds. `AuditEntryValid` is `Decidable` with zero axiom dependencies
  (verified via the `#print axioms` lines at the bottom of `AuditBound.lean`,
  which print into every `lake build` log).
- **Strict monotonicity, bound to the real ledger head (S4-D-30):**
  backdating is rejected with strict `>` against the ledger head; an entry
  stamped exactly at the head is a replay. The head is no longer a bare
  caller-supplied timestamp: `VerifyContext` carries the
  (`ledgerHeadHash`, `ledgerHeadTsMs`) pair read from the verified signed
  chain, and the predicate enforces digest shape + genesis consistency so a
  stub context (e.g. `ledger_head_ts = 0` against a non-empty chain) is
  refuted. `scripts/audit_bound_harness.py` proves the derivation against a
  golden fixture generated by biject-api's real chain code. Known
  limitation: strict monotonicity assumes a single writer — fine for the
  single-agent demo, needs a sequencer for multi-writer.
- **OC3 auth is form-session (read) + WS-Security UsernameToken (write).**
  OC3 CE has SOAP + session ODM export only. **No OC4 REST. Never claim
  OAuth.** Seeded study data is 100% synthetic — no real PHI, ever.
- **SDK tracing is OFF** (`OPENAI_AGENTS_DISABLE_TRACING=1` /
  `set_tracing_disabled(True)`): PHI-adjacent payloads must not leave the
  host. Sprint decision #3.

## Public claim boundary *(sprint v4)*

The demo proves: formal action-gating on a defined action surface, enforced
pre-commit, with a third-party-checkable proof artifact. It does **not** prove
21 CFR Part 11 compliance or coverage outside the formalized surface. PROVED
means the predicate holds under kernel checking — not that FDA would agree.
Adapter claim: "reference integration for OpenAI Agents SDK, validated end to
end; Bedrock and Foundry adapters written, not yet run."

## File-to-role map *(sprint v4)*

- `PolicyEnv/` — standalone Lean 4 Lake project (toolchain
  `leanprover/lean4:v4.28.0`, same as the platform's `lean-worker/PolicyEnv`).
  - `PolicyEnv/Contract.lean` — typed kernel-side mirror of the tool-call
    contract (`AuditEntry`, `VerifyContext`, action/reason enums,
    `forwardSkewMs`), plus the trust-boundary documentation.
  - `PolicyEnv/AuditBound.lean` — `AuditEntryValid` (S4-D-13, hardened by
    S4-D-30: ledger-head binding, missing vs unknown reason split) + derived
    `Decidable` instance + compile-time regression vectors (`lake build` is
    the regression suite; the accepting vector's numbers come from the
    ledger fixture below and are cross-checked by the harness).
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
- `scripts/audit_bound_harness.py` (S4-D-30) — verifies the golden ledger
  fixture (`scripts/fixtures/audit_ledger/`, produced by
  `scripts/gen_audit_fixture.py` driving biject-api's real chain code),
  derives the ledger head from the chain, verifies both Ed25519 surfaces,
  and fails if the Lean accepting vector and the fixture disagree. Runtime
  dep: `cryptography` (`scripts/requirements.txt`). The fixture ships
  PUBLIC keys only; all signing keys are ephemeral, never committed.
- `docs/DAY1-2-RUNBOOK.md` — the manual, host-side steps that cannot be done
  from a code sandbox, in execution order, with the sprint's gates and
  fallback decision points.

## Deploy/build notes *(sprint v4)*

- Coolify must use **Raw Compose Deployment** mode — Application mode
  silently strips `ipam` from compose networks (learned on the platform
  deploy; recorded in the knowledge graph as `CoolifyTraefikDeployment`).
- Lean build: `cd PolicyEnv && lake build` (that also runs the compile-time
  regression vectors). Toolchain pinned in `PolicyEnv/lean-toolchain`.
- Secrets only via untracked `.env` (see `infra/hetzner/.env.example`);
  nothing secret is committed. API keys live in env files on the host.

## Repo layout *(platform)*

- `docker-compose.yml` — the topology (the root file, not `infra/hetzner/`'s). Every image pinned: biject services to a full
  40-character git SHA, third-party images to a `sha256` digest.
- `scripts/verify-pins.sh` — enforces that rule. Runs in CI on every PR.
- `scripts/check-no-secrets.py` — parses every tracked compose file and fails on a
  literal value under a credential-shaped key. Deliberately a parser, not a pattern,
  and it carries its own regression suite (`--self-test`); see § CI.
- `scripts/pin-images.sh` — moves a pin. Updates the image tag **and** the matching
  `BIJECT_IMAGE_SHA` together, because a container that reports a different SHA than the
  one deployed is worse than one that reports nothing.
- `scripts/smoke.sh` — health probes, plus a comparison of each service's reported
  `image_sha` against the pin. `docker compose ps` says a container is healthy; only this
  says it is the build this repo claims to deploy — and where it *cannot* say that, it
  now says so. A service pinned with `BIJECT_IMAGE_SHA` that reports no `image_sha`
  fails; a service carrying no `BIJECT_IMAGE_SHA` at all (`biject-api`, `biject-console`)
  is listed as NOT VERIFIED in the summary instead of passing silently.
- `.env.example` — every variable the stack reads. `.env` itself is gitignored.

## Rules for `docker-compose.yml` *(platform)*

- **No floating tags.** No `:latest`, no `:main`, no `${VAR}` in an `image:` line. The
  service repos publish exactly one tag per merge — the commit SHA — precisely so there
  is nothing else to point at.
- **No `build:` stanza.** A build key beside an `image:` lets someone run something other
  than what is pinned under the pinned name, which silently defeats the pin.
  `verify-pins.sh` fails on it.
- **Pin third-party images by digest.** There is no commit to point at, but the
  principle holds: the tag is a label, the digest is the identity. Note that this
  principle is currently applied *only* to third-party images — first-party services
  are pinned by git-SHA tag, which is a label too. That inconsistency is real and is
  recorded under § Gate conditions rather than argued away.
- **Move pins with the script.** Hand-editing works right up until the day the
  `BIJECT_IMAGE_SHA` beside it is forgotten.

## Deploying *(platform)*

1. Merge to `main` in a service repo. Its `ci` run publishes
   `ghcr.io/bijectai/<repo>:<sha>` and prints the pin line in the job summary.
2. `./scripts/pin-images.sh <service> <sha>`
3. `./scripts/verify-pins.sh`
4. Commit — the diff *is* the deploy record.
5. `docker compose up -d && ./scripts/smoke.sh`


## Test baseline *(platform)*

The platform half has no test suite — it has no application code. What stands in for one:

```
./scripts/verify-pins.sh              # every image immutable; no build: stanza
docker compose config -q              # the file parses and resolves
python3 scripts/check-no-secrets.py --self-test      # the checker still checks
git ls-files '*compose*.y*ml' | xargs python3 scripts/check-no-secrets.py
./scripts/smoke.sh                    # after `docker compose up -d`
```

The first three are what CI runs. `smoke.sh` needs a running stack and is a
local/post-deploy check. `check-no-secrets.py` needs `pyyaml`; everything else here is
dependency-free.

The sprint half builds with `cd PolicyEnv && lake build`, which also runs its
compile-time regression vectors. CI does not run it.



## CI *(platform)*

`.github/workflows/ci.yml` runs two jobs. This repo publishes no image, so the four-job
service standard does not apply — see
`.claude/deviations/repo-structure-standard.md`.

1. **`compose`** — `docker compose config -q` against a synthetic environment, so a
   malformed file or an unresolvable variable fails on the PR rather than at deploy;
   then `scripts/check-no-secrets.py` over every tracked compose file.
2. **`pins`** — `scripts/verify-pins.sh`. Every image immutable, no `build:` stanza.

`check-no-secrets.py` **parses** the YAML rather than grepping it, and it **carries its
own regression suite**. Both details are load-bearing, and the second one is the more
important.

This check has been bypassed four times. The first three were line-based patterns —
block sequence, then quoted entries, then flow style — each covering only the YAML
spellings its author had in mind. Parsing fixed that whole class: a parser sees every
spelling as the same data, follows anchors and aliases, and checks the whole document
rather than `environment:` alone, since a credential in `labels:` or `build.args:` is
just as committed.

The fourth bypass is the instructive one, because the parser was right about
*structure* and wrong about *values*: it treated any `$`-prefixed string as a safe
reference, so **`${VAR:-fallback}` passed** even though the fallback is a literal
shipped in the file. It also skipped non-string scalars (a numeric password) and its
key-name list missed common access-key spellings.

Four rounds of fixing one report at a time did not converge, so every bypass ever found
is now a pinned case in the script's `SAFE`/`UNSAFE` lists, and `--self-test` runs them
in CI before the scan does. **Add a case before fixing a new report.** The suite has
already paid for itself — it caught a regression during its own introduction.

Safe forms are `${VAR}`, `${VAR:?message}`, `$VAR`, and an empty placeholder.
`${VAR:-fallback}` is *not* safe. Public keys (`AUDIT_VERIFY_PUBKEY`) are excluded by
name, because a public half is meant to be committed.


## Automated review

`.github/workflows/codex-review.yml` runs Codex over every pull request. It is copied
from `biject-api`, which is the reference implementation, and the workflow file is
byte-identical there and here — fix it in one, copy it to the others.

It is **advisory and structurally unable to change anything**:

- the job that runs the model holds `contents: read` and a read-only sandbox;
- the job that can write holds `pull-requests: write` only, never checks out PR code,
  and never runs the model;
- no job produces a commit, and the check is not required, so a finding can never block
  a merge.

Two pieces are read from the **base** branch rather than the PR — `.github/codex/`
(the classifier and the prompt template) and `AGENTS.md` (the rules). A pull request
therefore cannot weaken the reviewer that is judging it; a rule added in a PR takes
effect only once that PR merges.

`.github/codex/classify.py` picks the model and reasoning effort from a RED/YELLOW/GREEN
zone map, and scopes the rules to the directories the diff touches by extracting the
`## Code Review Rules` section from every governing `AGENTS.md`. **That heading is
load-bearing** — rename it and the reviewer silently runs with no project rules at all.
Only the zone patterns differ between repos; the rest of the file is identical
everywhere.

Cost control: `CODEX_REVIEW_ENABLED` is a kill switch (set it to anything but `true` to
stop all spend without editing a file), draft PRs are skipped, a burst of pushes cancels
the in-flight review, and a diff that is only lockfiles, images, or `README.md` is
skipped before any model call.

## Gate conditions

- **Codex review is not provisioned.** `.github/workflows/codex-review.yml` stays inert
  until the repository has the `OPENAI_API_KEY` secret and the `CODEX_REVIEW_ENABLED`
  variable set to `true`. Both are best set at the organization level so every repo
  picks them up at once. Note the first PR to land after enabling it may fail the gate
  job with a "control-plane skew" error if `main` does not yet carry
  `.github/codex/classify.py` — that is expected exactly once, and merging fixes it.

- **Four of the six images are not published yet.** `biject-proxy` and
  `biject-console` have merged to `main` and their CI published the tags this file
  pins, so those two pull. The rest name commits CI will build but has not, so
  `docker compose pull` 404s on them.
- **`biject-api` has no publish workflow.** It is the only service whose repo does not
  yet carry the standard CI, so `ghcr.io/bijectai/biject-api:<sha>` has no producer.
  Adding that workflow to `biject-api` is the single blocking item for a full-stack
  `docker compose up`; it was out of scope for the change that created this repo.
- **`AUDIT_SIGNING_KEY` is unset in every environment.** `biject-api` raises at import
  without it, so the stack will not start. Generate one and put it in `.env` before first
  boot — and note that rotating it later invalidates every prior signature and breaks
  chain continuity, so it needs a migration plan, not a re-roll.

- **First-party images are pinned by tag, and a tag is mutable.** A 40-hex git SHA
  *looks* immutable but is still an OCI tag: anyone who can push to the GHCR package
  can retarget it, and the stack would then pull unreviewed content under an unchanged
  pin. `verify-pins.sh` checks the shape of the reference, not the bytes behind it, and
  `smoke.sh` cannot catch it either — both sides of its comparison (the compose pin and
  the container's `BIJECT_IMAGE_SHA`) come from this file, so they agree by construction
  no matter what image is running.

  The fix is to pin as `ghcr.io/bijectai/<repo>:<git-sha>@sha256:<digest>` — Docker
  pulls by digest and the tag stays readable, so the diff still names a commit you can
  look up. It has not been done yet for one concrete reason: the GHCR packages are
  private, so a digest cannot be resolved without a registry credential, and four of the
  six images do not exist yet to have a digest at all. Do this when `pin-images.sh` can
  authenticate to GHCR: have it resolve the manifest digest at pin time and write both,
  and tighten `verify-pins.sh` to require the `@sha256:` suffix once every service
  publishes. Until then the guarantee is "immutable by convention, enforced by who holds
  push access to the package", which is weaker than this file's other pins.


## Knowledge graph

This platform has a shared, persistent knowledge graph via the `knowledge-graph` MCP
server — the Supabase project `brain` (`entities` / `observations` / `relations`, in the
`biject` project namespace). It holds what isn't recoverable from reading the code: why
things are the way they are, constraints that will bite you, and who owns what.

- At the start of a task, `search_nodes` for the components you're about to touch.
- At the end, record what changed and why: `create_entities` for new components,
  `add_observations` for decisions and gotchas, `create_relations` for dependencies
  (active voice, e.g. `ApiGateway depends_on AuthService`).
- Retract facts that have become wrong with `delete_observations`. Stale knowledge is
  worse than none.
- Record **why**, not **what** — the diff already says what changed.
- Use `search_nodes`, not `read_graph`.
