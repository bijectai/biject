"""OpenAI Agents SDK function tools for the biject → OpenClinica 3 demo.

Ticket: S4-D-21 (transport rewrite: proxy route contract).

ARCHITECTURAL DECISION (recorded per sprint plan)
=================================================
These tools are plain OpenAI Agents SDK ``@function_tool`` functions that speak
HTTP(S) JSON to the Rust verification proxy — deliberately NOT an MCP server.
Rationale: the enforcement bound in this system is at the NETWORK layer, not
the protocol layer. The agent host has no route to OpenClinica except the
proxy (see infra/hetzner/firewall/), so whatever protocol the agent speaks,
every tool call funnels through the proxy, which checks the typed audit entry
against the Lean kernel BEFORE forwarding to OC. MCP would buy no additional
enforcement here — it is the post-sprint ergonomics upgrade, not a security
requirement.

WIRE CONTRACT (proxy routes)
============================
The proxy serves per-operation routes; there is no ``{op, params}`` envelope.

* ``GET  /queries/open?studyOid=<OID>`` — gated read; may 403 until a read
  policy is compiled upstream.
* ``GET  /items/context?itemOid=<6-seg path>`` — gated read of an item's
  current value (+ ``current_value_hash``).
* ``POST /items/write`` — body is EXACTLY six camelCase fields (the proxy
  denies unknown fields): ``itemOid``, ``newValue``, ``actorId``,
  ``reasonCode``, ``tsUnixMs``, ``sigEd25519``. 200 carries
  ``{call_id, verdict, forwarded, latency_us, elab_us, total_latency_us,
  new_value_hash}``; 403 carries a denial ``{reason, ...}`` (verdict
  vocabulary is ``allowed | blocked | skipped``; latency fields are passed
  through untouched).

An item is addressed by a six-segment OID path
``Study/Subject/Event/Form/ItemGroup/Item``; every segment matches
``[A-Za-z0-9_-]{1,64}``. Client-side validation here mirrors that closed
class — fail-fast ergonomics, not the enforcement point (the proxy
re-validates regardless).

AUTH
====
Every proxy route except ``GET /healthz`` / ``GET /readyz`` requires
``X-Biject-Proxy-Key`` (from ``BIJECT_PROXY_API_KEY``); OpenClinica routes —
all three used here — additionally require ``X-Biject-Agent-Id`` (from
``BIJECT_AGENT_ID``). The base URL comes from ``BIJECT_PROXY_URL``. None of
the three has a default: a missing value raises ``RuntimeError`` at call time
rather than guessing an endpoint or sending an unauthenticated call.

INVARIANTS
==========
1. NO free-text parameters feed the kernel. Every tool param is a
   closed-pattern identifier/OID path or an integer enum. The one string of
   substance, ``new_value``, is DATA being written to OC, not reasoning — only
   its SHA-256 hash reaches the kernel (the proxy binds the hash into the
   audit entry; the raw value goes to OC alone).
2. The audit-entry provenance fields (``actorId``, ``tsUnixMs``,
   ``sigEd25519``) are NEVER chosen by the LLM. They come from the harness
   signing pipeline — ``agent/audit_entry.py`` (S4-A-30) — which this module
   calls inside the write flow, after reading the item's current value so the
   signed entry binds the observed ``old_value``.

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
import urllib.error
import urllib.parse
import urllib.request

from agents import function_tool, set_tracing_disabled

# Sprint decision — SDK tracing OFF: PHI-adjacent payloads must not leave the
# host. (Equivalent env-var form: OPENAI_AGENTS_DISABLE_TRACING=1.)
set_tracing_disabled(True)

# Harness signing pipeline (S4-A-30). Lands in parallel at agent/audit_entry.py
# (repo root on sys.path). Until it is present, the write tool raises at call
# time — a write must be impossible to even attempt unsigned, and the proxy
# would refuse an unsigned entry anyway. A missing-module fallback is the ONLY
# error swallowed here: a broken audit_entry module must fail loudly.
try:
    from agent.audit_entry import build_audit_entry
except ModuleNotFoundError:  # module not landed / repo root not on sys.path
    build_audit_entry = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Environment (no defaults — missing values fail loudly at call time)
# ---------------------------------------------------------------------------
_PROXY_URL_ENV = "BIJECT_PROXY_URL"
_PROXY_KEY_ENV = "BIJECT_PROXY_API_KEY"
_AGENT_ID_ENV = "BIJECT_AGENT_ID"

# ---------------------------------------------------------------------------
# Client-side mirrors of the contract's closed character classes.
# ---------------------------------------------------------------------------
# The proxy re-validates regardless (the proxy is the enforcement point; this
# is fail-fast ergonomics, not security).
_OID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_ITEM_PATH_SEGMENTS = 6  # Study/Subject/Event/Form/ItemGroup/Item

_REASON_CODES = frozenset(range(8))  # 0..7, see contract for meanings
_NEW_VALUE_MAX_LEN = 4000

# This tool corrects an existing value; the audit-entry action enum is fixed
# to 1=modify and is not an LLM-facing parameter.
_ACTION_MODIFY = 1

_REQUEST_TIMEOUT_S = 30


def _require_env(name: str, why: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set. {why}")
    return value


def _proxy_url() -> str:
    """Resolve the proxy base URL from the environment (no default: a missing
    value must fail loudly, never fall back to a direct-to-OC guess)."""
    url = _require_env(
        _PROXY_URL_ENV,
        "The agent may only talk to OpenClinica via the verification proxy; "
        "refusing to guess an endpoint.",
    )
    return url.rstrip("/")


def _auth_headers() -> dict:
    """Both auth headers, required on every route this module calls."""
    return {
        "X-Biject-Proxy-Key": _require_env(
            _PROXY_KEY_ENV,
            "Every proxy route requires the X-Biject-Proxy-Key header; set it "
            "in the agent host environment.",
        ),
        "X-Biject-Agent-Id": _require_env(
            _AGENT_ID_ENV,
            "OpenClinica proxy routes require the X-Biject-Agent-Id header; "
            "set it in the agent host environment.",
        ),
    }


def _require_oid_segment(name: str, value: str) -> str:
    """Reject anything outside the contract's closed OID-segment class before
    it even leaves the process. Natural language cannot fit this pattern."""
    if not isinstance(value, str) or not _OID_SEGMENT_RE.match(value or ""):
        raise ValueError(
            f"{name}={value!r} does not match the OID segment pattern "
            "^[A-Za-z0-9_-]{1,64}$"
        )
    return value


def _require_item_path(name: str, value: str) -> list:
    """Validate a six-segment item path and return its segments, in order:
    [study, subject, event, form, item_group, item]."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string item path, got {type(value).__name__}")
    segments = value.split("/")
    if len(segments) != _ITEM_PATH_SEGMENTS:
        raise ValueError(
            f"{name}={value!r} must have exactly {_ITEM_PATH_SEGMENTS} "
            "'/'-separated segments (Study/Subject/Event/Form/ItemGroup/Item), "
            f"got {len(segments)}"
        )
    for i, segment in enumerate(segments):
        _require_oid_segment(f"{name} segment {i}", segment)
    return segments


