/-
  PolicyEnv.AuditBound — S4-D-13 (sprint v4)

  Decidable Tier 1–2 predicate for the enforced audit bound on EDC
  write-corrections. The proxy calls verify with a typed `AuditEntry` +
  `VerifyContext` (see `PolicyEnv.Contract`); the kernel checks
  `AuditEntryValid` and the proxy forwards to the EDC only on PROVED.

  Ticket acceptance properties, checked at compile time below:
  * PROVED on a valid entry;
  * REFUTED on a backdated entry, on a missing/out-of-range reason code,
    and on an entry future-dated beyond the skew window.

  House rules: no `sorry`, no `axiom`, no `native_decide`, no `unsafe`,
  no `extern`. All checks are Nat comparisons, String (in)equality, enum
  bounds, and one Bool equation — nothing that can time out `decide`.
-/
import PolicyEnv.Contract

namespace PolicyEnv

open Contract

/--
  The enforced audit bound. An entry is valid against the current ledger
  head and proxy clock iff ALL of:

  1. actor present — `actorId` non-empty;
  2. target present — `itemOid` non-empty;
  3. action code in enum range;
  4. reason code in enum range (free text cannot get this far: the field is
     a `Nat` by construction);
  5. not backdated — timestamp STRICTLY greater than the ledger head
     (`ledgerHeadTsMs < tsUnixMs`; an entry stamped exactly at the head is
     a replay, not a successor — no off-by-one here is deliberate);
  6. within the forward skew window — `tsUnixMs ≤ nowMs + forwardSkewMs`
     (`nowMs` is the trusted proxy clock, see `Contract`);
  7. signature verified — `sigOk = true` (trusted verdict from the signing
     pipeline, see `Contract`);
  8. hashes bound — a `modify` must actually change the value
     (`oldValueHash ≠ newValueHash`), and a `modify` must have had a prior
     value (`oldValueHash ≠ ""`).

  Every field of `AuditEntry` is consumed by some clause: `itemOid` (2),
  `actorId` (1), `action` (3, 8), `reasonCode` (4), `tsUnixMs` (5, 6),
  `oldValueHash`/`newValueHash` (8), `sigOk` (7).
-/
def AuditEntryValid (e : AuditEntry) (ctx : VerifyContext) : Prop :=
  e.actorId ≠ ""
  ∧ e.itemOid ≠ ""
  ∧ e.action < actionCount
  ∧ e.reasonCode < reasonCodeCount
  ∧ ctx.ledgerHeadTsMs < e.tsUnixMs
  ∧ e.tsUnixMs ≤ ctx.nowMs + forwardSkewMs
  ∧ e.sigOk = true
  ∧ (e.action = actionModify → e.oldValueHash ≠ e.newValueHash)
  ∧ (e.action = actionModify → e.oldValueHash ≠ "")

/--
  Decidability: the predicate is a finite conjunction of decidable atoms
  (Nat `<`/`≤`, String `≠`, Bool `=`, and implications between them), so
  the instance derives by instance search alone — no classical axioms.
-/
instance instDecidableAuditEntryValid (e : AuditEntry) (ctx : VerifyContext) :
    Decidable (AuditEntryValid e ctx) := by
  unfold AuditEntryValid
  infer_instance

/-! ## Compile-time regression vectors

`lake build` fails if any of these stops holding. All proofs are by
`decide` — plain kernel evaluation, never `native_decide`.
-/

/-- A context: ledger head stamped at t=1_755_100_000_000, proxy clock 500 ms later. -/
private def ctx0 : VerifyContext :=
  { ledgerHeadTsMs := 1755100000000, nowMs := 1755100000500 }

/-- A well-formed decimal-shift correction, 400 ms after the ledger head. -/
private def validEntry : AuditEntry :=
  { itemOid := "I_LABS_CREAT"
    actorId := "AGENT_RECONCILER_01"
    action := actionModify
    reasonCode := 3        -- DECIMAL_SHIFT
    tsUnixMs := 1755100000400
    oldValueHash := "9b2f0f4d5f3ab1c0aa10e46d3a3f6f4de2b9760e6a1f0d3c5b7a9e8d6c4b2a10"
    newValueHash := "1c56a8e6f0b9d2c4a7e3f5d8b0c2a4e6f8d0b2c4a6e8f0d2b4c6a8e0f2d4b6c8"
    sigOk := true }

/-- PROVED: the valid entry satisfies the bound. -/
example : AuditEntryValid validEntry ctx0 := by decide

/-- REFUTED: backdated — stamped exactly at the ledger head (strict `<` bites). -/
example : ¬ AuditEntryValid { validEntry with tsUnixMs := 1755100000000 } ctx0 := by
  decide

/-- REFUTED: backdated — stamped before the ledger head. -/
example : ¬ AuditEntryValid { validEntry with tsUnixMs := 1755099999999 } ctx0 := by
  decide

/-- REFUTED: missing reason — reason code outside the enum range. -/
example : ¬ AuditEntryValid { validEntry with reasonCode := reasonCodeCount } ctx0 := by
  decide

/-- REFUTED: future-dated beyond the skew window (1 ms past `nowMs + forwardSkewMs`). -/
example : ¬ AuditEntryValid { validEntry with tsUnixMs := 1755100300501 } ctx0 := by
  decide

/-- PROVED: future-dated exactly at the skew bound is still admissible (`≤`). -/
example : AuditEntryValid { validEntry with tsUnixMs := 1755100300500 } ctx0 := by
  decide

/-- REFUTED: actor missing. -/
example : ¬ AuditEntryValid { validEntry with actorId := "" } ctx0 := by decide

/-- REFUTED: signature verification failed upstream. -/
example : ¬ AuditEntryValid { validEntry with sigOk := false } ctx0 := by decide

/-- REFUTED: a `modify` whose hashes are equal is not a modification. -/
example :
    ¬ AuditEntryValid
        { validEntry with newValueHash := validEntry.oldValueHash } ctx0 := by
  decide

/-- REFUTED: a `modify` with no prior value bound (`oldValueHash = ""`). -/
example : ¬ AuditEntryValid { validEntry with oldValueHash := "" } ctx0 := by decide

/-- REFUTED: action code outside the enum range. -/
example : ¬ AuditEntryValid { validEntry with action := actionCount } ctx0 := by decide

/-- PROVED: an `annotate` does not require the hashes to differ. -/
example :
    AuditEntryValid
      { validEntry with
          action := actionAnnotate
          newValueHash := validEntry.oldValueHash } ctx0 := by
  decide

end PolicyEnv
