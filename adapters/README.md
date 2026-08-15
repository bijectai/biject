# adapters/

Framework-specific integrations that let third-party agent runtimes call biject's
verification proxy.

## Dependency rule (hard)

`adapters/` is **OUTSIDE the core dependency graph**. biject core must **never**
import from `adapters/` — not for types, not for constants, not "temporarily".
The dependency arrow points one way only:

```
adapters/*  --->  verification proxy (HTTP)  --->  OpenClinica
```

Adapters may carry their own third-party dependencies (e.g. `openai-agents`);
those dependencies must never leak into core's requirements.

## Status / public claim

The public claim for this directory is exactly:

> Reference integration for OpenAI Agents SDK, validated end to end; Bedrock
> and Foundry adapters written, not yet run.

- `adapters/openai/` — OpenAI Agents SDK function tools (`tools.py`). This is
  the reference integration exercised by the OpenClinica 3 demo.
- Bedrock and Foundry adapters exist in written form but have **not** been run;
  do not represent them as validated.

## Contract

Tool signatures mirror `contracts/tool_calls.json` (DRAFT v0 — pending the
S4-A-12 freeze; the frozen contract supersedes the draft). Adapters must not
add parameters, defaults, or free-text fields beyond the contract.
