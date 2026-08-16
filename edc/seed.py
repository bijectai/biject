"""seed.py — seed study S_BJTDEMO with realistic messy data (S4-D-20).

What this script does and deliberately does NOT do
==================================================
1. STUDY METADATA IMPORT IS MANUAL. Upload ``edc/study_def.xml`` via the OC3
   UI: Tasks -> Build Study (create the study, then use the CRF/metadata
   import screens). Metadata import over the SOAP study web service is
   unreliable in OpenClinica 3.17, so this script does not attempt it —
   ``seed.py`` handles clinical DATA import only.

2. DATA IMPORT. Pushes ``edc/seed_data.xml`` through the OpenClinica-ws SOAP
   Data service (``OC3Client.import_data``) **subject by subject**, printing a
   per-subject PASS/FAIL line. Per-subject batching means one bad subject
   fails alone instead of poisoning the whole transactional import, and the
   console output doubles as the seeding record for the sprint log.

3. ``--verify`` re-reads the study via the session-authenticated ODM export
   (``includeDNs=y``) and prints the count of OPEN discrepancy notes, plus a
   one-line summary of each, so you can confirm the messy data actually
   raised queries.

Prerequisites (one-time, in the OC3 UI, before running this script)
===================================================================
* Study S_BJTDEMO built from ``study_def.xml`` and set to Available.
* Study subjects SS_001..SS_008 registered (Tasks -> Add Subject), and
  SE_VISIT1 scheduled for each — OC3's data import updates existing
  event/CRF shells; it does not register subjects or schedule events.
  (If this becomes a recurring chore, the OpenClinica-ws studySubject/v1 and
  event/v1 SOAP services can automate it — out of scope for this ticket.)
* The importing user is a "web services" user type with data-entry rights.

90-MINUTE TIMEBOX NOTE (sprint agreement)
=========================================
If OC edit-check rules do not fire discrepancy notes on import, create the
queries manually in the Notes & Discrepancies UI and log the deviation — the
demo needs the queries to exist and be resolvable, not to have been
rules-engine-generated.

Usage
=====
::

    export OC_BASE_URL=https://oc.example.org/OpenClinica
    export OC_USER=root OC_PASS=...
    python edc/seed.py                 # import all 8 subjects
    python edc/seed.py --verify        # import, then count open queries
    python edc/seed.py --only SS_002   # re-import a single subject
    python edc/seed.py --verify-only   # skip import, just count open queries
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

from lxml import etree

# Allow both invocation styles: ``python edc/seed.py`` (script; sibling import)
# and ``python -m edc.seed`` from the repo root (package import).
try:
    from edc.oc3_client import NS_ODM, OC3Client, OC3Error
except ImportError:  # script-style invocation: edc/ itself is sys.path[0]
    from oc3_client import NS_ODM, OC3Client, OC3Error

_HERE = Path(__file__).resolve().parent
_DEFAULT_DATA = _HERE / "seed_data.xml"
_NSMAP = {"odm": NS_ODM}


def _load_seed_tree(path: Path) -> etree._Element:
    """Parse seed_data.xml with comments preserved for local inspection."""
    try:
        return etree.parse(str(path)).getroot()
    except (OSError, etree.XMLSyntaxError) as exc:
        raise SystemExit(f"Cannot read seed data {path}: {exc}")


def _strip_comments(el: etree._Element) -> None:
    """Remove XML comments in place — the planted-error annotations are for
    humans reading the repo, not for the OC import parser."""
    for comment in el.xpath("//comment()"):
        comment.getparent().remove(comment)


def _per_subject_documents(
    root: etree._Element,
) -> list[tuple[str, etree._Element]]:
    """Split the seed ODM into one standalone ODM document per subject.

    Each split document keeps the original ODM/ClinicalData attributes and the
    OpenClinica ``UpsertOn`` extension, but contains exactly one SubjectData —
    so each SOAP importData call succeeds or fails for one subject only.
    """
    clinical = root.find(f"{{{NS_ODM}}}ClinicalData")
    if clinical is None:
        raise SystemExit("seed data has no <ClinicalData> element")
    subjects = clinical.findall(f"{{{NS_ODM}}}SubjectData")
    if not subjects:
        raise SystemExit("seed data has no <SubjectData> elements")

    documents: list[tuple[str, etree._Element]] = []
    for subject in subjects:
        subject_key = subject.get("SubjectKey", "?")
        # Deep-copy the whole tree, then prune it down to this one subject.
        # (Copying the full tree keeps namespaces/attrs/UpsertOn intact with
        # zero reconstruction logic.)
        doc = copy.deepcopy(root)
        doc_clinical = doc.find(f"{{{NS_ODM}}}ClinicalData")
        for other in doc_clinical.findall(f"{{{NS_ODM}}}SubjectData"):
            if other.get("SubjectKey") != subject_key:
                doc_clinical.remove(other)
        _strip_comments(doc)
        documents.append((subject_key, doc))
    return documents


def _seed(client: OC3Client, data_path: Path, only: str | None) -> int:
    """Import each subject; print per-subject results; return failure count."""
    root = _load_seed_tree(data_path)
    study_oid = root.find(f"{{{NS_ODM}}}ClinicalData").get("StudyOID", "?")
    documents = _per_subject_documents(root)
    if only:
        documents = [(k, d) for k, d in documents if k == only]
        if not documents:
            raise SystemExit(f"Subject {only!r} not found in {data_path.name}")

    print(
        f"Seeding {len(documents)} subject(s) into {study_oid} "
        f"via {client.ws_base_url} ...\n"
    )
    failures = 0
    for subject_key, doc in documents:
        try:
            result = client.import_data(doc)
            print(f"  [PASS] {subject_key}: importData -> {result}")
        except OC3Error as exc:
            failures += 1
            print(f"  [FAIL] {subject_key}: {exc}")
    print(f"\nDone: {len(documents) - failures} imported, {failures} failed.")
    if failures:
        print(
            "Check that the subject is registered and SE_VISIT1 is scheduled "
            "(OC3 data import cannot create either) — see module docstring."
        )
    return failures


def _verify(client: OC3Client, study_oid: str) -> None:
    """Re-read via ODM export and report OPEN discrepancy-note count."""
    queries = client.list_open_queries(study_oid)
    print(
        f"\nVerification: {len(queries)} OPEN discrepancy note(s) " f"in {study_oid}."
    )
    for q in queries:
        print(
            f"  [{q.status:>7}] subject={q.subject_key} item={q.item_oid} "
            f"form={q.form_oid}: {q.description or '(no note text)'}"
        )
    if not queries:
        # The sprint's agreed fallback — repeat it where it will be seen.
        print(
            "  NOTE: 0 open queries. Per the 90-minute timebox agreement: if "
            "OC edit-check rules did not fire discrepancy notes on import, "
            "create the queries manually in the Notes & Discrepancies UI and "
            "log the deviation — the demo needs the queries to exist and be "
            "resolvable, not to have been rules-engine-generated."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed.py",
        description=(
            "Seed study S_BJTDEMO with the messy demo data (SOAP data import "
            "only — study metadata is uploaded manually via the OC UI, see "
            "module docstring). Connection settings from OC_BASE_URL / "
            "OC_USER / OC_PASS / OC_WS_BASE_URL."
        ),
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=_DEFAULT_DATA,
        help=f"Path to the seed ODM file (default: {_DEFAULT_DATA})",
    )
    parser.add_argument(
        "--only",
        metavar="SUBJECT_KEY",
        help="Import just this one subject (e.g. SS_002)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After importing, re-read via ODM export and count open queries",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip the import; only run the open-query verification read",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification (lab boxes with self-signed certs only)",
    )
    args = parser.parse_args(argv)

    try:
        client = OC3Client(verify_tls=not args.insecure)
        study_oid = (
            _load_seed_tree(args.data)
            .find(f"{{{NS_ODM}}}ClinicalData")
            .get("StudyOID", "S_BJTDEMO")
        )
        failures = 0
        if not args.verify_only:
            failures = _seed(client, args.data, args.only)
        if args.verify or args.verify_only:
            _verify(client, study_oid)
        return 1 if failures else 0
    except OC3Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
