# Sprint v4 Day 1–2 — repo placement deviation

The knowledge graph (`BijectMetaRepo`, `RepoStructureStandard`) records
`bijectai/biject` as the **meta/topology repo**: "owns the deployment topology
and nothing else — no application code, no image published from here." The
platform's contracts likewise have a dedicated home (`bijectai/biject-contracts`,
vendored into services) and the enforcement proxy has its own repo
(`bijectai/biject-proxy`, fail-closed scaffold already real and tested as of
2026-08-12).

Sprint v4's ticket FILES paths (`infra/hetzner/…`, `edc/…`, `adapters/…`,
`PolicyEnv/…`, `contracts/tool_calls.json`) name no repo, the sprint branch was
provisioned on `bijectai/biject` (which at branch time contained only the
initial-commit README — none of the meta-repo content the KG describes has been
pushed to it), and the sprint work was explicitly scoped away from
`biject-api`. Decision: **all Day 1–2 Dev artifacts land in this repo** on
`claude/biject-sprint-plan-v4-ry6ab2`.

Known tensions to reconcile post-sprint (none block the demo):

1. **Meta-repo purity** — `edc/`, `adapters/`, and `PolicyEnv/` are application
   code in a repo the KG says should carry topology only. Either the demo code
   moves to its own repo(s) after the sprint, or the KG entry is updated to
   reflect that this repo now hosts the demo vertical.
2. **Contracts home** — `contracts/tool_calls.json` here is a DRAFT v0; the
   S4-A-12 freeze should land in `biject-contracts` per the vendoring
   convention (`scripts/vendor.py`, `contracts.lock`, drift check), with this
   repo vendoring the frozen bundle like every other consumer.
3. **Proxy** — S4-A-10's "Rust proxy skeleton" overlaps with the existing
   `biject-proxy` (Python/httpx per its KG observations, enforcement path
   tested). The sprint's Rust-migration gate should be checked against that
   repo's actual state before anyone writes a second proxy.
