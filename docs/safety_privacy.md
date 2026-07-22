# Safety and Privacy

OpenGlass is a research prototype and reference implementation for visual assistance experiments. It is not a certified navigation or mobility aid, does not replace a cane, guide dog, orientation-and-mobility training, or human assistance, and should not be treated as a safety-critical system.

## Intended Use Boundary

OpenGlass is intended for controlled research, prototyping, and evaluation. It can help explore local-first camera/audio sensing, multimodal response latency, and speech interaction patterns, but the system can be wrong, late, incomplete, or unavailable.

Users and researchers should avoid relying on OpenGlass for high-risk decisions such as crossing streets, avoiding moving vehicles, medical decisions, or entering hazardous areas.

## Latency and Reliability Limits

Latency is not a hard real-time guarantee. Several factors can delay or degrade responses:

- Low light.
- Motion blur.
- Distant or small text.
- Occlusion.
- Reflective surfaces.
- Wireless jitter.
- Local model load, GPU memory pressure, or background processes.
- ASR, VLM, and TTS failures.

When evidence is uncertain, users should stop, retake the frame, ask for confirmation, or seek human assistance.

## First-Person Camera Privacy

First-person cameras may capture:

- Bystanders and faces.
- Screens and private documents.
- Homes, workplaces, classrooms, clinics, and other sensitive spaces.
- Location clues, names, phone numbers, QR codes, and account information.

Local inference reduces default data exposure because raw frames and audio can remain on user-controlled devices. It is not a formal privacy guarantee. Any saved image, audio file, transcript, or log can still contain sensitive information.

## Data Handling

Recommended defaults:

- Minimize raw frame and audio retention.
- Disable logging unless it is needed for a specific experiment.
- Delete raw captures after analysis when possible.
- Store research logs in access-controlled locations.
- Redact local IPs, local paths, names, and device identifiers before publication.
- Review evaluation images for bystanders, screens, documents, and private spaces.

## Cloud Baselines

Cloud API adapters are optional evaluation baselines. They may transmit images, prompts, metadata, or generated text to external providers and must be explicitly enabled by the researcher. Review provider terms, consent requirements, and data retention policies before running cloud baselines.

## Hardware Safety

Battery, soldering, charging, wiring, and 3D-printed structure documentation is still under verification. Before wearable tests:

- Inspect solder joints and insulation.
- Avoid exposed conductors near skin.
- Confirm battery and charging-module ratings.
- Avoid charging while worn unless explicitly validated.
- Check for heat, sharp edges, loose parts, cable strain, and pressure points.
- Stop testing if the device becomes hot, unstable, or physically uncomfortable.

## Research Practice

For studies with blind or low-vision participants, use an approved study protocol, informed consent, a safe test environment, and accessible fallback procedures. Collect only data needed for the research question, and make it easy for participants to pause or withdraw.
