# biject-wall — the verdict wall

A single self-contained page (`wall.html`: inline CSS + JS, system font stack,
no CDNs, no build step) that renders biject's live verdict feed at projector
size. One row per proxy decision, newest on top.

Tagline on the page, and the claim it makes: **"Every write gated by a
kernel-checked audit bound"** — and the footer pins the claim boundary:
*PROVED = the supplied structured parameters satisfy a kernel-checked
predicate.* Nothing on this page claims more than that.

## Data source

`biject-trace`'s advisory in-memory feed (see
`biject-trace/app/verdict_stream.py` — the shape is defined there, not in
`biject-contracts`, because nothing may consume it as an authorization or
record input):

- `GET {TRACE_BASE}/v1/verdicts/stream` — SSE, replay + live events, 15s
  heartbeats. Primary source.
- `GET {TRACE_BASE}/v1/verdicts?limit=50` — newest-first buffer page. Used for
  the initial fill and as the polling fallback while SSE is down.

The feed is advisory and lost on restart; the signed ledger served by
`biject-trace` at `/v1/entries` is the record. Any doubt about what the wall
shows is settled there.

## Verdict mapping

The wire vocabulary is `verdict: allowed | blocked | skipped` (absent when the
proxy denied without obtaining a verdict), `forwarded: bool`, `deny_reason`.
Renaming wire `allowed`/`blocked` to display PROVED/REFUTED is the **only**
renaming this page performs:

| Wire event | Display |
| --- | --- |
| `verdict: "allowed"`, `forwarded: true` | **PROVED** (green) |
| `verdict: "blocked"` | **REFUTED** (red) — with the failed predicate clause excerpted from `lean_trace`, clause tokens (`notBackdated`, `reasonCodeValid`, `sigOk`, …) highlighted |
| `verdict: "skipped"` | **SKIPPED** (amber) |
| `verdict: null` | **DENIED** (red) — the proxy denied without a verdict; `deny_reason` shown (`verifier_timeout`, `verifier_unreachable`, `malformed_verdict`, `contract_violation`, …) |
| `verdict: "allowed"`, `forwarded: false` | **DENIED** (red) — a verdict was obtained but the call still did not reach its downstream; `deny_reason` shown when present |

Denials render as `REFUTED: <clause>` / `DENIED: <reason>` — the clause or
enumerated reason itself, never a paraphrase of it.

## Latency columns

Three columns, each labelled with the real wire field it displays. They are
never merged and never renamed:

| Column | Wire field | Unit |
| --- | --- | --- |
| `kernel elab_us` | `elab_us` (pure Lean elaboration) | microseconds |
| `verify latency_us` | `latency_us` (verifier end-to-end) | microseconds |
| `proxy total` | `total_latency_us` (whole proxied call) | rendered as ms |

## Page configuration (query parameters)

| Parameter | Default | Meaning |
| --- | --- | --- |
| `?trace=` | `/trace` (same-origin; nginx proxies it to `biject-trace:8010`) | Base URL for the verdict feed. Cross-origin values need CORS on the trace side. |
| `?queries_url=` | *(unset)* | Optional open-query counter source, polled every 10s. Accepts `{"open_query_count": N}`, `{"queries": [...]}` (length is shown), or a bare array. Unset, the counter slot shows `—` with an explanatory tooltip. Errors dim the chip and never touch the stream. |

The status dot reports stream connectivity: green = SSE connected, amber =
reconnecting, blue = polling `/v1/verdicts` as fallback.

## Run it

Local, against any trace instance:

```bash
cd wall && python3 -m http.server 8770
open "http://localhost:8770/wall.html?trace=http://localhost:8010"
```

As a container on the platform network (nginx serves the page and proxies
`/trace/` to `http://biject-trace:8010/`):

```bash
docker build -t biject-wall wall/
docker run --rm -p 8090:80 --network biject_default biject-wall
open http://localhost:8090/
```

The nginx upstream is resolved per request through Docker's embedded DNS, so
the wall starts (and shows "reconnecting") even while `biject-trace` is down —
an observer comes up and reports an outage rather than joining it.

Not in `docker-compose.yml`: the platform topology pins published,
SHA-tagged images only and permits no `build:` stanzas. Adding the wall there
is a separate deploy decision once a `biject-wall` image is published under
that rule.
