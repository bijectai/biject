/-
  PolicyEnv.Contract — S4-D-13, hardened by S4-D-30 (sprint v4)

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
    checks stay `Nat` comparisons, `String` (in)equality, `String` byte
    length, and enum bounds).
  * The reason for change is an integer enum index (`reasonCode`), never free
    text. S4-D-30: the field is `Option Nat` so that ABSENT-on-the-wire and
    OUT-OF-RANGE are two distinct refuted cases, both enforced in the kernel
    rather than only at the proxy's extraction boundary.

  ## Ledger-head binding (S4-D-30)

  `VerifyContext` no longer carries a bare, caller-suppliable timestamp. The
  proxy must present the head of the REAL Merkle-chain audit ledger as a
  pair:

  * `ledgerHeadHash` — the `entry_hash` of the chain head, read from the
    verified signed chain (biject-api `backend/app/audit.py`; genesis
    sentinel = 64 hex zeros, `genesisHeadHash`), cross-checked against the
    `audit_head.json` head pointer;
  * `ledgerHeadTsMs` — the head entry's timestamp, derived from the chain
    contents as `max(params.ts_unix_ms, timestamp_ns / 10^6)` of the head
    entry (the stricter of the head's claimed time and its recorded append
    time), `0` only for the empty chain.

  The predicate enforces well-formedness (`ledgerHeadHash` is digest-shaped)
  and genesis consistency (`ledgerHeadTsMs = 0` iff the head is the genesis
  sentinel), so a stub that fabricates a timestamp without reading the chain
  cannot present a coherent context. The derivation itself is exercised by
  `scripts/audit_bound_harness.py` against a golden ledger fixture generated
  by the real biject-api chain code (`scripts/fixtures/audit_ledger/`).

  ## Trust boundary (read this paragraph before changing anything)

  The predicate trusts exactly two inputs it cannot check itself, and both
  are named here, in the same paragraph, deliberately:

  * `sigOk : Bool` — the verdict of Ed25519 verification of the agent's
    `sigEd25519` over the signed-digest preimage RECOMPUTED FROM THE
    CANONICAL BYTES THE LEDGER STORES (S4-D-30): the pipe-joined
    canonicalization `actorId|action|itemOid|oldValueHash|newValueHash|
    reasonCode|tsUnixMs` is rebuilt from the typed params exactly as the
    ledger persists them in the entry's `params` (which feed the entry's
    canonical `entry_hash`), hashed, and the signature is verified over that
    digest's ASCII hex — so the bytes the signature attests are byte-
    identical to bytes the signed chain records, and nothing can differ
    between what was proven and what was written. Verification happens in
    the signing pipeline outside the kernel (the proxy / harness — see
    `scripts/audit_bound_harness.py`); the kernel consumes only the boolean.
    A bug there is a bypass here.
  * `nowMs : Nat` (in `VerifyContext`) — "now" is proxy-supplied. The
    proxy's clock is therefore a trusted input: a proxy with a wrong clock
    can admit a future-dated entry (up to `forwardSkewMs`) or reject a valid
    one. The ledger-head pair (`ledgerHeadHash`, `ledgerHeadTsMs`), by
    contrast, is read from the signed audit chain after full chain
    verification (Merkle recompute + Ed25519 over `entry_hash`) and is not
    additionally trusted.

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

/-- Number of defined reason codes; a valid `reasonCode` is `some r` with
`r < reasonCodeCount`. -/
def reasonCodeCount : Nat := 8

/-! ### Clock skew -/

