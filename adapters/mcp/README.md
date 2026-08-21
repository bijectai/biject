# adapters/mcp — biject-oc-mcp

A stdio MCP server (official python `mcp` SDK) exposing biject's
verification-proxy OpenClinica tool surface to **any MCP client** — Claude
Code, Codex, OpenCode, or anything else that speaks MCP over
stdio. This is the post-sprint MCP path (spec Workstream G, minus any
client-specific assumptions); the sprint demo itself deliberately uses plain
SDK function tools (`adapters/openai/tools.py`), and this shim changes no
enforcement property of that design.

**Every tool is a thin HTTPS call to the Rust verification proxy. This
process decides nothing.** The proxy checks a typed, signed audit entry
against the Lean kernel before anything reaches the EDC; a PROVED verdict
means the supplied structured parameters satisfy a kernel-checked predicate —
nothing more. Denials arrive as `REFUTED: <clause>` and are returned to the
model as data.

## Tools (exactly three)

| Tool | Params | Backing call |
| --- | --- | --- |
| `list_open_queries` | `study_oid` | `GET /queries/open?studyOid=` |
| `get_item_context` | `item_oid` (6-segment path) | `GET /items/context?itemOid=` |
| `write_item_correction` | `item_oid`, `new_value`, `reason_code` | read context, sign, `POST /items/write` |

An item is addressed by a six-segment OID path
`Study/Subject/Event/Form/ItemGroup/Item`; every segment matches
`[A-Za-z0-9_-]{1,64}`. `reason_code` is an integer enum 0–7, never free
text. No tool parameter is free-form input to a decision: the one string of
substance, `new_value`, is data written to the EDC — only its SHA-256 hash
ever reaches the kernel.

The write flow inside `write_item_correction`:

1. `GET /items/context` — observe the item's current value (nothing is ever
   signed against an unobserved value; a refusal here ends the flow).
2. `agent.audit_entry.build_audit_entry(...)` — the harness signing pipeline
   (S4-A-30) stamps `actorId` / `tsUnixMs` and signs the canonical digest
   with the Ed25519 key from the environment. The model never chooses any of
   the three.
3. `POST /items/write` with the flat six-field camelCase body (`itemOid`,
   `newValue`, `actorId`, `reasonCode`, `tsUnixMs`, `sigEd25519`); the proxy
   denies unknown fields.

A stale read (the EDC value changed between steps 1 and 3) makes the proxy's
recomputed digest mismatch the signature and the write is refuted — that is
optimistic concurrency working, not an error in the shim; call the tool again
to re-read and re-sign.

Timeouts: 30 s on reads, 120 s on the write POST. On a proxy 403 the tool
returns a structured result carrying the proxy's `reason` plus a truncated
`lean_trace` excerpt (advisory text, never a decision input) — never an
exception dump. Transport failures likewise come back as structured results
with an error kind.

## Configuration (environment)

| Variable | Required | Meaning |
| --- | --- | --- |
| `BIJECT_PROXY_URL` | yes (no default) | Base URL of the verification proxy. A missing value fails loudly; the shim never guesses an endpoint. |
| `BIJECT_PROXY_API_KEY` | yes | Sent as `X-Biject-Proxy-Key` on every call. Proxy auth only — **never** an EDC credential. |
| `BIJECT_AGENT_ID` | yes | Sent as `X-Biject-Agent-Id` on every call (the calling-agent identity; distinct from `actorId`). |

Signing-pipeline passthrough (consumed by `agent/audit_entry.py`, which the
write flow imports — see its docstring for the canonicalization contract):

| Variable | Required for writes | Meaning |
| --- | --- | --- |
| `AGENT_SIGNING_KEY` | yes | base64 of a 32-byte Ed25519 seed. Environment only, **never a tracked file**; it is never logged, echoed, or returned (§2B.3). |
| `ACTOR_ID` | yes | Harness identity stamped into the audit entry (e.g. `AGENT_RECONCILER_01`). Never chosen by the model. |
| `PYTHONPATH` | yes, unless installed differently | Must include this repo's root so `agent.audit_entry` is importable (the shim also finds it on its own when run from this checkout). |

Reads work without the signing variables; the write tool refuses before any
network call when the signing pipeline is unavailable.

## Client configuration example (generic MCP client)

Most MCP clients take a command/args/env stanza. JSON form:

