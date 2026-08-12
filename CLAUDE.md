# biject — engineering notes for Claude

Project-level guidance and durable facts for this repository. Keep entries factual and
current; when a change touches one of these areas, update the relevant section in the
same PR.

## 1. Context

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

**This repo is the meta repo. It owns the deployment topology and nothing else.**
`docker-compose.yml` names every service and pins each one to an immutable image; there
is no application code here and no image published from here.

A pin is a fact about what is running. Changing one is a deploy, and it shows up as a
one-line diff with a commit SHA you can look up. Most of the rules below exist to keep
that property true.

## 2. Ownership

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

## Repo layout

- `docker-compose.yml` — the topology. Every image pinned: biject services to a full
  40-character git SHA, third-party images to a `sha256` digest.
- `scripts/verify-pins.sh` — enforces that rule. Runs in CI on every PR.
- `scripts/pin-images.sh` — moves a pin. Updates the image tag **and** the matching
  `BIJECT_IMAGE_SHA` together, because a container that reports a different SHA than the
  one deployed is worse than one that reports nothing.
- `scripts/smoke.sh` — health probes, plus a comparison of each service's reported
  `image_sha` against the pin. `docker compose ps` says a container is healthy; only this
  says it is the build this repo claims to deploy.
- `.env.example` — every variable the stack reads. `.env` itself is gitignored.

## Rules for this file

- **No floating tags.** No `:latest`, no `:main`, no `${VAR}` in an `image:` line. The
  service repos publish exactly one tag per merge — the commit SHA — precisely so there
  is nothing else to point at.
- **No `build:` stanza.** A build key beside an `image:` lets someone run something other
  than what is pinned under the pinned name, which silently defeats the pin.
  `verify-pins.sh` fails on it.
- **Pin third-party images by digest.** There is no commit to point at, but the
  principle holds: the tag is a label, the digest is the identity.
- **Move pins with the script.** Hand-editing works right up until the day the
  `BIJECT_IMAGE_SHA` beside it is forgotten.

## Deploying

1. Merge to `main` in a service repo. Its `ci` run publishes
   `ghcr.io/bijectai/<repo>:<sha>` and prints the pin line in the job summary.
2. `./scripts/pin-images.sh <service> <sha>`
3. `./scripts/verify-pins.sh`
4. Commit — the diff *is* the deploy record.
5. `docker compose up -d && ./scripts/smoke.sh`


## Test baseline

There is no test suite here — this repo has no application code. What stands in for one:

```
./scripts/verify-pins.sh              # every image immutable; no build: stanza
docker compose config -q              # the file parses and resolves
./scripts/smoke.sh                    # after `docker compose up -d`
```

`verify-pins.sh` and `docker compose config -q` are what CI runs. `smoke.sh` needs a
running stack and is a local/post-deploy check.



## CI

`.github/workflows/ci.yml` runs two jobs. This repo publishes no image, so the four-job
service standard does not apply — see
`.claude/deviations/repo-structure-standard.md`.

1. **`compose`** — `docker compose config -q` against a synthetic environment, so a
   malformed file or an unresolvable variable fails on the PR rather than at deploy.
2. **`pins`** — `scripts/verify-pins.sh`. Every image immutable, no `build:` stanza.


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

- **No image has actually been published to GHCR yet.** Every service repo's `ci`
  publishes on merge to `main`; until those merges happen, the tags this file pins do not
  exist and `docker compose pull` will 404. The pins are correct — they name the commits
  that CI will build — but the stack cannot come up against them until CI has run.
- **`biject-api` has no publish workflow.** It is the only service whose repo does not
  yet carry the standard CI, so `ghcr.io/bijectai/biject-api:<sha>` has no producer.
  Adding that workflow to `biject-api` is the single blocking item for a full-stack
  `docker compose up`; it was out of scope for the change that created this repo.
- **`AUDIT_SIGNING_KEY` is unset in every environment.** `biject-api` raises at import
  without it, so the stack will not start. Generate one and put it in `.env` before first
  boot — and note that rotating it later invalidates every prior signature and breaks
  chain continuity, so it needs a migration plan, not a re-roll.


## Knowledge graph

This platform has a shared, persistent knowledge graph via the `knowledge-graph` MCP
server. It holds what isn't recoverable from reading the code: why things are the way
they are, constraints that will bite you, and who owns what.

- At the start of a task, `search_nodes` for the components you're about to touch.
- At the end, record what changed and why: `create_entities` for new components,
  `add_observations` for decisions and gotchas, `create_relations` for dependencies
  (active voice, e.g. `ApiGateway depends_on AuthService`).
- Retract facts that have become wrong with `delete_observations`. Stale knowledge is
  worse than none.
- Record **why**, not **what** — the diff already says what changed.
- Use `search_nodes`, not `read_graph`.
