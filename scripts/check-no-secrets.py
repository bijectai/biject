#!/usr/bin/env python3
"""No compose file may assign a literal value to a secret-shaped key.

Secrets arrive from `.env`, which is gitignored. A value written into a tracked
compose file is a committed secret, and `.env.example` is where an empty
placeholder belongs.

Why this is a parser, and why it carries its own tests
------------------------------------------------------
This check has been bypassed four times. The first three were line-based
patterns, each covering the YAML spellings its author happened to think of:

    environment:                      # block mapping   — caught by v1
      API_KEY: secret
    environment:                      # block sequence  — missed by v1
      - API_KEY=secret
      - "API_KEY=secret"              # quoted          — missed by v2
    environment: {API_KEY: secret}    # flow mapping    — missed by v3
    environment: ["API_KEY=secret"]   # flow sequence   — missed by v3

Parsing the YAML fixed that whole class. The fourth bypass was different and
worth studying: the parser was right about *structure* and wrong about
*values*. It treated any `$`-prefixed string as a safe reference, so
`${API_KEY:-hunter2}` passed — even though the fallback is a literal secret
sitting in a tracked file. It also skipped non-string scalars, so a numeric
password passed, and its key-name list missed common access-key spellings.

The lesson from four rounds is that reasoning about this one case at a time
does not converge. So every bypass ever found is now a permanent case in
SAFE/UNSAFE below, and `--self-test` runs them. Add a case before fixing a
new report.

Usage:

    python3 scripts/check-no-secrets.py --self-test
    python3 scripts/check-no-secrets.py docker-compose.yml
    git ls-files -z '*compose*.y*ml' | xargs -0 python3 scripts/check-no-secrets.py
"""

from __future__ import annotations

import hashlib
import re
import sys

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment problem, not a finding
    sys.exit("check-no-secrets: PyYAML is required (pip install pyyaml)")

