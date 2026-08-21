# Lean request 1 — kernel-env work needed for the OpenClinica demo (Dev only)

**From:** Fable, Track 1 execution, 2026-08-21. Per §2.1 no `.lean` file was created or
modified by an agent; this file is the hand-back. Everything below that is *not* Lean
(registry entries, Python conjecture builder, Ed25519 verification in biject-api, the
proxy changes) is being prepared agent-side and will slot in once the Lean side exists.

---

## Ask 1 — trivially-true read predicate (unblocks gated reads)

**Decision context:** reads stay gated (user decision, 2026-08-21). Instead of ungating
the proxy, `openclinica_list_queries` and `openclinica_get_item_context` get real
policies that always prove. Investigation (evidence in the Track 1 study, quoting
`biject-api-1/backend/app/orchestrator.py:175-207`) established this **cannot be done
with registry data alone**: the conjecture is always synthesized as
`example : PolicyEnv.{lean_function} {args} = true := by decide`, so an always-true
verdict needs an actual compiled Bool constant/function in the kernel env.

**Requested change** (in `biject-api-1/lean-worker/PolicyEnv/`):

1. New module, e.g. `PolicyEnv/ReadAccess.lean`:
   - `PolicyEnv.readAllowed : Bool := true` (nullary), with whatever house-rule
     framing you prefer. Zero axioms, `decide`-provable, trivially.
2. Import it from the root `PolicyEnv.lean` (the orchestrator's
   `_fetch_available_modules` reads that file's import list and skips policies whose
   module is not listed — `orchestrator.py:162-172, 573-576`).
3. Rebuild the `.olean`s / run whatever `policy-sign.yml` signing step applies.

**What Fable then does (no Lean):** register two registry entries —
`{"lean_module": "PolicyEnv.ReadAccess", "lean_function": "readAllowed",
"applies_to_tools": ["openclinica_list_queries"], "parameter_map": {},
"param_transforms": {}, "tier": 1}` (and the same for
`openclinica_get_item_context`) — producing the conjecture
`example : PolicyEnv.readAllowed  = true := by decide`.

**One thing to confirm on your side:** an empty `parameter_map` yields zero args
(`' '.join([])`); please confirm nothing in the register/validate path rejects an
empty map and that the double-space conjecture elaborates (it should — whitespace is
insignificant there). If a nullary function offends the template, a one-arg
`readAllowed (_ : Nat) : Bool := true` with `parameter_map: {"dummy": "study_oid"}`
does NOT work (study_oid is not numeric) — prefer nullary.

**Why not skip the kernel for reads:** `skipped` is a denial at the proxy by design;
this route keeps "everything goes through the kernel" literally true, with a ledger
entry per read.

---

## Ask 2 — AuditBound promotion + write-path wiring (Workstream F)

The predicate exists and is green in the standalone project
(`biject/PolicyEnv/PolicyEnv/AuditBound.lean`, S4-D-13/S4-D-30; `lake build` verified
2026-08-21, axiom-free, harness PASS). What is missing is the platform side:

1. **Promotion:** `Contract.lean` + `AuditBound.lean` into
   `lean-worker/PolicyEnv/` (both projects already pin `leanprover/lean4:v4.28.0`;
   the standalone project was built drop-in compatible per S4-D-13's deviation note).
   Layout is your call per the repo-placement deviation.
2. **Conjecture shape:** the existing numeric template cannot express
   `AuditEntryValid` (structured `AuditEntry`/`VerifyContext` with String fields).
   The compile-time vectors in `AuditBound.lean:154-243` already show the target
   form. Proposal — Fable builds a dedicated Python conjecture builder in
   biject-api's orchestrator for `tool_name == "openclinica_write_item"` that renders
   exactly that literal-structure form from the proxy's 12 typed params, once you
   confirm/pin the constructor names and field order. Only hashes, enum ints,
   timestamps, and validated identifier strings enter the conjecture — no free text
   (the proxy's extraction already guarantees this).
3. **`sigOk` supplier:** the kernel takes `sigOk` on trust
   (`Contract.lean` trust boundary: "A bug there is a bypass here"), and today NO
   runtime component verifies `sig_ed25519` — only the offline harness. Fable will
   implement the Ed25519 verification in biject-api (Python: recompute the pipe
   preimage `actorId|action|itemOid|oldValueHash|newValueHash|reasonCode|tsUnixMs`
   from the ledger-destined params, sha256, verify the sig over the digest's ASCII
   hex against a configured agent public key, feed the boolean into the conjecture
   context). Needs from you: blessing of the env-var name for the agent public key
   (proposed `AGENT_VERIFY_PUBKEY`, hex/base64 32 bytes, same decode conventions as
   `AUDIT_VERIFY_PUBKEY`).
4. **Two contract reconciliations queued for S4-A-12** (flagged, not solved here):
   (a) `action` in the signed preimage — fixture/harness/Lean/`tool_calls.json` use
   the integer enum, the deployed proxy binary used the string `"modify"`; Track 1
   aligns the proxy to the integer per S4-D-30's recorded reconciliation direction.
   (b) `validate_tool_call_params` caps param strings at 256 chars while a maximal
   6-segment `item_oid` path is 389 — latent rejection of long-but-valid OIDs.

**Test cases the proxy exercises against the wired predicate** (E.5): valid write →
allowed; backdated `ts_unix_ms` ≤ ledger head → blocked, `notBackdated` in
`lean_trace`; missing `reasonCode` → refused at the proxy boundary (never reaches the
kernel); out-of-range `reasonCode` (e.g. 9999) → blocked, `reasonCodeValid`; bad
signature → blocked, `sigOk`; stub ledger context (`ledger_head_ts = 0` against a
non-empty chain) → blocked (genesis-consistency clauses).
