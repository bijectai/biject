"""OpenAI Agents SDK function tools for the biject → OpenClinica 3 demo.

Ticket: S4-D-21.

ARCHITECTURAL DECISION (recorded per sprint plan)
=================================================
These tools are plain OpenAI Agents SDK ``@function_tool`` functions that POST
JSON over HTTP(S) to the Rust verification proxy — deliberately NOT an MCP
server. Rationale: the enforcement bound in this system is at the NETWORK
layer, not the protocol layer. The agent host has no route to OpenClinica
except the proxy (see infra/hetzner/firewall/), so whatever protocol the agent
speaks, every tool call funnels through the proxy, which verifies the typed
audit entry against the Lean kernel BEFORE forwarding to OC. MCP would buy no
additional enforcement here — it is the post-sprint ergonomics upgrade, not a
security requirement.

CONTRACT
========
The tool signatures mirror ``contracts/tool_calls.json`` (DRAFT v0 — pending
S4-A-12 freeze by Adeel; the frozen contract supersedes it). Two invariants:

1. NO free-text parameters that feed reasoning into the kernel. Every param is
   a closed-pattern identifier/OID or an integer enum. The one string of
   substance, ``new_value``, is DATA being written to OC, not reasoning — and
   only its SHA-256 hash reaches the kernel (the proxy binds the hash into the
   audit entry; the raw value goes to OC alone).
2. The audit-entry provenance fields (``actorId``, ``tsUnixMs``,
   ``sigEd25519``) are NEVER chosen by the LLM. They are populated by the
   harness signing pipeline (agent/audit_entry.py, ticket S4-A-30, other dev)
   — see ``build_audit_entry()`` below, which is a stub until that lands.

TRACING
=======
Sprint decision: OpenAI Agents SDK tracing is OFF. Tool payloads here are
PHI-adjacent (subject keys, corrected clinical values) and must not leave the
host via the SDK's trace exporter. We disable it programmatically at import
time; setting the env var ``OPENAI_AGENTS_DISABLE_TRACING=1`` on the agent
host is the belt-and-braces equivalent and is also set in the compose file.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from agents import function_tool, set_tracing_disabled

# Sprint decision — SDK tracing OFF: PHI-adjacent payloads must not leave the
# host. (Equivalent env-var form: OPENAI_AGENTS_DISABLE_TRACING=1.)
set_tracing_disabled(True)

# ---------------------------------------------------------------------------
# Proxy endpoint
# ---------------------------------------------------------------------------
# All tool calls POST to the Rust verification proxy; the proxy is the ONLY
# network route from the agent host toward OC (enforced by
# infra/hetzner/firewall/). Example: https://proxy:8443/tool
_PROXY_URL_ENV = "BIJECT_PROXY_URL"

# ---------------------------------------------------------------------------
# Client-side mirrors of the contract's closed character classes.
# ---------------------------------------------------------------------------
# The proxy re-validates against contracts/tool_calls.json regardless (the
# proxy is the enforcement point; this is fail-fast ergonomics, not security).
_OID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,256}$")

_REASON_CODES = frozenset(range(8))  # 0..7, see contract for meanings
_ACTIONS = frozenset({0, 1, 2})  # 0=create, 1=modify, 2=annotate

_NEW_VALUE_MAX_LEN = 4000


def _proxy_url() -> str:
    """Resolve the proxy base URL from the environment (no default: a missing
    value must fail loudly, never fall back to a direct-to-OC guess)."""
    url = os.environ.get(_PROXY_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{_PROXY_URL_ENV} is not set. The agent may only talk to OC via "
            "the verification proxy; refusing to guess an endpoint."
        )
    return url.rstrip("/")


def _require_oid(name: str, value: str) -> str:
    """Reject anything outside the contract's closed OID class before it even
    leaves the process. Natural language cannot fit this pattern."""
    if not _OID_RE.match(value or ""):
        raise ValueError(
            f"{name}={value!r} does not match the OID pattern "
            "^[A-Za-z0-9_-]{1,256}$ from contracts/tool_calls.json"
        )
    return value


def _post_to_proxy(payload: dict) -> dict:
    """POST a tool-call envelope to the verification proxy and return its JSON
    response. Plain HTTP(S) JSON — see the module docstring for why this is
    not MCP. Uses stdlib urllib so the adapter adds no HTTP dependency."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _proxy_url(),  # e.g. https://proxy:8443/tool
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Certificate verification stays ON (default opener). If the proxy uses an
    # internal CA, install it in the container trust store — do not disable
    # verification here.
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_audit_entry(
    *,
    item_oid: str,
    new_value: str,
    action: int,
    reason_code: int,
    subject_key: str,
    study_oid: str,
    study_event_oid: str,
    form_oid: str,
    item_group_oid: str,
) -> dict:
    """Placeholder for the harness signing pipeline (ticket S4-A-30).

    The real implementation lives in ``agent/audit_entry.py`` (S4-A-30, other
    dev). It — not the LLM, and not this adapter — is responsible for:

    * ``actorId``   — resolved from the harness's own identity config;
    * ``tsUnixMs``  — stamped from the host clock at signing time;
    * ``sha256(new_value)`` — the ONLY form of ``new_value`` the kernel sees;
    * canonicalizing the entry bytes and producing ``sigEd25519`` with the
      harness's private key (which never enters this module).

    Keeping this a hard stub (rather than a fake signer) is deliberate: a
    write must be impossible to even attempt until the real signing pipeline
    is wired in, and the proxy would reject an unsigned entry anyway.
    """
    raise NotImplementedError(
        "build_audit_entry is populated by the harness signing pipeline — "
        "agent/audit_entry.py, ticket S4-A-30 (other dev). actorId, tsUnixMs "
        "and sigEd25519 are never chosen by the LLM; wire S4-A-30 in before "
        "using write_item_correction."
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@function_tool
def list_open_queries(study_oid: str) -> str:
    """List the open data-clarification queries (discrepancy notes) for a
    study in OpenClinica.

    Args:
        study_oid: OpenClinica study OID (letters, digits, '_' and '-' only —
            e.g. "S_DEMO01"). No other characters are accepted.
    """
    _require_oid("study_oid", study_oid)
    result = _post_to_proxy(
        {
            "op": "listOpenQueries",
            "params": {"studyOid": study_oid},
        }
    )
    # Return the proxy's JSON verbatim as a string for the model to read.
    return json.dumps(result)


@function_tool
def write_item_correction(
    item_oid: str,
    new_value: str,
    action: int,
    reason_code: int,
    subject_key: str,
    study_oid: str,
    study_event_oid: str,
    form_oid: str,
    item_group_oid: str,
) -> str:
    """Write a correction to a single item value in OpenClinica. The
    verification proxy checks a signed, typed audit entry against the Lean
    kernel before anything reaches OpenClinica; unverifiable corrections are
    rejected, not forwarded.

    Args:
        item_oid: OID of the item being corrected (e.g. "I_DEMO_WEIGHT_KG").
        new_value: The corrected value to store (max 4000 chars). This is
            data, not reasoning — it is forwarded to OpenClinica, while the
            kernel only ever sees its SHA-256 hash.
        action: Integer action kind: 0=create, 1=modify, 2=annotate.
        reason_code: Integer reason enum (never free text): 0=source data
            confirmed, 1=transcription error, 2=unit correction, 3=decimal
            shift, 4=cross-field reconciliation, 5=missing value completion,
            6=investigator confirmed, 7=other documented.
        subject_key: OC study-subject key OID (e.g. "SS_1001").
        study_oid: OC study OID.
        study_event_oid: OC study-event definition OID (e.g. "SE_VISIT1").
        form_oid: OC form/CRF OID (e.g. "F_VITALS_V1").
        item_group_oid: OC item-group OID (e.g. "IG_VITALS").
    """
    # -- fail-fast validation mirroring contracts/tool_calls.json -----------
    for name, value in (
        ("item_oid", item_oid),
        ("subject_key", subject_key),
        ("study_oid", study_oid),
        ("study_event_oid", study_event_oid),
        ("form_oid", form_oid),
        ("item_group_oid", item_group_oid),
    ):
        _require_oid(name, value)
    if action not in _ACTIONS:
        raise ValueError(f"action must be one of {sorted(_ACTIONS)}, got {action!r}")
    if reason_code not in _REASON_CODES:
        raise ValueError(
            f"reason_code must be an integer 0-7 (see contract), got {reason_code!r}"
        )
    if not isinstance(new_value, str) or len(new_value) > _NEW_VALUE_MAX_LEN:
        raise ValueError(f"new_value must be a string of at most {_NEW_VALUE_MAX_LEN} chars")

    # -- audit-entry fields: harness-populated, never LLM-chosen ------------
    # build_audit_entry (S4-A-30) supplies actorId / tsUnixMs / sigEd25519 and
    # the sha256(new_value) binding. Until it lands this raises
    # NotImplementedError, which is intended: no unsigned write attempts.
    audit = build_audit_entry(
        item_oid=item_oid,
        new_value=new_value,
        action=action,
        reason_code=reason_code,
        subject_key=subject_key,
        study_oid=study_oid,
        study_event_oid=study_event_oid,
        form_oid=form_oid,
        item_group_oid=item_group_oid,
    )

    result = _post_to_proxy(
        {
            "op": "writeItemCorrection",
            "params": {
                "itemOid": item_oid,
                "newValue": new_value,
                "action": action,
                "reasonCode": reason_code,
                "subjectKey": subject_key,
                "studyOid": study_oid,
                "studyEventOid": study_event_oid,
                "formOid": form_oid,
                "itemGroupOid": item_group_oid,
                # Harness-signed provenance (S4-A-30):
                "actorId": audit["actorId"],
                "tsUnixMs": audit["tsUnixMs"],
                "sigEd25519": audit["sigEd25519"],
            },
        }
    )
    return json.dumps(result)


# Exported tool list for Agent(tools=...) call sites.
BIJECT_TOOLS = [list_open_queries, write_item_correction]
