# Sprint v4 — Day 1–2 Dev runbook (host-side steps)

Everything in this repo that could be authored as code/config is done. This
runbook is the ordered list of steps that **must run on the real Hetzner host**
(or against the live OC instance) — they cannot be automated from a sandbox.
Check items off in this file as you go; log any fallback decisions in
`.claude/deviations/`.

## Day 1

### 1. S4-D-00 — prereq + capacity (first 30 min)
- [ ] Copy repo to the host, `OPENAI_API_KEY=… ./scripts/preflight.sh`
- [ ] PASS on: ≥6 GB free memory (else **provision the second Hetzner box
      now**, don't debug OOM tonight), disk, docker/compose versions,
      egress to `api.openai.com`, Responses API auth
- [ ] Commit the preflight log path/date here: `……`

### 2. S4-D-10 — compose skeleton
- [ ] Namecheap: A record for the demo subdomain → host IP; set `DEMO_DOMAIN`
      in `infra/hetzner/.env` (copy from `.env.example`)
- [ ] Coolify: **Raw Compose Deployment** mode (Application mode strips
      `ipam`) pointing at `infra/hetzner/docker-compose.yml`
- [ ] `docker compose up -d` → Traefik up, TLS issued, health endpoints reachable

### 3. S4-D-11 — OpenClinica 3.17 CE (start image pull in tmux FIRST, then
   work on other things while it runs)
- [ ] Bring up the OC stack per `infra/hetzner/openclinica/README.md`
- [ ] OC login page over TLS; create admin account; create the study shell
- [ ] **Confirm `OpenClinica-ws` responds** (WSDL URL in the README) — the
      spike has nothing to hit without it
- [ ] Upload `edc/study_def.xml` via OC UI → Build Study (metadata import
      via ws is unreliable in 3.17; data import is scripted, metadata is not)

### 4. S4-D-12 — HARD GATE spike (must pass by EOD Day 1)
- [ ] `python edc/oc3_client.py write I_LABS_CREAT 1.52 --subject SS_001 …`
      → value visible in the OC UI
- [ ] `python edc/oc3_client.py list-queries S_BJTDEMO` → at least one open
      query with item context
- [ ] **If either fails at EOD:** decide the fallback tonight (OC4 cloud
      trial = real REST + token but loses self-host, or thin authenticated
      Postgres write service), log it in `.claude/deviations/S4-D-12.md`.
      Do not let this cross into Day 2.

### 5. S4-D-13 — already verified in-repo
- [x] `cd PolicyEnv && lake build` — compiles, `Decidable`, compile-time
      regression vectors (18 as of the S4-D-30 hardening), zero axioms (see
      `.claude/deviations/S4-D-13.md` and `.claude/deviations/S4-D-30.md`;
      S4-D-30 also added `scripts/audit_bound_harness.py`, which must pass)
- [ ] Optional on host: re-run `lake build` to confirm toolchain pin resolves

## Day 2

### 6. S4-D-20 — seed study (timebox: 90 min)
- [ ] `python edc/seed.py` → per-subject import results
- [ ] `python edc/seed.py --verify` → open discrepancy-note count ≥ N, ≥3
      query types
- [ ] If OC edit-check rules don't fire on import by minute 90: create the
      queries manually in Notes & Discrepancies, log the deviation, move on
- [ ] Spot-check: each open query is resolvable from other seeded fields

### 7. S4-D-21 — tool surface + egress lockdown
- [ ] Set `BIJECT_PROXY_URL` for the agent env; smoke one tool call landing
      on the proxy
- [ ] Apply `infra/hetzner/firewall/docker-user-rules.sh` (parameterize the
      subnets first)
- [ ] `infra/hetzner/firewall/verify_lockdown.sh` exits 0 — direct
      agent→OC call **refused** (tested, not assumed), proxy-routed health
      call succeeds
- [ ] Confirm host outbound to `api.openai.com` still works post-lockdown

## Cross-cutting
- [ ] `OPENAI_API_KEY` and all secrets in env files only — never committed
- [ ] SDK tracing off in the agent env (`OPENAI_AGENTS_DISABLE_TRACING=1`)
- [ ] Record decisions/gotchas in the knowledge graph (Supabase `brain`)
