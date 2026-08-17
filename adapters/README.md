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

| Adapter | Form | Status |
| --- | --- | --- |
| `adapters/openai/` | Agents SDK function tools (`tools.py`) | Reference integration, exercised by the OpenClinica 3 demo agent. |
| `adapters/bedrock/` | OpenAPI 3 spec for a Bedrock AgentCore Gateway target (`biject-proxy-tools.openapi.yaml`) + README | **WRITTEN, NOT YET RUN** — never attached to a gateway, never sent a request to a deployed proxy (S4-D-31). |
| `adapters/foundry/` | OpenAPI 3 spec for an Azure AI Foundry OpenAPI tool (`biject-proxy-tools.openapi.json`) + README | **WRITTEN, NOT YET RUN** — never imported into a Foundry project, never sent a request to a deployed proxy (S4-D-31). |

Wherever the Bedrock or Foundry adapters are referenced — README, deck, demo
narration — the words "written, not yet run" (or an equivalent) must travel
with them. Do not represent them as validated, exercised, or known-working.

The two OpenAPI documents are one spec in two renderings: the Foundry JSON is
generated from the Bedrock YAML and the two must not drift in paths or
schemas. Both point at the proxy's §8 routes (`POST /items/write`,
`GET /queries/open`) and byte-match its `deny_unknown_fields` request structs;
their server entry is a placeholder (`https://{demoDomain}`, `.invalid`
default) until S4-D-10 provisions the real host.

## Contract

Tool signatures mirror `contracts/tool_calls.json` (DRAFT v0 — pending the
S4-A-12 freeze; the frozen contract supersedes the draft) and, for the
Bedrock/Foundry specs, the proxy's own request structs, which are the
enforcement-side ground truth. Adapters must not add parameters, defaults, or
free-text fields beyond the contract.
