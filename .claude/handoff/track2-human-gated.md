# Track 2 — human-gated items (flagged 2026-08-21, none attempted agent-side)

Per the Track 1/Track 2 split: these need Dev (or Adeel where noted). Fable is not
attempting or working around any of them. Ordered by how early they unblock things.

0. **⚠ FIRST, gates the very first image publish: verify the biject-api
   Dockerfile secret-mount build.** The `LEAN_SIGNING_KEY` build-ARG leak
   (build-arg values are recorded in image config history; `docker history`
   would expose the seed) is FIXED on biject-api `fable/track1` @ 9f0f017 —
   but the build itself is **UNVERIFIED**: no Docker daemon existed on the
   authoring machine. Before ANY biject-api image is pushed to GHCR, run:

   ```bash
   LEAN_SIGNING_KEY=$(python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())") \
   docker build --secret id=lean_signing_key,env=LEAN_SIGNING_KEY -f backend/Dockerfile .
   ```

   and confirm (a) the build succeeds, (b) `docker history <image>` shows no
   trace of the key. Details:
   `biject-api/.claude/deviations/track1-dockerfile-secret-mount.md`.

1. **Credentials (demo-wide blockers, no value exists in any environment):**
   - `AUDIT_SIGNING_KEY` — base64 32-byte Ed25519 seed; biject-api refuses to start
     without it. Generate: `python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"`.
     Rotation breaks chain continuity — treat the first value as long-lived.
   - `AUDIT_VERIFY_PUBKEY` — public half, derived IN biject-api's environment (recipe
     in biject-trace CLAUDE.md); goes to biject-trace.
   - `AGENT_SIGNING_KEY` (new, for the S4-A-30 signing pipeline) — base64 32-byte
     Ed25519 seed on the agent host only; its public half (`AGENT_VERIFY_PUBKEY`)
     goes to biject-api for sigOk verification. Never on the enforcement host.
   - OpenClinica service account (with "Authorize SOAP web services in this account"
     checked — the ws interceptor rejects otherwise) + password; note the ws WAR
     wants the SHA-1 hex as `OPENCLINICA_WS_PASSWORD`.
   - Proxy API key (once the Track 1 auth middleware lands): `BIJECT_PROXY_API_KEY`
     value for the proxy env + the same value on the agent host.
   - `TRACE_INGEST_TOKEN` (optional but recommended): shared between proxy and
     biject-trace for wall-feed ingest.
   - LLM provider API key for the demo agent (hosted provider, pinned model).
2. **OpenClinica WAR downloads** (~100MB, manual): `OpenClinica.war` +
   `OpenClinica-ws.war` (3.17 CE) into `infra/hetzner/openclinica/dist/` — the image
   build fails fatally without both. Same artifacts will be reused by the AWS port.
3. **AWS provisioning** once `infra/aws/` Terraform is ready (being prepared under
   Track 1): two EC2 per spec A.1 (m6i.2xlarge enforcement, t3.large agent), SGs as
   coded, Elastic IP, Namecheap A records for `proxy`/`wall`/`oc`. Fable prepares +
   writes the apply plan; a human runs `terraform apply` and the DNS changes.
4. **S4-D-12 spike — run FIRST, in the first hour after an instance + credentials
   exist, before anything else in Workstream D.** Use `docs/DAY1-2-RUNBOOK.md` §4.
   In the same first hour, confirm live the two path fixes made from the 3.17.2
   source study (ODM read `/OpenClinica/rest/clinicaldata/xml/view/...`; SOAP import
   `/OpenClinica-ws/ws/data/v1`, WSDL at `.../dataWsdl.wsdl`) and the WS-Security
   SHA1-as-PasswordText behavior. Log the outcome to
   `.claude/deviations/S4-D-12.md` either way — a failed attempt must leave a note.
5. **Lean/kernel work:** see `.claude/handoff/lean-request-1.md` (read predicate;
   AuditBound promotion; sigOk wiring interface; two S4-A-12 reconciliations).
6. **S4-A-12 contract freeze + `v0.1.0` tag on biject-contracts** (Adeel as arbiter).
   Queued reconciliation items are listed in lean-request-1.md §Ask 2.4 and the
   pre-existing deviation notes.
7. **Pushes/PRs for all Track 1 branches** (Fable stops before push, per §2.2): the
   branch + SHA inventory is reported in the running Track 1 status report.
