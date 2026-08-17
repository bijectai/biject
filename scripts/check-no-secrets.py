#!/usr/bin/env python3
"""No compose file may assign a literal value to a secret-shaped key.

Secrets arrive from `.env`, which is gitignored. A value written into a tracked
compose file is a committed secret, and `.env.example` is where an empty
placeholder belongs.

Why this is a parser and not a grep
-----------------------------------
It was a grep, three times, and it had a hole every time. YAML gives the same
mapping several legal spellings, and each version of the pattern covered the
ones its author happened to think of:

    environment:                      # block mapping   — caught by v1
      API_KEY: secret
    environment:                      # block sequence  — missed by v1
      - API_KEY=secret
      - "API_KEY=secret"              # quoted          — missed by v2
    environment: {API_KEY: secret}    # flow mapping    — missed by v3
    environment: ["API_KEY=secret"]   # flow sequence   — missed by v3

Each round closed one spelling and left the next one open, which is the signal
that the approach was wrong rather than the pattern. A YAML parser normalizes
every spelling to the same structure, so this checks the *data* and inherits
new syntax for free. It also follows anchors and aliases, which no line-based
pattern can.

What counts as a secret
-----------------------
A key whose name looks like credential material, holding a literal value. A
`${VAR}` reference is fine — that is the whole point of the pattern this repo
uses — and so is an empty value, which is what a placeholder looks like.

Run it locally the same way CI does:

    python3 scripts/check-no-secrets.py docker-compose.yml
    git ls-files '*compose*.y*ml' | xargs python3 scripts/check-no-secrets.py
"""

from __future__ import annotations

import re
import sys

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment problem, not a finding
    sys.exit("check-no-secrets: PyYAML is required (pip install pyyaml)")

# Substrings that make a key name credential-shaped. Deliberately broad: a false
# positive costs one `${VAR}` rewrite, a false negative costs a leaked key.
SECRET_KEY = re.compile(
    r"(SIGNING_KEY|SECRET|PASSWORD|PASSWD|API_KEY|_TOKEN|TOKEN_|^TOKEN$|CREDENTIAL|PRIVATE_KEY)",
    re.IGNORECASE,
)


def is_reference(value: str) -> bool:
    """True when the value defers to the environment rather than carrying a secret.

    `${VAR}`, `${VAR:-default}` and `${VAR:?message}` are all references. So is
    `$VAR`. Anything else is a literal sitting in a tracked file.
    """
    return value.lstrip().startswith("$")


def findings_for(value: str, key: str, path: str) -> list[str]:
    if not SECRET_KEY.search(key):
        return []
    text = value.strip()
    if not text:            # empty placeholder — the documented safe form
        return []
    if is_reference(text):  # ${VAR} / ${VAR:?msg} — reads from .env at runtime
        return []
    return [f"{path} = {text[:24]}{'…' if len(text) > 24 else ''}"]


def walk(node, path: str) -> list[str]:
    """Recurse the whole document, not just `environment:`.

    A credential is just as committed in `labels:`, `build.args:`, or a
    `command:` as it is in `environment:`, so nothing is scoped out.
    """
    out: list[str] = []

    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(value, str):
                out += findings_for(value, str(key), child)
            elif isinstance(value, (dict, list)):
                out += walk(value, child)
        return out

    if isinstance(node, list):
        for i, item in enumerate(node):
            child = f"{path}[{i}]"
            if isinstance(item, str):
                # Sequence entries carry their own `KEY=value` pairs.
                name, sep, value = item.partition("=")
                if sep:
                    out += findings_for(value, name, f"{path}.{name.strip()}")
            elif isinstance(item, (dict, list)):
                out += walk(item, child)
        return out

    return out


def main(argv: list[str]) -> int:
    paths = argv[1:]
    if not paths:
        print("usage: check-no-secrets.py <compose.yml> [...]", file=sys.stderr)
        return 2

    failed = False
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                documents = list(yaml.safe_load_all(fh))
        except (OSError, yaml.YAMLError) as exc:
            # Fail closed: a file that cannot be parsed cannot be cleared.
            print(f"::error title=Unreadable compose file::{path}: {exc}")
            failed = True
            continue

        hits: list[str] = []
        for doc in documents:
            if doc is not None:
                hits += walk(doc, "")

        if hits:
            failed = True
            print(f"::error title=Committed secret::{path} assigns a literal value to a secret-shaped key.")
            for hit in hits:
                print(f"  {path}: {hit}")
        else:
            print(f"  ok  {path}")

    if failed:
        print()
        print("Secrets come from .env, which is gitignored. Reference them as")
        print("${VAR} (or ${VAR:?message} to fail closed when unset) and put an")
        print("empty placeholder in .env.example.")
        return 1

    print()
    print("No literal secrets in any compose file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
