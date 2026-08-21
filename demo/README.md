# biject demo — running the agent

Operator-facing instructions for the two-pass demo: an OpenAI Agents SDK
agent resolves open data queries in OpenClinica, with every tool call routed
through the verification proxy. The prompt library (with expected behaviour
per prompt and the run log) is `demo/prompts.md`; the runner is
`agent/run_demo.py`.

Claim boundary, before anything else: **PROVED means the supplied structured
parameters satisfy a kernel-checked predicate** — nothing broader. Denials
render as `REFUTED: <clause>` (wire verdicts: `allowed | blocked | skipped`;
anything other than `allowed` is a denial). Keep every spoken and on-screen
description inside that boundary.

## Prerequisites

- Python 3.10+ on the agent host.
- `pip install openai-agents` (the Agents SDK; import name `agents`) plus
  `agent/requirements.txt` (`cryptography`, for the harness signing
  pipeline `agent/audit_entry.py`).
- A reachable verification proxy, and — for real runs — the live stack
  behind it (verifier, policies, OpenClinica, seeded study).

## Environment

All values live in env files on the host, never in the repo. No variable
below has an in-code default; the runner and the tools fail loudly when one
is missing rather than guessing.

| Variable | Meaning |
|---|---|
| `BIJECT_PROXY_URL` | Base URL of the verification proxy. The agent's ONLY route to the EDC. No default — a missing value is a hard error, never a direct-to-OC guess. |
| `BIJECT_PROXY_API_KEY` | Value for the `X-Biject-Proxy-Key` header (required on every proxy route except `GET /healthz` / `GET /readyz`). |
| `BIJECT_AGENT_ID` | Value for the `X-Biject-Agent-Id` header (required on the OpenClinica routes; attribution). |
| `AGENT_SIGNING_KEY` | Base64 of a 32-byte Ed25519 seed for the harness signing pipeline. Environment only — never a tracked file, never a log line, never any output surface. |
| `ACTOR_ID` | Harness identity stamped into audit entries (e.g. `AGENT_RECONCILER_01`). Supplied by the harness, never chosen by the model. |
| `OPENAI_API_KEY` | Hosted-provider credential for the Agents SDK. |
| `OPENAI_MODEL` | The pinned model string (see `demo/prompts.md` § Model pinning). No default; the runner refuses to start without it. |
| `OPENAI_AGENTS_DISABLE_TRACING=1` | Belt-and-braces: SDK tracing must stay OFF (PHI-adjacent payloads must not leave the host). The runner and `adapters/openai/tools.py` also disable it programmatically. |

OpenClinica credentials appear **nowhere** in this list, on purpose. They
exist only in the proxy's environment. Verify with a `grep -r` over the agent
host's home directory before the demo.

## Run

From the repo root (any cwd works — the runner bootstraps `sys.path`):

```bash
# 1. Toolset restriction check — run before EVERY demo session.
#    Prints the tool list; exits nonzero unless it is exactly the three
#    biject proxy tools with no shell/code/file/web-shaped names.
python3 agent/run_demo.py --verify-toolset

# 2. Operator REPL. Type prompts from demo/prompts.md; /quit to exit.
python3 agent/run_demo.py
```

## Network isolation (§5.3) — before any demo

Toolset restriction is necessary but **insufficient** — the actual
enforcement bound is the network layer. Before any demo run, execute the
lockdown acceptance test **from the agent host, as the agent's own user**:

```bash
infra/hetzner/firewall/verify_lockdown.sh
```

It must exit 0: the direct agent→OpenClinica probe (and direct Postgres
probe) must be **refused — tested, not assumed** — while the proxy-routed
health call succeeds (proving the refusal is policy, not a dead network).
Save the output: it becomes demo pass 2b, the live answer to "couldn't the
agent just call OpenClinica directly?". Never run the demo without a fresh
pass.

## Recovery moves (from the demo runbook)

| If | Then |
|---|---|
| Agent stalls in pass 1 | Use the H.1 **backup prompt** (`demo/prompts.md` §H.1). |
| Agent refuses in pass 2 | Use the H.2 **follow-up** (`demo/prompts.md` §H.2); if it still refuses, narrate honestly and drive the tool call from the harness — presented as harness-driven, never as the agent's own call. |
| A write fails for a non-kernel reason (SOAP fault) | Say so plainly. A SOAP fault is **NEVER** presented as a kernel verdict — not as `REFUTED`, not as any verdict at all. |
| Wall stops streaming | Continue in the OpenClinica UI; the data change is the substance, the wall is presentation. |
| Whole stack down | Reset script, then rerun. If unrecoverable, show the recorded rehearsal video and say it is a recording. |

Never present a recording as live. Never present a SOAP failure as a
`REFUTED` verdict. Both are the same category of error as claiming PROVED
means more than "the supplied structured parameters satisfy a kernel-checked
predicate".

## Known gaps (2026-08-21)

- No live stack exists yet: the H.3 recorded runs in `demo/prompts.md` are
  PENDING; reads are gated and currently denied until a read policy is
  compiled upstream. See `STATE.md` at the workspace root for the full
  reconciliation.
