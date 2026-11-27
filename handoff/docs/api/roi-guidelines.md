# ROI contouring guidelines — API for people and agents

Contract: `CONTRACT.md` §122 (v2.14). Owner of the routes: labeling-server (the hub). This file is written so that an
agent can do the whole job from it alone. If something here disagrees with what the server answers, the server is
right — report it to the platform owner.

## What this is

The hub keeps one contouring guideline per **ROI** (e.g. `Liver`) and **language** (`en`, `ko`). People read them at
`https://platform.oncosoft.io/roi-catalog` (Guidelines view). Any signed-in account may edit them: in the page, or
through this API with a personal token.

A ROI page has three parts. Only the second can be changed here:

| Part | Field | Editable |
|---|---|---|
| Header: region, model group, modalities, cleared countries, aliases | `header` | no — derived from the ROI catalogue |
| Guideline text: sources, border tables, notes, figures | `html` | **yes** |
| Ground truth examples (pictures made from model data) | `examples_html` | no |

Every save is a new **revision**. Nothing is ever overwritten or deleted, and any revision can be restored, so a
mistake is cheap — but each save is recorded with your username.

## Rules before you write

1. **No patient names, patient ids, hospital chart numbers or dates of birth** — not in text, not in a picture, not
   in a file name. The server cannot check this. You must.
2. Change only what you were asked to change. Read the current text first and keep the rest byte for byte.
3. Always send the `base_revision` you read. Never retry a `409` by just raising the number: re-read, merge, then save.
4. Write a `note` that says what changed and why (one line). It is shown in the history.
5. Keep both languages in step when the change is about content: edit `en` and `ko`, each with its own revision.

## Get a token

Sign in at `https://platform.oncosoft.io`, open **계정 / Account** (labelers: in the sidebar; others: the user menu),
go to the **API 토큰 / API tokens** card (`https://platform.oncosoft.io/account#tokens`), create one and copy it. It starts with `olt_`, is shown once, and acts as you. Up to 10 live tokens per person; revoke
one you no longer use. Never commit a token or paste it into a guideline, a chat or a report.

```bash
export HUB=https://platform.oncosoft.io          # inside the office network also http://10.101.1.183:8790
export OLT=olt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Every call carries `Authorization: Bearer $OLT`. Without a valid token: `401 bad_token`.

## Routes

All under `$HUB/api/v1/roi-guidelines`. JSON in, JSON out, UTF-8.

### List

```bash
curl -s -H "Authorization: Bearer $OLT" "$HUB/api/v1/roi-guidelines?q=liver"
```

Query (all optional): `q` (text in the ROI name), `region` (`Head & Neck`, `Thorax`, `Abdomen`, `Pelvis`),
`modality` (`CT`, `MR`).

```json
{"count": 1, "items": [{"roi": "Liver", "group": "Abdomen", "modalities": ["CT", "MR"], "regions": ["Abdomen"],
  "has_guideline": true, "has_examples": true,
  "langs": {"en": {"revision": 3, "at": "2026-11-28T05:10:00Z", "by": "kjh"},
            "ko": {"revision": 1, "at": "2026-11-28T03:00:00Z", "by": "import"}}}]}
```

### Read one

```bash
curl -s -H "Authorization: Bearer $OLT" "$HUB/api/v1/roi-guidelines/Liver?lang=en" > liver.en.json
```

`lang` = `en` (default) or `ko`. `revision=n` reads an old revision.

```json
{"roi": "Liver", "lang": "en", "revision": 3, "by": "kjh", "at": "2026-11-28T05:10:00Z", "note": "IVC wording",
 "header": {"regions": ["Abdomen"], "group": "Abdomen", "modalities": ["CT", "MR"], "cleared": ["FDA", "Japan", "KR"], "aliases": ["liver"]},
 "html": "<div class=\"src\">…</div><h3>Borders — Liver</h3>…",
 "examples_html": "…", "files_base": "/api/v1/roi-guidelines/files/"}
```

A ROI that has no text yet answers `revision: 0` and `html: ""`. An unknown ROI: `404 unknown_roi`.

### Save

```bash
curl -s -X PUT -H "Authorization: Bearer $OLT" -H "Content-Type: application/json" \
  "$HUB/api/v1/roi-guidelines/Liver" \
  --data-binary @- <<'JSON'
{"lang": "en", "base_revision": 3, "note": "Exclude the gallbladder: wording made explicit",
 "html": "<div class=\"src\">…</div><h3>Borders — Liver</h3>…"}
JSON
```

Send the **whole** guideline text, not a fragment. Answer: `200 {"roi": "Liver", "lang": "en", "revision": 4, "by": "you", "at": "…"}`.

Build the JSON body with a real JSON encoder (`jq`, Python `json.dumps`) — never by pasting HTML into a string by hand.

```bash
jq -n --arg html "$(cat liver.en.html)" --argjson base 3 \
  '{lang: "en", base_revision: $base, note: "…", html: $html}' > body.json
