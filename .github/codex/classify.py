#!/usr/bin/env python3
"""Gate for the Codex PR review workflow (S4-A-00).

Decides three things, with no LLM call and no network access:

  1. Whether a diff is worth reviewing at all (lockfile-only churn is not).
  2. Which risk zone it lands in, which selects the model and reasoning effort.
  3. Which review rules apply, by collecting the ``## Code Review Rules`` section
     from every ``AGENTS.md`` that governs a changed file.

Step 3 emulates, for ``codex exec``, the rule-scoping that the hosted Codex Code
Review product does natively: a frontend-only diff carries the repo-wide rules and
nothing else, so unrelated guidance never competes for the model's attention.

Stdlib only, so the workflow needs no pip install beyond the Codex action itself.

Usage
-----
In CI (both the gate job and the review job run this; it is cheap and deterministic)::

    python .github/codex/classify.py --base origin/main --head HEAD
    python .github/codex/classify.py --base origin/main --head HEAD \
        --emit-prompt /tmp/review-prompt.md

Locally, against a hand-written file list::

    printf 'docker-compose.yml\\n' | python .github/codex/classify.py --changed-from -
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

# --- Zone map -------------------------------------------------------------
#
# Ordered most-specific first; the FIRST pattern a path matches wins, and the
# highest-risk zone across all changed paths wins overall. Mirrors the
# RED/YELLOW/GREEN governance in CLAUDE.md. Patterns are specific to biject;
# everything else in this file is byte-identical across the platform's repos.
#
# Keep this in sync with the repo layout in CLAUDE.md when files move.

RED_PATTERNS = (
    "docker-compose.yml",      # every change here is a deploy
    "scripts/verify-pins.sh",  # the check that keeps every image immutable
    "scripts/pin-images.sh",   # moves a pin and its BIJECT_IMAGE_SHA together
)

YELLOW_PATTERNS = (
    "scripts/*",
    ".github/*",
    ".env.example",
    "AGENTS.md",
    "*/AGENTS.md",
    "CLAUDE.md",
)

GREEN_PATTERNS = (
    "*.md",
)

# Paths that are pure churn. If EVERY changed file matches, skip the review
# entirely — no model call, no comment, no spend.
SKIP_PATTERNS = (
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    ".gitignore",
    ".gitattributes",
    "LICENSE",
    "README.md",
    ".vscode/*",
    ".idea/*",
    "*.png",
    "*.jpg",
    "*.svg",
    "*.ico",
)

# Zone -> (model, reasoning effort). RED and YELLOW get the coding-specialised
# model; GREEN routes to the cheap one because presentation-layer diffs are
# frequent and low-signal.
#
# Rate-limit history worth keeping: at the bottom usage tier gpt-5.3-codex was
# capped at 10,000 TPM, and a single review's first request is ~14k tokens, so it
# 429'd before starting. Usage tier is earned by cumulative amount *paid* —
# granted credits do not count toward it — and a $5 payment lifted the org to
# 500,000 TPM. If reviews start failing with "Request too large", check the tier
# before blaming the prompt.
ZONE_CONFIG = {
    "red": ("gpt-5.3-codex", "high"),
    "yellow": ("gpt-5.3-codex", "medium"),
    "green": ("gpt-5.6-luna", "low"),
}

ZONE_RANK = {"green": 0, "yellow": 1, "red": 2}

RULES_HEADING = "## Code Review Rules"


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    """True if `path` matches any pattern.

    Patterns ending in `/*` are treated as directory prefixes matching at any
    depth, which fnmatch alone does not do (its `*` does not cross separators
    in the way we want here).
    """
    for pat in patterns:
        if pat.endswith("/*"):
            if path == pat[:-2] or path.startswith(pat[:-1]):
                return True
        elif fnmatch.fnmatch(path, pat):
            return True
    return False


def zone_for(path: str) -> str:
    if _matches(path, RED_PATTERNS):
        return "red"
    if _matches(path, YELLOW_PATTERNS):
        return "yellow"
    if _matches(path, GREEN_PATTERNS):
        return "green"
    # Unknown path: default to YELLOW rather than GREEN. An unclassified file is
    # more likely to be new backend code than new documentation, and the failure
    # mode of over-reviewing is a few cents.
    return "yellow"


def changed_files_from_git(base: str, head: str, cwd: Path) -> list[str]:
    """Changed files in the merge-base diff, as forward-slash relative paths.

    Runs in `cwd` so the diff is read from the checkout of the code under review,
    which is not necessarily the checkout this script was loaded from.
    """
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
    ).stdout
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def governing_agents_files(paths: list[str], rules_root: Path) -> list[Path]:
    """Every AGENTS.md that governs at least one changed path.

    Walks up from each changed path to `rules_root`, collecting any AGENTS.md it
    finds. Discovery is by filesystem walk rather than a hardcoded list, so adding
    a nested AGENTS.md later needs no change here.

    `rules_root` is deliberately a separate tree from the code under review. In CI
    it points at the BASE checkout, so a pull request cannot weaken or delete the
    rules it will be judged against. A rule added in a PR therefore takes effect
    only once that PR merges.

    Returned root-first, then by increasing depth, so the model reads repo-wide
    guidance before the specific rules that refine it.
    """
    found: set[Path] = set()
    root_rules = rules_root / "AGENTS.md"
    if root_rules.is_file():
        found.add(root_rules)

    for rel in paths:
        current = (rules_root / rel).parent
        while True:
            candidate = current / "AGENTS.md"
            if candidate.is_file():
                found.add(candidate)
            if current == rules_root or rules_root not in current.parents:
                break
            current = current.parent

    return sorted(found, key=lambda p: (len(p.relative_to(rules_root).parts), str(p)))


def extract_rules(agents_file: Path) -> str:
    """The `## Code Review Rules` section of an AGENTS.md, heading excluded.

    Reads from the heading to the next `## ` at the same level, or EOF.
    Returns "" when the file carries no such section.
    """
    lines = agents_file.read_text(encoding="utf-8").splitlines()
    try:
        start = next(
            i for i, ln in enumerate(lines) if ln.strip() == RULES_HEADING
        )
    except StopIteration:
        return ""

    body: list[str] = []
    for ln in lines[start + 1 :]:
        if ln.startswith("## "):
            break
        # Demote the rule-group headings one level so they nest under the
        # per-source heading the prompt wraps them in.
        body.append("#" + ln if ln.startswith("### ") else ln)
    return "\n".join(body).strip()


def build_rules_blob(agents_files: list[Path], rules_root: Path) -> str:
    """Concatenate the applicable rule sections, each labelled with its source.

    The label matters: the prompt asks the model to name the rule it invokes, and
    the source path is how a reader finds the rule the finding refers to.
    """
    chunks: list[str] = []
    for f in agents_files:
        rules = extract_rules(f)
        if not rules:
            continue
        rel = f.relative_to(rules_root).as_posix()
        scope = "repository-wide" if rel == "AGENTS.md" else f"applies to `{f.parent.relative_to(rules_root).as_posix()}/`"
        chunks.append(f"### Rules from `{rel}` ({scope})\n\n{rules}")
    return "\n\n".join(chunks)


def write_github_output(**kwargs: str) -> None:
    """Append key=value pairs to $GITHUB_OUTPUT when running under Actions."""
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            fh.write(f"{key}={value}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main", help="base ref for the diff")
    ap.add_argument("--head", default="HEAD", help="head ref for the diff")
    ap.add_argument(
        "--changed-from",
        help="read changed paths from a file (or '-' for stdin) instead of git",
    )
    ap.add_argument(
        "--repo-root",
        default=".",
        help="checkout of the code under review (git diff is read from here)",
    )
    ap.add_argument(
        "--rules-root",
        help=(
            "checkout the AGENTS.md rules and prompt template are read from. "
            "Defaults to --repo-root. In CI, set this to a BASE-branch checkout so "
            "a pull request cannot edit the rules it is judged against."
        ),
    )
    ap.add_argument(
        "--template",
        default=".github/codex/prompts/review.md",
        help="prompt template containing the {{RULES}} placeholder, relative to --rules-root",
    )
    ap.add_argument(
        "--emit-prompt",
        help="render the template with the scoped rules and write it here",
    )
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    rules_root = Path(args.rules_root).resolve() if args.rules_root else repo_root

    if args.changed_from == "-":
        paths = [ln.strip().replace("\\", "/") for ln in sys.stdin if ln.strip()]
    elif args.changed_from:
        paths = [
            ln.strip().replace("\\", "/")
            for ln in Path(args.changed_from).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    else:
        paths = changed_files_from_git(args.base, args.head, cwd=repo_root)

    if not paths:
        print("No changed files — skipping review.", file=sys.stderr)
        write_github_output(skip="true", reason="no-changed-files")
        return 0

    if all(_matches(p, SKIP_PATTERNS) for p in paths):
        print(
            f"All {len(paths)} changed file(s) are churn (lockfiles/assets/README) "
            "— skipping review.",
            file=sys.stderr,
        )
        write_github_output(skip="true", reason="churn-only")
        return 0

    # Churn files still count as "changed" for the diff the model reads, but they
    # must not drag the zone up or pull in rules, so drop them before classifying.
    significant = [p for p in paths if not _matches(p, SKIP_PATTERNS)]

    zone = max((zone_for(p) for p in significant), key=lambda z: ZONE_RANK[z])
    model, effort = ZONE_CONFIG[zone]

    agents_files = governing_agents_files(significant, rules_root)
    rules_blob = build_rules_blob(agents_files, rules_root)
    rule_sources = ",".join(
        f.relative_to(rules_root).as_posix() for f in agents_files
    )

    print(
        f"zone={zone} model={model} effort={effort}\n"
        f"changed={len(paths)} significant={len(significant)}\n"
        f"rule sources: {rule_sources or '(none)'}",
        file=sys.stderr,
    )

    write_github_output(
        skip="false",
        zone=zone,
        model=model,
        effort=effort,
        rule_sources=rule_sources,
        file_count=str(len(significant)),
    )

    if args.emit_prompt:
        template_path = Path(args.template)
        if not template_path.is_absolute():
            template_path = rules_root / template_path
        template = template_path.read_text(encoding="utf-8")
        rendered = (
            template.replace("{{RULES}}", rules_blob or "_No scoped rules apply to this diff._")
            .replace("{{ZONE}}", zone)
            .replace("{{CHANGED_FILES}}", "\n".join(f"- {p}" for p in significant))
        )
        out = Path(args.emit_prompt)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
        print(f"Wrote prompt to {out} ({len(rendered)} chars)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
