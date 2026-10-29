# Custom icon legend — sample set

Test material for the custom icon legend
([docs/architecture.md](../../docs/architecture.md#custom-icon-legend)).

The glyphs are a fictional internal notation, "AnyCompany Bank ACB-STD-04". They
are deliberately arbitrary: nothing about an orange hexagon indicates a Tier-1
application node. That is the point. The legend exists because an on-premises
drawing uses an organization's own vocabulary, and a model shown the drawing
alone reads it with generic priors. A sample built from recognizable industry
icons would not test the feature.


## Files

| Path | Use |
|---|---|
| `icons/acb-*.png` | One glyph crop per file, 256x256 PNG. Upload individually through the icon legend UI, or together through `POST /api/icon-legend/bulk/images`. |
| `icons-legend-sheet.png` | All glyphs on one sheet with their codes, as a legend appears on a real drawing. Useful for checking that a derived definition matches the right symbol. |
| `diagrams/onprem-site-topology.png` | Active-active across two owned sites, drawn entirely in the notation. |
| `diagrams/onprem-dr-failover.png` | Site-level DR and replication paths in the same notation. |
| `bulk-icons.csv` | Text authoring path for `POST /api/icon-legend/bulk/text`. |
| `bulk-icons.json` | The same rows in JSON, for the JSON branch of the same endpoint. |
| `negative/*.png` | Inputs that are not infrastructure icons. |
| `GLYPH_DESCRIPTIONS.md` | The same descriptions in markdown, for pasting into the icon form or for reviewing a derived draft. |

## Ground truth

The meaning of each glyph is recorded in `bulk-icons.csv`, not in the images. A
model interpreting an image has only the pixels, so this table is the reference a
reviewer checks a derived definition against.

| Code | Meaning | Expected `icon_type` | Expected confidence |
|---|---|---|---|
| `ACB-01` | Tier-1 application node running the core banking monolith | `server` | high |
| `ACB-02` | Synchronously replicated core banking database cluster | `database` | high |
| `ACB-03` | Hardware traffic director pair in active-passive mode | `load_balancer` | medium |
| `ACB-04` | Owned on-premises datacenter site | `datacenter` | medium |
| `ACB-05` | PCI-segmented network zone boundary | `network` | low |
| `ACB-06` | Fibre-channel SAN storage array | `storage` | medium |
| `ACB-07` | Hardware security module holding payment signing keys | `other` | low |
| `ACB-08` | Perimeter firewall enforcing the zone policy | `firewall` | high |
| `ACB-09` | Internal API gateway fronting the service estate | `gateway` | medium |
| `ACB-10` | Shared platform service consumed by multiple applications | `service` | low |
| `ACB-11` | Unclassified component placeholder (ambiguous by design) | `other` | low |
| `ACB-12` | Tier-2 application node (same outline as ACB-01, different fill) | `server` | medium |

`ACB-11` is featureless by design and should return low confidence, which is the
case a reviewer is expected to correct or reject. `ACB-01` and `ACB-12` share an
outline and differ only in fill, so interpreting them identically indicates the
reading ignored colour.

Expected confidence is what a competent reading would produce for an
organization-specific glyph, not a pass or fail threshold. A model cannot know
that a hexagon means "core banking node", so a high-confidence claim of the
precise business meaning would be over-confident, and a description of the shape
plus a plausible component type is the correct outcome.

## Text authoring path

`bulk-icons.csv` and `bulk-icons.json` contain valid rows followed by four
deliberate errors, because the endpoint parses strictly and reports bad rows
rather than dropping them. Expected response:

| Row | Outcome |
|---|---|
| `ACB-13` | Rejected: `invalid icon_type 'mainframe'` |
| Empty label | Rejected: `missing label` |
| Whitespace label | Rejected: `missing label` |
| `ACB-14` | Accepted. Only `label` and `icon_type` are validated at this endpoint. |

So a successful call reports 13 created and 3 errors.

### Pairing images to those rows

Both files carry an optional `image_filename` column naming the crop in `icons/`
for each valid row. After the text upload, the review queue offers to upload
those files and matches each one to the row that named it, so a text-authored
row can acquire the visual example it would otherwise lack.

Uploading all twelve files from `icons/` exercises the reporting paths in one
run:

| Outcome | Rows |
|---|---|
| Paired | `ACB-01` … `ACB-12` |
| Unmatched row | `ACB-14`, which names `acb-99-missing.png` — a file that does not exist |
| Unmatched file | none, unless a file outside `icons/` is included |

Each pair is cross-checked against the row's written description, so a
deliberately wrong pairing is worth trying: attach `acb-02.png` (the database
cylinder) to the `ACB-08` firewall row and the response should report it under
`mismatches` rather than accepting it silently.

## Suggested end-to-end run

1. Sign in as a curator.
2. Upload `icons/` through the bulk image path. Each image is interpreted and
   lands as a **draft**.
3. Open the review queue and compare each derived definition against the ground
   truth above. Correct where the reading is wrong. Confirm a draft cannot be
   approved without the image being visible.
4. Approve the batch. Only then do the entries become `active` and reach a prompt.
5. Upload `diagrams/onprem-site-topology.png` as a design document and run a
   validation.
6. Repeat with the legend entries set back to draft, and compare. The difference
   between the two runs is what the legend contributes.

Step 6 is the measurement that matters. Everything before it only confirms the
authoring flow works.
