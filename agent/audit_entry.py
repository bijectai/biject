"""S4-A-30 — the harness signing pipeline for audit-entry provenance.

This module is the real implementation behind the
``adapters/openai/tools.py::build_audit_entry`` stub. It — not the LLM, and
not the adapter — supplies the three provenance fields of a write-correction
tool call:

* ``actorId``   — resolved from the harness's own identity config
  (``ACTOR_ID`` env or an explicit argument), never chosen by the model;
* ``tsUnixMs``  — stamped from the host clock at signing time;
* ``sigEd25519`` — Ed25519 signature over the canonical signed-digest
  preimage, produced with the harness's private key (``AGENT_SIGNING_KEY``
  env; the key never enters the adapter and never appears in any output).

Canonicalization (the contract)
===============================
The signature covers the pipe-joined preimage, seven fields in fixed order::

    actor_id|action|item_oid|old_value_hash|new_value_hash|reason_code|ts_unix_ms

mirroring biject-proxy ``src/extract.rs::signed_digest`` and
``scripts/gen_audit_fixture.py`` / ``scripts/audit_bound_harness.py``. Field
rules:

* ``action`` serializes as this repo's INTEGER enum (0=create, 1=modify,
  2=annotate — ``contracts/tool_calls.json``); the proxy is being aligned to
  the integer form in parallel (see ``.claude/deviations/S4-D-30.md``).
* ``item_oid`` at runtime is the canonical 6-segment path
  ``StudyOID/SubjectKey/StudyEventOID/FormOID/ItemGroupOID/ItemOID``, each
  segment ``[A-Za-z0-9_-]{1,64}`` — byte-identical to what the proxy
  re-derives and what reaches the kernel as ``item_oid``. (The golden
  fixture's vectors carry bare item OIDs; the preimage functions here are
  shape-agnostic and accept those as data.)
* ``old_value_hash`` / ``new_value_hash`` — lowercase sha256 hex over the
  UTF-8 bytes of the value strings. For ``action = 0`` (create) the old
  hash is the empty string, per ``PolicyEnv/PolicyEnv/Contract.lean``.
* ``reason_code`` / ``ts_unix_ms`` — plain decimal integers.

The digest is ``sha256(preimage)`` as 64 lowercase hex chars, and the
Ed25519 signature is over the ASCII bytes of that hex digest (NOT the raw
32-byte digest, NOT the preimage) — one rule shared with the ledger's own
``entry_hash`` signing convention. The output field is standard base64 with
padding: exactly 88 chars matching ``^[A-Za-z0-9+/]{86}==$``.

``old_value`` and optimistic concurrency
========================================
``old_value`` is an explicit input: the CALLER obtains the current EDC value
(via the proxy's read surface) and passes it here — the model never chooses
it. The proxy independently hashes the value it observes in the EDC at write
time. If the value changed between the read and the write (a stale
``old_value``), the recomputed digest no longer matches the signature, the
verifier computes ``sigOk = false``, and the kernel denies the entry — the
denial renders as ``REFUTED: <clause>``. That is correct
optimistic-concurrency behavior, not an error in this module: re-read the
current value and re-sign (with a fresh ``tsUnixMs``) to retry.

Timing: the kernel requires ``ledgerHeadTsMs < tsUnixMs <= nowMs + 5000``
(proxy clock). Stamp-and-sign at the last moment before POSTing, and never
reuse a signed entry on retry — an entry stamped at or before the ledger
head is treated as a replay and refuted.

Key handling (Section 2B.3)
===========================
``AGENT_SIGNING_KEY`` holds base64 of a 32-byte Ed25519 seed, read from the
environment only — never from a tracked file. A missing or malformed key
fails loudly with a typed error whose message never includes the offending
value. No key, seed, or private-key object is ever logged, echoed, or
returned. Public keys, when published for the verifier side, are the hex of
the 32 raw public bytes (same convention as the fixture's
``agent_public_key.hex``).

Verifier helpers
================
``canonical_preimage`` / ``signed_digest`` / ``sign_digest`` /
``verify_signature`` are pure functions with no environment access, exported
so the component that eventually computes ``sigOk`` (biject-api or the
proxy) can import or copy them and recompute the digest from the
ledger-stored canonical params. Per ``Contract.lean``: the kernel consumes
only the boolean — a bug in that pipeline is a bypass of the kernel check.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "ACTION_CREATE",
    "ACTION_MODIFY",
    "ACTION_ANNOTATE",
    "SigningKeyError",
    "ActorIdError",
    "sha256_hex",
    "item_path",
    "canonical_preimage",
    "signed_digest",
    "sign_digest",
    "verify_signature",
    "build_audit_entry",
]

# ── environment variable names ───────────────────────────────────────────────
AGENT_SIGNING_KEY_ENV = "AGENT_SIGNING_KEY"  # base64 of a 32-byte Ed25519 seed
ACTOR_ID_ENV = "ACTOR_ID"  # harness identity, e.g. "AGENT_RECONCILER_01"

# ── contract constants ───────────────────────────────────────────────────────
# Integer action enum per contracts/tool_calls.json (0=create 1=modify
# 2=annotate). The preimage serializes the INTEGER (S4-D-30 deviation note);
# the proxy's string enum is being aligned to it in parallel.
ACTION_CREATE = 0
ACTION_MODIFY = 1
ACTION_ANNOTATE = 2
_ACTIONS = frozenset({ACTION_CREATE, ACTION_MODIFY, ACTION_ANNOTATE})
_REASON_CODES = frozenset(range(8))  # 0..7 integer enum, never free text

# contracts/tool_calls.json $defs.identifier — actor/principal ids.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
# One OID path segment (mirrors biject-proxy extract.rs ItemPath rules).
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# contracts/tool_calls.json sigEd25519 — standard base64 with padding, 88 chars.
_SIG_B64_RE = re.compile(r"^[A-Za-z0-9+/]{86}==$")


class SigningKeyError(RuntimeError):
    """Raised when AGENT_SIGNING_KEY is missing or malformed. The message
    describes the problem and NEVER includes the offending value."""


class ActorIdError(RuntimeError):
    """Raised when no valid actor id can be resolved (ACTOR_ID env / arg)."""


# ── pure canonicalization helpers (no environment access) ────────────────────


def sha256_hex(s: str) -> str:
    """Lowercase sha256 hex over the UTF-8 bytes of ``s`` — the contract's
    hashing rule for value hashes and for the signed digest itself."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def item_path(
    *,
    study_oid: str,
    subject_key: str,
    study_event_oid: str,
    form_oid: str,
    item_group_oid: str,
    item_oid: str,
) -> str:
    """The canonical 6-segment item path — what reaches the kernel as
    ``item_oid`` and what the runtime preimage must carry. Validates every
    segment against the closed class ``[A-Za-z0-9_-]{1,64}`` (natural
    language cannot fit this pattern)."""
    segments = (
        ("study_oid", study_oid),
        ("subject_key", subject_key),
        ("study_event_oid", study_event_oid),
        ("form_oid", form_oid),
        ("item_group_oid", item_group_oid),
        ("item_oid", item_oid),
    )
    for name, value in segments:
        if not isinstance(value, str) or not _SEGMENT_RE.match(value or ""):
            raise ValueError(
                f"{name}={value!r} is not a valid OID path segment "
                "(^[A-Za-z0-9_-]{1,64}$)"
            )
    return "/".join(value for _, value in segments)


