import { BrowserSessionAdapter } from './browser-session-adapter.js';

class FakeSocket {
    constructor(session) { this.readyState = 1; this.session = session; this.listeners = new Set(); }
    addEventListener(type, listener) { if (type === 'message') this.listeners.add(listener); }
    removeEventListener(type, listener) { if (type === 'message') this.listeners.delete(listener); }
    send(value) {
        if (JSON.parse(value).type !== 'stop') return;
        queueMicrotask(() => {
            this.session.cleanup();
            for (const listener of [...this.listeners]) {
                listener({ data: JSON.stringify({ type: 'stopped' }) });
            }
        });
    }
}

function percentile(values, ratio) {
    const sorted = [...values].sort((a, b) => a - b);
    if (!sorted.length) return null;
    return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * ratio) - 1)];
}

async function run() {
    let active = null;
    const originalResults = [];
    const telemetry = [];
    const makeSession = () => {
        const session = {
            forceListenActive: false,
            audioPlayer: { turnActive: true, stopAll() {}, endTurn() { this.turnActive = false; } },
            onForceListenChange() {},
            _handleResult(result) { originalResults.push(result); },
            cleanup() { this.ws.readyState = 3; if (active === this) active = null; },
        };
        session.ws = new FakeSocket(session);
        return session;
    };
    const adapter = new BrowserSessionAdapter({
        getSession: () => active,
        startSession: async () => { active = makeSession(); return active; },
        setSystemPrompt() {},
        sendTelemetry: (event) => telemetry.push(event),
        closeTimeoutMs: 50,
    });
    active = makeSession();
    adapter.bindSession(active);

    let stopPassed = 0;
    for (let index = 1; index <= 20; index += 1) {
        const before = originalResults.length;
        const ack = adapter.stopSpeech({ event_id: index });
        active._handleResult({ is_listen: false, text: 'old', audio_data: 'old' });
        const fenced = originalResults.length === before;
        active._handleResult({ is_listen: true });
        const resume = adapter.resumeSpeech({ event_id: 1000 + index });
        if (ack.ok && resume.ok && fenced && originalResults.length === before + 1) stopPassed += 1;
    }

    let resetPassed = 0;
    let pollutedOldOutputs = 0;
    for (let index = 1; index <= 20; index += 1) {
        const old = active;
        const before = originalResults.length;
        const ack = await adapter.handleControl({
            accepted: true, intent: 'reset_session', event_id: 100 + index,
            skill_id: 'idle_chat', slots: {}, system_prompt: `idle-${index}`,
        });
        old._handleResult({ is_listen: false, text: 'late', audio_data: 'late' });
        if (originalResults.length !== before) pollutedOldOutputs += 1;
        if (ack.ok && ack.generation === index) resetPassed += 1;
    }

    let roundTripPassed = 0;
    for (let index = 1; index <= 20; index += 1) {
        const find = index % 2 === 1;
        const skill = find ? 'find_object' : 'read_text';
        const slots = find ? { target: `物体${index}` } : {};
        const ack = await adapter.handleControl({
            accepted: true, intent: 'activate_skill', event_id: 200 + index,
            skill_id: skill, slots, system_prompt: `${skill}-${index}`,
        });
        const before = originalResults.length;
        active._handleResult({ is_listen: false, text: `fresh-${index}` });
        if (ack.ok && originalResults.length === before + 1) roundTripPassed += 1;
    }

    const latencies = adapter.snapshot().restart_latencies_ms;
    return {
        kind: 'BROWSER_ADAPTER_SIMULATION',
        stop: { passed: stopPassed, total: 20 },
        reset: { passed: resetPassed, total: 20 },
        find_read_roundtrip: { passed: roundTripPassed, total: 20 },
        restart_latency_ms: {
            p50: percentile(latencies, 0.50),
            p90: percentile(latencies, 0.90),
            samples: latencies.length,
        },
        old_output_pollution_count: pollutedOldOutputs,
        dropped_old_text: adapter.snapshot().dropped_old_text,
        dropped_old_audio: adapter.snapshot().dropped_old_audio,
        telemetry_events: telemetry.length,
        note: 'Synthetic browser adapter test; not a microphone/model manual result.',
    };
}

try {
    const result = await run();
    document.getElementById('result').textContent = JSON.stringify(result, null, 2);
    document.body.dataset.status = 'PASS';
    window.__acceptanceResult = result;
} catch (error) {
    document.getElementById('result').textContent = String(error?.stack || error);
    document.body.dataset.status = 'FAIL';
}
