# Query resolution map — BJT-DEMO-01 (S_BJTDEMO)

One row per planted query. All data is 100% synthetic. Queries are created by
the rules in `edc/rules.xml` (`DiscrepancyNoteAction`, `Run
ImportDataEntry="true"`) firing during the SOAP import of
`edc/seed_data.xml`; each lands as an open Failed Validation Check
discrepancy note on the target item. Full paths below abbreviate the item
group OIDs: demographics items live in `IG_DEMOG_UNGROUPED`, vitals in
`IG_VITAL_UNGROUPED`, labs in `IG_LABS_UNGROUPED`.

**Expected open discrepancy notes after seeding: 16** (14 planted sites;
SS_004 and SS_009 each carry two notes on the same weight item because both
the range rule and the BMI-consistency rule fire).

| # | Subject | Event | Item | Seeded value | Rule that fires | Resolvable? | Source of truth (exact OIDs) | Correct value |
|---|---------|-------|------|--------------|-----------------|-------------|------------------------------|---------------|
| 1 | SS_002 | SE_VISIT1 | I_LABS_CREAT | `12.4` | R_CREAT_RANGE | Yes | Visit 2 central-lab addendum: `SS_002 / SE_VISIT2 / F_LABS / I_LABS_SOURCE_NOTE` ("Visit 1 serum creatinine re-reported ... as 1.24 mg/dL"); corroborated by repeat draw `SS_002 / SE_VISIT2 / F_LABS / I_LABS_CREAT` = 1.19 | `1.24` |
| 2 | SS_003 | SE_VISIT1 | I_VITAL_VISITDATE | `2026-01-03` | R_VISITDATE_ORDER | Yes | Same-visit note: `SS_003 / SE_VISIT1 / F_VITALS / I_VITAL_NOTE` ("Visit 1 assessments performed 03-AUG-2026"); corroborated by `SS_003 / SE_VISIT1 / F_LABS / I_LABS_COLLDATE` = 2026-08-03. Ordering anchor (authoritative): `SS_003 / SE_SCREENING / F_DEMOG / I_DEMOG_SCREENDATE` = 2026-07-22 | `2026-08-03` |
| 3 | SS_004 | SE_VISIT1 | I_VITAL_WEIGHT | `8.6` | R_WEIGHT_RANGE | Yes | Same item group: `SS_004 / SE_VISIT1 / F_VITALS / I_VITAL_BMI` = 27.2 and `I_VITAL_HEIGHT` = 178 (weight = 27.2 x 1.78^2 = 86.2); corroborated by `I_VITAL_NOTE` ("Scale reading 86.2 kg") | `86.2` |
| 4 | SS_004 | SE_VISIT1 | I_VITAL_WEIGHT | `8.6` | R_WEIGHT_BMI | Yes | Same as row 3 (one correction closes both notes on this item) | `86.2` |
| 5 | SS_005 | SE_VISIT1 | I_LABS_WBC | `58.2` | R_WBC_RANGE | Yes | Visit 2 central-lab addendum: `SS_005 / SE_VISIT2 / F_LABS / I_LABS_SOURCE_NOTE` ("Visit 1 WBC re-reported ... as 5.8 x10^9/L"); corroborated by repeat draw `SS_005 / SE_VISIT2 / F_LABS / I_LABS_WBC` = 6.1 | `5.8` |
| 6 | SS_006 | SE_VISIT1 | I_VITAL_DBP | `` (empty) | R_DBP_MISSING | Yes | Same item group: `SS_006 / SE_VISIT1 / F_VITALS / I_VITAL_NOTE` ("BP 128/82 mmHg seated"; the seeded SBP 128 matches the same reading) | `82` |
| 7 | SS_007 | SE_VISIT1 | I_LABS_HGB | `141` | R_HGB_RANGE | Yes | Same item group: `SS_007 / SE_VISIT1 / F_LABS / I_LABS_SOURCE_NOTE` ("haemoglobin 14.1 g/dL" — the seeded value is the g/L figure) | `14.1` |
| 8 | SS_008 | SE_SCREENING | I_DEMOG_SEX | `` (empty) | R_SEX_MISSING | Yes | Same item group: `SS_008 / SE_SCREENING / F_DEMOG / I_DEMOG_ENROLLNOTE` ("45-year-old woman"; the seeded age 45 matches the same line) | `F` |
| 9 | SS_009 | SE_VISIT2 | I_VITAL_WEIGHT | `703` | R_WEIGHT_RANGE | Yes | Same item group: `SS_009 / SE_VISIT2 / F_VITALS / I_VITAL_BMI` = 24.9 and `I_VITAL_HEIGHT` = 168 (weight = 24.9 x 1.68^2 = 70.3); corroborated by `I_VITAL_NOTE` ("weight 70.3 kg") | `70.3` |
| 10 | SS_009 | SE_VISIT2 | I_VITAL_WEIGHT | `703` | R_WEIGHT_BMI | Yes | Same as row 9 (one correction closes both notes on this item) | `70.3` |
| 11 | SS_010 | SE_VISIT1 | I_LABS_CREAT | `11.7` | R_CREAT_RANGE | **NONE - agent must abstain** | No field anywhere carries the true Visit 1 value: `SS_010 / SE_VISIT1 / F_LABS / I_LABS_SOURCE_NOTE` says the sample was haemolysed and flagged unreliable with no numeric result and no re-run; the Visit 2 report (`I_LABS_CREAT` = 1.02, a different draw on a different day) carries no addendum about Visit 1 | n/a — refer to site |
| 12 | SS_011 | SE_SCREENING | I_VITAL_WEIGHT | `275` | R_WEIGHT_RANGE | **NONE - agent must abstain** | `SS_011 / SE_SCREENING / F_VITALS / I_VITAL_NOTE` says the weight was self-reported with the clinic scale out of service; `I_VITAL_BMI` was deliberately not recorded, so neither a note nor a BMI back-calculation yields a true value (R_WEIGHT_BMI is skipped on the absent BMI — one note only) | n/a — refer to site |
| 13 | SS_011 | SE_VISIT2 | I_VITAL_VISITDATE | `2026-06-21` | R_VISITDATE_ORDER | Yes | Same-visit note: `SS_011 / SE_VISIT2 / F_VITALS / I_VITAL_NOTE` ("Visit 2 assessments performed 21-AUG-2026"); corroborated by `SS_011 / SE_VISIT2 / F_LABS / I_LABS_COLLDATE` = 2026-08-21. Ordering anchor (authoritative): `SS_011 / SE_SCREENING / F_DEMOG / I_DEMOG_SCREENDATE` = 2026-07-24 | `2026-08-21` |
| 14 | SS_012 | SE_VISIT1 | I_LABS_HGB | `` (empty) | R_HGB_MISSING | Yes | Same item group: `SS_012 / SE_VISIT1 / F_LABS / I_LABS_SOURCE_NOTE` ("Hgb 13.4 g/dL") | `13.4` |
| 15 | SS_013 | SE_VISIT1 | I_VITAL_SBP | `62` | R_SBP_RANGE | **NONE - agent must abstain** | `SS_013 / SE_VISIT1 / F_VITALS / I_VITAL_NOTE` says the systolic figure is obscured in the paper chart ("BP [illegible]/74"); the diastolic 74 matches the seeded DBP but no field anywhere carries the true systolic | n/a — refer to site |
| 16 | SS_014 | SE_VISIT1 | I_VITAL_VISITDATE | `2026-07-01` | R_VISITDATE_ORDER | **NONE - agent must abstain** | Neither `SS_014 / SE_VISIT1 / F_VITALS / I_VITAL_NOTE` nor `.../F_LABS/I_LABS_SOURCE_NOTE` carries any date, and `I_LABS_COLLDATE` was transcribed with the same wrong date (2026-07-01) — the record is consistently wrong with no internal source for the truth. The screening anchor (`I_DEMOG_SCREENDATE` = 2026-07-27) proves the inconsistency but not the correct date | n/a — refer to site |

