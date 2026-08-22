"""Standalone transport tests for adapters/openai/tools.py (S4-D-21 rewrite).

No network, no OpenAI Agents SDK, no signing pipeline: ``agents`` is stubbed
in ``sys.modules`` before the module under test is imported (the stub's
``function_tool`` is an identity decorator, so the tools stay plain callables
here), ``urllib.request.urlopen`` is monkeypatched per-test, and
``build_audit_entry`` (agent/audit_entry.py, S4-A-30 — lands in parallel) is
monkeypatched on the tools module.

Run: pytest adapters/openai/test_tools.py
"""

from __future__ import annotations

import inspect
import io
import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Stub the OpenAI Agents SDK BEFORE importing the module under test, so the
# tests run standalone and the decorated tools remain plain functions.
# ---------------------------------------------------------------------------
_TRACING_CALLS = []

_agents_stub = types.ModuleType("agents")
_agents_stub.function_tool = lambda f: f  # identity decorator
_agents_stub.set_tracing_disabled = _TRACING_CALLS.append
sys.modules["agents"] = _agents_stub

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters.openai import tools  # noqa: E402  (needs the stub above)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

BASE = "https://proxy.example:8443"
PROXY_KEY = "test-proxy-key"
AGENT_ID = "agent-007"

ITEM_PATH = "S_BJT01/SS_1001/SE_VISIT1/F_VITALS_V1/IG_VITALS/I_WEIGHT_KG"
# urlencode percent-encodes '/' in query values; hardcoded so the test is
# byte-exact against the wire, not a mirror of the implementation.
ITEM_PATH_ENC = "S_BJT01%2FSS_1001%2FSE_VISIT1%2FF_VITALS_V1%2FIG_VITALS%2FI_WEIGHT_KG"

SIG = "A" * 86 + "=="
AUDIT = {"actorId": "svc.biject-agent", "tsUnixMs": 1755700000000, "sigEd25519": SIG}

ALLOWED_200 = {
    "call_id": "c-0001",
    "verdict": "allowed",
    "forwarded": True,
    "latency_us": 1200,
    "elab_us": 300,
    "total_latency_us": 1650,
    "new_value_hash": "ab" * 32,
}


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UrlopenRecorder:
    """Replaces urllib.request.urlopen; replays queued (status, payload)."""

    def __init__(self, events=None):
        self.requests = []  # list of (Request, timeout)
        self.queued = []  # list of (status, payload)
        self.events = events if events is not None else []

    def queue(self, payload, status=200):
        self.queued.append((status, payload))

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        self.events.append(("http", req.get_method(), req.full_url))
        assert self.queued, f"unexpected request: {req.get_method()} {req.full_url}"
        status, payload = self.queued.pop(0)
        if status == 200:
            return _FakeResponse(payload)
        raise urllib.error.HTTPError(
            req.full_url,
            status,
            "Forbidden",
            {},
            io.BytesIO(json.dumps(payload).encode("utf-8")),
        )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("BIJECT_PROXY_URL", BASE)
    monkeypatch.setenv("BIJECT_PROXY_API_KEY", PROXY_KEY)
    monkeypatch.setenv("BIJECT_AGENT_ID", AGENT_ID)


@pytest.fixture
def proxy(monkeypatch, env):
    recorder = UrlopenRecorder()
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    return recorder


def _assert_auth_headers(req):
    # urllib's Request stores header keys str.capitalize()-d; HTTP header
    # names are case-insensitive on the wire, so this is lookup mechanics,
    # not a contract change.
    assert req.get_header("X-Biject-Proxy-Key".capitalize()) == PROXY_KEY
    assert req.get_header("X-Biject-Agent-Id".capitalize()) == AGENT_ID


def _no_signer(monkeypatch, calls):
    def boom(**kwargs):
        calls.append(kwargs)
        raise AssertionError("build_audit_entry must not be called")

    monkeypatch.setattr(tools, "build_audit_entry", boom)


