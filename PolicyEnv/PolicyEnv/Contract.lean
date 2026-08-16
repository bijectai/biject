/-
  PolicyEnv.Contract — S4-D-13 (sprint v4)

  Typed kernel-side mirror of the frozen tool-call contract
  (`contracts/tool_calls.json`, ticket S4-A-12) for the `writeItemCorrection`
  operation. Every field the kernel consumes is declared here; nothing else
  ever reaches the kernel.

  ## No natural language, ever

  biject never receives natural language at verification time. The proxy
  extracts a typed structured entry and sends only that. In particular:

  * The corrected value itself (`newValue` on the wire) is forwarded to the
    EDC by the proxy but does NOT enter the kernel — only its SHA-256 hex
    digest (`newValueHash`) does. Same for the prior value (`oldValueHash`).
    Hashes are opaque `String`s here (no `ByteVec 32` in the kernel; kernel
    checks stay `Nat` comparisons, `String` (in)equality, and enum bounds).
  * The reason for change is an integer enum index (`reasonCode`), never free
    text.

  ## Trust boundary (read this paragraph before changing anything)

  The predicate trusts exactly two inputs it cannot check itself, and both
  are named here, in the same paragraph, deliberately:

  * `sigOk : Bool` — the verdict of Ed25519 signature verification over the
    canonical audit-entry bytes. Verification happens in the signing pipeline
    outside the kernel (the proxy / audit service); the kernel consumes only
    the boolean. A bug there is a bypass here.
  * `nowMs : Nat` (in `VerifyContext`) — "now" is proxy-supplied. The proxy's
    clock is therefore a trusted input: a proxy with a wrong clock can admit
    a future-dated entry (up to `forwardSkewMs`) or reject a valid one. The
    ledger-head timestamp, by contrast, is read from the signed audit chain
    and is not additionally trusted.

  Everything else in `AuditEntryValid` is checked, not trusted.

  ## Known limitation (recorded now, per sprint plan)

  Strict timestamp monotonicity against the ledger head assumes a single
  writer. Under concurrent writers two in-flight entries can both validate
  against the same head and race at append time. Fine for the single-agent
  demo; a multi-writer deployment needs a sequencer in front of the chain.
-/

namespace PolicyEnv
namespace Contract

/-! ### Action codes (integer enum, mirrors `action` in tool_calls.json) -/

/-- `action = 0`: create a value where none existed. -/
def actionCreate : Nat := 0
/-- `action = 1`: modify an existing value. -/
def actionModify : Nat := 1
/-- `action = 2`: annotate without changing the value. -/
def actionAnnotate : Nat := 2
/-- Number of defined action codes; a valid `action` is `< actionCount`. -/
def actionCount : Nat := 3

/-! ### Reason codes (integer enum, mirrors `reasonCode` in tool_calls.json)

Free-text reasons are banned from the kernel by design; the wire contract
carries only the index. The meanings are fixed in the contract file:

  0 = SOURCE_DATA_CONFIRMED
  1 = TRANSCRIPTION_ERROR
  2 = UNIT_CORRECTION
  3 = DECIMAL_SHIFT
  4 = CROSS_FIELD_RECONCILIATION
  5 = MISSING_VALUE_COMPLETION
  6 = INVESTIGATOR_CONFIRMED
  7 = OTHER_DOCUMENTED
-/

/-- Number of defined reason codes; a valid `reasonCode` is `< reasonCodeCount`. -/
def reasonCodeCount : Nat := 8

/-! ### Clock skew -/

/--
  Forward clock-skew window in milliseconds: an entry may be timestamped at
  most this far ahead of the proxy's `nowMs`. 5 minutes comfortably covers
  NTP drift between the agent harness and the proxy on the demo host(s)
  while still rejecting meaningfully future-dated entries. (S4-D-30 hardens
  and re-justifies this constant against 21 CFR Part 11 §11.10(e).)
-/
def forwardSkewMs : Nat := 300000

/-! ### Kernel-side structures -/

/--
  The audit entry as the kernel sees it — the typed extraction of one
  `writeItemCorrection` call. Field-for-field provenance from the wire
  contract (`contracts/tool_calls.json`):

  * `itemOid`      ← `itemOid` (opaque EDC item identifier)
  * `actorId`      ← `actorId` (opaque agent/user identifier)
  * `action`       ← `action` (integer enum, see action codes above)
  * `reasonCode`   ← `reasonCode` (integer enum, see reason codes above)
  * `tsUnixMs`     ← `tsUnixMs` (entry timestamp, Unix epoch ms)
  * `oldValueHash` ← SHA-256 hex of the value being replaced (proxy reads it
                     from the EDC before forwarding; empty string when
                     `action = actionCreate`, i.e. nothing existed)
  * `newValueHash` ← SHA-256 hex of `newValue` (the value itself never
                     enters the kernel)
  * `sigOk`        ← result of Ed25519 verification of `sigEd25519` over the
                     canonical entry bytes — TRUSTED input, see the trust
                     boundary paragraph above.

  `oldValueHash`/`newValueHash` are bound by the predicate (a `modify` where
  they are equal is not a modification) — they are not dangling fields.
-/
structure AuditEntry where
  itemOid : String
  actorId : String
  action : Nat
  reasonCode : Nat
  tsUnixMs : Nat
  oldValueHash : String
  newValueHash : String
  sigOk : Bool
deriving Repr, DecidableEq

/--
  Per-verification context supplied by the proxy alongside the entry.

  * `ledgerHeadTsMs` — timestamp of the current head of the signed audit
    chain (read from the chain itself).
  * `nowMs` — the proxy's clock at verification time — TRUSTED input, see
    the trust boundary paragraph above.
-/
structure VerifyContext where
  ledgerHeadTsMs : Nat
  nowMs : Nat
deriving Repr, DecidableEq

end Contract
end PolicyEnv
