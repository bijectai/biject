# adapters/foundry — Azure AI Foundry OpenAPI tool

> **STATUS: WRITTEN, NOT YET RUN.** This spec has not been imported into any
> Azure AI Foundry project, no agent has carried it as a tool, and no request
> shaped by it has been sent to a deployed proxy (none exists yet — S4-D-10
> has not run on the host). Everything below describes how the attachment is
> *intended* to work, per Azure documentation read at authoring time; none of
> it has been observed. Ticket S4-D-31.

`biject-proxy-tools.openapi.json` is an OpenAPI 3.0.3 document exposing the
same two tools as the reference OpenAI adapter (`adapters/openai/tools.py`),
pointed at the Rust verification proxy's §8 routes:

| operationId | Route | What it is |
| --- | --- | --- |
| `openclinica_list_queries` | `GET /queries/open?studyOid=` | List open data-clarification queries. Reads are gated too. |
| `openclinica_write_item` | `POST /items/write` | Write one item correction — verified against the Lean kernel before anything reaches OpenClinica. |

It is generated from — and must never drift from —
`adapters/bedrock/biject-proxy-tools.openapi.yaml`: identical paths, schemas,
and responses; only the `info` block differs. It is kept as JSON because the
Foundry SDK examples load the spec with `json.load`. Field names and casing
byte-match the proxy's `deny_unknown_fields` structs
(`biject-proxy/src/extract.rs`, `src/lib.rs`); the proxy refuses bodies with
extra fields.

## How it WOULD be attached (not yet done)

1. **Server URL.** The spec's server is the placeholder
   `https://{demoDomain}` with an intentionally unresolvable `.invalid`
   default. The real proxy hostname lands with S4-D-10; substitute it before
   importing. Some importers do not expand OpenAPI server variables at all —
   plan on replacing the whole `servers[0].url` with the literal hostname as
   a pre-import step rather than relying on variable substitution.
2. **Register the tool** on a Foundry agent with the `azure-ai-agents` SDK:
   `OpenApiTool(name=..., spec=json.load(open("biject-proxy-tools.openapi.json")), auth=...)`,
   then pass `tool.definitions` when creating the agent. Foundry requires an
   `operationId` matching `^[a-zA-Z0-9_]+$`; both of ours are underscore-only
   by construction (they are the proxy's tool names).
3. **`X-Biject-Agent-Id` header injection.** Both routes deny 403 without
   this header. It is deliberately NOT an operation parameter — agent
   identity must never be a value the model fills in. The intended mechanism
   is a Foundry **custom-keys connection** carrying one key named
   `X-Biject-Agent-Id` with the agent id as its value, referenced from the
   tool via connection-based auth (`OpenApiConnectionAuthDetails`), which per
   the documentation sends each custom key as a request header. The agent id
   is an identity, not a secret; the connection mechanism is simply the
   documented way to pin a static outbound header. Whether it lands the
   header byte-exact is a first-run question.

## Known open problem: the signing pipeline

`actorId`, `tsUnixMs`, and `sigEd25519` must come from the harness signing
pipeline (ticket S4-A-30) — never from the model. A Foundry OpenAPI tool
surfaces all six body fields to the model, which means the model would have
to fabricate a signature. That fails kernel verification and the proxy
denies — **fail closed, so no unverified write can result** — but it also
means `openclinica_write_item` is not *functional* through this tool until a
signing injection point exists. Unlike Bedrock's gateway, Foundry's OpenAPI
tool offers no request-transform hook we know of, so the injection point
would have to sit in front of the proxy (or inside its trust boundary) —
an open design question, recorded, not solved.
`openclinica_list_queries` has no signed fields and does not have this
problem.

## What has NOT been established

- That Foundry accepts this document unmodified (schema-feature coverage —
  `additionalProperties: false`, `enum` on responses, `format: int64` — is
  unexercised).
- That the custom-keys connection lands `X-Biject-Agent-Id` byte-exact.
- That the tool schema Foundry derives keeps the closed patterns and enums
  intact for the model.
- Anything at all involving a live proxy or a live OpenClinica.

Each of these is a first-run checklist item, not a claim.