def canonical_preimage(
    actor_id: str,
    action: int,
    item_oid: str,
    old_value_hash: str,
    new_value_hash: str,
    reason_code: int,
    ts_unix_ms: int,
) -> str:
    """The exact preimage ``sigEd25519`` covers: pipe-joined, seven fields,
    fixed order, no trailing separator, no whitespace, no quoting. ``action``
    is the integer enum. Mirrors biject-proxy ``extract.rs::signed_digest``
    and ``scripts/audit_bound_harness.py::signed_digest_from_params``."""
    return (
        f"{actor_id}|{action}|{item_oid}|{old_value_hash}"
        f"|{new_value_hash}|{reason_code}|{ts_unix_ms}"
    )


def signed_digest(
    actor_id: str,
    action: int,
    item_oid: str,
    old_value_hash: str,
    new_value_hash: str,
    reason_code: int,
    ts_unix_ms: int,
) -> str:
    """sha256 hex (64 lowercase chars) of the canonical preimage. This is the
    value the ledger stores as ``signed_digest`` and the verifier recomputes
    from the ledger-stored canonical params to decide ``sigOk``."""
    return sha256_hex(
        canonical_preimage(
            actor_id,
            action,
            item_oid,
            old_value_hash,
            new_value_hash,
            reason_code,
            ts_unix_ms,
        )
    )