# Key names that make a value credential-shaped. Deliberately broad: a false
# positive costs one `${VAR}` rewrite, a false negative costs a leaked key.
# PUBKEY / PUBLIC_KEY are excluded on purpose — public halves are meant to be
# committed, and biject-trace takes AUDIT_VERIFY_PUBKEY exactly that way.
SECRET_KEY = re.compile(
    r"""(
          SIGNING_KEY | SECRET   | PASSWORD    | PASSWD      | PASSPHRASE
        | API_KEY     | APIKEY   | ACCESS_KEY  | SECRET_KEY  | SESSION_KEY
        | ENCRYPTION_KEY          | PRIVATE_KEY | PRIVKEY    | SSH_KEY
        | _TOKEN(?![A-Za-z])      | TOKEN_      | ^TOKEN$
        | BEARER      | CREDENTIAL | _AUTH$     | AUTH_       | SALT
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Keys that match above but cannot carry the material. Public halves are meant
# to be committed — biject-trace takes AUDIT_VERIFY_PUBKEY that way. And the
# `*_FILE` / `*_PATH` convention names *where* a secret lives: `API_KEY_FILE:
# /run/secrets/api` is the recommended way to feed a container, so flagging it
# would push people off the safest pattern available.
PUBLIC_KEY = re.compile(r"(PUBKEY|PUBLIC_KEY|_PUB$|_FILE$|_PATH$|_FILEPATH$)", re.IGNORECASE)

# Compose's own `secrets:` section matches SECRET_KEY but declares secrets
# rather than holding them, so it must not act as an inheriting parent.
SCHEMA_SECTIONS = {"secrets", "secret", "configs", "config"}

# Keys inside that section that name where a secret lives — a path, a handle,
# an env var — never the material itself.
SCHEMA_FIELDS = {
    "file", "external", "name", "environment", "driver", "driver_opts",
    "labels", "target", "uid", "gid", "mode", "template_driver",
}


def inheritable(key: str) -> bool:
    """True when a container under this key holds credential material.

    A secret-shaped key whose value is a list or map still holds a secret —
    `API_KEY: [sk-live-real]` is a credential — so the key has to travel down
    with the recursion. It was dropped at the boundary, which is how v7 got in.
    """
    if key.lower() in SCHEMA_SECTIONS:
        return False
    return bool(SECRET_KEY.search(key)) and not PUBLIC_KEY.search(key)

# ${NAME:-default} / ${NAME-default} substitute a value when NAME is unset, so
# the default is itself committed content. ${NAME:?msg} / ${NAME?msg} fail
# closed instead — the text is an error message, never a value.
INTERPOLATION = re.compile(r"\$\{([^{}]*)\}")
WITH_DEFAULT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):?-(.*)$", re.DOTALL)
PLAIN_REF = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$")


def literal_part(value: str) -> str | None:
    """Return the committed literal in `value`, or None when it holds none.

    `${VAR}`, `${VAR:?msg}` and `$VAR` defer entirely to the environment.
    `${VAR:-fallback}` does not: `fallback` ships in the file.
    """
    text = value.strip()
    if not text:
        return None  # empty placeholder — the documented safe form

    interpolations = INTERPOLATION.findall(text)
    if interpolations:
        for inner in interpolations:
            match = WITH_DEFAULT.match(inner)
            if match and match.group(2).strip():
                return match.group(2).strip()

        # An interpolation does not sanitize the text around it. `${UNSET}sk-live-x`
        # is a reference glued to a committed literal, and reading "starts with a
        # reference" as "contains no secret" is how this was bypassed. Strip the
        # interpolations and judge what is left: pure punctuation is structure (a
        # separator, a URL scheme), anything alphanumeric is content that shipped
        # in the file.
        remainder = INTERPOLATION.sub("", text)
        remainder = re.sub(r"\$[A-Za-z_][A-Za-z0-9_]*", "", remainder)
        if any(ch.isalnum() for ch in remainder):
            return remainder.strip()
        return None

    if PLAIN_REF.match(text):
        return None

    return text


def findings_for(value, key: str, path: str) -> list[str]:
    if not SECRET_KEY.search(key) or PUBLIC_KEY.search(key):
        return []

    # Non-string scalars are credentials too — a numeric password or token is
    # perfectly legal YAML. Booleans are not credentials, and treating them as
    # such would flag every SECRET_ENABLED: true.
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return []

    literal = literal_part(value)
    if literal is None:
        return []

    # NEVER print the value. CI logs are retained and readable by anyone who
    # can see the run, so echoing the credential to stdout would turn one
    # committed secret into a second, longer-lived exposure — the exact thing
    # CLAUDE.md §2B.3 forbids, in the tool that exists to enforce it. §2B.3
    # also names the way out: "emit a key identifier or a fingerprint instead
    # of the material". The fingerprint is enough to tell two findings apart
    # and to confirm a fix changed the value, and it is not reversible.
    digest = hashlib.sha256(literal.encode("utf-8")).hexdigest()[:12]
    return [f"{path}  (literal, sha256:{digest}, {len(literal)} chars)"]


ASSIGNMENT = re.compile(r"(?:^|\s)-{0,2}([A-Za-z_][A-Za-z0-9_.-]*)=(\S*)")


def embedded_assignments(text: str, path: str) -> list[str]:
    """Catch every `KEY=value` pair living inside a plain string scalar.

    Several ways this shows up. `-API_KEY=x` with no space after the dash is
    not a YAML sequence entry at all — it parses as one scalar, so the
    structural walk never sees a key. A `command:` can carry `--api-key=x`. And
    one scalar can hold several assignments at once.

    Every pair is checked, not just the first. Splitting on the first `=` and
    stopping meant `SOMEVAR=x API_KEY=secret` was judged entirely on `SOMEVAR`,
    which is how this was bypassed. The credential is committed whether or not
    Compose would accept the file, so it is reported either way. Hyphens are
    normalized to underscores so CLI-style flags match the same names.
    """
    out: list[str] = []
    for name, value in ASSIGNMENT.findall(text):
        candidate = name.strip().strip("-").replace("-", "_")
        if candidate:
            out += findings_for(value, candidate, f"{path}.{candidate}")
    return out


def walk(node, path: str, inherited: str | None = None) -> list[str]:
    """Recurse the whole document, not just `environment:`.

    A credential is just as committed in `labels:`, `build.args:`, or a
    `command:`, so nothing is scoped out.

    `inherited` is the nearest secret-shaped ancestor key, carried down so a
    container value stays covered — see `inheritable`.
    """
    out: list[str] = []

    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            child = f"{path}.{name}" if path else name
            if isinstance(value, (dict, list)):
                # Carry a secret-shaped key down; a plain key does not clear an
                # inherited one, so `API_KEY: {a: {b: buried}}` stays covered.
                out += walk(value, child, name if inheritable(name) else inherited)
            else:
                out += findings_for(value, name, child)
                if isinstance(value, str):
                    out += embedded_assignments(value, child)
                # Under a secret-shaped ancestor, the leaf's own key is just a
                # label — unless it is Compose schema naming where a secret
                # lives rather than carrying it.
                if inherited and not inheritable(name) and name.lower() not in SCHEMA_FIELDS:
                    out += findings_for(value, inherited, child)
        return out

    if isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, (dict, list)):
                out += walk(item, f"{path}[{i}]", inherited)
            else:
                if isinstance(item, str):
                    # Sequence entries carry their own `KEY=value` pairs — and
                    # possibly more than one, which is why this shares the
                    # scalar path rather than splitting on the first `=`.
                    out += embedded_assignments(item, path)
                if inherited:
                    out += findings_for(item, inherited, f"{path}[{i}]")
        return out

    return out


def scan_text(text: str) -> list[str]:
    hits: list[str] = []
    for doc in yaml.safe_load_all(text):
        if doc is not None:
            hits += walk(doc, "")
    return hits


# --------------------------------------------------------------------------
# Regression cases. Every bypass this check has ever had is pinned here.

UNSAFE = [
    ("v1 block mapping",      "services: {a: {environment: {API_KEY: secret}}}"),
    ("v1 block sequence",     "services:\n  a:\n    environment:\n      - API_KEY=secret\n"),
    ("v2 double-quoted",      'services:\n  a:\n    environment:\n      - "API_KEY=secret"\n'),
    ("v2 single-quoted",      "services:\n  a:\n    environment:\n      - 'DB_PASSWORD=p4ss'\n"),
    ("v3 flow mapping",       "services:\n  a:\n    environment: {API_KEY: secret}\n"),
    ("v3 flow sequence",      'services:\n  a:\n    environment: ["OPENAI_API_KEY=leaked"]\n'),
    ("v4 fallback :-",        "services:\n  a:\n    environment:\n      API_KEY: ${API_KEY:-hunter2}\n"),
    ("v4 fallback -",         "services:\n  a:\n    environment:\n      API_KEY: ${API_KEY-hunter2}\n"),
    ("v4 numeric scalar",     "services:\n  a:\n    environment:\n      DB_PASSWORD: 987654321\n"),
    ("v4 access-key name",    "services:\n  a:\n    environment:\n      AWS_ACCESS_KEY_ID: AKIAIOSFODNN7EXAMPLE\n"),
    ("v4 passphrase name",    "services:\n  a:\n    environment:\n      PASSPHRASE: opensesame\n"),
    ("anchor/alias",          "x: &c {API_KEY: anchored}\nservices:\n  a:\n    environment: *c\n"),
    ("inside labels",         "services:\n  a:\n    labels:\n      com.x.API_KEY: leaked\n"),
    ("inside build args",     "services:\n  a:\n    build:\n      args:\n        NPM_TOKEN: npm_real\n"),
    ("folded scalar",         "services:\n  a:\n    environment:\n      API_KEY: >-\n        folded\n"),
    ("unspaced list entry",   "services:\n  a:\n    environment:\n      -API_KEY=packed\n"),
    ("cli flag literal",      "services:\n  a:\n    command: server --api-key=abc123\n"),
    ("v5 concat after ref",   "services:\n  a:\n    environment:\n      API_KEY: ${UNSET}sk-live-real\n"),
    ("v5 concat before ref",  "services:\n  a:\n    environment:\n      API_KEY: hunter2${UNSET}\n"),
    ("v5 second assignment",  "services:\n  a:\n    command: server --flag=1 --api-key=abc123\n"),
    ("v5 list 2nd assignment",'services:\n  a:\n    environment:\n      - "SOMEVAR=x API_KEY=leaked"\n'),
    ("v7 list-valued key",    "services:\n  a:\n    environment:\n      API_KEY: [sk-live-real]\n"),
    ("v7 map-valued key",     "services:\n  a:\n    environment:\n      DB_PASSWORD: {inner: hunter2}\n"),
    ("v7 nested sequence",    "services:\n  a:\n    environment:\n      AUTH_TOKEN:\n        - nested-secret\n"),
    ("v7 deep nesting",       "services:\n  a:\n    environment:\n      API_KEY:\n        a:\n          b: buried\n"),
]

SAFE = [
    ("plain reference",       "services:\n  a:\n    environment:\n      API_KEY: ${API_KEY}\n"),
    ("fail-closed reference", "services:\n  a:\n    environment:\n      K_SECRET: ${K_SECRET:?must be set}\n"),
    ("fail-closed no colon",  "services:\n  a:\n    environment:\n      K_SECRET: ${K_SECRET?must be set}\n"),
    ("empty default",         "services:\n  a:\n    environment:\n      API_KEY: ${API_KEY:-}\n"),
    ("bare dollar ref",       "services:\n  a:\n    environment:\n      API_KEY: $API_KEY\n"),
    ("empty placeholder",     'services:\n  a:\n    environment:\n      API_KEY: ""\n'),
    ("quoted reference",      'services:\n  a:\n    environment:\n      - "API_KEY=${API_KEY}"\n'),
    ("list fail-closed",      "services:\n  a:\n    environment:\n      - 'ACME_TOKEN=${ACME_TOKEN:?set}'\n"),
    ("public key is public",  "services:\n  a:\n    environment:\n      AUDIT_VERIFY_PUBKEY: abc123def\n"),
    ("boolean is not a key",  "services:\n  a:\n    environment:\n      SECRET_ENABLED: true\n"),
    ("non-secret url",        "services:\n  a:\n    environment:\n      REDIS_URL: redis://redis:6379\n"),
    ("null value",            "services:\n  a:\n    environment:\n      API_KEY:\n"),
    ("cli flag reference",    "services:\n  a:\n    command: server --api-key=${API_KEY}\n"),
    # `--max-tokens` is not a credential. The guard on _TOKEN keeps the
    # plural from matching, which is why that alternative is written
    # _TOKEN(?![A-Za-z]) rather than plain _TOKEN.
    ("plural token flag",     "services:\n  a:\n    command: server --max-tokens=100\n"),
    ("concat of refs only",   "services:\n  a:\n    environment:\n      API_KEY: ${A}${B}\n"),
    ("refs with separator",   "services:\n  a:\n    environment:\n      API_KEY: ${A}-${B}\n"),
    # Both exercise the multi-assignment scan specifically: several pairs in
    # one scalar, none of them a committed literal.
    ("two deferred pairs",    'services:\n  a:\n    environment:\n      - "A=${A} API_KEY=${K}"\n'),
    ("path with $VAR",        'services:\n  a:\n    environment:\n      - "PATH=/usr/bin:$PATH"\n'),
    # Compose's own `secrets:` schema. Inheriting a secret-shaped parent key
    # down into children would flag every one of these, and they are file
    # paths and handles, not credentials — the fix for v7 must not break them.
    ("compose secrets block", "secrets:\n  db_password:\n    file: ./db_password.txt\n"),
    ("external secret",       "secrets:\n  api_key:\n    external: true\n    name: prod_api_key\n"),
    ("service secrets list",  "services:\n  a:\n    secrets:\n      - db_password\n"),
    ("secret env indirection","secrets:\n  api_key:\n    environment: API_KEY_FROM_ENV\n"),
    ("list of refs under key",'services:\n  a:\n    environment:\n      API_KEY: ["${A}", "${B}"]\n'),
    # The *_FILE convention points at a secret rather than carrying one, and
    # it is the pattern this repo would recommend. Found while checking that
    # the v7 fix did not break a realistic `secrets:` deployment.
    ("secret file path",      "services:\n  a:\n    environment:\n      API_KEY_FILE: /run/secrets/api\n"),
    ("secret path suffix",    "services:\n  a:\n    environment:\n      SIGNING_KEY_PATH: /run/secrets/k\n"),
]


def redaction_failures() -> list[str]:
    """The report must never contain the credential it found.

    A separate assertion rather than another SAFE/UNSAFE row, because this is a
    property of the *output* and those cases only check the verdict. Round six
    was exactly this: detection was correct and the finding line echoed the
    secret into a retained CI log.
    """
    canary = "SUPERSECRETVALUE123456789"
    docs = [
        f"services:\n  a:\n    environment:\n      API_KEY: {canary}\n",
        f'services:\n  a:\n    environment:\n      - "API_KEY={canary}"\n',
        f"services:\n  a:\n    environment:\n      API_KEY: ${{UNSET}}{canary}\n",
        f"services:\n  a:\n    environment:\n      API_KEY: ${{UNSET:-{canary}}}\n",
        f"services:\n  a:\n    command: server --api-key={canary}\n",
    ]
    out = []
    for i, doc in enumerate(docs):
        hits = scan_text(doc)
        if not hits:
            out.append(f"redaction case {i} was not detected at all")
            continue
        blob = " ".join(hits)
        # Any run of the canary long enough to be useful counts as a leak.
        if any(canary[:n] in blob for n in range(8, len(canary) + 1)):
            out.append(f"redaction case {i} leaked the value into its finding")
    return out


def self_test() -> int:
    failures = 0
    for label, text in UNSAFE:
        if not scan_text(text):
            print(f"  FAIL  should have been caught: {label}")
            failures += 1
    for label, text in SAFE:
        hits = scan_text(text)
        if hits:
            print(f"  FAIL  false positive: {label} -> {hits}")
            failures += 1
    for problem in redaction_failures():
        print(f"  FAIL  {problem}")
        failures += 1
    total = len(UNSAFE) + len(SAFE) + 5
    if failures:
        print(f"\nself-test: {failures}/{total} failed")
        return 1
    print(
        f"  self-test ok: {len(UNSAFE)} bypasses caught, {len(SAFE)} safe forms allowed, "
        "5 redaction checks clean"
    )
    return 0


def main(argv: list[str]) -> int:
    paths = argv[1:]
    if paths == ["--self-test"]:
        return self_test()
    if not paths:
        print("usage: check-no-secrets.py [--self-test] <compose.yml> [...]", file=sys.stderr)
        return 2

    failed = False
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                hits = scan_text(fh.read())
        except (OSError, yaml.YAMLError) as exc:
            # Fail closed: a file that cannot be parsed cannot be cleared.
            print(f"::error title=Unreadable compose file::{path}: {exc}")
            failed = True
            continue

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
        print("empty placeholder in .env.example. Note that ${VAR:-fallback}")
        print("commits the fallback — that is a literal, not a reference.")
        return 1

    print()
    print("No literal secrets in any compose file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
