"""S4-A-30 harness signing pipeline (``agent.audit_entry``).

The audit-entry provenance fields (``actorId``, ``tsUnixMs``,
``sigEd25519``) are populated here — never by the LLM, never by the
adapters. See ``agent/audit_entry.py`` for the canonicalization contract and
key-handling rules.
"""

from .audit_entry import (
    ACTION_ANNOTATE,
    ACTION_CREATE,
    ACTION_MODIFY,
    ActorIdError,
    SigningKeyError,
    build_audit_entry,
    canonical_preimage,
    item_path,
    sha256_hex,
    sign_digest,
    signed_digest,
    verify_signature,
)

__all__ = [
    "ACTION_ANNOTATE",
    "ACTION_CREATE",
    "ACTION_MODIFY",
    "ActorIdError",
    "SigningKeyError",
    "build_audit_entry",
    "canonical_preimage",
    "item_path",
    "sha256_hex",
    "sign_digest",
    "signed_digest",
    "verify_signature",
]
