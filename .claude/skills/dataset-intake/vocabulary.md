# Vocabulary: license tier, ROI category, region

The master copy is in Notion: "📖 칸 어휘 — 무슨 값을 언제 쓰나" (under 04-2 Public Data List) and Track P §1(b).

## Commercial use and tier

Read the dataset's own license text. If the sources disagree, check them in this order:
1. The record of the place the files come from (Zenodo, TCIA, grand-challenge page).
2. Its project page (conditions that are not in the API).
3. The original source, if this is a derived dataset. The original is the ceiling: a CC BY copy of
   NC data is suspect.
4. The paper (its license covers the text, not the data).

If they conflict, take the lower one and say so in findings.

| License | commercial_use | Tier |
|---|---|---|
| CC0, CC BY, CC BY-SA | Y | PUB |
| CC BY-NC, CC BY-NC-SA, CC BY-NC-ND, "academic use only", "not for commercial use" | N | NC |
| Restrictive DUA, no redistribution, internal or hospital data | N | IO |
| None found, unclear, gated page not readable | U | — (ask) |

- U does not block download or the local pilot. It does block registration: the pipeline refuses
  approval while commercial use is U.
- With Y, also note the conditions it comes with. CC BY almost always requires a citation; put it in the
  `citation` field.
- License and access are different things: CC BY data behind a sign-up form is still CC BY.

## ROI category (what a label map draws)

| Category | Meaning | Examples |
|---|---|---|
| OAR | Normal anatomy, any organ, bone, vessel or nerve that is not disease | liver, kidney, vertebrae, spinal canal |
| CTV-GTV | Tumour, whatever the dataset calls it (GTV, CTV, tumor, lesion that is a neoplasm), benign included | kidney tumor, meningioma GTV, vestibular schwannoma |
| Findings | Abnormal findings that are not tumour | effusion, oedema, haematoma, cyst, infarct, fracture, stenosis, resection cavity |
| 마스크 없음 | Images only, no segmentation | diagnostic images, bounding boxes only |
| 기타 | Not anatomy | metal or artefact masks, body outline, dose, landmarks |

- If you could not check, leave the category empty. "None" and "not checked" are different answers.

## Region (for the prefix)

- HNN, THX, ABD, PEL, or FULL.
- Write what the data covers. If the original scan was larger than what the dataset holds, use the
  region of what it holds.
