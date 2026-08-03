# CAD and 3D Printing

This page documents the current OpenGlass CAD and 3D-printing release. The STEP source and 3MF print plate are public drafts; independent STL exports are not yet available.

## Downloads

- [`A02_frame_source.step`](A02_frame_source.step) - editable STEP source with the export-time private path removed.
- [`A03_print_plate.3mf`](A03_print_plate.3mf) - 3MF print plate with the designer user ID removed; embedded settings require verification before printing.

## Target Artifact Structure

```text
hardware/cad_3d_print/
  README.md
  source/
    openglass_frame_<version>.step
  stl/
    frame_<version>.stl
    temple_left_<version>.stl
    temple_right_<version>.stl
    cover_<version>.stl
  plate/
    openglass_print_plate_<version>.3mf
  settings/
    slicer_profile_<printer>_<version>.md
```

## Required Print Documentation

Each public release should document:

- Printer model and firmware if relevant.
- Slicer name and version.
- Part orientation.
- Material.
- Nozzle diameter.
- Layer height.
- Wall/perimeter settings.
- Infill.
- Support settings.
- Bed adhesion settings.
- Post-processing steps.
- Expected print time and material mass when verified.

## Version Naming

Use versioned filenames so that CAD, STL, 3MF, BOM, and assembly instructions can be matched:

```text
openglass_frame_v0.1.step
frame_v0.1.stl
openglass_print_plate_v0.1.3mf
```

Do not reuse a version number after geometry changes.

## Verification Table

| Item | Public release requirement |
| --- | --- |
| Editable STEP | Remove private metadata and verify geometry opens in common CAD tools |
| STL exports | Export from verified source geometry and inspect orientation and scale |
| 3MF plate | Review embedded slicer settings and metadata before publication |
| Material | Confirm final material rather than mixing draft material notes |
| Supports | Document support placement and removal risks |
| Tolerances | Check fit for electronics, wiring, covers, and charging access |
| Wearability | Check edges, pressure points, heat, and cable strain |

## Safety Notes

3D-printed frames can crack, deform, or expose sharp edges. Electronics cavities should avoid pinching battery leads or camera cables. Any printed part used near the face should be inspected for rough surfaces, loose fasteners, and heat transfer from electronics.
