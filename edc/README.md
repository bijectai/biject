# edc/ — OpenClinica 3.17 CE demo study setup (BJT-DEMO-01)

Everything needed to stand up the synthetic demo study `S_BJTDEMO`
(BJT-DEMO-01) in the self-hosted OpenClinica 3.17 CE instance
(`infra/hetzner/openclinica/`), seed it with deliberately messy data, and
reset it between demo runs. **All study data is 100% synthetic — no real
patients, no PHI.**

| File | Role |
|------|------|
| `study_def.xml` | ODM documentation of record for the study structure (OC3 cannot import ODM metadata — the study is built manually per this README) |
| `rules.xml` | OC Rules XML: the query-generation mechanism (uploaded in the UI, step 5) |
| `seed_data.xml` | 14 subjects x up to 3 events of messy clinical data (pushed over SOAP by `seed.py`) |
| `query_resolution_map.md` | One row per planted query: what fires, whether it is resolvable, and from which exact OIDs |
| `seed.py` | SOAP data import (one call per subject per event) + `--verify` open-query count |
| `reset_demo.sh` | <60s DB snapshot/restore between demo runs |
| `oc3_client.py` | The OC3 read/write client `seed.py` and the demo agent tools use |

## Why the mechanism looks the way it does (OC 3.17.2, source-verified)

* **Rules, not edit checks, raise the queries.** Rules with a
  `DiscrepancyNoteAction` and `Run ImportDataEntry="true"` fire during SOAP
  import and auto-create open *Failed Validation Check* discrepancy notes.
* **Soft edit checks would break seeding.** In the ws import path a soft
  check violation (CRF VALIDATION column / ODM RangeCheck) fails the entire
  `importData` call — nothing is written. The CRF templates below therefore
  carry **no VALIDATION and no WIDTH_DECIMAL entries at all**.
* **Re-import cannot reset a demo.** The import rule runner dedups against
  `rule_action_run_log` (same item + value + rule fires only once, ever).
  Reset = DB restore (`reset_demo.sh`), never re-seeding.

## Manual UI sequence

Do these in order on the OC UI (`https://<DEMO_DOMAIN>/OpenClinica`, behind
the Traefik basic-auth). OC auto-generates every OID from the names you type,
so **use the exact names given here** — all OIDs in `seed_data.xml` and
`rules.xml` depend on them.

### 1. Build the study

Tasks -> Build Study -> Create Study:

* Study Name: `BJT-DEMO-01`
* **Unique Protocol ID: `BJT-DEMO`** (not `BJT-DEMO-01`: the study OID is
  `S_` + first 8 alphanumerics, so `BJT-DEMO` gives `S_BJTDEMO` while
  `BJT-DEMO-01` would give `S_BJTDEMO0`)
* Principal Investigator / Sponsor: any placeholder; mark everything
  synthetic.

After creation confirm the OID shown on the study page is `S_BJTDEMO`.

### 2. Create the three study events

Build Study -> Create Study Event Definitions. Names generate the OIDs
(`SE_` + name, uppercased, non-alphanumerics stripped):

| Name (type exactly) | Type | Repeating | Resulting OID |
|---------------------|------|-----------|---------------|
| `Screening` | Scheduled | No | `SE_SCREENING` |
| `Visit 1` | Scheduled | No | `SE_VISIT1` |
| `Visit 2` | Scheduled | No | `SE_VISIT2` |

### 3. Create and upload the three CRFs (Excel templates)

Build Study -> Create CRF: author three spreadsheets from the stock OC 3.17
CRF template. Naming drives the OIDs: CRF OID = `F_` + first 12 chars of the
CRF name; item OID = `I_` + first 5 chars of the CRF name + `_` + item name
(so CRF `VITALS` yields the `I_VITAL_*` prefix); version OID = CRF OID + `_`
+ version (`v1.0` -> `V10`). Leave every item **ungrouped** (GROUP_LABEL
empty) — ungrouped items land in `IG_<CRF5>_UNGROUPED`, which is what
`rules.xml` and `seed_data.xml` reference.

Common to all three sheets:

* CRF sheet: `VERSION` = `v1.0`.
* Items sheet: `RESPONSE_TYPE` = `text` for every item; `DATA_TYPE` as per
  the tables; **leave `VALIDATION`, `VALIDATION_ERROR_MESSAGE`,
  `WIDTH_DECIMAL` and `GROUP_LABEL` empty everywhere** (see "Why" above —
  width/decimal violations are hard import errors, soft checks kill the
  import). Marking items `REQUIRED` is fine: the SOAP import does not
  enforce it, and the "missing required field" queries rely on that.

**CRF `DEMOG`** (version OID `F_DEMOG_V10`):

| ITEM_NAME | DESCRIPTION_LABEL | DATA_TYPE | Resulting OID |
|-----------|-------------------|-----------|----------------|
| `SCREENDATE` | Screening / informed consent date | DATE | `I_DEMOG_SCREENDATE` |
| `AGE` | Age at screening (years) | INT | `I_DEMOG_AGE` |
| `SEX` | Sex (M/F) | ST | `I_DEMOG_SEX` |
| `ENROLLNOTE` | Enrollment log transcription | ST | `I_DEMOG_ENROLLNOTE` |

**CRF `VITALS`** (version OID `F_VITALS_V10`; note the `I_VITAL_` prefix —
5-char truncation):

