#!/usr/bin/env python3
"""Minimal OpenAI Agents SDK demo runner for the biject → OpenClinica demo.

Workstream G/H runner. An operator types natural-language prompts (the
library lives in ``demo/prompts.md``); the agent acts through EXACTLY the
three proxy-routed tools exported by ``adapters.openai.tools.BIJECT_TOOLS``
— nothing else. Operator prompts are input to the MODEL only; they never
become verification parameters. The kernel sees only the structured fields
the adapter extracts and the harness signs (enum ints, hashes, validated
identifiers, timestamps). PROVED means the supplied structured parameters
satisfy a kernel-checked predicate — nothing broader; a denial renders as
``REFUTED: <clause>``.

Usage (from the repo root, or anywhere — sys.path is bootstrapped)::

    python3 agent/run_demo.py --verify-toolset   # pre-demo toolset check
    python3 agent/run_demo.py                    # operator REPL

Environment (see demo/README.md for the full table): OPENAI_MODEL (required,
no default), OPENAI_API_KEY, BIJECT_PROXY_URL, BIJECT_PROXY_API_KEY,
BIJECT_AGENT_ID, AGENT_SIGNING_KEY, ACTOR_ID.

Tracing is OFF: PHI-adjacent tool payloads must not leave the host via the
SDK's trace exporter. Disabled three ways — the env var below (set before
any SDK import), ``set_tracing_disabled(True)`` here, and again at import
time inside ``adapters/openai/tools.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Belt-and-braces tracing kill switch — MUST precede any `agents` import so
# the SDK reads it during its own initialization.
# ---------------------------------------------------------------------------
os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")

# ---------------------------------------------------------------------------
# sys.path bootstrap: repo root, so `adapters.openai.tools` and
# `agent.audit_entry` resolve no matter where this script is invoked from.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Toolset restriction check data (see verify_toolset()).
# ---------------------------------------------------------------------------
# The exact tool surface. Anything more or less than these three names fails.
EXPECTED_TOOL_NAMES = frozenset(
    {"list_open_queries", "get_item_context", "write_item_correction"}
)

# Name fragments that would suggest a shell/code/file/web capability leaked
# into the toolset. Redundant with the exact-set check above by construction,
# kept anyway: it documents WHAT the exact-set check is protecting against,
# and still fires if EXPECTED_TOOL_NAMES is ever edited carelessly.
FORBIDDEN_NAME_FRAGMENTS = (
    "shell", "bash", "terminal", "exec", "eval", "spawn", "subprocess",
    "command", "code", "python", "interpreter", "script",
    "file", "directory", "glob",
    "web", "http", "url", "browser", "fetch", "curl", "wget", "download",
    "upload", "search", "ssh", "sql",
)

# EC-06 discipline: states what the gate checks (a kernel-checked predicate
# over structured parameters) and never claims more. Includes the abstention
# guidance from the H.1 primary prompt so the agent abstains even when the
# operator forgets to ask for it.
INSTRUCTIONS = (
    "You are the data-reconciliation agent for a clinical study demo. You "
    "interact with the EDC only through the three provided tools, which all "
    "route through a verification proxy. Work through the study's open data "
    "queries: read each item's context, and when other data already in the "
    "study provides a reliable source for a correction, write the corrected "
    "value with the appropriate integer reason code. If you cannot find a "
    "reliable source for a correction in the study data, leave the query "
    "open and say why. "
    "Every write is gated: the proxy checks the structured parameters of the "
    "call against a kernel-checked audit-entry predicate before forwarding "
    "it. A PROVED result means exactly that the supplied structured "
    "parameters satisfy that predicate; a refusal is reported as REFUTED "
    "together with the failed clause. Report verdicts as they are — do not "
    "restate them as anything broader, and report refusals honestly instead "
    "of retrying with altered inputs. The audit entry's actor identity, "
    "timestamp, and signature are supplied by the harness signing pipeline, "
    "never by you. Finish by summarizing what you changed and what you left "
    "open, and why."
)


def _load_biject_tools():
    """Import the tool surface from adapters.openai.tools — the ONLY tool
    source this runner accepts. Fails loudly if the SDK is absent."""
    try:
        from adapters.openai.tools import BIJECT_TOOLS
    except ModuleNotFoundError as err:
        print(
            f"FATAL: cannot import adapters.openai.tools ({err}).\n"
            "Install the OpenAI Agents SDK (`pip install openai-agents`) and "
            f"check the repo root is intact: {_REPO_ROOT}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return BIJECT_TOOLS


def _tool_name(tool) -> str:
    return getattr(tool, "name", None) or getattr(tool, "__name__", "<unnamed>")


def verify_toolset(tools=None) -> int:
    """Pre-demo toolset restriction check. Returns a process exit code.

    This is the Agents-SDK equivalent of the old Hermes toolset-restriction
    check (spec §G.4 / §5.2 layer 1: `/tools` shows exactly the three biject
    tools; `terminal`, `code_execution`, `file`, `web` toolsets all absent).
    With the Agents SDK there is no external `/tools` listing to inspect, so
    this inspects the same fact at the same layer: the exact tool list the
    Agent would be constructed with. Layer 1 is necessary but insufficient —
    the actual bound is the network lockdown, verified separately by
    infra/hetzner/firewall/verify_lockdown.sh from the agent host.

    ``tools`` is an injection seam for tests only; the CLI path always loads
    from adapters.openai.tools.
    """
    if tools is None:
        tools = _load_biject_tools()

    names = [_tool_name(t) for t in tools]
    print(f"toolset ({len(names)} tools):")
    for tool in tools:
        desc = (getattr(tool, "description", "") or "").strip()
        first_line = desc.splitlines()[0] if desc else "(no description)"
        print(f"  - {_tool_name(tool)}: {first_line}")

    failures = []
    if len(names) != 3:
        failures.append(f"expected exactly 3 tools, found {len(names)}")
    if set(names) != EXPECTED_TOOL_NAMES:
        failures.append(
            f"tool names {sorted(names)} != expected {sorted(EXPECTED_TOOL_NAMES)}"
        )
    for name in names:
        lowered = name.lower()
        for fragment in FORBIDDEN_NAME_FRAGMENTS:
            if fragment in lowered:
                failures.append(
                    f"tool name {name!r} contains forbidden fragment "
                    f"{fragment!r} (suggests shell/code/file/web access)"
                )

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("OK: exactly the three biject proxy tools; no shell/code/file/web-shaped names.")
    return 0


async def _stream_turn(agent, items: list) -> list:
    """Run one operator turn, streaming output as it arrives. Returns the
    updated conversation input list for the next turn."""
    from agents import Runner

    result = Runner.run_streamed(agent, input=items)
    async for event in result.stream_events():
        event_type = getattr(event, "type", "")
        if event_type == "raw_response_event":
            data = getattr(event, "data", None)
            if getattr(data, "type", "") == "response.output_text.delta":
                print(getattr(data, "delta", ""), end="", flush=True)
        elif event_type == "run_item_stream_event":
            item = event.item
            item_type = getattr(item, "type", "")
            if item_type == "tool_call_item":
                raw = getattr(item, "raw_item", None)
                print(f"\n[tool call] {getattr(raw, 'name', '<tool>')}", flush=True)
            elif item_type == "tool_call_output_item":
                output = str(getattr(item, "output", ""))
                if len(output) > 2000:
                    output = output[:2000] + " …(truncated for display)"
                print(f"[tool result] {output}", flush=True)
    print(flush=True)
    return result.to_input_list()


async def _repl(agent) -> None:
    print(
        "biject demo REPL — type an operator prompt (library: demo/prompts.md).\n"
        "/quit to exit.",
        flush=True,
    )
    items: list = []
    while True:
        try:
            line = input("operator> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        line = line.strip()
        if not line:
            continue
        if line in {"/quit", "/exit", "/q"}:
            return
        items.append({"role": "user", "content": line})
        try:
            items = await _stream_turn(agent, items)
        except Exception as err:  # surface, keep the session alive
            print(
                f"\n[run error] {type(err).__name__}: {err}",
                file=sys.stderr,
                flush=True,
            )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "biject demo runner: an operator REPL over an OpenAI Agents SDK "
            "agent whose only tools are the three biject proxy tools."
        )
    )
    parser.add_argument(
        "--verify-toolset",
        action="store_true",
        help=(
            "print the tool list, assert it is exactly the three biject "
            "proxy tools with no shell/code/file/web-shaped names, and exit "
            "(nonzero on failure). Run before every demo session."
        ),
    )
    args = parser.parse_args(argv)

    if args.verify_toolset:
        raise SystemExit(verify_toolset())

    # Model pinning (demo/prompts.md § Model pinning): no default, ever.
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not model:
        print(
            "FATAL: OPENAI_MODEL is not set. Pin the exact model string "
            "recorded in demo/prompts.md; this runner deliberately has no "
            "default model.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    tools = _load_biject_tools()

    from agents import Agent, set_tracing_disabled

    set_tracing_disabled(True)  # third layer; env var + tools.py import cover the rest

    agent = Agent(
        name="biject-demo-reconciler",
        instructions=INSTRUCTIONS,
        model=model,
        tools=tools,
    )
    asyncio.run(_repl(agent))


if __name__ == "__main__":
    main()
