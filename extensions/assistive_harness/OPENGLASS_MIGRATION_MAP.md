# OpenGlass Migration Map

Phase A deliberately separates portable policy from browser-specific transport.

| Phase A component | OpenGlass-side destination |
|---|---|
| `schemas.py` | shared structured control-event schema |
| `router.py` | local deterministic voice-command router |
| `registry.py` + prompts | configurable Skill registry/prompt bundle |
| `echo_guard.py` | output/playback-aware ASR echo filter |
| `state_machine.py` | device-independent control priority and dedupe |
| `asr/` | phone/host local ASR service behind the same interface |
| `cv/base.py` | future advisory perception provider interface |
| `server.py` WS messages | replaceable host/phone transport adapter |
| `browser-session-adapter.js` | reference semantics for an OpenGlass session adapter |

OpenGlass migration should keep `ControlEvent`, `SkillRegistry`, prompt SHA,
generation fence and STOP priority stable. Replace only AudioMirror capture,
session lifecycle calls, and the control transport. No dependency on ESP32, Rokid,
DOM layout or the MiniCPM private wire format exists in the Python core.

The first Rokid implementation of this boundary now lives in
[`phase_b/`](phase_b/README.md). It consumes the existing APK JPEG/PCM endpoints,
reuses this Core and the existing Gateway, and implements the browser adapter's
STOP/RESUME/RESET/Skill generation contract in Python.