# ---------------------------------------------------------------------------
# Import-time behaviour
# ---------------------------------------------------------------------------

def test_tracing_disabled_at_import():
    assert _TRACING_CALLS == [True]


def test_biject_tools_exports_three_tools():
    assert tools.BIJECT_TOOLS == [
        tools.list_open_queries,
        tools.get_item_context,
        tools.write_item_correction,
    ]


def test_write_tool_description_is_the_ec06_string():
    exact = (
        "Write a corrected item value to the EDC. The call is gated by a "
        "kernel-checked audit-entry predicate; writes lacking a valid, "
        "non-backdated audit entry with a reason code are refused before "
        "reaching the EDC."
    )
    doc = inspect.cleandoc(tools.write_item_correction.__doc__)
    assert doc.split("\n\n")[0] == exact


# ---------------------------------------------------------------------------
# Envelope shape: GET /queries/open
# ---------------------------------------------------------------------------

def test_list_open_queries_envelope(proxy):
    proxy.queue({"queries": [{"id": 7}], "study_oid": "S_BJT01"})
    out = json.loads(tools.list_open_queries("S_BJT01"))

    assert len(proxy.requests) == 1
    req, timeout = proxy.requests[0]
    assert req.get_method() == "GET"
    assert req.full_url == f"{BASE}/queries/open?studyOid=S_BJT01"
    assert req.data is None
    assert req.get_header("Content-type") is None
    _assert_auth_headers(req)
    assert timeout == 30
    assert out == {"queries": [{"id": 7}], "study_oid": "S_BJT01"}


def test_trailing_slash_in_base_url_is_normalized(proxy, monkeypatch):
    monkeypatch.setenv("BIJECT_PROXY_URL", BASE + "/")
    proxy.queue({"queries": []})
    tools.list_open_queries("S_BJT01")
    assert proxy.requests[0][0].full_url == f"{BASE}/queries/open?studyOid=S_BJT01"


def test_list_open_queries_denial_passthrough(proxy):
    proxy.queue({"reason": "REFUTED: no read policy compiled"}, status=403)
    out = json.loads(tools.list_open_queries("S_BJT01"))
    assert out == {"http_status": 403, "reason": "REFUTED: no read policy compiled"}


# ---------------------------------------------------------------------------
# Envelope shape: GET /items/context
# ---------------------------------------------------------------------------

def test_get_item_context_envelope(proxy):
    payload = {
        "item_oid": ITEM_PATH,
        "current_value": "84.0",
        "current_value_hash": "cd" * 32,
    }
    proxy.queue(payload)
    out = json.loads(tools.get_item_context(ITEM_PATH))

    assert len(proxy.requests) == 1
    req, _ = proxy.requests[0]
    assert req.get_method() == "GET"
    assert req.full_url == f"{BASE}/items/context?itemOid={ITEM_PATH_ENC}"
    assert req.data is None
    _assert_auth_headers(req)
    assert out == payload


def test_get_item_context_denial_passthrough(proxy):
    proxy.queue({"reason": "REFUTED: no read policy compiled"}, status=403)
    out = json.loads(tools.get_item_context(ITEM_PATH))
    assert out == {"http_status": 403, "reason": "REFUTED: no read policy compiled"}


# ---------------------------------------------------------------------------
# Write flow: ordering, signing inputs, byte-exact POST body
# ---------------------------------------------------------------------------

