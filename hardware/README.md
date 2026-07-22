# OpenGlass Hardware

[AI glasses open-source report (Chinese)](AI_GLASSES_OPEN_SOURCE_REPORT.md)

The hardware track prepares a reproducible, 3D-printable glasses frame for OpenGlass sensing prototypes. The goal is to document the physical frame, module placement, wiring, assembly, print parameters, and validation status without publishing unverified artifacts prematurely.

The current public draft includes 15 documentation images, a sanitized BOM, an editable STEP file, and a 3MF print plate. STL exports, wiring diagrams, pin maps, and complete hardware validation results are not yet available.

## Intended Artifact Categories

- Editable CAD source for the glasses frame.
- Printable STL exports.
- 3MF print plate or slicer project.
- Sanitized BOM.
- Wiring diagram and pin map.
- Soldering and insulation notes.
- Component and assembly photographs.
- Print settings and post-processing instructions.
- Validation notes for fit, comfort, thermal behavior, charging, and sensing.

## Design Direction

The hardware direction is organized around:

- A modular frame.
- Temples and covers suitable for small electronics.
- Internal routing for camera, power, and sensor wiring.
- Component placement for ESP32-S3 sensing, camera, microphone, battery, switch, and charging module.
- Access to charging and maintenance ports when verified.
- Room for future sensing or interaction modules.

## Current Release Status

| Area | Status |
| --- | --- |
| Editable CAD | [`cad_3d_print/A02_frame_source.step`](cad_3d_print/A02_frame_source.step) published after metadata sanitization; geometry still requires a human opening check |
| STL exports | Not yet present in the public repository |
| 3MF print plate | [`cad_3d_print/A03_print_plate.3mf`](cad_3d_print/A03_print_plate.3mf) published after user-ID removal; printer and slicer settings still require verification |
| BOM | [`bom/A01_bom_public.xlsx`](bom/A01_bom_public.xlsx) published after metadata and tracking-parameter sanitization |
| Photos | 15 project-owner-approved images published under [`assets/images/`](assets/images/) |
| Wiring diagram | Not yet public |
| Soldering safety | Not yet public |
| Validation results | Not yet public |

## Verification Items

| Topic | Status |
| --- | --- |
| Camera model naming | `OV5640-AF` confirmed by the project owner; `DC5640-AF` in older notes is treated as a legacy label |
| Wire specification | Conflicting source notes; requires verification |
| Battery capacity and runtime | Requires verification |
| USB-C charging and debugging behavior | Requires verification |
| Autofocus behavior | Requires verification |
| Comfort and wear duration | Requires user testing |
| Final print material and slicer settings | Requires verification |

## Safety Boundary

The hardware package is not yet a validated wearable product. Before any user-facing test, verify electrical insulation, battery handling, charging behavior, heat, cable strain, sharp edges, and mechanical fit. Do not wear a prototype while charging unless that behavior has been explicitly validated.

## Related Pages

- [`AI_GLASSES_OPEN_SOURCE_REPORT.md`](AI_GLASSES_OPEN_SOURCE_REPORT.md) - 中文硬件开源报告（已引用图片、BOM、STEP 和 3MF）
- [`cad_3d_print/README.md`](cad_3d_print/README.md)
- [`bom/README.md`](bom/README.md)
- [`../docs/safety_privacy.md`](../docs/safety_privacy.md)
