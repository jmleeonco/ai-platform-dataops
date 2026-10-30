# Naming: prefix, PatientID, PatientName

This is a copy of Notion "04-1. Data prefix 규칙" (v0.6) and "Track P". If the two differ, Notion is right.

## Prefix

```
<SOURCE>_<REGION>[_<MODALITY>][_<SUB>]_<TIER>
```

| Rule | Value | Wrong |
|---|---|---|
| Letters | Upper-case A–Z and digits 0–9 | `ycc_hnn_mr` |
| Separator | `_` only | `YCC-HNN-MR`, `(ACC)` |
| Length | 3–20 characters | `s` |
| First character | A letter | `2020_YCC` |
| Last character | Not `_` | `YCC_HNN_` |
| Uniqueness | No other prefix may be the same, legacy ones included (Notion Data List) | — |
| Second token | A region code | `YCC_BRST_IMPL` |
| Last token | A tier: `PUB`, `NC` or `IO` | `TCIA_FULL_PETCT` (legacy, before tiers existed) |

- **SOURCE.** Who made the data. In-house and hospital data use the internal codes in `codes.json`
  (INT, YCC, SVS, …). Public data use the program or challenge that made it, not the site that hosts it:
  TCGA-READ hosted on TCIA is `TCGA`, not `TCIA`.
  - A new source code needs governance approval before use.
  - A dataset whose name is too long for a prefix gets a short code, with the full name noted in Notion.
  - `UNK` (source unknown) is only for headers that were anonymised and leave no other way to find the
    source. If a person can tell the source, use that code.
- **REGION.** `HNN` (head and neck, brain), `THX`, `ABD`, `PEL` (pelvis, prostate), or `FULL` (two or
  more regions, or the whole body). Write the region the data you hold covers, not the original scan:
  a spine crop of a whole-body scan is that region, not FULL.
- **MODALITY.** Optional, but recommended for a single-modality set: `CT`, `MR`, `MRCT` (both), `MULTI`,
  `PETCT`, `RTS`, `RTP`, `CBCTP`.
- **SUB.** Optional free token for a sub-type: `CON`, `NON`, `PED`, `BRACHY`, `4D`, a year, a cohort
  name such as `CIRT`.
- **TIER.** Required and explicit; no suffix is not allowed:
  - `PUB` when commercial use is Y.
  - `NC` when it is N.
  - `IO` for internal-only data: in-house data, or a restrictive DUA.
- A prefix never changes once given. If it was wrong, the dataset is re-registered under a new prefix.
  The collector proposes the prefix and the Curator fixes it. In the pipeline the agent proposes, and the
  owner can edit it in "설정 · 기록".

## PatientID and PatientName

| Data | PatientID = PatientName | Example |
|---|---|---|
| Public, keeps its case IDs | `<prefix>_<original case ID>` | `TCGA_PEL_MRCT_PUB_TCGA-CL-4957`, `AMOS_ABD_MRCT_NC_amos_0001` |
| Pseudonymised (pseudonymisation on in the pipeline) | `<prefix>_NNNN`, numbered from 0001 | `YCC_PEL_CT_CIRT_IO_0001` |

- PatientName is the same string as PatientID, not `prefix^id`.
- The whole ID must fit in 64 characters, so keep the prefix short when case IDs are long.
- For NIfTI, the "original case ID" is the subject key taken from the file name by `subject_pattern`.
  For DICOM it is the PatientID tag.
- An original DICOM export changes only these two tags when pseudonymisation is off. When it is on, the
  pipeline also empties names and identifiers and replaces UIDs (`dataops/deid.py`).

## Worked examples

These show how the rule is applied. They are not a list of registered prefixes: a prefix that matches a row
here is not thereby taken. Only the Notion Data List says whether a prefix already exists.

| Dataset | Prefix | Why |
|---|---|---|
| AMOS 2022 (CT + MR, abdomen, CC BY-NC-SA) | `AMOS_ABD_MRCT_NC` | Challenge code, abdomen, both modalities, non-commercial |
| TCGA-READ (MR + CT, rectum, CC BY 3.0) | `TCGA_PEL_MRCT_PUB` | Track P no. 1; the program is TCGA, the host is TCIA |
| TotalSegmentator (CT, whole body, CC BY 4.0) | `TSEG_FULL_CT_PUB` | Track P lists TSEG while 04-1 lists TSG: ask which to use |
| Medical Segmentation Decathlon Task03 Liver (CC BY-SA 4.0) | `MSD_ABD_CT_LIVER_PUB` | One prefix per task folder |
| CIRT prostate CT + RTSTRUCT (internal) | `YCC_PEL_CT_CIRT_IO` | Hospital code, pelvis, internal only, pseudonymised |
| NCC cervix brachytherapy MR (internal) | `NCC_PEL_MR_BRACHY_IO` | Existing Data List entry |