def test_write_flow_order_signing_and_body(monkeypatch, env):
    events = []
    recorder = UrlopenRecorder(events)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    recorder.queue({"item_oid": ITEM_PATH, "current_value": "84.0",
                    "current_value_hash": "cd" * 32})
    recorder.queue(ALLOWED_200)

    def fake_build_audit_entry(**kwargs):
        events.append(("sign", kwargs))
        return dict(AUDIT)

    monkeypatch.setattr(tools, "build_audit_entry", fake_build_audit_entry)

    out = json.loads(tools.write_item_correction(ITEM_PATH, "86.0", 1))

    # Ordering: context read BEFORE signing BEFORE write.
    assert [e[0] if e[0] == "sign" else (e[1], e[2].split("?")[0]) for e in events] == [
        ("GET", f"{BASE}/items/context"),
        "sign",
        ("POST", f"{BASE}/items/write"),
    ]

    # Signing inputs: decomposed path segments + the OBSERVED old value.
    sign_kwargs = events[1][1]
    assert sign_kwargs == {
        "item_oid": "I_WEIGHT_KG",
        "new_value": "86.0",
        "action": 1,
        "reason_code": 1,
        "subject_key": "SS_1001",
        "study_oid": "S_BJT01",
        "study_event_oid": "SE_VISIT1",
        "form_oid": "F_VITALS_V1",
        "item_group_oid": "IG_VITALS",
        "old_value": "84.0",
        "ts_unix_ms": None,
    }

    # Context GET envelope.
    ctx_req, _ = recorder.requests[0]
    assert ctx_req.get_method() == "GET"
    assert ctx_req.full_url == f"{BASE}/items/context?itemOid={ITEM_PATH_ENC}"
    _assert_auth_headers(ctx_req)

    # Write POST envelope: byte-exact flat six-field camelCase body.
    write_req, _ = recorder.requests[1]
    assert write_req.get_method() == "POST"
    assert write_req.full_url == f"{BASE}/items/write"
    _assert_auth_headers(write_req)
    assert write_req.get_header("Content-type") == "application/json"
    expected_body = {
        "itemOid": ITEM_PATH,
        "newValue": "86.0",
        "actorId": "svc.biject-agent",
        "reasonCode": 1,
        "tsUnixMs": 1755700000000,
        "sigEd25519": SIG,
    }
    assert write_req.data == json.dumps(expected_body).encode("utf-8")
    assert set(json.loads(write_req.data)) == set(expected_body)  # no extra fields

    # Verdict response returned verbatim-but-structured.
    assert out == ALLOWED_200


def test_write_denial_passthrough(monkeypatch, proxy):
    proxy.queue({"item_oid": ITEM_PATH, "current_value": "84.0",
                 "current_value_hash": "cd" * 32})
    denial = {
        "reason": "REFUTED: tsUnixMs not after ledger head",
        "lean_trace": "AuditEntryValid: strict monotonicity clause failed",
    }
    proxy.queue(denial, status=403)
    monkeypatch.setattr(tools, "build_audit_entry", lambda **kw: dict(AUDIT))

    out = json.loads(tools.write_item_correction(ITEM_PATH, "86.0", 3))
    assert out == {"http_status": 403, **denial}
    assert len(proxy.requests) == 2  # context + write, nothing else


def test_write_context_denial_short_circuits_before_signing(monkeypatch, proxy):
    proxy.queue({"reason": "REFUTED: no read policy compiled"}, status=403)
    signer_calls = []
    _no_signer(monkeypatch, signer_calls)

    out = json.loads(tools.write_item_correction(ITEM_PATH, "86.0", 1))
    assert out == {"http_status": 403, "reason": "REFUTED: no read policy compiled"}
    assert signer_calls == []  # nothing signed against an unobserved value
    assert len(proxy.requests) == 1  # no POST /items/write


def test_write_boundary_new_value_4000_chars_accepted(monkeypatch, proxy):
    proxy.queue({"item_oid": ITEM_PATH, "current_value": "", "current_value_hash": "e" * 64})
    proxy.queue(ALLOWED_200)
    monkeypatch.setattr(tools, "build_audit_entry", lambda **kw: dict(AUDIT))
    out = json.loads(tools.write_item_correction(ITEM_PATH, "v" * 4000, 5))
    assert out == ALLOWED_200