curl -s -X PUT -H "Authorization: Bearer $OLT" -H "Content-Type: application/json" \
  "$HUB/api/v1/roi-guidelines/Liver" --data-binary @body.json
```

### History and restore

```bash
curl -s -H "Authorization: Bearer $OLT" "$HUB/api/v1/roi-guidelines/Liver/history?lang=en"
# {"items": [{"revision": 4, "by": "you", "at": "…", "note": "…", "bytes": 15310}, …]}   newest first

curl -s -X POST -H "Authorization: Bearer $OLT" -H "Content-Type: application/json" \
  "$HUB/api/v1/roi-guidelines/Liver/restore" \
  -d '{"lang": "en", "revision": 3, "base_revision": 4, "note": "undo rev 4"}'
# a restore is a new revision (5) holding the text of revision 3
```

### Pictures

```bash
curl -s -X POST -H "Authorization: Bearer $OLT" -F "file=@liver_borders.png" \
  "$HUB/api/v1/roi-guidelines/Liver/files"
# 201 {"path": "Liver/3f9a1c0b7e2d4a65.png", "src": "/ui/api/roi-guidelines/files/Liver/3f9a1c0b7e2d4a65.png"}
```

`png`, `jpeg` or `webp`, at most 5 MB. Put the returned `src` into `<img src="…" alt="…">` exactly as given.
Pictures from other sites cannot be linked: upload them. The server names the file by its content, so your file
name is not kept. `src` always starts with `/ui/api/…` (the page shows it with the reader's login); to download a
picture with your token, use `files_base` + `path`: `$HUB/api/v1/roi-guidelines/files/Liver/3f9a1c0b7e2d4a65.png`.

### Start a page for a ROI that has none

```bash
curl -s -X POST -H "Authorization: Bearer $OLT" -H "Content-Type: application/json" \
  "$HUB/api/v1/roi-guidelines" -d '{"roi": "Kidney_L"}'
```

The name must be an official name of the ROI registry: otherwise `422 not_in_registry`. `409 exists` if it has a page.

## The edit loop

1. `GET` the ROI in the language you edit. Keep `revision` and `html`.
2. Change `html` locally. Touch only what was asked.
3. `PUT` with `base_revision` = the `revision` from step 1 and a `note`.
4. `200` → read it back once and check your change is there. Done.
5. `409 revision_conflict` → someone saved meanwhile. `GET` again, apply your change on top of **their** text, `PUT`
   with the new revision. Do not discard their change.
6. `422 html_rejected` → fix what `reasons` names and send again. Nothing was saved.

## What the text may contain

The server rejects a text that breaks a rule (it does not clean it up for you).

- Elements: `h2 h3 h4 h5 p br hr ul ol li table thead tbody tr th td div span b strong i em u sub sup a img details
  summary blockquote figure figcaption code`.
- Attributes: `class`, `colspan`, `rowspan`, `open`, `title`, `alt`, `href`, `src`.
- `href`: `http:`, `https:` or `#…`. `src`: only a picture of this store.
- Links come back from the server with `target="_blank" rel="noopener"`; leave them as they are (they are accepted
  on save). A text you read and send back unchanged always passes.
- `reasons` of a `422` look like `{"rule": "element|attribute|href|src|markup", "at": "line:col", "detail": "…"}`.
- Not allowed: `script`, `style`, `iframe`, any `on…` attribute, the `style` attribute, `data:` and `javascript:` URLs.
- At most 512 KB.
- No `h1` and no header badges: the page adds the title and the header itself.

Classes the page styles (use them as the existing texts do): `src` (a source box: title, publisher, summary, link),
`tw` + `table.bd` (a border table: `th` = Cranial / Caudal / Anterior / Posterior / Lateral / Medial / Definition),
`mut` (grey remark), `more` on `details` (collapsed further references), `guide`, `caps`.

## Errors

| Status | `error` | Meaning | What to do |
|---|---|---|---|
| 401 | `bad_token` | no token, wrong or revoked | make a new token |
| 404 | `unknown_roi` | no such ROI page | check the name with the list route |
| 409 | `revision_conflict` | `base_revision` is not the current one (`current_revision` tells it) | re-read, merge, save |
| 409 | `exists` | the ROI already has a page | read it instead |
| 413 | `too_large` | text > 512 KB or picture > 5 MB | shorten / shrink |
| 415 | `bad_type` | picture is not png / jpeg / webp | convert |
| 422 | `html_rejected` | a rule above is broken; `reasons: [{rule, at}]` | fix and resend |
| 422 | `not_in_registry` | not an official ROI name | ask a lead to add the ROI first |

## A prompt you can give your agent

> Read `https://platform.oncosoft.io/docs/api/roi-guidelines.md`. My token is in the environment variable `OLT`.
> In the contouring guideline of ROI `<name>`, `<what to change>` — in English and Korean. Show me the difference
> before you save, then save with a note, and read it back.
