# adapters/bedrock — Bedrock AgentCore Gateway OpenAPI target

> **STATUS: WRITTEN, NOT YET RUN.** No AgentCore Gateway has been created from
> this spec, no target has been attached, and no request shaped by it has been
> sent to a deployed proxy (none exists yet — S4-D-10 has not run on the host).
> Everything below describes how the attachment is *intended* to work, per AWS
> documentation read at authoring time; none of it has been observed. Ticket
> S4-D-31.

`biject-proxy-tools.openapi.yaml` is an OpenAPI 3.0.3 document exposing the
same two tools as the reference OpenAI adapter (`adapters/openai/tools.py`),
pointed at the Rust verification proxy's §8 routes:

| operationId | Route | What it is |
| --- | --- | --- |
| `openclinica_list_queries` | `GET /queries/open?studyOid=` | List open data-clarification queries. Reads are gated too. |
| `openclinica_write_item` | `POST /items/write` | Write one item correction — verified against the Lean kernel before anything reaches OpenClinica. |

The request/response shapes are authored directly from the proxy source
(`biject-proxy/src/extract.rs`, `src/lib.rs`) so field names and casing
byte-match its `deny_unknown_fields` structs. The proxy refuses bodies with
extra fields, so the gateway must forward the tool arguments as-is — no
envelope of its own.

## How it WOULD be attached (not yet done)

1. **Server URL.** The spec's server is the placeholder
   `https://{demoDomain}` with an intentionally unresolvable `.invalid`
   default. Before creating the target, substitute the real proxy hostname —
   it lands with S4-D-10 (host provisioning). Do not invent one.
2. **Create the gateway** (once per environment):
   `aws bedrock-agentcore-control create-gateway ...` with the MCP protocol
   front and whatever inbound authorizer the agent host uses.
3. **Create the OpenAPI target** on that gateway with
   `create-gateway-target`, target configuration
   `mcp.openApiSchema` (inline payload or S3 URI pointing at this file).
   AgentCore derives one tool per operation from `operationId`, which is why
   the two operationIds are exactly the proxy's tool names
   (`openclinica_write_item`, `openclinica_list_queries`).
4. **`X-Biject-Agent-Id` header injection.** Both routes deny 403 without
   this header. It is deliberately NOT an operation parameter — agent
   identity must never be a value the model fills in. The intended mechanism
   is an AgentCore **API-key credential provider** attached to the target
   with `credentialLocation: HEADER` and
   `credentialParameterName: X-Biject-Agent-Id`, storing the agent id as the
   "key". The agent id is an identity, not a secret, but the credential
   provider is the documented way to pin a static outbound header on a
   target; whether it behaves as documented here is exactly the kind of thing
   the first run has to establish.

## Known open problem: the signing pipeline

`actorId`, `tsUnixMs`, and `sigEd25519` must come from the harness signing
pipeline (ticket S4-A-30) — never from the model. A gateway target as
specified here surfaces all six body fields to the model, which means the
model would have to fabricate a signature. That fails kernel verification and
the proxy denies — **fail closed, so no unverified write can result** — but it
also means `openclinica_write_item` is not *functional* through this target
until a signing injection point exists (candidate: a gateway
interceptor/transform in front of the proxy, or signing moved into the proxy's
trust boundary). `openclinica_list_queries` has no signed fields and does not
have this problem. Recording the gap here rather than papering over it mirrors
the hard-stub decision in `adapters/openai/tools.py` (`build_audit_entry`).

## What has NOT been established

- That AgentCore Gateway accepts this document unmodified (schema-feature
  coverage — `additionalProperties: false`, server variables, `enum` on
  responses — is unexercised).
- That header injection via a credential provider lands the header byte-exact.
- That the tool schemas AgentCore derives keep the closed patterns and enums
  intact for the model.
- Anything at all involving a live proxy or a live OpenClinica.

Each of these is a first-run checklist item, not a claim.
