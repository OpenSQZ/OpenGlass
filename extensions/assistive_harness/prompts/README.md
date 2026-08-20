# User-editable Skill prompts

These files are the system prompts used when the Voice Skill Harness creates a
new MiniCPM-o Duplex Session. Prompt text is read again on every activation, so
editing an existing `.txt` file takes effect on the next Skill switch without
restarting the Harness service.

## Shipped prompts

| Skill ID | Prompt file | Default |
|---|---|---|
| `idle_chat` | `idle_chat_zh.txt` | enabled |
| `find_object` | `find_object_zh.txt` | enabled |
| `read_text` | `read_text_zh.txt` | enabled |
| `describe_scene` | `describe_scene_zh.txt` | enabled |
| `obstacle_avoidance` | `obstacle_avoidance_zh.txt` | enabled / experimental |

The find/read/obstacle task bodies were derived from the project's frozen
AAAI_SI C1 prompts. `find_object_zh.txt` additionally contains `{{target}}`,
because the new Session must receive the target extracted from the command
that closed the old Session. No external absolute path is required at runtime.

## Editing an existing prompt

1. Back up the target `.txt` file.
2. Replace its contents with a UTF-8 system prompt.
3. Keep every declared template variable, such as `{{target}}`.
4. Switch to chat/another Skill and activate this Skill again. (The same
   Skill/same slots command is intentionally deduplicated.) The new hot Session
   will read the new text and record its rendered SHA-256 in telemetry.

## Adding a Skill

Create another `.txt` file and add a matching entry under `skills:` in
`../config/skills.example.yaml`. Configuration changes require restarting the
8021 Harness service; prompt-only changes do not.

Obstacle avoidance has not passed a safety evaluation. Use it only for
stationary, supervised tests; never treat the Demo as a mobility safety device.
