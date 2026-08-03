# Release Checklist

Use this checklist before adding public assets, release packages, or result claims.

## Repository Claims

- [ ] README claims match files that are actually present.
- [ ] The model is described as running on a nearby host, not on ESP32.
- [ ] Optional cloud baselines are documented as optional.
- [ ] Experimental Omni features are labeled as experimental.
- [ ] Hardware artifacts are not described as verified until verification is complete.
- [ ] License selection is currently pending and must be confirmed by the repository owner before adding a license file or license badge.

## Secrets and Local Data

- [ ] No real Wi-Fi SSID or password.
- [ ] No API keys, tokens, credentials, cookies, or private headers.
- [ ] No `.env` files.
- [ ] No private local IP addresses.
- [ ] No absolute local paths.
- [ ] No model cache paths or local weight paths.
- [ ] No private device names or device registries.

## Models and Dependencies

- [ ] No model weights are committed.
- [ ] Upstream projects are linked rather than vendored unless license review approves vendoring.
- [ ] Upstream commits are pinned for reproducible runtime instructions.
- [ ] Dependency versions are documented for clean-machine testing.

## Personal Information and Research Data

- [ ] No personal email addresses or identifiers unless intentionally public.
- [ ] Evaluation images have consent and publication rights.
- [ ] Bystanders, faces, screens, documents, and sensitive spaces are reviewed.
- [ ] Raw audio, transcripts, and logs are minimized or removed.
- [ ] Participant-study material has protocol and consent review.

## Third-Party Code and Asset Provenance

- [ ] Firmware-derived web server code has documented provenance.
- [ ] Generated web UI assets have source and license notes.
- [ ] Figures, photos, logos, and diagrams have ownership recorded.
- [ ] Paper figures reused as assets have publication rights checked.

## Hardware Release Gate

- [ ] STEP metadata is sanitized.
- [ ] STL exports are generated and checked.
- [ ] 3MF print plate is reviewed for embedded metadata and settings.
- [ ] BOM purchase links are sanitized.
- [ ] Camera model naming is verified.
- [ ] Wire specification is verified.
- [ ] Battery model, capacity, runtime, charging behavior, and safety notes are verified.
- [ ] USB-C charging/debugging behavior is verified.
- [ ] Autofocus behavior is verified.
- [ ] Final material, nozzle, layer height, infill, support, orientation, and slicer version are verified.
- [ ] Soldering safety and strain relief are documented.

## Runtime Release Gate

- [ ] Exact model server, worker, gateway, and bridge roles are documented.
- [ ] Runtime ports and endpoints are verified.
- [ ] ESP32 audio endpoint compatibility is verified.
- [ ] `devices.json` schema and parser are public and tested.
- [ ] Stop, Restart, Stop All, and Restart All semantics are verified from source.
- [ ] Rokid source, APK, protocol, and permissions are reviewed before publication.
- [ ] Prompt switching, multiturn, and barge-in limitations are stated clearly.

## Validation

- [ ] `git diff --check` passes.
- [ ] JSON examples pass `python -m json.tool`.
- [ ] Markdown relative links resolve locally.
- [ ] UTF-8 files decode cleanly.
- [ ] Secret/path scan is run on changed public files.
- [ ] No source code changes are included in documentation-only releases unless explicitly intended.
