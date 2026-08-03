# Bill of Materials

This page documents the public BOM for OpenGlass hardware. Component specifications and availability still require verification before each build.

## Download

- [`A01_bom_public.xlsx`](A01_bom_public.xlsx) - public BOM with workbook identity metadata and Taobao share-tracking parameters removed.

## Recommended BOM Schema

| Field | Description |
| --- | --- |
| `category` | Mechanical, electronics, power, wiring, fastener, adhesive, tool, or optional accessory |
| `part` | Human-readable part name |
| `verified_model` | Confirmed model or specification |
| `quantity` | Quantity required for one build |
| `function` | Role in the OpenGlass prototype |
| `source_or_vendor` | Vendor or sourcing note with tracking parameters removed |
| `optional_replacement` | Tested substitute if available |
| `verification_status` | Verified, pending, substitute needed, or not for public release |
| `safety_notes` | Battery, heat, soldering, insulation, or mechanical caution |

## Verification Items

| Topic | Status |
| --- | --- |
| Camera module name | `OV5640-AF` confirmed by the project owner |
| Wire type | Conflicting notes must be resolved before publication |
| Battery model and capacity | Must be verified against the physical cell and vendor data |
| Battery runtime | Requires measured test conditions |
| Charging module | Charging-only versus data/debug behavior must be verified |
| Autofocus | Requires firmware and optical validation |
| Wear comfort | Requires structured user testing |
| Final material and print settings | Must match the CAD/printing release |

## Link Sanitization

The current public workbook has had share-tracking parameters removed. Future BOM links should also remove:

- Tracking parameters.
- Personal account or affiliate parameters.
- Search-session identifiers.
- Local purchase-history context.

When possible, prefer a generic vendor page, manufacturer page, or specification sheet over a personal purchase link.

## Safety Notes

The public BOM should include conservative warnings for:

- Lithium battery handling.
- Charging-module ratings.
- Polarity and short-circuit risks.
- Soldering temperature and ventilation.
- Insulation and strain relief.
- Mechanical pressure near the user's face.

Do not publish uncertain battery, charging, autofocus, or comfort claims as verified facts.
