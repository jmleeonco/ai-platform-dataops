# Folder layouts and how to express them in a proposal

## What the pipeline does with a proposal

Deterministic code applies the proposal (`dataops/spec.py`); nothing in it is executed.
- **NIfTI images** under a source with role `image` become CT or MR DICOM series, with one study per subject.
- **NIfTI labels** under a source with role `label` become an RTSTRUCT on their paired image. The label must
  have exactly the image's grid (shape and affine); otherwise the label is skipped and reported.
  - `label_kind` `label_map`: one file holds every structure, one ROI per label value.
  - `label_kind` `binary_masks`: one file per structure (0 / non-zero). All masks paired with one image become
    one RTSTRUCT with one ROI per file; masks may overlap (CTV around GTV). The ROI name is group 1 of
    `roi_name_regex` searched in the file name, or the file name without `.nii.gz`.
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
- `sources[].pattern`
  - A file-name glob (`*`, `?`, `[..]`; no `/`) that limits the source to matching files, or `""` for all files.
  - Use it when images and labels share a folder: two sources with the same path, one `image` with e.g.
    `volume-*.nii.gz`, one `label` with `labels-*.nii.gz`. At one path a source with a pattern wins over one
    without. Files under the path that match no pattern are reported as left out.
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
  - `same_folder`: the label is in the same folder as exactly one image (folder per subject or per exam).
- `label_kind`: `label_map` or `binary_masks` (see above). `roi_name_regex`: for `binary_masks`, a regex whose
  group 1 is the ROI name, e.g. `_RS_[^_]+_\d+_(.+)\.nii`; `""` uses the file name. Keep dates and IDs out of the
  group: the ROI name leaves the hospital.
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
| 3 | Flat image/label pairs by prefix | `img0001.nii.gz` + `label0001.nii.gz`, `volume-N` + `labels-N` (CT-ORG), or `*_ct.nii.gz` + `*_seg.nii.gz` | one source per role; in the same folder, two sources with the same path and a `pattern` each; `subject` pairing with a regex that takes the shared ID | yes |
| 4 | Folder per subject, fixed names | `s0001/ct.nii.gz`, `s0001/segmentations/*.nii.gz` | the subject key is in the folder name, but `subject_pattern` reads the file name only, so if image file names repeat, say so in questions; labels pair with `same_folder` | partly: needs subject-from-folder (not yet) |
| 5 | Subject-prefixed files | `case_00001_imaging.nii.gz`, `case_00001_segmentation.nii.gz` (KiTS) | two sources with the same path and patterns `*_imaging.nii.gz` / `*_segmentation.nii.gz`; `subject` pairing | yes |
| 6 | Parallel label-family folders | `labels_organ/`, `labels_tumor/` beside `images/` | one label source per family; each becomes its own RTSTRUCT | yes, if `same_name` |
| 7 | Per-organ binary masks | `segmentations/liver.nii.gz`, `kidney_left.nii.gz` … per subject (TotalSegmentator) | `label_kind` `binary_masks`; masks in the image's folder pair with `same_folder`; masks in a subfolder (`segmentations/`) cannot be paired yet: ask | yes when the masks sit beside the image |
| 14 | Clinical NIfTI export with ROI masks | `<subject>/<date>/<subject>_CT_…nii.gz` + `<subject>_RS_…_<ROI>.nii.gz` per ROI in the same folder | image and label sources at the export root with patterns (`*_CT_*`, `*_RS_*`); `same_folder`; `binary_masks`; `roi_name_regex` taking only the ROI part; internal, so tier IO and pseudonymisation on | yes |
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
