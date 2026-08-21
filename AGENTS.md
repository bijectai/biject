# AGENTS.md — biject (meta repo)

Guidance for coding agents and automated reviewers (Codex, Claude) working in this
repository.

**`CLAUDE.md` at the repo root is the source of truth** for context, ownership, the
three §2B review rules, the rules for `docker-compose.yml`, and the deploy procedure.
Read it before making changes. This file carries the reviewer-facing procedure only; it
deliberately does not restate `CLAUDE.md`, so the two cannot drift.

## What review is, and is not

Automated review here is **advisory**. The reviewer reads the diff and posts findings;
it cannot edit, commit, or merge, and its findings are not a required status check.

Keep deterministic checks out of review. Pin immutability and compose validity are
mechanical and belong in CI (`scripts/verify-pins.sh`, `docker compose config -q`).
Reserve review for the judgement a human would otherwise have to repeat.

## What a diff here means

**Every change to `docker-compose.yml` is a deploy.** There is no application code in
this repo; a diff is a statement about what will be running in production. Review it as
you would review a deploy, not as you would review a config tweak.

## Code Review Rules

Review every diff against **§2B.1 (verification inputs)**, **§2B.2 (enforcement
ordering)**, and **§2B.3 (secret material)** in `CLAUDE.md`, including the "in this
repo" notes under each. Those three are the rules; what follows is what they look like
when they are about to be broken *here*.

### Pins

- A tag that is not a full 40-character git SHA on a `ghcr.io/bijectai/*` image, or an
  image without an `@sha256:` digest on a third-party one. CI catches this; flag it
  anyway with the reason, because the fix is usually "you pasted a short SHA".
- An `image:` tag built from a variable. "Deployed" must be answerable by reading the
  file, not by knowing what was in someone's environment.
- A `build:` key. It lets something other than the pinned image run under the pinned
  name.
- An image pin moved without its `BIJECT_IMAGE_SHA` moving with it, or vice versa. The
  container would then misreport what it is, which is worse than reporting nothing —
  `scripts/smoke.sh` compares the two, and a mismatch defeats that check.
- Several unrelated services re-pinned in one commit with no explanation. Each pin is a
  separate deploy decision; ask which of them was actually intended.

### Topology

- **`depends_on: biject-api` added to `biject-proxy`.** The proxy is fail-closed and must
  come up whether or not the verifier is available; an ordering dependency stops it
  starting exactly when you most want it up and denying. Same for `biject-console`, which
  must be able to come up to *report* an outage.
- **`biject-proxy`'s healthcheck pointed at `/readyz`.** A proxy denying every call is
  working, not unhealthy. Pointing the healthcheck at readiness gets it restarted for
  doing its job.
- **`:ro` removed from `biject-trace`'s volume mount.** `biject-api`'s invariant is that
  every ledger append goes through its single lock-guarded path; the read-only mount is
  what enforces that at the kernel rather than on trust. High severity.
- A new service added without a healthcheck, or with a healthcheck that probes a
  dependency rather than itself.
- Host ports changed without a matching change in `scripts/smoke.sh` and the READMEs.

### Secrets

- **Any key, seed, token, or password written into `docker-compose.yml`.** It belongs in
  `.env`, which is gitignored. `.env.example` carries an empty placeholder and the
  command to generate a real value. Highest-severity finding in this repo.
- `AUDIT_SIGNING_KEY` passed to any service other than `biject-api`. `biject-trace` gets
  `AUDIT_VERIFY_PUBKEY` — the public half — and nothing more.
- A default value supplied for a secret via `${VAR:-something}`. A secret with a fallback
  is a secret that will be deployed wrong quietly; `AUDIT_SIGNING_KEY` uses `${VAR:?...}`
  so the stack refuses to start instead.

## Conventions

- Comments in `docker-compose.yml` explain *why* a service is configured the way it is,
  particularly where the configuration looks like an omission — the missing `depends_on`
  and the `/healthz` healthcheck both need their reasons stated, or someone will "fix"
  them.
- Architectural deviations from a ticket's premise get a note in `.claude/deviations/`.
  See that directory's `README.md` for what a note records.
