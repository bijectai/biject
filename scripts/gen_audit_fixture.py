#!/usr/bin/env python3
"""S4-D-30 — golden audit-ledger fixture generator.

Generates ``scripts/fixtures/audit_ledger/`` by driving the REAL platform ledger
code — ``biject-api/backend/app/audit.py`` (``_append_audit``) and its
``write_manifest`` head-pointer writer — so every byte of the fixture (entry
serialization, canonical ``entry_hash``, ``prev_hash`` chaining, genesis
sentinel, Ed25519-over-``entry_hash`` ledger signature, ``audit_head.json``)
is produced by the rules the production ledger enforces, not by a
reimplementation. ``scripts/audit_bound_harness.py`` then proves its own
reimplementation byte-equal against this fixture.

Determinism: all entry content (call ids, params, timestamps) is pinned below
and ``time.time_ns`` is monkeypatched per append. NOTE, however, that the
agent signature ``sig_ed25519`` lives INSIDE ``params`` — canonical content
that feeds ``entry_hash`` — and the keys are EPHEMERAL, so regenerating the
fixture produces NEW entry hashes: after a regeneration the accepting-vector
literals in ``PolicyEnv/PolicyEnv/AuditBound.lean`` (``fixtureHeadHash``,
``oldValueHash``, …) must be re-pinned from this script's output, and
``scripts/audit_bound_harness.py`` will fail loudly until they are. Key
handling:

  * the ledger signing seed and the agent signing key are generated fresh in
    this process and NEVER written anywhere (Section 2B.3 — no private key
    material is committed, ever; these are test keys, not deployed keys);
  * only the two PUBLIC keys are written next to the fixture, so the harness
    can verify the signatures.

Requirements (generation only — the harness itself needs none of this):
  * a sibling read-only checkout of biject-api (default ``../biject-api``);
  * ``pip install pydantic aiofiles cryptography``.

Usage:  python3 scripts/gen_audit_fixture.py [--biject-api PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "scripts" / "fixtures" / "audit_ledger"

# ── pinned entry content (the source of the Lean accepting vector's numbers) ──
#
# Three PROVED write-corrections in the demo study, mirroring the params the
# verification proxy constructs (actor/action/item/old-hash/new-hash/reason/
# ts + context + signature fields). Values are synthetic; hashes are real
# sha256 over the value strings. ``action`` and ``reasonCode`` are the integer
# enums of contracts/tool_calls.json.
_SKEW_MS = 5000  # matches Contract.lean forwardSkewMs and the proxy's AUDIT_SKEW_MS

FIXTURE_ENTRIES = [
    # (call_id, item_oid, old_value, new_value, reason_code,
    #  ts_unix_ms, timestamp_ns, ledger_head_ts_before)
    ("S4D30-FIX-0001", "I_VITALS_WEIGHT", "72", "72.5", 1,
     1755400000100, 1755400000150000000, 0),
    ("S4D30-FIX-0002", "I_VITALS_HR", "620", "62", 3,
     1755400000200, 1755400000250000000, 1755400000150),
    ("S4D30-FIX-0003", "I_LABS_CREAT", "12", "1.2", 3,
     1755400000300, 1755400000355000000, 1755400000250),
]

ACTOR_ID = "AGENT_RECONCILER_01"
ACTION_MODIFY = 1  # integer enum per contracts/tool_calls.json (0=create 1=modify 2=annotate)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def signed_digest(actor_id: str, action: int, item_oid: str, old_hash: str,
                  new_hash: str, reason_code: int, ts_unix_ms: int) -> str:
    """The agent-signature preimage digest — pipe-joined canonicalization,
    field order fixed, mirroring biject-proxy src/extract.rs::signed_digest
    (with ``action`` as this repo's integer enum; see .claude/deviations/S4-D-30.md)."""
    preimage = f"{actor_id}|{action}|{item_oid}|{old_hash}|{new_hash}|{reason_code}|{ts_unix_ms}"
    return sha256_hex(preimage)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--biject-api", default=str(REPO_ROOT.parent / "biject-api"),
                    help="path to a read-only biject-api checkout")
    args = ap.parse_args()

    backend = Path(args.biject_api) / "backend"
    if not (backend / "app" / "audit.py").exists():
        print(f"FATAL: {backend}/app/audit.py not found", file=sys.stderr)
        return 1

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat)

    # Ephemeral keys — generated here, never persisted (public halves only).
    ledger_seed = os.urandom(32)
    agent_key = Ed25519PrivateKey.generate()

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for old in ("audit.log", "audit_head.json"):
        (FIXTURE_DIR / old).unlink(missing_ok=True)

    # The real audit module reads both of these at import time.
    os.environ["AUDIT_SIGNING_KEY"] = base64.b64encode(ledger_seed).decode()
    os.environ["AUDIT_LOG_PATH"] = str(FIXTURE_DIR / "audit.log")
    sys.path.insert(0, str(backend))
    from app import audit  # noqa: E402  — the REAL ledger code, imported read-only
    from app.models import AuditEntry  # noqa: E402

    # Deterministic timestamp_ns per append.
    ns_values = [e[6] for e in FIXTURE_ENTRIES]
    real_time_ns = time.time_ns
    time.time_ns = lambda: ns_values.pop(0)  # audit._append_audit calls time.time_ns()

    async def build() -> None:
        for (call_id, item_oid, old_v, new_v, reason, ts_ms, _ns, head_before) in FIXTURE_ENTRIES:
            old_h, new_h = sha256_hex(old_v), sha256_hex(new_v)
            digest = signed_digest(ACTOR_ID, ACTION_MODIFY, item_oid, old_h, new_h, reason, ts_ms)
            sig = base64.b64encode(agent_key.sign(digest.encode("ascii"))).decode()
            entry = AuditEntry(
                timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"),
                call_id=call_id,
                agent_id=ACTOR_ID,
                tool_name="openclinica_write_item",
                params={
                    "actor_id": ACTOR_ID,
                    "action": ACTION_MODIFY,
                    "item_oid": item_oid,
                    "old_value_hash": old_h,
                    "new_value_hash": new_h,
                    "reason_code": reason,
                    "ts_unix_ms": ts_ms,
                    "ledger_head_ts": head_before,
                    "now_unix_ms": ts_ms + 50,
                    "skew_ms": _SKEW_MS,
                    "sig_ed25519": sig,
                    "signed_digest": digest,
                },
                verdict="allowed",
                policy_id="audit_bound_v1",
                lean_trace="",
                explanation="kernel PROVED AuditEntryValid",
                latency_us=1200,
                conjecture="AuditEntryValid entry ctx",
                lean_result="proved",
            )
            await audit._append_audit(entry)

    asyncio.run(build())
    time.time_ns = real_time_ns

    # Public halves only.
    ledger_pub = Ed25519PrivateKey.from_private_bytes(ledger_seed).public_key()
    (FIXTURE_DIR / "ledger_public_key.hex").write_text(
        ledger_pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex() + "\n")
    (FIXTURE_DIR / "agent_public_key.hex").write_text(
        agent_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex() + "\n")

    # Report the values the Lean accepting vector must carry.
    lines = (FIXTURE_DIR / "audit.log").read_text().splitlines()
    head = json.loads(lines[-1])
    head_ts = max(head["params"]["ts_unix_ms"], head["timestamp_ns"] // 1_000_000)
    print(f"fixture entries : {len(lines)}")
    print(f"head entry_hash : {head['entry_hash']}")
    print(f"head ts (ms)    : {head_ts}  (= max(params.ts_unix_ms, timestamp_ns//1e6))")
    print(f"head new_value_hash (candidate oldValueHash): {head['params']['new_value_hash']}")
    print(f"sha256('1.25') (candidate newValueHash)     : {sha256_hex('1.25')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