```json
{
  "mcpServers": {
    "biject": {
      "command": "python",
      "args": ["-m", "biject_oc_mcp"],
      "env": {
        "BIJECT_PROXY_URL": "https://proxy.example.internal",
        "BIJECT_PROXY_API_KEY": "<proxy key>",
        "BIJECT_AGENT_ID": "AGENT_RECONCILER_01",
        "ACTOR_ID": "AGENT_RECONCILER_01",
        "AGENT_SIGNING_KEY": "<base64 32-byte Ed25519 seed>",
        "PYTHONPATH": "/path/to/biject"
      }
    }
  }
}
```

Run `python` with this directory on `sys.path` (or `pip install .` here, which
also installs a `biject-oc-mcp` console script usable as the `command`).

Claude Code equivalent:

```
claude mcp add biject \
  -e BIJECT_PROXY_URL=https://proxy.example.internal \
  -e BIJECT_PROXY_API_KEY=... -e BIJECT_AGENT_ID=AGENT_RECONCILER_01 \
  -e ACTOR_ID=AGENT_RECONCILER_01 -e AGENT_SIGNING_KEY=... \
  -e PYTHONPATH=/path/to/biject \
  -- python -m biject_oc_mcp
```

**Credential handling (§2B.3):** `AGENT_SIGNING_KEY` and
`BIJECT_PROXY_API_KEY` are credentials. If your client persists the `env`
block to a config file, that file must stay local and untracked. Prefer
clients that pass environment through rather than storing it. The
OpenClinica username/password exist only in the proxy's environment — this
shim never receives, needs, or accepts them.

## Toolset restriction is not enforcement

Giving an agent only these three tools constrains a *well-behaved* agent. It
does nothing against a jailbroken agent, a buggy tool implementation, or any
other process on the agent host that opens a raw socket to the EDC. The spec
(`biject-demo-completion-spec-v5.md` §5.2) names four layers; this shim is
layer 1 only:

1. **Toolset restriction** — necessary, insufficient. This package.
2. **Network isolation** — *the actual guarantee.* The agent host has no
   route to the EDC except the proxy (`infra/hetzner/firewall/`, verified by
   `verify_lockdown.sh`, §5.3).
3. **Credential isolation** — EDC credentials live only in the proxy's
   environment; this shim holds proxy auth only.
4. **Container the agent** — if shell-capable tooling must stay enabled,
   run the agent in a container whose only route out is the proxy.

Deploy this shim assuming layer 2 holds; do not present the tool list as a
security boundary.

## Claim boundary (EC-06 / spec §2.4)

PROVED means the supplied structured parameters satisfy a kernel-checked
predicate on the formalized action surface — it is not a compliance
determination, and this shim adds no claim of its own. Denials render as
`REFUTED: <clause>` and are passed through as data. The write tool's
description in `server.py` is fixed verbatim by the spec; do not edit it
without a spec change.

## Layout and status

```
adapters/mcp/
  biject_oc_mcp/
    __init__.py
    __main__.py     # python -m biject_oc_mcp
    server.py       # the three tools + HTTP layer + MCP wiring
  pyproject.toml
  test_server.py    # offline tests; the HTTP layer is mocked
  README.md
```

Written against the proxy wire contract (routes, headers, body shape,
verdict vocabulary `allowed | blocked | skipped`, latency fields
`latency_us` / `elab_us` / `total_latency_us`); the proxy is being aligned to
the same contract in parallel. This shim has not yet run against a deployed
proxy — the tests below are offline. Reads may return 403 until read
policies are compiled upstream; that is the gate failing closed, not a shim
defect.

## Tests

No network: the HTTP layer is replaced by a recorder, the signing pipeline by
a recording fake. Covered: per-route envelope (method, URL, auth headers,
timeouts), write-flow ordering (observe, then sign over the observed value,
then POST), the exact six-field write body, 403 passthrough with `lean_trace`
excerpting, structured transport-error results, fail-fast validation, and the
registration surface (exactly three tools, exact write description).

```
python -m venv .venv && .venv/bin/pip install mcp pytest cryptography
.venv/bin/python -m pytest test_server.py
```

## Dependency rule

`adapters/` is outside the core dependency graph (see `adapters/README.md`);
core code never imports from here. This package's own runtime import of
`agent/audit_entry.py` points the other way — adapter depending on the
harness signing pipeline — which is the allowed direction.
