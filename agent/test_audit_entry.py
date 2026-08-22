"""S4-A-30 tests — agent/audit_entry.py against the golden ledger fixture,
the harness's own preimage builder, and the key-handling rules.

Run from the biject repo root:  python3 -m pytest agent/test_audit_entry.py -q

Only ``cryptography`` (agent/requirements.txt) and ``pytest`` are needed.
All signing keys used here are runtime-ephemeral, generated per test and
never persisted (Section 2B.3); the fixture ships PUBLIC keys only.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent.audit_entry import (
    ACTOR_ID_ENV,
    AGENT_SIGNING_KEY_ENV,
    SigningKeyError,
    build_audit_entry,
    canonical_preimage,
    item_path,
    sha256_hex,
    sign_digest,
    signed_digest,
    verify_signature,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "scripts" / "fixtures" / "audit_ledger"
SIG_B64_RE = re.compile(r"^[A-Za-z0-9+/]{86}==$")  # contracts/tool_calls.json


def _fixture_entries() -> list[dict]:
    lines = (FIXTURE_DIR / "audit.log").read_text().splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def _agent_pubkey_bytes() -> bytes:
    return bytes.fromhex((FIXTURE_DIR / "agent_public_key.hex").read_text().strip())


def _ephemeral_env(monkeypatch) -> Ed25519PrivateKey:
    """Point AGENT_SIGNING_KEY/ACTOR_ID at a fresh ephemeral identity and
    return the private key so tests can verify with its public half."""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes_raw()
    monkeypatch.setenv(AGENT_SIGNING_KEY_ENV, base64.b64encode(seed).decode())
    monkeypatch.setenv(ACTOR_ID_ENV, "AGENT_TEST_01")
    return key


# ── (1) golden fixture: recompute digest from stored params + verify sig ─────


def test_golden_fixture_digests_and_signatures():
    entries = _fixture_entries()
    assert entries, "golden fixture audit.log is empty or missing"
    pub = _agent_pubkey_bytes()
    for i, e in enumerate(entries):
        p = e["params"]
        # NB: the fixture's vectors carry bare item OIDs (e.g. I_VITALS_WEIGHT);
        # the preimage function is shape-agnostic and takes them as data.
        digest = signed_digest(
            p["actor_id"],
            p["action"],  # integer action enum in the golden vectors
            p["item_oid"],
            p["old_value_hash"],
            p["new_value_hash"],
            p["reason_code"],
            p["ts_unix_ms"],
        )
        assert digest == p["signed_digest"], f"entry {i}: digest mismatch"
        assert verify_signature(pub, p["sig_ed25519"], digest), (
            f"entry {i}: stored sig_ed25519 does not verify against "
            "agent_public_key.hex over the recomputed digest"
        )


def test_golden_fixture_rejects_tampered_param():
    head = _fixture_entries()[-1]
    p = dict(head["params"])
    p["new_value_hash"] = sha256_hex("6.66")
    digest = signed_digest(
        p["actor_id"], p["action"], p["item_oid"], p["old_value_hash"],
        p["new_value_hash"], p["reason_code"], p["ts_unix_ms"],
    )
    assert digest != head["params"]["signed_digest"]
    assert not verify_signature(_agent_pubkey_bytes(), p["sig_ed25519"], digest)


# ── (2) roundtrip sign/verify with an ephemeral key ──────────────────────────


def test_build_audit_entry_roundtrip(monkeypatch):
    key = _ephemeral_env(monkeypatch)
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    entry = build_audit_entry(
        item_oid="I_LABS_CREAT",
        new_value="1.25",
        action=1,
        reason_code=1,
        subject_key="SS_1001",
        study_oid="S_BJTDEMO01",
        study_event_oid="SE_VISIT1",
        form_oid="F_LABS_V1",
        item_group_oid="IG_LABS",
        old_value="1.2",
    )
    assert set(entry) == {"actorId", "tsUnixMs", "sigEd25519"}
    assert entry["actorId"] == "AGENT_TEST_01"
    assert isinstance(entry["tsUnixMs"], int)

    # Recompute the digest exactly as a verifier would: full 6-segment path
    # at runtime (Q3), integer action (Q2), hashes over the value strings.
    path = item_path(
        study_oid="S_BJTDEMO01",
        subject_key="SS_1001",
        study_event_oid="SE_VISIT1",
        form_oid="F_LABS_V1",
        item_group_oid="IG_LABS",
        item_oid="I_LABS_CREAT",
    )
    assert path == "S_BJTDEMO01/SS_1001/SE_VISIT1/F_LABS_V1/IG_LABS/I_LABS_CREAT"
    digest = signed_digest(
        entry["actorId"], 1, path,
        sha256_hex("1.2"), sha256_hex("1.25"), 1, entry["tsUnixMs"],
    )
    assert verify_signature(pub, entry["sigEd25519"], digest)

    # A stale old_value yields a different digest -> sigOk computes to false
    # (the kernel denies; optimistic concurrency working as intended).
    stale_digest = signed_digest(
        entry["actorId"], 1, path,
        sha256_hex("999"), sha256_hex("1.25"), 1, entry["tsUnixMs"],
    )
    assert not verify_signature(pub, entry["sigEd25519"], stale_digest)


def test_build_audit_entry_honors_a_claimed_timestamp(monkeypatch):
    """Spec §4.2: the agent supplies the timestamp it WANTS; the kernel judges
    it against the proxy-derived bound. The signer must sign the claimed value
    honestly — a backdate attempt is refuted downstream, not laundered here."""
    key = _ephemeral_env(monkeypatch)
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    claimed = 1_755_400_000_100  # deliberately in the past
    entry = build_audit_entry(
        item_oid="I_LABS_CREAT",
        new_value="1.25",
        action=1,
        reason_code=1,
        subject_key="SS_1001",
        study_oid="S_BJTDEMO01",
        study_event_oid="SE_VISIT1",
        form_oid="F_LABS_V1",
        item_group_oid="IG_LABS",
        old_value="1.2",
        ts_unix_ms=claimed,
    )
    assert entry["tsUnixMs"] == claimed
    path = item_path(
        study_oid="S_BJTDEMO01",
        subject_key="SS_1001",
        study_event_oid="SE_VISIT1",
        form_oid="F_LABS_V1",
        item_group_oid="IG_LABS",
        item_oid="I_LABS_CREAT",
    )
    digest = signed_digest(
        entry["actorId"], 1, path, sha256_hex("1.2"), sha256_hex("1.25"), 1, claimed
    )
    assert verify_signature(pub, entry["sigEd25519"], digest)

    import pytest as _pytest

    for bad in (0, -5, "yesterday", 1.5, True):
        with _pytest.raises(ValueError):
            build_audit_entry(
                item_oid="I_LABS_CREAT",
                new_value="1.25",
                reason_code=1,
                subject_key="SS_1001",
                study_oid="S_BJTDEMO01",
                study_event_oid="SE_VISIT1",
                form_oid="F_LABS_V1",
                item_group_oid="IG_LABS",
                old_value="1.2",
                ts_unix_ms=bad,
            )


def test_sign_digest_verify_signature_pure_roundtrip():
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    digest = signed_digest(
        "AGENT_RECONCILER_01", 1, "I_LABS_CREAT",
        sha256_hex("1.2"), sha256_hex("1.25"), 1, 1755400000900,
    )
    sig = sign_digest(key, digest)
    assert verify_signature(pub, sig, digest)
    assert not verify_signature(pub, sig, digest[:-1] + "0")


# ── (3) 88-char base64 signature pattern ─────────────────────────────────────


def test_signature_matches_contract_pattern(monkeypatch):
    _ephemeral_env(monkeypatch)
    entry = build_audit_entry(
        item_oid="I_VITALS_WEIGHT",
        new_value="72.5",
        reason_code=1,
        subject_key="SS_1001",
        study_oid="S_BJTDEMO01",
        study_event_oid="SE_VISIT1",
        form_oid="F_VITALS_V1",
        item_group_oid="IG_VITALS",
        old_value="72",
    )
    sig = entry["sigEd25519"]
    assert len(sig) == 88
    assert SIG_B64_RE.match(sig), sig
    # Golden-fixture signatures obey the same pattern.
    for e in _fixture_entries():
        assert SIG_B64_RE.match(e["params"]["sig_ed25519"])


# ── (4) preimage byte-for-byte vs the harness's signed_digest_from_params ────


def _load_harness_module():
    spec = importlib.util.spec_from_file_location(
        "audit_bound_harness", REPO_ROOT / "scripts" / "audit_bound_harness.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_preimage_matches_harness_builder():
    harness = _load_harness_module()
    samples = [
        {  # the Lean accepting vector's candidate params (AuditBound.lean)
            "actor_id": "AGENT_RECONCILER_01",
            "action": 1,
            "item_oid": "I_LABS_CREAT",
            "old_value_hash": sha256_hex("1.2"),
            "new_value_hash": sha256_hex("1.25"),
            "reason_code": 1,
            "ts_unix_ms": 1755400000900,
        },
        {  # a runtime-shaped sample: full 6-segment path as item_oid
            "actor_id": "AGENT_TEST_01",
            "action": 1,
            "item_oid": "S_BJTDEMO01/SS_1001/SE_VISIT1/F_LABS_V1/IG_LABS/I_LABS_CREAT",
            "old_value_hash": sha256_hex("8.9"),
            "new_value_hash": sha256_hex("4.1"),
            "reason_code": 3,
            "ts_unix_ms": 1765000000000,
        },
    ]
    # Every ledger-stored params dict from the fixture, too.
    samples += [e["params"] for e in _fixture_entries()]
    for p in samples:
        ours = signed_digest(
            p["actor_id"], p["action"], p["item_oid"], p["old_value_hash"],
            p["new_value_hash"], p["reason_code"], p["ts_unix_ms"],
        )
        theirs = harness.signed_digest_from_params(p)
        assert ours == theirs, f"digest divergence from harness for {p['item_oid']}"
        # And the preimage itself is the pipe-joined seven-tuple, byte for byte.
        expected_preimage = (
            f"{p['actor_id']}|{p['action']}|{p['item_oid']}|{p['old_value_hash']}"
            f"|{p['new_value_hash']}|{p['reason_code']}|{p['ts_unix_ms']}"
        )
        assert canonical_preimage(
            p["actor_id"], p["action"], p["item_oid"], p["old_value_hash"],
            p["new_value_hash"], p["reason_code"], p["ts_unix_ms"],
        ) == expected_preimage


# ── (5) missing/invalid AGENT_SIGNING_KEY fails loud without echoing ─────────


_BUILD_KWARGS = dict(
    item_oid="I_VITALS_HR",
    new_value="62",
    reason_code=3,
    subject_key="SS_1001",
    study_oid="S_BJTDEMO01",
    study_event_oid="SE_VISIT1",
    form_oid="F_VITALS_V1",
    item_group_oid="IG_VITALS",
    old_value="620",
)


def test_missing_key_fails_loud(monkeypatch):
    monkeypatch.delenv(AGENT_SIGNING_KEY_ENV, raising=False)
    monkeypatch.setenv(ACTOR_ID_ENV, "AGENT_TEST_01")
    with pytest.raises(SigningKeyError, match="is not set"):
        build_audit_entry(**_BUILD_KWARGS)


@pytest.mark.parametrize(
    "bad_value, why",
    [
        ("!!!not-base64!!!", "not base64"),
        (base64.b64encode(b"short seed").decode(), "wrong length"),
        (base64.b64encode(b"x" * 64).decode(), "wrong length (64 bytes)"),
    ],
)
def test_invalid_key_fails_loud_and_never_echoes(monkeypatch, bad_value, why):
    monkeypatch.setenv(AGENT_SIGNING_KEY_ENV, bad_value)
    monkeypatch.setenv(ACTOR_ID_ENV, "AGENT_TEST_01")
    with pytest.raises(SigningKeyError) as exc_info:
        build_audit_entry(**_BUILD_KWARGS)
    message = str(exc_info.value)
    assert bad_value not in message, f"key material echoed in error ({why})"
    # The chained cause must not carry the value either.
    cause = exc_info.value.__cause__
    assert cause is None or bad_value not in str(cause)


def test_valid_key_but_missing_actor_id_fails_loud(monkeypatch):
    _ephemeral_env(monkeypatch)
    monkeypatch.delenv(ACTOR_ID_ENV, raising=False)
    from agent.audit_entry import ActorIdError

    with pytest.raises(ActorIdError, match="ACTOR_ID"):
        build_audit_entry(**_BUILD_KWARGS)