| ITEM_NAME | DESCRIPTION_LABEL | DATA_TYPE | Resulting OID |
|-----------|-------------------|-----------|----------------|
| `VISITDATE` | Date of assessment | DATE | `I_VITAL_VISITDATE` |
| `SBP` | Systolic BP (mmHg) | INT | `I_VITAL_SBP` |
| `DBP` | Diastolic BP (mmHg) | INT | `I_VITAL_DBP` |
| `HR` | Heart rate (bpm) | INT | `I_VITAL_HR` |
| `WEIGHT` | Body weight (kg) | REAL | `I_VITAL_WEIGHT` |
| `HEIGHT` | Height (cm) | REAL | `I_VITAL_HEIGHT` |
| `BMI` | BMI (kg/m2, site-calculated) | REAL | `I_VITAL_BMI` |
| `NOTE` | Vitals chart transcription | ST | `I_VITAL_NOTE` |

**CRF `LABS`** (version OID `F_LABS_V10`):

| ITEM_NAME | DESCRIPTION_LABEL | DATA_TYPE | Resulting OID |
|-----------|-------------------|-----------|----------------|
| `COLLDATE` | Sample collection date | DATE | `I_LABS_COLLDATE` |
| `HGB` | Hemoglobin (g/dL) | REAL | `I_LABS_HGB` |
| `WBC` | WBC (10^9/L) | REAL | `I_LABS_WBC` |
| `CREAT` | Serum creatinine (mg/dL) | REAL | `I_LABS_CREAT` |
| `SOURCE_NOTE` | Central lab report transcription | ST | `I_LABS_SOURCE_NOTE` |

After each upload open the CRF's detail page and **verify the generated OIDs
match the tables** (OC appends random suffixes on collisions; a mismatch
here would make every later step fail). Fix by deleting and re-uploading
with corrected names.

### 4. Assign CRFs to events

Build Study -> Update Study Event Definitions:

* `Screening`: add `DEMOG` and `VITALS` (default version v1.0).
* `Visit 1`: add `VITALS` and `LABS`.
* `Visit 2`: add `VITALS` and `LABS`.

Then set the study status to **Available** (Build Study -> Update Study ->
status), or imports and rule uploads will be refused.

### 5. Upload rules.xml (Rules module)

Tasks -> Monitor and Manage Data -> **Rules** -> Create New Rule(s) ->
upload `edc/rules.xml` -> review the validation screen (it checks every
Target/Expression OID against the live study — this is where any step-3
naming mistake surfaces) -> confirm. Expect **10 rule definitions across 18
assignments**, all Available. Without this step seeding "succeeds" but zero
queries are raised.

### 6. Register subjects and schedule events

Tasks -> Add Subject, 14 times. Use **Study Subject IDs `001` .. `014`** —
the subject OID is `SS_` + the ID, so these yield exactly `SS_001`..`SS_014`
as used in `seed_data.xml`. Enrollment date: the subject's screening date
(see `seed_data.xml`), sex/DOB per the seeded demographics (or leave at
site defaults — the CRF data, not the registration form, is what the demo
reads).

Scheduling all 3 events x 14 subjects: the Add Subject screen lets you
schedule the **first** event (`Screening`) at registration time; afterwards
open each subject in the Subject Matrix and schedule `Visit 1` and
`Visit 2` (view subject -> Schedule New Event). That is 14 registrations +
28 extra schedules of UI work; there is no true bulk screen in OC 3.17 CE.
Scripted alternative if this becomes a recurring chore: the OpenClinica-ws
`studySubject/v1` (create) and `event/v1` (schedule) SOAP services can do
both — `oc3_client.py` does not implement them yet, so this is a documented
option, not a shipped path. Schedule ALL three events for ALL 14 subjects
even though SS_013/SS_014 get no Visit 2 data — the import updates event
shells, it cannot create them.

### 7. Flag the web-services account

Administration -> Users: the account `seed.py` will use needs

* a data-entry-capable role at the study, and
* **"Authorize SOAP web services in this account" = yes** (the
  `run_webservices` flag) — without it every SOAP call returns the
  "Authorization is required to execute SOAP web services" fault.

### 8. Seed, verify, snapshot

On a machine that can reach the proxy/OC (see the network notes in
`infra/hetzner/openclinica/docker-compose.openclinica.yml`):

```bash
export OC_BASE_URL=https://<DEMO_DOMAIN>/OpenClinica
export OC_USER=<ws-user> OC_PASS=<password>
# OC_WS_BASE_URL defaults to "$OC_BASE_URL-ws"

python edc/seed.py --verify        # 40 subject-event imports; expect 16 open queries
```

`seed.py` imports one SOAP call per subject per event, Screening first —
required, because the cross-event date rule resolves the screening date from
the DB (details in the `seed.py` docstring). Then freeze the baseline
**immediately, before anyone touches the demo**:

```bash
./edc/reset_demo.sh snapshot       # golden post-seeding pg_dump (run on the OC host)
```

### 9. Reset between demo runs

```bash
./edc/reset_demo.sh                # restore the snapshot, <60s
```

Re-running `seed.py` is NOT a reset: the import rule runner dedups fired
actions against `rule_action_run_log`, so re-imported values raise no new
queries. The restore rolls back item values, discrepancy notes, audit rows
and the rule action log together.

## Expected end state after seeding

16 open queries across 12 subjects — by class: 2 out-of-range labs
(resolvable via the Visit 2 repeat draw/addendum), 5 unit/decimal-shift
notes on 3 items (resolvable via height+BMI back-calculation or the
same-visit source note), 2 date-order inconsistencies (screening CRF
authoritative), 3 missing required fields (recoverable from source-note
fields), and 4 deliberately **unresolvable** queries where the correct agent
behavior is to abstain and refer to the site. Per-query detail and exact
source-of-truth OIDs: `query_resolution_map.md`.
