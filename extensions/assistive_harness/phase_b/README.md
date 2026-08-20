# Rokid Phase B Adapter

This adapter replaces the Phase A browser transport while preserving the
frozen Harness policy and the existing MiniCPM backend:

```text
Rokid APK --JPEG/PCM over Wi-Fi--> Phase B Adapter :18080
                                      |-- audio.mirror/frame.shadow --> Harness :8021
                                      |-- audio_chunk + JPEG --------> Gateway :8040
                                      `-- MiniCPM audio -------------> PC speaker
```

The backend remains `llama.cpp-omni -> Worker -> Gateway :8040 -> MiniCPM-o
4.5`. The old ESP32 and Rokid bridge scripts remain reference implementations;
`demo_rokid_phase_b_harness.py` is the Phase B entry point.

## Frozen control contract

- `STOP`: block and flush PC playback immediately. Device PCM continues to
  reach Harness ASR and Gateway with `force_listen=true`.
- `RESUME`: release the playback gate without replacing the Session.
- `RESET`: stop the old Gateway Session with light cleanup, increment the
  generation, create a new Session using the idle prompt, and reject stale
  output. After `restart_complete`, the PC plays a short two-note ready cue;
  initial startup stays silent. Use `--no-session-ready-chime` to disable it,
  or `--session-ready-chime-volume` (default `0.32`) to tune it.
- Skill activation, cancellation, and return-to-chat use the same replacement
  flow with the prompt and slots supplied by Harness.
- After a Skill Session reaches `restart_complete`, Harness injects the
  original one-shot task into that new Session. This makes read-text and
  experimental obstacle activation answer once instead of only changing the
  system prompt.
- Rokid PCM is repacketized into 100 ms float32 `audio.mirror` frames, matching
  Phase A. A bounded drop-oldest queue prevents device capture from being
  blocked by Gateway latency.
- PC speaker queue depth plus `--playback-echo-tail-s` (default `0.80`) keeps
  EchoGuard active through buffered playback and its short acoustic tail.

## Prerequisites

Run the existing Worker/Gateway on port 8040 and the Assistive Harness on port
8021. Install optional adapter dependencies from `requirements-phase-b.txt` in
the same Python environment.

The current Rokid APK must target the host computer on port 18080 and provide:

- `POST /rokid/image` with raw JPEG bytes;
- `WS /rokid/audio` with 16 kHz mono signed PCM16 little-endian packets.

## Start

From the repository root:

```powershell
python .\demo_rokid_phase_b_harness.py `
  --gateway localhost:8040 `
  --harness-url ws://127.0.0.1:8021/ws/control `
  --input-gain 12 `
  --image-rotate-cw 270 `
  --session-ready-chime-volume 0.32 `
  --playback-echo-tail-s 0.80
```

The local Gateway currently uses plain `ws://`. Use `--gateway-tls` only when
the deployed Gateway endpoint actually serves `wss://`. First-round model audio
plays on the computer; `--no-play` disables playback for diagnostics.

The Rokid SDK PCM observed on the current device is materially quieter than the
browser microphone signal. `--input-gain 12` is the initial device calibration;
gain is applied before both Harness and Gateway and saturates instead of wrapping
PCM16. Recalibrate from real-device RMS logs rather than lowering the shared
Phase A VAD threshold.

The ready cue briefly suppresses Rokid PCM forwarding for the cue plus a short
guard interval so the laptop speaker notification is not immediately recycled
into Harness or MiniCPM-o.

Streaming model text is aggregated by Session, generation and end-of-turn. It
is printed as `[AssistiveHarness][MODEL]` and written under the current run:

- `model_events.jsonl` for structured records;
- `model_transcript.txt` for quick human review.

This is generated model text, not ASR over the final TTS waveform. Keep a
session recording when exact spoken audio must be audited.

Health and the normalized latest frame are available at:

- `GET http://127.0.0.1:18080/health`
- `GET http://127.0.0.1:18080/capture`

Healthy input is approximately 25 audio packets/s and 1 JPEG/s, with one audio
client, zero sustained queue drops, `harness_connected=true`, and
`gateway_status=running`. `device_input_ready=true` confirms that at least one
PCM packet or JPEG has reached this process. If `[ROKID][NO_INPUT]` appears,
restart Sensor Mode on the glasses (`STOP -> RUN`) after the PC listener is up;
an APK WebSocket disconnected by a PC restart may not reconnect by itself.

Stop the adapter with `Ctrl+C` (or `Ctrl+Break` on Windows). The HTTP listener,
Rokid WebSocket handler, Gateway Session, Harness connection, and PC speaker are
closed with bounded waits. A 10-second process watchdog is the final fallback,
so a native audio cleanup stall cannot leave port 18080 occupied indefinitely.

## First device acceptance order

1. Ordinary speech produces a MiniCPM response on the PC speaker.
2. While it speaks, say `停一下`; playback must stop and Harness must ACK
   `stop_speech`.
3. Say `恢复对话`; the same Session resumes.
4. Say `重新开始`; the Session ID and generation must change.
5. Activate `找物`, `识字`, `场景描述`, and supervised experimental `避障`;
   each switch must complete with a new Session and no old-generation output.

Experimental obstacle output is not a navigation or safety guarantee.
