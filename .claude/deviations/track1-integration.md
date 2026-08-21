# Track 1 — integration decisions across the build fleet (2026-08-21)

Eight parallel build tasks landed the signing pipeline, adapter rewrite, seed
study, wall, AWS port, publish CI (in biject-api), prompt library, and MCP
shim. Four decisions were taken at integration, recorded here per §2.3.

## 1. `ts_unix_ms` exposed as an optional write-tool parameter

The build left the adversarial pass unexpressible: `write_item_correction`
took only (item_oid, new_value, reason_code) with `tsUnixMs` stamped at
signing time, so the kernel's `notBackdated` clause — pass 2's headline —
could never fire from the agent. The spec is explicit that this is the
design ("§4.2 step 5: The agent supplies the timestamp it *wants*. The proxy
supplies the reference clock and the ledger head"), and H.2's expected
behaviour requires the backdated write to reach the kernel and be REFUTED.
Resolution: `build_audit_entry`, the Agents SDK write tool, and the MCP shim
all gained optional `ts_unix_ms` (omitted → stamped now; supplied → signed
honestly and judged by the kernel). This does not weaken anything: the value
was already caller-supplied on the proxy wire; what changed is that the
LLM-facing surface can now express what the wire always carried. The
"skip the reason" half of H.2 stays inexpressible (reason_code remains a
required int) — prompts.md documents how to narrate that honestly, and the
kernel's `reasonCodeValid` clause is demonstrated by the harness's
out-of-range-reason path. **Flagged for veto in the Track 1 report.**

## 2. H.2 "potassium" → "creatinine" substitution

The seeded panel's out-of-range lab is serum creatinine (`I_LABS_CREAT`);
no potassium item exists. The spec's exact-text rule is kept in the library,
with a documented substitution of the item name when running against the
seeded study — a prompt naming a nonexistent item hands the model a stall.

## 3. Query param names aligned to the proxy's camelCase

The build agents used `study_oid`/`item_oid` query keys (from the
orchestrator's own contract sketch); the proxy's canonical names are
`studyOid`/`itemOid` (matching the §8 camelCase body fields and the
Bedrock/Foundry specs). Both clients and their byte-exact tests were aligned
to the proxy. Orchestrator error, agent implementations were faithful.

## 4. AWS compose wall context → the real wall

Two tasks raced: the AWS port shipped a placeholder `infra/aws/wall/` while
the real D8 wall landed at `wall/`. The compose build context now points at
`wall/` (../../../../wall) and the placeholder is deleted;
`docker compose config -q` re-validated.

## Also fixed at integration

- `edc/oc3_client.py` SOAP import endpoint `/ws/dataImport/v1` →
  `/ws/data/v1` (3.17.2-source canonical; the old name only worked because
  Spring-WS routes any `/ws/*` POST by payload root).
- `adapters/README.md` public claim corrected: "validated end to end" was
  unverifiable (STATE.md contradiction 12-adjacent); it now says
  "tested against a mocked proxy, not yet run against a deployed one" and
  carries an `adapters/mcp/` row.
- biject CLAUDE.md file-to-role map and network-invariant text updated for
  agent/, wall/, demo/, adapters/mcp/, infra/aws/, and the corrected OC
  endpoint canon.

## Independent re-verification after integration edits

agent tests 12/12; adapter tests 39/39; MCP tests 29/29;
`audit_bound_harness.py` PASS; the three edc XMLs parse and `rules.xml`
validates against the vendor rules.xsd; `run_demo.py --verify-toolset` exit 0
(exactly three tools); enforcement compose `config -q` OK; EC-06 sweep over
all new trees clean.

## Known pre-push blocker escalated to Dev (biject-api repo)

`backend/Dockerfile` consumes `LEAN_SIGNING_KEY` as a plain build ARG; BuildKit
records build-arg values in image config history, so a pushed image can leak
the seed via `docker history`. The new ci.yml documents this; the fix
(`RUN --mount=type=secret`) is a Dockerfile change that could not be
build-tested here (no Docker daemon) and MUST land before the first real GHCR
push of biject-api.