/--
  Forward clock-skew window in milliseconds: an entry may be timestamped at
  most this far ahead of the proxy's `nowMs`.

  S4-D-30 tightened this from 300000 (5 min) to 5000, reconciling it with
  the enforcement proxy's deployed default (`AUDIT_SKEW_MS = 5000` in
  biject-proxy) so the kernel bound and the proxy bound are the same number
  — the kernel is the authority, and it must not be 60x looser than the
  component it authorizes.

  21 CFR Part 11 §11.10(e) justification: §11.10(e) requires computer-
  generated, time-stamped audit trails whose record sequencing is reliable.
  This constant is the entire tolerance for how far a record's CLAIMED time
  may lead the TRUSTED clock (`nowMs`); together with the strict
  monotonicity bound against the verified ledger head it guarantees the
  recorded order of the signed chain and the claimed times of its entries
  can never disagree by more than 5 s. NTP-disciplined hosts on one network
  hold well under 1 s of drift, so 5 s admits every honestly-clocked writer
  while a 5-minute window would let a record claim a time far enough ahead
  to misorder it against other §11.10(e) trails (EDC audit rows, system
  logs) recorded in between. Fail-closed on the boundary: exactly
  `nowMs + forwardSkewMs` is admitted, one millisecond past it is refuted.
-/
def forwardSkewMs : Nat := 5000

/-! ### Ledger-head sentinels (mirror biject-api `backend/app/audit.py`) -/

/--
  Genesis sentinel of the signed audit chain: the `prev_hash` of entry 0 and
  the head of an EMPTY chain. 64 hex zeros — the width of a sha256 digest,
  distinguishable from any real `entry_hash`. Byte-identical to `GENESIS` in
  biject-api `backend/app/audit.py`.
-/
def genesisHeadHash : String :=
  "0000000000000000000000000000000000000000000000000000000000000000"

/--
  Byte length of a sha256 hex digest — the only shape `ledgerHeadHash` may
  have. Checked as `utf8ByteSize` (hex digests are pure ASCII, so byte
  length equals character length; `utf8ByteSize` is axiom-free on this
  toolchain where `String.length` is not).
-/
def digestHexLength : Nat := 64

/-! ### Kernel-side structures -/

/--
  The audit entry as the kernel sees it — the typed extraction of one
  `writeItemCorrection` call. Field-for-field provenance from the wire
  contract (`contracts/tool_calls.json`):

  * `itemOid`      ← `itemOid` (opaque EDC item identifier)
  * `actorId`      ← `actorId` (opaque agent/user identifier)
  * `action`       ← `action` (integer enum, see action codes above)
  * `reasonCode`   ← `reasonCode` (integer enum, see reason codes above;
                     `none` = the field was absent on the wire — refuted as
                     missing-reason, distinct from out-of-range)
  * `tsUnixMs`     ← `tsUnixMs` (entry timestamp, Unix epoch ms)
  * `oldValueHash` ← SHA-256 hex of the value being replaced (proxy reads it
                     from the EDC before forwarding; empty string when
                     `action = actionCreate`, i.e. nothing existed)
  * `newValueHash` ← SHA-256 hex of `newValue` (the value itself never
                     enters the kernel)
  * `sigOk`        ← result of Ed25519 verification of `sigEd25519` over the
                     signed digest recomputed from the ledger-stored
                     canonical params — TRUSTED input, see the trust
                     boundary paragraph above.

  `oldValueHash`/`newValueHash` are bound by the predicate (a `modify` where
  they are equal is not a modification) — they are not dangling fields.
-/
structure AuditEntry where
  itemOid : String
  actorId : String
  action : Nat
  reasonCode : Option Nat
  tsUnixMs : Nat
  oldValueHash : String
  newValueHash : String
  sigOk : Bool
deriving Repr, DecidableEq

/--
  Per-verification context supplied by the proxy alongside the entry.

  * `ledgerHeadHash` — `entry_hash` of the verified chain head (or
    `genesisHeadHash` for the empty chain), read from the signed audit
    chain and cross-checked against the `audit_head.json` pointer.
  * `ledgerHeadTsMs` — the head entry's timestamp, derived from the chain
    contents (see the ledger-head binding section above); `0` only at
    genesis.
  * `nowMs` — the proxy's clock at verification time — TRUSTED input, see
    the trust boundary paragraph above.
-/
structure VerifyContext where
  ledgerHeadHash : String
  ledgerHeadTsMs : Nat
  nowMs : Nat
deriving Repr, DecidableEq

end Contract
end PolicyEnv
