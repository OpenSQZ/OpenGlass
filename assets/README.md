# Asset Review Policy

This directory holds candidate public assets. Every file still needs source, ownership, privacy, and publication review before a public release.

## Current Review Status

| Asset | README use | Review status |
| --- | --- | --- |
| `photos/openglass_prototype_front_2.png` | Current README hero | No face or visible private text detected; creator/ownership confirmation still required |
| Other photographs | Not referenced | Privacy, consent, and publication rights pending |
| System/runtime figures | Not referenced | Factual, credential, manuscript-rights, and publication review pending |
| Organization logos | Not referenced | Logo usage permission pending |

Referencing a candidate on a local documentation branch is not final publication approval. The repository owner must confirm the hero image before merge or release.

## Expected Subdirectories

```text
assets/
  figures/   Public diagrams and paper-safe figures
  photos/    Reviewed public photographs
  logos/     Logos with clear usage rights
```

## Source and Ownership Requirements

Every public asset should have:

- Source or creator recorded.
- Permission or license status recorded.
- Relationship to a paper, poster, demo, or hardware package documented.
- Review status tracked before the asset is referenced from public docs.

## Privacy Review

Before publishing first-person or wearable photos, check for:

- Bystanders and faces.
- Screens, private documents, whiteboards, badges, and account information.
- Location clues or private interiors.
- Children or protected groups.
- Reflections that reveal sensitive context.

Crop, blur, replace, or omit assets when privacy cannot be cleared.

## Manuscript and Figure Rights

Figures from papers, posters, slides, or submissions should not be assumed public just because they exist locally. Confirm publication rights and final venue policies before using them in README pages or documentation.

## File Size and Compression

- Prefer optimized PNG or WebP for diagrams.
- Prefer compressed JPEG/WebP for photos when fidelity is sufficient.
- Keep source project files outside public assets unless they are intentionally released.
- Avoid committing raw videos or large uncompressed media without explicit review.

## Naming Conventions

Use descriptive lowercase names:

```text
system_architecture_v0_1.png
hardware_frame_front_v0_1.jpg
runtime_pipeline_v0_1.webp
```

Avoid names that reveal people, private locations, local device names, or manuscript-internal status.

## Prohibited Public Asset Material

Do not publish source PDFs, raw private videos, unreviewed session recordings, private logs, audio captures, transcript files, model outputs containing personal data, or unpublished submission packages as public assets.
