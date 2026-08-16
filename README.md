# biject — sprint v4 demo

Formal action-gating for AI agents, demonstrated on a clinical-data
reconciliation task: an OpenAI Agents SDK agent resolves open queries in a
self-hosted OpenClinica 3.17 CE EDC, and **every write is gated by a Lean
kernel-decided audit bound, enforced pre-forward by a verification proxy** that
is the only network route to the EDC.

What the demo proves: formal action-gating on a defined action surface,
enforced pre-commit, with a third-party-checkable proof artifact. What it does
not prove: 21 CFR Part 11 compliance, breach prevention, or coverage of any
action outside the formalized surface. PROVED means the predicate holds under
kernel checking.

| Path | Role |
|---|---|
| `PolicyEnv/` | Lean 4 audit-bound predicate (`AuditEntryValid`), decidable, compile-time regression vectors |
| `contracts/` | Tool-call JSON contract (draft, pending freeze) |
| `adapters/openai/` | Agents SDK function tools → proxy (reference integration; Bedrock/Foundry written post-sprint, not yet run) |
| `edc/` | OpenClinica 3 client (session ODM read, SOAP write), synthetic seed study |
| `infra/hetzner/` | Compose + Traefik + OpenClinica stack + network lockdown |
| `scripts/preflight.sh` | Host capacity/egress preflight |
| `docs/DAY1-2-RUNBOOK.md` | Host-side execution runbook |

Engineering rules and invariants: see [`CLAUDE.md`](CLAUDE.md). Deviations from
ticket text: [`.claude/deviations/`](.claude/deviations/).
