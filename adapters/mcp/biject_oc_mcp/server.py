"""biject_oc_mcp.server — stdio MCP shim for the biject verification proxy.

Post-sprint MCP path (spec Workstream G, client-agnostic: any MCP client
works — Claude Code, Codex, OpenCode, ...). Every tool here is a thin HTTPS
call to the Rust verification proxy; this process decides nothing.

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
  new_value_hash}``; 403 carries a denial ``{reason, ...}``. Verdict
  vocabulary is ``allowed | blocked | skipped``; latency fields are passed
  through untouched.

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
   substance, ``new_value``, is DATA being written to the EDC, not reasoning —
   only its SHA-256 hash reaches the kernel (the proxy binds the hash into
   the audit entry; the raw value goes to the EDC alone).
2. The audit-entry provenance fields (``actorId``, ``tsUnixMs``,
   ``sigEd25519``) are NEVER chosen by the model. They come from the harness
   signing pipeline — ``agent/audit_entry.py`` (S4-A-30) — which this module
   calls inside the write flow, after reading the item's current value so the
   signed entry binds the observed ``old_value``.
3. This shim never holds EDC credentials. It authenticates to the proxy only;
   the OpenClinica service account exists solely in the proxy's environment
   (spec §5.2 layer 3).
4. Exposing only these three tools is ergonomics, not enforcement. The
   enforcement bound is at the network layer (spec §5.2 layer 2;
   ``infra/hetzner/firewall/``): the agent host has no route to the EDC
   except the proxy, whatever the tool list says.

ERRORS
======
A proxy 403 is returned as DATA — a structured result carrying the proxy's
``reason`` (rendered upstream in ``REFUTED: <clause>`` style) plus a
truncated ``lean_trace`` excerpt (advisory text, never a decision input) —
so the model can report WHY the call was refused. Transport failures and
non-403 HTTP errors also come back as structured results with an error kind,
never a stack trace. Configuration errors (missing env, missing signing
pipeline) raise, because a misconfigured shim must fail loudly, not quietly
degrade.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Annotated, Any

try:  # official python SDK >= 2.0
    from mcp.server.mcpserver import MCPServer
except ModuleNotFoundError:  # official python SDK 1.x — FastMCP, same surface
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[assignment]
from mcp.types import ToolAnnotations
from pydantic import Field

# ---------------------------------------------------------------------------
# Harness signing pipeline (S4-A-30) — agent/audit_entry.py at the repo root.
# ---------------------------------------------------------------------------
# The three provenance fields of a write are produced there, never here and
# never by the model. Import policy mirrors adapters/openai/tools.py: a
# *missing* module is tolerated at import time (the write tool then raises at
# call time — a write must be impossible to even attempt unsigned, and the
# proxy would refuse an unsigned entry anyway), but a *broken* module or a
# missing dependency (e.g. cryptography) fails loudly right here.


def _import_build_audit_entry():
    try:
        from agent.audit_entry import build_audit_entry as fn

        return fn
    except ModuleNotFoundError as exc:
        if exc.name not in ("agent", "agent.audit_entry"):
            raise  # a dependency of audit_entry is missing — fail loud
    # Convenience for in-repo launches: adapters/mcp/biject_oc_mcp/server.py
    # -> parents[3] is the repo root that holds agent/.
    repo_root = Path(__file__).resolve().parents[3]
    if (repo_root / "agent" / "audit_entry.py").is_file():
        sys.path.insert(0, str(repo_root))
        from agent.audit_entry import build_audit_entry as fn

        return fn
    return None


build_audit_entry = _import_build_audit_entry()

# ---------------------------------------------------------------------------
# Environment (no defaults — missing values fail loudly at call time)
# ---------------------------------------------------------------------------
_PROXY_URL_ENV = "BIJECT_PROXY_URL"
_PROXY_KEY_ENV = "BIJECT_PROXY_API_KEY"
_AGENT_ID_ENV = "BIJECT_AGENT_ID"

# ---------------------------------------------------------------------------
# Client-side mirrors of the contract's closed character classes.
# The proxy re-validates regardless (the proxy is the enforcement point; this
# is fail-fast ergonomics, not security).
# ---------------------------------------------------------------------------
_OID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_ITEM_PATH_SEGMENTS = 6  # Study/Subject/Event/Form/ItemGroup/Item

_REASON_CODES = frozenset(range(8))  # 0..7 integer enum, never free text
_NEW_VALUE_MAX_LEN = 4000

# This tool corrects an existing value; the audit-entry action enum is fixed
# to 1=modify and is not a model-facing parameter.
_ACTION_MODIFY = 1

# Timeouts per the wire contract: 30 s reads, 120 s writes.
_READ_TIMEOUT_S = 30
_WRITE_TIMEOUT_S = 120

# lean_trace is advisory text and can be long; return an excerpt, not a dump.
_LEAN_TRACE_EXCERPT_CHARS = 2000
_ERROR_DETAIL_MAX_CHARS = 300


def _require_env(name: str, why: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set. {why}")
    return value


def _proxy_url() -> str:
    """Resolve the proxy base URL from the environment (no default: a missing
    value must fail loudly, never fall back to a direct-to-EDC guess)."""
    url = _require_env(
        _PROXY_URL_ENV,
        "The agent may only talk to the EDC via the verification proxy; "
        "refusing to guess an endpoint.",
    )
    return url.rstrip("/")


def _auth_headers() -> dict:
    """Both auth headers, required on every route this module calls."""
    return {
        "X-Biject-Proxy-Key": _require_env(
            _PROXY_KEY_ENV,
            "Every proxy route requires the X-Biject-Proxy-Key header; set it "
            "in the MCP server's environment.",
        ),
        "X-Biject-Agent-Id": _require_env(
            _AGENT_ID_ENV,
            "OpenClinica proxy routes require the X-Biject-Agent-Id header; "
            "set it in the MCP server's environment.",
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
        raise ValueError(
            f"{name} must be a string item path, got {type(value).__name__}"
        )
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


# ---------------------------------------------------------------------------
# HTTP layer. _http_send is the single seam tests replace (no network in
# tests); everything above it — URL, headers, body, timeout — is under test.
# ---------------------------------------------------------------------------


def _http_send(
    method: str, url: str, headers: dict, data: bytes | None, timeout_s: int
) -> tuple[int, bytes]:
    """Perform one HTTP request; return (status_code, body_bytes).

    Certificate verification stays ON (default opener). If the proxy uses an
    internal CA, install it in the process trust store — do not disable
    verification here.
    """
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def _denial_result(body: bytes) -> dict:
    """Shape a proxy 403 into a structured tool result: the denial fields as
    data (``reason`` arrives rendered in ``REFUTED: <clause>`` style upstream)
    with any ``lean_trace`` reduced to an advisory excerpt."""
    try:
        denial = json.loads(body.decode("utf-8"))
        if not isinstance(denial, dict):
            denial = {"reason": "proxy denial body was not a JSON object"}
    except (ValueError, UnicodeDecodeError):
        denial = {"reason": "proxy denial body was not valid JSON"}
    result = {"http_status": 403, **denial}
    trace = result.get("lean_trace")
    if isinstance(trace, str):
        if len(trace) > _LEAN_TRACE_EXCERPT_CHARS:
            result["lean_trace"] = trace[:_LEAN_TRACE_EXCERPT_CHARS]
            result["lean_trace_truncated"] = True
        result["lean_trace_note"] = (
            "advisory excerpt of the kernel trace; explanatory text only, "
            "never a decision input"
        )
    return result


def _request(
    method: str,
    path: str,
    *,
    query: dict | None = None,
    body: dict | None = None,
    timeout_s: int = _READ_TIMEOUT_S,
) -> dict:
    """Send one request to the verification proxy and return its parsed JSON.

    A 403 denial is returned as data (see ``_denial_result``); transport
    failures and other HTTP errors are returned as structured error results —
    never an exception dump."""
    url = _proxy_url() + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = _auth_headers()
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        status, raw = _http_send(method, url, headers, data, timeout_s)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # No URL in the detail: a base URL is an output surface that can
        # carry a credential (userinfo).
        return {
            "error_kind": type(exc).__name__,
            "detail": str(exc)[:_ERROR_DETAIL_MAX_CHARS],
            "path": path,
        }
    if status == 403:
        return _denial_result(raw)
    if status != 200:
        return {
            "http_status": status,
            "error_kind": "proxy_error",
            "detail": raw.decode("utf-8", errors="replace")[:_ERROR_DETAIL_MAX_CHARS],
            "path": path,
        }
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {
            "http_status": status,
            "error_kind": "invalid_json",
            "detail": "proxy 200 body was not valid JSON",
            "path": path,
        }
    if not isinstance(parsed, dict):
        return {"result": parsed}
    return parsed


# ---------------------------------------------------------------------------
# Tools — exactly three, every one a thin call to the proxy.
# ---------------------------------------------------------------------------

_ITEM_OID_PARAM_DESC = (
    "Six-segment item path Study/Subject/Event/Form/ItemGroup/Item "
    '(e.g. "S_DEMO01/SS_1001/SE_VISIT1/F_VITALS_V1/IG_VITALS/I_WEIGHT_KG"). '
    "Each segment is letters, digits, '_' and '-' only, at most 64 characters."
)


def list_open_queries(
    study_oid: Annotated[
        str,
        Field(
            description=(
                "EDC study OID — letters, digits, '_' and '-' only, at most 64 "
                'characters (e.g. "S_DEMO01"). No other characters are accepted.'
            )
        ),
    ],
) -> dict[str, Any]:
    """List the open data-clarification queries for a study."""
    _require_oid_segment("study_oid", study_oid)
    return _request(
        "GET",
        "/queries/open",
        query={"studyOid": study_oid},
        timeout_s=_READ_TIMEOUT_S,
    )


def get_item_context(
    item_oid: Annotated[str, Field(description=_ITEM_OID_PARAM_DESC)],
) -> dict[str, Any]:
    """Read the current value and context of a single EDC item."""
    _require_item_path("item_oid", item_oid)
    return _request(
        "GET",
        "/items/context",
        query={"itemOid": item_oid},
        timeout_s=_READ_TIMEOUT_S,
    )


def write_item_correction(
    item_oid: Annotated[str, Field(description=_ITEM_OID_PARAM_DESC)],
    new_value: Annotated[
        str,
        Field(
            description=(
                "The corrected value to store (max 4000 chars). This is data "
                "being written to the EDC, not reasoning — the kernel only "
                "ever sees its SHA-256 hash."
            )
        ),
    ],
    reason_code: Annotated[
        int,
        Field(
            description=(
                "Integer reason enum (never free text): 0=source data "
                "confirmed, 1=transcription error, 2=unit correction, "
                "3=decimal shift, 4=cross-field reconciliation, 5=missing "
                "value completion, 6=investigator confirmed, 7=other "
                "documented."
            )
        ),
    ],
    ts_unix_ms: Annotated[
        int | None,
        Field(
            description=(
                "Optional entry timestamp (Unix milliseconds). Omit it and "
                "the entry is stamped at signing time. If supplied, the "
                "kernel checks it against the audit ledger head and the "
                "enforcement clock; a timestamp at or before the ledger "
                "head, or beyond the allowed forward skew, is refused "
                "before reaching the EDC."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """The write flow: read current value, sign the audit entry, POST."""
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
        not isinstance(ts_unix_ms, int)
        or isinstance(ts_unix_ms, bool)
        or ts_unix_ms <= 0
    ):
        raise ValueError(
            f"ts_unix_ms must be a positive Unix-ms integer when supplied, got {ts_unix_ms!r}"
        )

    # Checked before any network traffic: a write must be impossible to even
    # attempt unsigned. actorId, tsUnixMs and sigEd25519 are never chosen by
    # the model, so without the signing pipeline there is nothing to send.
    if build_audit_entry is None:
        raise RuntimeError(
            "agent/audit_entry.py (harness signing pipeline, S4-A-30) is not "
            "importable — put the repo root on sys.path (PYTHONPATH) with the "
            "module present."
        )

    # -- (a) read the item's current value first ----------------------------
    # The signed entry binds the old_value actually observed, so the context
    # read MUST happen before signing. A refusal or failure on the read is
    # returned as-is: nothing gets signed against an unobserved value.
    context = _request(
        "GET",
        "/items/context",
        query={"itemOid": item_oid},
        timeout_s=_READ_TIMEOUT_S,
    )
    if context.get("http_status") == 403 or "error_kind" in context:
        return context
    if "current_value" not in context:
        return {
            "error_kind": "context_missing_current_value",
            "detail": (
                "the context read returned no current_value; refusing to sign "
                "against an unobserved old value"
            ),
            "path": "/items/context",
        }
    current_value = context["current_value"]

    # -- (b) audit-entry provenance: harness-signed, never model-chosen -----
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
    return _request(
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
        timeout_s=_WRITE_TIMEOUT_S,
    )
    # The proxy's verdict response comes back verbatim-but-structured:
    # call_id, verdict (allowed|blocked|skipped), forwarded,
    # latency_us/elab_us/total_latency_us, new_value_hash on 200;
    # reason (+ lean_trace excerpt) with http_status 403 on denial.


# ---------------------------------------------------------------------------
# MCP server wiring (stdio). Descriptions are passed explicitly so the
# user-visible strings are exact — §2.4 / EC-06 claim discipline: gating is
# described as a kernel-checked predicate over the supplied structured
# parameters; refusals are refusals, never a compliance claim.
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "biject_oc_mcp",
    instructions=(
        "Tools for resolving open data queries in an EDC through the biject "
        "verification proxy. Reads and writes are gated: the proxy checks a "
        "kernel-checked predicate over typed parameters and may refuse a call "
        "(the refusal reason is returned as data). Use list_open_queries to "
        "find work, get_item_context to observe an item's current value, and "
        "write_item_correction to submit a corrected value."
    ),
)

mcp.tool(
    name="list_open_queries",
    description=(
        "List the open data-clarification queries (discrepancy notes) for a "
        "study in the EDC. This is a gated read through the verification "
        "proxy; it may be refused (403) until a read policy is compiled "
        "upstream, in which case the refusal reason is returned."
    ),
    annotations=ToolAnnotations(
        title="List Open EDC Queries",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)(list_open_queries)

mcp.tool(
    name="get_item_context",
    description=(
        "Read the current value and context of a single EDC item, addressed "
        "by its six-segment OID path. This is a gated read through the "
        "verification proxy; a refusal (403) returns the reason instead of "
        "the value."
    ),
    annotations=ToolAnnotations(
        title="Get EDC Item Context",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)(get_item_context)

# The write description is fixed verbatim by the spec (§G.2, §2.4) — do not
# edit it without a spec change.
WRITE_TOOL_DESCRIPTION = (
    "Write a corrected item value to the EDC. The call is gated by a "
    "kernel-checked audit-entry predicate; writes lacking a valid, "
    "non-backdated audit entry with a reason code are refused before "
    "reaching the EDC."
)

mcp.tool(
    name="write_item_correction",
    description=WRITE_TOOL_DESCRIPTION,
    annotations=ToolAnnotations(
        title="Write EDC Item Correction",
        readOnlyHint=False,
        # The prior value is preserved in the EDC's own audit history; a
        # correction replaces, it does not erase.
        destructiveHint=False,
        # Each call signs a fresh audit entry; a replayed entry is refused.
        idempotentHint=False,
        openWorldHint=True,
    ),
)(write_item_correction)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
