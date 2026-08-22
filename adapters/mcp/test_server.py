"""Offline tests for biject_oc_mcp — the HTTP layer is mocked, no network.

What is under test, per route:
* envelope — method, URL, query encoding, auth headers, Content-Type, timeout;
* write-flow ordering — context read, THEN sign, THEN POST, with the observed
  old value bound into the signed entry;
* 403 passthrough — denials come back as structured data (reason + lean_trace
  excerpt), never an exception;
* fail-fast validation — nothing outside the contract's closed classes ever
  leaves the process.

``_http_send`` is the single seam replaced (a recorder returning scripted
(status, body) pairs); ``build_audit_entry`` is replaced by a recording fake —
the signing pipeline has its own tests in ``agent/test_audit_entry.py``.

Run:  pytest test_server.py
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse

import pytest

import biject_oc_mcp.server as server

BASE = "https://proxy.test.invalid"
STUDY = "S_DEMO01"
ITEM_PATH = "S_DEMO01/SS_1001/SE_VISIT1/F_VITALS_V1/IG_VITALS/I_WEIGHT_KG"

AUTH_HEADERS = {
    "X-Biject-Proxy-Key": "test-proxy-key",
    "X-Biject-Agent-Id": "AGENT_TEST_01",
}

WRITE_OK = {
    "call_id": "c-0001",
    "verdict": "allowed",
    "forwarded": True,
    "latency_us": 812,
    "elab_us": 204,
    "total_latency_us": 1420,
    "new_value_hash": "cd" * 32,
}

FAKE_AUDIT = {
    "actorId": "AGENT_TEST_01",
    "tsUnixMs": 1755750000123,
    "sigEd25519": "A" * 86 + "==",
}


@pytest.fixture(autouse=True)
def proxy_env(monkeypatch):
    monkeypatch.setenv("BIJECT_PROXY_URL", BASE)
    monkeypatch.setenv("BIJECT_PROXY_API_KEY", AUTH_HEADERS["X-Biject-Proxy-Key"])
    monkeypatch.setenv("BIJECT_AGENT_ID", AUTH_HEADERS["X-Biject-Agent-Id"])


class HttpRecorder:
    """Scripted stand-in for server._http_send. Records every call; pops one
    scripted (status, body_bytes) per call, or raises it if it is an
    exception instance."""

    def __init__(self, responses=(), events=None):
        self.calls = []
        self.responses = list(responses)
        self.events = events if events is not None else []

    def __call__(self, method, url, headers, data, timeout_s):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "data": data,
                "timeout_s": timeout_s,
            }
        )
        self.events.append(("http", method, url.split("?")[0]))
        assert self.responses, f"unexpected HTTP call: {method} {url}"
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        status, body = resp
        return status, json.dumps(body).encode() if isinstance(body, dict) else body


def _install(monkeypatch, recorder, sign=None):
    monkeypatch.setattr(server, "_http_send", recorder)
    if sign is not None:
        monkeypatch.setattr(server, "build_audit_entry", sign)


def _make_sign(events, record):
    def _fake(**kwargs):
        events.append(("sign",))
        record.update(kwargs)
        return dict(FAKE_AUDIT)

    return _fake


# ---------------------------------------------------------------------------
# Envelope per route
# ---------------------------------------------------------------------------


def test_list_open_queries_envelope(monkeypatch):
    rec = HttpRecorder([(200, {"queries": [], "study_oid": STUDY})])
    _install(monkeypatch, rec)
    result = server.list_open_queries(STUDY)
    assert result == {"queries": [], "study_oid": STUDY}
    (call,) = rec.calls
    assert call["method"] == "GET"
    assert call["url"] == f"{BASE}/queries/open?studyOid={STUDY}"
    assert call["data"] is None
    assert call["timeout_s"] == 30
    for name, value in AUTH_HEADERS.items():
        assert call["headers"][name] == value
    assert "Content-Type" not in call["headers"]


def test_get_item_context_envelope(monkeypatch):
    rec = HttpRecorder([(200, {"item_oid": ITEM_PATH, "current_value": "181"})])
    _install(monkeypatch, rec)
    result = server.get_item_context(ITEM_PATH)
    assert result["current_value"] == "181"
    (call,) = rec.calls
    assert call["method"] == "GET"
    expected_qs = urllib.parse.urlencode({"itemOid": ITEM_PATH})
    assert call["url"] == f"{BASE}/items/context?{expected_qs}"
    assert call["timeout_s"] == 30
    for name, value in AUTH_HEADERS.items():
        assert call["headers"][name] == value


def test_trailing_slash_on_base_url_is_stripped(monkeypatch):
    monkeypatch.setenv("BIJECT_PROXY_URL", BASE + "/")
    rec = HttpRecorder([(200, {"queries": []})])
    _install(monkeypatch, rec)
    server.list_open_queries(STUDY)
    assert rec.calls[0]["url"].startswith(f"{BASE}/queries/open")


# ---------------------------------------------------------------------------
# Write flow: ordering, segment mapping, body shape, timeouts
# ---------------------------------------------------------------------------


def test_write_flow_ordering_and_body(monkeypatch):
    events = []
    signed = {}
    rec = HttpRecorder(
        [
            (200, {"item_oid": ITEM_PATH, "current_value": "181", "current_value_hash": "ab" * 32}),
            (200, WRITE_OK),
        ],
        events=events,
    )
    _install(monkeypatch, rec, sign=_make_sign(events, signed))

    result = server.write_item_correction(ITEM_PATH, "81", 3)

    # Ordering: observe, then sign over the observed value, then POST.
    assert events == [
        ("http", "GET", f"{BASE}/items/context"),
        ("sign",),
        ("http", "POST", f"{BASE}/items/write"),
    ]

    # The signed entry binds the six path segments and the OBSERVED old value.
    assert signed == {
        "item_oid": "I_WEIGHT_KG",
        "new_value": "81",
        "action": 1,
        "reason_code": 3,
        "subject_key": "SS_1001",
        "study_oid": "S_DEMO01",
        "study_event_oid": "SE_VISIT1",
        "form_oid": "F_VITALS_V1",
        "item_group_oid": "IG_VITALS",
        "old_value": "181",
            "ts_unix_ms": None,
    }

    # The write body is EXACTLY the six camelCase fields, nothing else.
    post = rec.calls[1]
    body = json.loads(post["data"])
    assert body == {
        "itemOid": ITEM_PATH,
        "newValue": "81",
        "actorId": FAKE_AUDIT["actorId"],
        "reasonCode": 3,
        "tsUnixMs": FAKE_AUDIT["tsUnixMs"],
        "sigEd25519": FAKE_AUDIT["sigEd25519"],
    }
    assert post["headers"]["Content-Type"] == "application/json"
    for name, value in AUTH_HEADERS.items():
        assert post["headers"][name] == value

    # Timeouts per the contract: 30 s for the read, 120 s for the write.
    assert rec.calls[0]["timeout_s"] == 30
    assert post["timeout_s"] == 120

    # The proxy's verdict response passes through untouched — wire vocabulary
    # (allowed|blocked|skipped, latency_us/elab_us/total_latency_us) intact.
    assert result == WRITE_OK


def test_write_denied_context_read_stops_the_flow(monkeypatch):
    events = []
    rec = HttpRecorder(
        [(403, {"reason": "REFUTED: skipped — no compiled policy for read"})],
        events=events,
    )
    signed = {}
    _install(monkeypatch, rec, sign=_make_sign(events, signed))

    result = server.write_item_correction(ITEM_PATH, "81", 3)

    assert result["http_status"] == 403
    assert result["reason"].startswith("REFUTED:")
    # Nothing was signed and nothing was written.
    assert ("sign",) not in events
    assert len(rec.calls) == 1
    assert signed == {}


def test_write_without_signing_pipeline_makes_no_network_call(monkeypatch):
    rec = HttpRecorder([])
    _install(monkeypatch, rec)
    monkeypatch.setattr(server, "build_audit_entry", None)
    with pytest.raises(RuntimeError, match="audit_entry"):
        server.write_item_correction(ITEM_PATH, "81", 3)
    assert rec.calls == []


def test_write_context_missing_current_value_refuses_to_sign(monkeypatch):
    events = []
    rec = HttpRecorder([(200, {"item_oid": ITEM_PATH})], events=events)
    signed = {}
    _install(monkeypatch, rec, sign=_make_sign(events, signed))
    result = server.write_item_correction(ITEM_PATH, "81", 3)
    assert result["error_kind"] == "context_missing_current_value"
    assert ("sign",) not in events
    assert len(rec.calls) == 1


# ---------------------------------------------------------------------------
# 403 passthrough as structured data
# ---------------------------------------------------------------------------


def test_read_denial_is_returned_as_data(monkeypatch):
    denial = {
        "reason": "REFUTED: skipped — no compiled policy for openclinica_list_queries",
        "call_id": "c-0002",
    }
    rec = HttpRecorder([(403, denial)])
    _install(monkeypatch, rec)
    result = server.list_open_queries(STUDY)
    assert result["http_status"] == 403
    assert result["reason"] == denial["reason"]
    assert result["call_id"] == "c-0002"


def test_write_denial_passthrough_with_lean_trace_excerpt(monkeypatch):
    long_trace = "clause notBackdated: tsUnixMs 1 <= ledgerHeadTsMs 2\n" * 200
    assert len(long_trace) > 2000
    rec = HttpRecorder(
        [
            (200, {"current_value": "181"}),
            (403, {"reason": "REFUTED: notBackdated", "lean_trace": long_trace}),
        ]
    )
    _install(monkeypatch, rec, sign=_make_sign([], {}))

    result = server.write_item_correction(ITEM_PATH, "81", 3)

    assert result["http_status"] == 403
    assert result["reason"] == "REFUTED: notBackdated"
    assert result["lean_trace"] == long_trace[:2000]
    assert result["lean_trace_truncated"] is True
    assert "advisory" in result["lean_trace_note"]


def test_short_lean_trace_is_not_truncated(monkeypatch):
    rec = HttpRecorder([(403, {"reason": "REFUTED: sigOk", "lean_trace": "short"})])
    _install(monkeypatch, rec)
    result = server.get_item_context(ITEM_PATH)
    assert result["lean_trace"] == "short"
    assert "lean_trace_truncated" not in result


def test_non_json_denial_body_still_returns_data(monkeypatch):
    rec = HttpRecorder([(403, b"<html>Forbidden</html>")])
    _install(monkeypatch, rec)
    result = server.list_open_queries(STUDY)
    assert result["http_status"] == 403
    assert "reason" in result


# ---------------------------------------------------------------------------
# Other failures: structured results, never an exception dump
# ---------------------------------------------------------------------------


def test_non_403_http_error_is_structured(monkeypatch):
    rec = HttpRecorder([(500, b"X" * 5000)])
    _install(monkeypatch, rec)
    result = server.list_open_queries(STUDY)
    assert result["http_status"] == 500
    assert result["error_kind"] == "proxy_error"
    assert len(result["detail"]) <= 300


def test_transport_error_is_structured_and_leaks_no_url(monkeypatch):
    rec = HttpRecorder([urllib.error.URLError("connection refused")])
    _install(monkeypatch, rec)
    result = server.get_item_context(ITEM_PATH)
    assert result["error_kind"] == "URLError"
    assert "connection refused" in result["detail"]
    # The base URL is an output surface that could carry a credential.
    assert "proxy.test.invalid" not in json.dumps(result)


def test_non_json_200_body_is_structured(monkeypatch):
    rec = HttpRecorder([(200, b"not json")])
    _install(monkeypatch, rec)
    result = server.list_open_queries(STUDY)
    assert result["error_kind"] == "invalid_json"


# ---------------------------------------------------------------------------
# Fail-fast validation (client-side mirror of the contract's closed classes)
# ---------------------------------------------------------------------------


def test_invalid_study_oid_rejected_before_any_call(monkeypatch):
    rec = HttpRecorder([])
    _install(monkeypatch, rec)
    with pytest.raises(ValueError, match="OID segment"):
        server.list_open_queries("please list the queries")
    assert rec.calls == []


@pytest.mark.parametrize(
    "bad_path",
    [
        "S_DEMO01/SS_1001/SE_VISIT1/F_VITALS_V1/IG_VITALS",  # 5 segments
        "S_DEMO01/SS 1001/SE_VISIT1/F_VITALS_V1/IG_VITALS/I_X",  # space
        "S_DEMO01//SE_VISIT1/F_VITALS_V1/IG_VITALS/I_X",  # empty segment
        "a/b/c/d/e/" + "x" * 65,  # over-long segment
    ],
)
def test_invalid_item_path_rejected(monkeypatch, bad_path):
    rec = HttpRecorder([])
    _install(monkeypatch, rec)
    with pytest.raises(ValueError):
        server.get_item_context(bad_path)
    assert rec.calls == []


@pytest.mark.parametrize("bad_code", [-1, 8, True, "3"])
def test_invalid_reason_code_rejected(monkeypatch, bad_code):
    rec = HttpRecorder([])
    _install(monkeypatch, rec, sign=_make_sign([], {}))
    with pytest.raises(ValueError, match="reason_code"):
        server.write_item_correction(ITEM_PATH, "81", bad_code)
    assert rec.calls == []


def test_overlong_new_value_rejected(monkeypatch):
    rec = HttpRecorder([])
    _install(monkeypatch, rec, sign=_make_sign([], {}))
    with pytest.raises(ValueError, match="new_value"):
        server.write_item_correction(ITEM_PATH, "x" * 4001, 3)
    assert rec.calls == []


def test_missing_proxy_url_fails_loudly(monkeypatch):
    rec = HttpRecorder([])
    _install(monkeypatch, rec)
    monkeypatch.delenv("BIJECT_PROXY_URL")
    with pytest.raises(RuntimeError, match="BIJECT_PROXY_URL"):
        server.list_open_queries(STUDY)
    assert rec.calls == []


def test_missing_agent_id_fails_loudly(monkeypatch):
    rec = HttpRecorder([])
    _install(monkeypatch, rec)
    monkeypatch.setenv("BIJECT_AGENT_ID", "")
    with pytest.raises(RuntimeError, match="BIJECT_AGENT_ID"):
        server.get_item_context(ITEM_PATH)
    assert rec.calls == []


# ---------------------------------------------------------------------------
# MCP registration: exactly three tools, exact write description
# ---------------------------------------------------------------------------

# Fixed verbatim by the spec (§G.2 / §2.4 claim discipline). If this test
# fails, the description drifted — fix the code, not the test.
EXPECTED_WRITE_DESCRIPTION = (
    "Write a corrected item value to the EDC. The call is gated by a "
    "kernel-checked audit-entry predicate; writes lacking a valid, "
    "non-backdated audit entry with a reason code are refused before "
    "reaching the EDC."
)


def _listed_tools():
    return asyncio.run(server.mcp.list_tools())


def _hint(annotations, name_snake, name_camel):
    value = getattr(annotations, name_snake, None)
    if value is None:
        value = getattr(annotations, name_camel, None)
    return value


def test_exactly_three_tools_registered():
    tools = _listed_tools()
    assert sorted(t.name for t in tools) == [
        "get_item_context",
        "list_open_queries",
        "write_item_correction",
    ]


def test_write_tool_description_is_exact():
    tools = {t.name: t for t in _listed_tools()}
    assert tools["write_item_correction"].description == EXPECTED_WRITE_DESCRIPTION


def test_tool_annotations_reflect_read_write_split():
    tools = {t.name: t for t in _listed_tools()}
    for name in ("list_open_queries", "get_item_context"):
        assert _hint(tools[name].annotations, "read_only_hint", "readOnlyHint") is True
    write = tools["write_item_correction"].annotations
    assert _hint(write, "read_only_hint", "readOnlyHint") is False
    assert _hint(write, "idempotent_hint", "idempotentHint") is False
