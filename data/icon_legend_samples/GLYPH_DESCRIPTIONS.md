# Icon legend — glyph descriptions (ACB-STD-04)

Ground truth for the sample notation in `data/icon_legend_samples/`. The
meanings are deliberately **not** written into the images: a model interpreting
an icon has only the pixels, so this file is the reference a reviewer checks a
derived definition against.

Use it two ways:

- **Reviewing the image path** — compare each derived draft against the row here.
- **Authoring by hand** — paste a description straight into the icon form, or
  upload `bulk-icons.csv` through the text path, which carries these same rows.

## Glyphs

| Code | Type | Description |
|---|---|---|
| `ACB-01` | `server` | Tier-1 application node running the core banking monolith |
| `ACB-02` | `database` | Synchronously replicated core banking database cluster |
| `ACB-03` | `load_balancer` | Hardware traffic director pair in active-passive mode |
| `ACB-04` | `datacenter` | Owned on-premises datacenter site |
| `ACB-05` | `network` | PCI-segmented network zone boundary |
| `ACB-06` | `storage` | Fibre-channel SAN storage array |
| `ACB-07` | `other` | Hardware security module holding payment signing keys |
| `ACB-08` | `firewall` | Perimeter firewall enforcing the zone policy |
| `ACB-09` | `gateway` | Internal API gateway fronting the service estate |
| `ACB-10` | `service` | Shared platform service consumed by multiple applications |
| `ACB-11` | `other` | Unclassified component placeholder (ambiguous by design) |
| `ACB-12` | `server` | Tier-2 application node (same outline as ACB-01, different fill) |

## Expected confidence

What a competent reading should produce for an organization-specific glyph —
not a pass/fail threshold. A model cannot know that a hexagon means "core
banking node", so a high-confidence claim of the precise business meaning would
be over-confident. Describing the shape plus a plausible component type is the
correct outcome.

| Code | Expected confidence | Why |
|---|---|---|
| `ACB-01` | high | Distinct shape and fill |
| `ACB-02` | high | Conventional cylinder reads as a database |
| `ACB-03` | medium | Directional shape suggests routing |
| `ACB-04` | medium | Building outline suggests a site |
| `ACB-05` | low | Concentric circles carry no inherent meaning |
| `ACB-06` | medium | Array-like body with indicator lights |
| `ACB-07` | low | Keyhole hints at cryptography, not at an HSM specifically |
| `ACB-08` | high | Brick pattern is a firewall convention |
| `ACB-09` | medium | Arrow through a boundary suggests a gateway |
| `ACB-10` | low | Generic circle with dots |
| `ACB-11` | low | **Featureless by design** — the case a reviewer must correct or reject |
| `ACB-12` | medium | Shares `ACB-01`'s outline, differs only in fill |

`ACB-01` and `ACB-12` are a deliberate pair: interpreting them identically
indicates the reading ignored colour.

## Error rows in the bulk files

`bulk-icons.csv` and `bulk-icons.json` also carry four invalid rows, because the
text endpoint parses strictly and reports bad rows rather than dropping them:

| Row | Outcome |
|---|---|
| `ACB-13` | Rejected: `invalid icon_type 'mainframe'` |
| Empty label | Rejected: `missing label` |
| Whitespace label | Rejected: `missing label` |
| `ACB-14` | Accepted — only `label` and `icon_type` are validated here |

A successful call therefore reports 13 created and 3 errors.
