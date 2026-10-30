# Folder layouts and how to express them in a proposal

## What the pipeline does with a proposal

Deterministic code applies the proposal (`dataops/spec.py`); nothing in it is executed.
- **NIfTI images** under a source with role `image` become CT or MR DICOM series, with one study per subject.
- **NIfTI label maps** under a source with role `label` become an RTSTRUCT on their paired image, with
  one ROI per label value. The label must have exactly the image's grid (shape and affine); otherwise
  the label is skipped and reported.
- **DICOM files** under an image source pass through, one series per SeriesInstanceUID.
  - Their subject key is the PatientID tag and their modality the Modality tag.
  - So `subject_pattern` and `modality_rules` only apply to NIfTI. For a DICOM-only dataset, give
    `subject_pattern` `"^(.+)$"` and no rules.
  - You cannot read PatientIDs, so leave `pilot_subjects` empty for DICOM; the pipeline picks labelled
    subjects itself.
- **Everything else** in the folder that is not under a source is left out and reported. List deliberate
  exclusions in `excluded`, with reasons.

## Proposal fields

- `sources[].path`
  - Paths are relative to the dataset root, use `/` and must exist.
  - Prefer whole folders, at the level that does not reveal names (a parent folder, not a patient folder).
  - Include only one copy of each scan.
- `subject_pattern.regex`
  - A Python regex searched in the file **name**, not the path.
  - Group 1 is the subject key, e.g. `^(amos_\d{4})`.
  - The image and label of one case must give the same key.
  - A subject may have several images, but then each needs a different file name.
- `modality_rules`
  - Regexes searched in the relative path. The first match wins, and every NIfTI image must match one rule.
  - Base each rule on documented facts (README, dataset.json `modality`, ID ranges, folder names) and cite
    them in `basis`.
- `label_pairing`
  - `same_name`: the label file name equals the image file name.
  - `subject`: the subject has exactly one image.
- `label_names`
  - Take them from the dataset's own label definition (e.g. dataset.json `labels`).
  - Skip background 0 and keep the original wording.
- `nifti_layout`
  - `standard`, unless the files are Onco NIfTI (LPS, arrays Z,Y,X, made by nii2OncoNii), which is `onco`.
- `pilot_subjects`
  - Exactly three subject keys, used as samples.
  - Prefer cases with labels. When the dataset has several modalities, take one of each.

## Known layouts

"Converts" says whether the pipeline can turn the layout into DICOM today. When it says **no**, do not
invent a mapping: put the folder in `excluded` with the reason "형식 미지원: <layout>" and ask in
`questions` whether to wait for support.

| # | Layout | How to recognise it | Proposal | Converts |
|---|---|---|---|---|
| 1 | nnU-Net / MSD | `imagesTr/`, `labelsTr/`, `imagesTs/`, `dataset.json` | images `imagesTr` (train) and `imagesTs` (test); labels `labelsTr`; `same_name`; subject `^(.+?)(?:_\d{4})?\.nii`; modality from dataset.json `modality`/`channel_names`; label names from `labels` | yes |
| 2 | Flat channel files `_0000` | `case_0000.nii.gz`, `case_0001.nii.gz` (channels) next to `case.nii.gz` (label) | each channel is its own image (series per file name); label pairs by `subject`, only if the subject has one image, otherwise ask | partly: multi-channel subjects need a question |
| 3 | Flat image/label pairs by prefix | `img0001.nii.gz` + `label0001.nii.gz`, or `*_ct.nii.gz` + `*_seg.nii.gz` | one source per role (Glob the folder for each pattern; the role comes from the source, so split by folder); otherwise `subject` pairing with a regex that takes the shared ID | yes when images and labels are in different folders; same folder: ask |
| 4 | Folder per subject, fixed names | `s0001/ct.nii.gz`, `s0001/segmentations/*.nii.gz` | the subject key is in the folder name, but `subject_pattern` reads the file name only, so if file names repeat, say so in questions | partly: needs subject-from-folder (not yet) |
| 5 | Subject-prefixed files | `case_00001_imaging.nii.gz`, `case_00001_segmentation.nii.gz` (KiTS) | images and labels in the same folder cannot be split by role today: ask | partly |
| 6 | Parallel label-family folders | `labels_organ/`, `labels_tumor/` beside `images/` | one label source per family; each becomes its own RTSTRUCT | yes, if `same_name` |
| 7 | Per-organ binary masks | `segmentations/liver.nii.gz`, `kidney_left.nii.gz` … per subject (TotalSegmentator) | many masks per image are not merged into one RTSTRUCT yet | **no**: exclude the masks, keep the images, ask |
| 8 | NBIA / TCIA manifest | `metadata.csv` with `Series UID`, `Subject ID`, `Collection`; tree `<Collection>/<Subject>/<date-study>/<series>/1-001.dcm` | DICOM image source at the collection folder; subject = PatientID tag | yes (DICOM pass-through) |
| 9 | IDC / UID tree | folders named by StudyInstanceUID / SeriesInstanceUID | DICOM image source at the root | yes |
| 10 | Flat SeriesUID folders (e.g. LNQ) | one folder per series UID, DICOM inside, SEG objects beside | images yes; DICOM SEG passes through as is (OncoStudio support unknown) | partly: ask about SEG |
| 11 | 2-D `.mat` / PNG / JPG | MATLAB files, 2-D slices | not imaging volumes for the SCP | **no** |
| 12 | Umbrella folders / copies / archives only | several datasets under one folder, `_2` or `copy` duplicates, only `.zip`/`.tar`/`.7z` | one dataset per run: pick the subfolder, exclude copies; archives are not unpacked | archives: **no** |
| 13 | Clinical export (MIM etc.) | `<staff>/<YYYY-MM>__Studies/<이름>_<환자번호>_<CT\|RTst>_<날짜>…/` with one series per folder, CT + RTSTRUCT | DICOM image source at the export root (never a patient folder); internal data, so tier IO and pseudonymisation needed; say "식별 정보 있음 — 비식별 필요" in findings | yes, with pseudonymisation on |

Also check every time:
- **Truncated series.** A folder name like `n<count>` that is larger than the files present means the
  series is incomplete. Report it in findings.
- **Derived copies.** A NIfTI copy of a DICOM set (e.g. `*_CTV/` beside the export) is not a second dataset.
  Exclude it and say what it was derived from.
- **Test splits without labels.** Count them per split and say so in findings.
- **MHA / NRRD files.** The scan counts them, but the pipeline converts NIfTI only. Report them and ask.