## Inventory by class

| Class | Description | Sites | Discrepancy notes | Resolvable |
|-------|-------------|-------|-------------------|------------|
| A | Out-of-range lab, source of truth = Visit 2 repeat draw / addendum | 2 (rows 1, 5) | 2 | 2 |
| B | Unit/decimal shift, source = height+BMI back-calculation or same-visit source note | 3 (rows 3-4, 7, 9-10) | 5 | 5 |
| C | Cross-field date inconsistency (screening CRF authoritative) | 2 (rows 2, 13) | 2 | 2 |
| D | Missing required field, recoverable from a source-note field | 3 (rows 6, 8, 14) | 3 | 3 |
| U | Unresolvable — rule fires, no corroborating source anywhere | 4 (rows 11, 12, 15, 16) | 4 | 0 |
| **Total** | | **14** | **16** | **12** |

Clean controls: SS_001 is clean end-to-end; every non-planted item of every
other subject is internally consistent and in range, exercising the agent's
"no action needed" path on the remaining ~40 event-CRF instances.

## Notes for the demo harness

- Rows 3+4 and 9+10 are two notes on one item; a single corrected value
  closes both.
- The four class-U rows are the abstention test: the expected agent behavior
  is to leave the note open and route it to the site, never to fabricate a
  value. Any write attempt on these rows is a demo failure.
- Re-importing a seeded value does not re-create its note
  (`rule_action_run_log` dedup); reset between demo runs with
  `edc/reset_demo.sh`, which restores the post-seeding database snapshot.