def sign_digest(key: Ed25519PrivateKey, digest: str) -> str:
    """Ed25519 over the ASCII bytes of the 64-char hex digest (NOT the raw
    32-byte digest, NOT the preimage). Returns standard base64 with padding —
    exactly 88 chars matching ``^[A-Za-z0-9+/]{86}==$``."""
    sig = base64.b64encode(key.sign(digest.encode("ascii"))).decode("ascii")
    if not _SIG_B64_RE.match(sig):  # structurally impossible; fail loud anyway
        raise RuntimeError("produced signature does not match the contract pattern")
    return sig


def verify_signature(pubkey_bytes: bytes, sig_b64: str, digest: str) -> bool:
    """The ``sigOk`` computation for a future verifier: True iff ``sig_b64``
    is a valid Ed25519 signature by ``pubkey_bytes`` (32 raw public bytes)
    over the ASCII bytes of ``digest``. Never raises — any malformed input or
    failed verification returns False (fail closed)."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        sig = base64.b64decode(sig_b64, validate=True)
        pub.verify(sig, digest.encode("ascii"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
    except Exception:  # noqa: BLE001 — verification must never raise
        return False


# ── key / identity resolution (environment only; fail loud, never echo) ──────


def _load_signing_key(signing_key_b64: str | None = None) -> Ed25519PrivateKey:
    """Load the Ed25519 signing key from ``signing_key_b64`` or the
    ``AGENT_SIGNING_KEY`` env var (base64 of a 32-byte seed).

    Fail-loud/never-echo pattern (mirrors biject-api's lean_worker signing
    loader): a missing or malformed key raises SigningKeyError whose message
    describes the problem without including the offending value. There is no
    ephemeral fallback."""
    raw_b64 = (
        signing_key_b64
        if signing_key_b64 is not None
        else os.environ.get(AGENT_SIGNING_KEY_ENV, "")
    )
    if not raw_b64:
        raise SigningKeyError(
            f"{AGENT_SIGNING_KEY_ENV} is not set — cannot sign audit entries. "
            "Set it to the base64 of a 32-byte Ed25519 seed (env only, never a "
            "tracked file)."
        )
    try:
        seed = base64.b64decode(raw_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise SigningKeyError(
            f"{AGENT_SIGNING_KEY_ENV} is not valid base64."
        ) from exc
    if len(seed) != 32:
        raise SigningKeyError(
            f"{AGENT_SIGNING_KEY_ENV} must decode to a 32-byte seed, "
            f"got {len(seed)} bytes."
        )
    try:
        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception as exc:  # noqa: BLE001 — never surface key bytes
        raise SigningKeyError(
            f"{AGENT_SIGNING_KEY_ENV} is not a valid Ed25519 seed."
        ) from exc


def _resolve_actor_id(actor_id: str | None = None) -> str:
    """Resolve the actor id from the explicit argument or the ``ACTOR_ID``
    env var, and validate it against the contract's identifier pattern
    (``^[A-Za-z0-9][A-Za-z0-9_.-]*$``, 1-128 chars). The model never chooses
    this value."""
    resolved = actor_id if actor_id is not None else os.environ.get(ACTOR_ID_ENV, "")
    if not resolved:
        raise ActorIdError(
            f"no actor id: pass actor_id= or set {ACTOR_ID_ENV} in the environment."
        )
    if not _IDENTIFIER_RE.match(resolved):
        raise ActorIdError(
            f"actor id {resolved!r} does not match the contract identifier "
            "pattern ^[A-Za-z0-9][A-Za-z0-9_.-]*$ (1-128 chars)"
        )
    return resolved


# ── the signing pipeline ─────────────────────────────────────────────────────


def build_audit_entry(
    *,
    item_oid: str,
    new_value: str,
    action: int = ACTION_MODIFY,
    reason_code: int,
    subject_key: str,
    study_oid: str,
    study_event_oid: str,
    form_oid: str,
    item_group_oid: str,
    old_value: str,
    actor_id: str | None = None,
    ts_unix_ms: int | None = None,
) -> dict:
    """Build the signed provenance fields for a write-correction tool call.

    Returns ``{"actorId": str, "tsUnixMs": int, "sigEd25519": str}`` — the
    exact three keys ``adapters/openai/tools.py`` consumes. Everything else
    (the value hashes, the canonical item path, the preimage) is derived here
    and covered by the signature; only the hashes ever reach the kernel.

    ``old_value`` is the CURRENT value of the item as read from the EDC via
    the proxy's read surface — the caller obtains it, the model never
    chooses it. If it is stale (the EDC value changed after the read), the
    verifier's recomputed digest will not match this signature, ``sigOk``
    computes to false, and the kernel denies the entry (rendered as
    ``REFUTED: <clause>``). That is the intended optimistic-concurrency
    outcome: re-read and re-sign to retry. Retries always need a fresh call
    here — ``tsUnixMs`` is stamped at signing time and the kernel refutes an
    entry stamped at or before the ledger head as a replay.

    ``action`` is the integer enum (0=create, 1=modify, 2=annotate); it
    serializes as the integer in the signed preimage. For ``action = 0``
    (create) the old-value hash is the empty string per Contract.lean; the
    proxy's write path today performs modify only.

    ``ts_unix_ms`` is the timestamp the caller WANTS on the entry (spec §4.2:
    the agent supplies the timestamp it wants; the proxy supplies the
    reference clock and the ledger head it is judged against). ``None`` — the
    normal case — stamps the host clock at signing time. Supplying a value in
    the past does not weaken anything: the signature honestly covers the
    claimed timestamp, and the kernel refutes it against the proxy-derived
    bound (``notBackdated`` / ``notFutureDated``). That refusal is demo pass
    2, working as designed.
    """
    if action not in _ACTIONS:
        raise ValueError(f"action must be one of {sorted(_ACTIONS)}, got {action!r}")
    if reason_code not in _REASON_CODES:
        raise ValueError(
            f"reason_code must be an integer 0-7 (see contract), got {reason_code!r}"
        )
    if not isinstance(new_value, str):
        raise ValueError("new_value must be a string")
    if action != ACTION_CREATE and not isinstance(old_value, str):
        raise ValueError("old_value must be a string (the current EDC value)")

    resolved_actor = _resolve_actor_id(actor_id)
    key = _load_signing_key()

    path = item_path(
        study_oid=study_oid,
        subject_key=subject_key,
        study_event_oid=study_event_oid,
        form_oid=form_oid,
        item_group_oid=item_group_oid,
        item_oid=item_oid,
    )
    old_value_hash = "" if action == ACTION_CREATE else sha256_hex(old_value)
    new_value_hash = sha256_hex(new_value)

    # Stamp at the last moment before signing unless the caller claimed a
    # specific timestamp: the kernel requires
    # ledgerHeadTsMs < tsUnixMs <= nowMs + 5000 against the proxy's clock,
    # so a claimed past/future value is signed honestly and refuted there.
    if ts_unix_ms is None:
        ts_unix_ms = int(time.time_ns() // 1_000_000)
    elif not isinstance(ts_unix_ms, int) or isinstance(ts_unix_ms, bool) or ts_unix_ms <= 0:
        raise ValueError("ts_unix_ms must be a positive Unix-ms integer when supplied")

    digest = signed_digest(
        resolved_actor,
        action,
        path,
        old_value_hash,
        new_value_hash,
        reason_code,
        ts_unix_ms,
    )
    sig = sign_digest(key, digest)

    return {"actorId": resolved_actor, "tsUnixMs": ts_unix_ms, "sigEd25519": sig}
