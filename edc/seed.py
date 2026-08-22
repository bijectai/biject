"""seed.py — seed study S_BJTDEMO with realistic messy data.

What this script does and deliberately does NOT do
==================================================
1. STUDY METADATA IS MANUAL. OC 3.17 cannot import ODM study metadata; the
   study, events, CRFs and rules are created in the OC3 UI following
   ``edc/README.md`` (build study -> events -> CRF Excel uploads -> event-CRF
   assignment -> ``rules.xml`` upload in the Rules module -> subject
   registration + event scheduling -> ws account flag). ``study_def.xml`` is
   the documentation of record those steps are checked against; this script
   handles clinical DATA import only.

2. DATA IMPORT. Pushes ``edc/seed_data.xml`` through the OpenClinica-ws SOAP
   Data service (``OC3Client.import_data``) as **one call per subject per
   event, in document order (Screening first)**, printing a per-call
   PASS/FAIL line. Two reasons for the split:

   * Per-subject batching means one bad subject fails alone instead of
     poisoning a whole transactional import.
   * Per-event ordering is REQUIRED for the cross-event date rule
     (R_VISITDATE_ORDER in ``rules.xml``): import-time rules are evaluated
     before the current call's data is committed, and a full-path reference
     into another event (SE_SCREENING...I_DEMOG_SCREENDATE) resolves from the
     DB only. The screening import must therefore be a separate, earlier SOAP
     call than the visit imports — verified against the 3.17.2 source
     (DataEndpoint: runRulesSetup runs before submitData).

3. WHY QUERIES APPEAR AT ALL (and why there are no soft edit checks): in the
   ws import path, soft edit checks (CRF VALIDATION column / ODM RangeCheck)
   fail the ENTIRE import — DataImportService.validateData adds an error for
   soft violations and DataEndpoint then returns <result>Fail</result>
   without writing anything. The CRFs therefore carry no edit checks at all,
   and the open queries come from the rules in ``edc/rules.xml``
   (DiscrepancyNoteAction with Run ImportDataEntry="true"), which fire during
   import and create open Failed Validation Check discrepancy notes.
   Expected count after a clean seed: 16 (see edc/query_resolution_map.md).

4. ``--verify`` re-reads the study via the session-authenticated ODM export
   (``includeDNs=y``) and prints the count of OPEN discrepancy notes, plus a
   one-line summary of each, so you can confirm the messy data actually
   raised the expected queries.

RESET / RE-RUN CAVEAT
=====================
Re-importing the same item+value does NOT re-fire a rule discrepancy note:
the import rule runner deduplicates against rule_action_run_log
(same item + value + rule => action dropped). Re-running this script against
a played-through study re-imports values but re-creates no queries. To reset
a demo, restore the post-seeding DB snapshot with ``edc/reset_demo.sh``.

Prerequisites (one-time, in the OC3 UI — full detail in edc/README.md)
======================================================================
* Study S_BJTDEMO built per README (unique protocol ID "BJT-DEMO"), events
  SE_SCREENING / SE_VISIT1 / SE_VISIT2, CRFs DEMOG/VITALS/LABS v1.0 uploaded
  and assigned, study Available.
* ``rules.xml`` uploaded via Tasks -> Rules and Available — without it the
  import succeeds but zero queries are raised.
* Study subjects SS_001..SS_014 registered (Study Subject IDs 001..014) and
  all three events scheduled for each — OC3 data import updates existing
  event/CRF shells; it does not register subjects or schedule events.
* The importing user has "Authorize SOAP web services in this account"
  checked and a data-entry role.

Usage
=====
::

    export OC_BASE_URL=https://oc.example.org/OpenClinica
    export OC_USER=root OC_PASS=...
    python edc/seed.py                 # import all 14 subjects, event by event
    python edc/seed.py --verify        # import, then count open queries (16)
    python edc/seed.py --only SS_002   # re-import a single subject (all events)
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

# Import order within a subject follows document order in seed_data.xml:
# SE_SCREENING first, then SE_VISIT1, then SE_VISIT2 (see docstring point 2).
_EXPECTED_SUBJECTS = [f"SS_{n:03d}" for n in range(1, 15)]


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


def _per_subject_event_documents(
    root: etree._Element,
) -> list[tuple[str, str, etree._Element]]:
    """Split the seed ODM into one standalone ODM document per subject+event.

    Each split document keeps the original ODM/ClinicalData attributes and the
    OpenClinica ``UpsertOn`` extension, but contains exactly one SubjectData
    holding exactly one StudyEventData — so each SOAP importData call succeeds
    or fails for one subject-event only, and a subject's Screening event is
    committed before its visit events (required by the cross-event date rule;
    see module docstring).
    """
    clinical = root.find(f"{{{NS_ODM}}}ClinicalData")
    if clinical is None:
        raise SystemExit("seed data has no <ClinicalData> element")
    subjects = clinical.findall(f"{{{NS_ODM}}}SubjectData")
    if not subjects:
        raise SystemExit("seed data has no <SubjectData> elements")

    documents: list[tuple[str, str, etree._Element]] = []
    for subject in subjects:
        subject_key = subject.get("SubjectKey", "?")
        events = subject.findall(f"{{{NS_ODM}}}StudyEventData")
        if not events:
            raise SystemExit(f"{subject_key} has no <StudyEventData> elements")
        for event in events:
            event_oid = event.get("StudyEventOID", "?")
            # Deep-copy the whole tree, then prune it down to this one
            # subject-event. (Copying the full tree keeps namespaces/attrs/
            # UpsertOn intact with zero reconstruction logic.)
            doc = copy.deepcopy(root)
            doc_clinical = doc.find(f"{{{NS_ODM}}}ClinicalData")
            for other_subject in doc_clinical.findall(f"{{{NS_ODM}}}SubjectData"):
                if other_subject.get("SubjectKey") != subject_key:
                    doc_clinical.remove(other_subject)
                    continue
                for other_event in other_subject.findall(
                    f"{{{NS_ODM}}}StudyEventData"
                ):
                    if other_event.get("StudyEventOID") != event_oid:
                        other_subject.remove(other_event)
            _strip_comments(doc)
            documents.append((subject_key, event_oid, doc))
    return documents


def _seed(client: OC3Client, data_path: Path, only: str | None) -> int:
    """Import each subject-event; print per-call results; return failures."""
    root = _load_seed_tree(data_path)
    study_oid = root.find(f"{{{NS_ODM}}}ClinicalData").get("StudyOID", "?")
    documents = _per_subject_event_documents(root)
    if only:
        documents = [(s, e, d) for s, e, d in documents if s == only]
        if not documents:
            raise SystemExit(f"Subject {only!r} not found in {data_path.name}")

    subjects = {s for s, _, _ in documents}
    print(
        f"Seeding {len(subjects)} subject(s) / {len(documents)} subject-event "
        f"import(s) into {study_oid} via {client.ws_base_url} ...\n"
    )
    failures = 0
    for subject_key, event_oid, doc in documents:
        try:
            result = client.import_data(doc)
            print(f"  [PASS] {subject_key} {event_oid}: importData -> {result}")
        except OC3Error as exc:
            failures += 1
            print(f"  [FAIL] {subject_key} {event_oid}: {exc}")
    print(f"\nDone: {len(documents) - failures} imported, {failures} failed.")
    if failures:
        print(
            "Check that the subject is registered and SE_SCREENING/SE_VISIT1/"
            "SE_VISIT2 are all scheduled (OC3 data import cannot create "
            "either), and that the CRF version OIDs in seed_data.xml "
            "(F_DEMOG_V10 / F_VITALS_V10 / F_LABS_V10) match the versions "
            "built in the UI — see edc/README.md."
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
    print(
        "\nExpected after a clean seed: 16 open notes "
        "(see edc/query_resolution_map.md)."
    )
    if not queries:
        print(
            "  NOTE: 0 open queries almost always means rules.xml was not "
            "uploaded (Tasks -> Rules) or its rules are not Available, OR "
            "this data was already imported once — the import rule runner "
            "dedups against rule_action_run_log and will not re-fire on the "
            "same item+value. Reset with edc/reset_demo.sh instead of "
            "re-importing."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed.py",
        description=(
            "Seed study S_BJTDEMO with the messy demo data (SOAP data import "
            "only, one call per subject per event — study metadata and "
            "rules.xml are uploaded manually via the OC UI, see module "
            "docstring and edc/README.md). Connection settings from "
            "OC_BASE_URL / OC_USER / OC_PASS / OC_WS_BASE_URL."
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
        help="Import just this one subject, all of its events (e.g. SS_002)",
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