# ---------------------------------------------------------------------------
# Validation rejections (never reach the network or the signer)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_study_oid", [
    "", "S BAD", "S_BAD!", "a/b", "x" * 65, "é_OID",
])
def test_list_open_queries_rejects_bad_oid(monkeypatch, proxy, bad_study_oid):
    signer_calls = []
    _no_signer(monkeypatch, signer_calls)
    with pytest.raises(ValueError):
        tools.list_open_queries(bad_study_oid)
    assert proxy.requests == []


@pytest.mark.parametrize("bad_path", [
    "",                                       # empty
    "S_BJT01",                                # 1 segment
    "S_BJT01/SS_1001/SE_V1/F_V1/IG_V",        # 5 segments
    ITEM_PATH + "/EXTRA",                     # 7 segments
    "S_BJT01//SE_V1/F_V1/IG_V/I_X",           # empty segment
    "S_BJT01/SS 1001/SE_V1/F_V1/IG_V/I_X",    # space in segment
    "S_BJT01/SS_1001/SE_V1/F_V1/IG_V/" + "x" * 65,  # segment too long
    "S_BJT01/SS_1001/SE_V1/F_V1/IG_V/I;X",    # punctuation
])
def test_item_path_rejections(monkeypatch, proxy, bad_path):
    signer_calls = []
    _no_signer(monkeypatch, signer_calls)
    with pytest.raises(ValueError):
        tools.get_item_context(bad_path)
    with pytest.raises(ValueError):
        tools.write_item_correction(bad_path, "86.0", 1)
    assert proxy.requests == []
    assert signer_calls == []


@pytest.mark.parametrize("bad_reason", [-1, 8, 100, "3", 1.0, None, True])
def test_write_rejects_bad_reason_code(monkeypatch, proxy, bad_reason):
    signer_calls = []
    _no_signer(monkeypatch, signer_calls)
    with pytest.raises(ValueError):
        tools.write_item_correction(ITEM_PATH, "86.0", bad_reason)
    assert proxy.requests == []
    assert signer_calls == []


@pytest.mark.parametrize("bad_value", ["v" * 4001, 42, None, b"bytes"])
def test_write_rejects_bad_new_value(monkeypatch, proxy, bad_value):
    signer_calls = []
    _no_signer(monkeypatch, signer_calls)
    with pytest.raises(ValueError):
        tools.write_item_correction(ITEM_PATH, bad_value, 1)
    assert proxy.requests == []
    assert signer_calls == []


# ---------------------------------------------------------------------------
# Environment handling: no defaults, clear failures, no network
# ---------------------------------------------------------------------------

def _clear_env(monkeypatch):
    for name in ("BIJECT_PROXY_URL", "BIJECT_PROXY_API_KEY", "BIJECT_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)


def test_missing_env_raises_runtime_error(monkeypatch):
    recorder = UrlopenRecorder()
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    _clear_env(monkeypatch)

    with pytest.raises(RuntimeError, match="BIJECT_PROXY_URL"):
        tools.list_open_queries("S_BJT01")

    monkeypatch.setenv("BIJECT_PROXY_URL", BASE)
    with pytest.raises(RuntimeError, match="BIJECT_PROXY_API_KEY"):
        tools.list_open_queries("S_BJT01")

    monkeypatch.setenv("BIJECT_PROXY_API_KEY", PROXY_KEY)
    with pytest.raises(RuntimeError, match="BIJECT_AGENT_ID"):
        tools.list_open_queries("S_BJT01")

    assert recorder.requests == []


def test_write_without_signing_pipeline_raises(monkeypatch, proxy):
    # build_audit_entry resolves to None while agent/audit_entry.py has not
    # landed; the write must fail before any network traffic.
    monkeypatch.setattr(tools, "build_audit_entry", None)
    with pytest.raises(RuntimeError, match="audit_entry"):
        tools.write_item_correction(ITEM_PATH, "86.0", 1)
    assert proxy.requests == []