def _request(method: str, path: str, *, query: dict = None, body: dict = None) -> dict:
    """Send one request to the verification proxy and return its parsed JSON.

    Plain stdlib urllib — see the module docstring for why this is not MCP and
    adds no HTTP dependency. A 403 denial is returned as data (with
    ``http_status: 403`` added) so the model can see WHY the proxy refused;
    every other HTTP error propagates as an exception.
    """
    url = _proxy_url() + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = _auth_headers()
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # Certificate verification stays ON (default opener). If the proxy uses an
    # internal CA, install it in the container trust store — do not disable
    # verification here.
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code == 403:
            try:
                denial = json.loads(err.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                denial = {"reason": "proxy denial body was not valid JSON"}
            return {"http_status": 403, **denial}
        raise


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@function_tool
def list_open_queries(study_oid: str) -> str:
    """List the open data-clarification queries (discrepancy notes) for a study in the EDC. This is a gated read through the verification proxy; it may be refused (403) until a read policy is compiled upstream, in which case the refusal reason is returned.

    Args:
        study_oid: EDC study OID — letters, digits, '_' and '-' only, at most
            64 characters (e.g. "S_DEMO01"). No other characters are accepted.
    """
    _require_oid_segment("study_oid", study_oid)
    result = _request("GET", "/queries/open", query={"studyOid": study_oid})
    return json.dumps(result)


@function_tool
def get_item_context(item_oid: str) -> str:
    """Read the current value and context of a single EDC item, addressed by its six-segment OID path. This is a gated read through the verification proxy; a refusal (403) returns the reason instead of the value.

    Args:
        item_oid: Six-segment item path Study/Subject/Event/Form/ItemGroup/Item
            (e.g. "S_DEMO01/SS_1001/SE_VISIT1/F_VITALS_V1/IG_VITALS/I_WEIGHT_KG").
            Each segment is letters, digits, '_' and '-' only, at most 64
            characters.
    """
    _require_item_path("item_oid", item_oid)
    result = _request("GET", "/items/context", query={"itemOid": item_oid})
    return json.dumps(result)


@function_tool
def write_item_correction(
    item_oid: str, new_value: str, reason_code: int, ts_unix_ms: int | None = None
) -> str:
    """Write a corrected item value to the EDC. The call is gated by a kernel-checked audit-entry predicate; writes lacking a valid, non-backdated audit entry with a reason code are refused before reaching the EDC.

    The audit entry (actor identity, timestamp, Ed25519 signature over the
    observed old value and the new value) is produced by the harness signing
    pipeline inside this tool — you never supply hashes or signatures. On
    refusal the proxy's reason (and Lean trace, when present) is returned so
    you can report WHY the write was refused.

    Args:
        item_oid: Six-segment item path Study/Subject/Event/Form/ItemGroup/Item
            (e.g. "S_DEMO01/SS_1001/SE_VISIT1/F_VITALS_V1/IG_VITALS/I_WEIGHT_KG").
            Each segment is letters, digits, '_' and '-' only, at most 64
            characters.
        new_value: The corrected value to store (max 4000 chars). This is data
            being written to the EDC, not reasoning — the kernel only ever
            sees its SHA-256 hash.
        reason_code: Integer reason enum (never free text): 0=source data
            confirmed, 1=transcription error, 2=unit correction, 3=decimal
            shift, 4=cross-field reconciliation, 5=missing value completion,
            6=investigator confirmed, 7=other documented.
        ts_unix_ms: Optional entry timestamp (Unix milliseconds). Omit it and
            the entry is stamped at signing time, which is what a normal
            correction wants. If you supply one, the kernel checks it against
            the audit ledger head and the enforcement clock; a timestamp at or
            before the ledger head, or beyond the allowed forward skew, is
            refused before reaching the EDC.
    """
    # -- fail-fast validation mirroring the wire contract -------------------
    segments = _require_item_path("item_oid", item_oid)
    if (
        not isinstance(reason_code, int)
        or isinstance(reason_code, bool)
        or reason_code not in _REASON_CODES
    ):
        raise ValueError(f"reason_code must be an integer 0-7, got {reason_code!r}")
    if not isinstance(new_value, str) or len(new_value) > _NEW_VALUE_MAX_LEN:
        raise ValueError(
            f"new_value must be a string of at most {_NEW_VALUE_MAX_LEN} chars"
        )
    if ts_unix_ms is not None and (
        not isinstance(ts_unix_ms, int) or isinstance(ts_unix_ms, bool) or ts_unix_ms <= 0
    ):
        raise ValueError(
            f"ts_unix_ms must be a positive Unix-ms integer when supplied, got {ts_unix_ms!r}"
        )

    if build_audit_entry is None:
        raise RuntimeError(
            "agent/audit_entry.py (harness signing pipeline, S4-A-30) is not "
            "importable — put the repo root on sys.path with the module "
            "present. actorId, tsUnixMs and sigEd25519 are never chosen by "
            "the LLM, so no write can be attempted without it."
        )

    # -- (a) read the item's current value first ----------------------------
    # The signed entry binds the old_value actually observed, so the context
    # read MUST happen before signing. A denial on the read is returned as-is:
    # nothing gets signed against an unobserved value.
    context = _request("GET", "/items/context", query={"itemOid": item_oid})
    if context.get("http_status") == 403:
        return json.dumps(context)
    current_value = context["current_value"]

    # -- (b) audit-entry provenance: harness-signed, never LLM-chosen -------
    # Segment mapping per the wire contract's path order
    # Study/Subject/Event/Form/ItemGroup/Item; item_oid here is the item's
    # own (leaf) OID, matching the decomposed audit-entry interface.
    audit = build_audit_entry(
        item_oid=segments[5],
        new_value=new_value,
        action=_ACTION_MODIFY,
        reason_code=reason_code,
        subject_key=segments[1],
        study_oid=segments[0],
        study_event_oid=segments[2],
        form_oid=segments[3],
        item_group_oid=segments[4],
        old_value=current_value,
        ts_unix_ms=ts_unix_ms,
    )

    # -- (c) the flat six-field write body (proxy denies unknown fields) ----
    result = _request(
        "POST",
        "/items/write",
        body={
            "itemOid": item_oid,
            "newValue": new_value,
            "actorId": audit["actorId"],
            "reasonCode": reason_code,
            "tsUnixMs": audit["tsUnixMs"],
            "sigEd25519": audit["sigEd25519"],
        },
    )
    # -- (d) the proxy's verdict response, verbatim-but-structured ----------
    # (call_id, verdict, forwarded, latency_us/elab_us/total_latency_us,
    # new_value_hash on 200; reason/lean_trace with http_status 403 on denial)
    return json.dumps(result)


# Exported tool list for Agent(tools=...) call sites.
BIJECT_TOOLS = [list_open_queries, get_item_context, write_item_correction]
