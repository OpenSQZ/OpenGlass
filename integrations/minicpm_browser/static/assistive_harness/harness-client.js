import { BrowserSessionAdapter } from './browser-session-adapter.js?v=phase-a-voice-reset-hot-v5';

function enabledByQuery() {
    return new URLSearchParams(window.location.search).get('assistive_harness') === '1';
}

function bytesToBase64(bytes) {
    let output = '';
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
        output += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return btoa(output);
}

function defaultControlUrl() {
    const params = new URLSearchParams(window.location.search);
    const override = params.get('assistive_harness_ws');
    if (override) return override;
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${scheme}//${window.location.hostname}:8021/ws/control`;
}

function makeStatusPanel() {
    const root = document.createElement('div');
    root.id = 'assistiveHarnessStatus';
    root.style.cssText = [
        'position:fixed', 'right:12px', 'bottom:12px', 'z-index:9999',
        'padding:6px 9px', 'border-radius:6px', 'font:12px sans-serif',
        'color:#fff', 'background:#7a5b00', 'opacity:.9', 'max-width:340px',
    ].join(';');
    const label = document.createElement('button');
    label.type = 'button';
    label.textContent = 'Harness: connecting ▸';
    label.style.cssText = 'border:0;background:transparent;color:inherit;font:inherit;cursor:pointer;padding:0';
    const details = document.createElement('pre');
    details.style.cssText = 'display:none;margin:6px 0 0;white-space:pre-wrap;font:11px/1.4 ui-monospace,monospace';
    details.textContent = 'Waiting for state…';
    label.addEventListener('click', () => {
        const open = details.style.display !== 'none';
        details.style.display = open ? 'none' : 'block';
        label.textContent = label.textContent.replace(open ? '▾' : '▸', open ? '▸' : '▾');
    });
    root.append(label, details);
    document.body.appendChild(root);
    return {
        root, label, details,
        setConnection(text, color) {
            const marker = details.style.display === 'none' ? '▸' : '▾';
            label.textContent = `${text} ${marker}`;
            root.style.background = color;
        },
    };
}

export function createAssistiveHarnessIntegration(options) {
    if (!enabledByQuery()) {
        return {
            enabled: false,
            bindSession() {},
            async attachAudioMirror() {},
            detachAudioMirror() {},
            mirrorFrame() {},
            close() {},
        };
    }

    const status = makeStatusPanel();
    const debugState = {
        connected: false, latest_transcript: '', intent: '', current_skill: 'idle_chat',
        target: '', generation: 0, restart_latency_ms: null, output_gate: false, cv_mode: 'shadow',
        old_session_id: '', new_session_id: '', cleanup_mode: '',
    };
    let socket = null;
    let reconnectTimer = null;
    let audioNode = null;
    let zeroGain = null;
    let lastFrameAt = 0;
    let closed = false;

    const renderDebugState = () => {
        status.details.textContent = [
            `connected: ${debugState.connected}`,
            `ASR: ${debugState.latest_transcript || '—'}`,
            `intent: ${debugState.intent || '—'}`,
            `skill: ${debugState.current_skill}`,
            `target: ${debugState.target || '—'}`,
            `generation: ${debugState.generation}`,
            `session: ${debugState.old_session_id || '—'} -> ${debugState.new_session_id || '—'}`,
            `reset_mode: ${debugState.cleanup_mode === 'light' ? 'hot/light' : (debugState.cleanup_mode || '—')}`,
            `restart_ms: ${debugState.restart_latency_ms ?? '—'}`,
            `output_gate: ${debugState.output_gate}`,
            `CV: ${debugState.cv_mode}`,
        ].join('\n');
    };
    const rawSend = (payload) => {
        if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
    };
    const adapter = new BrowserSessionAdapter({
        ...options,
        sendTelemetry: (payload) => {
            if (payload.type === 'control.ack') {
                debugState.generation = payload.generation ?? debugState.generation;
                debugState.restart_latency_ms = payload.restart_latency_ms ?? debugState.restart_latency_ms;
                debugState.old_session_id = payload.old_session_id ?? debugState.old_session_id;
                debugState.new_session_id = payload.new_session_id ?? debugState.new_session_id;
                debugState.cleanup_mode = payload.cleanup_mode ?? debugState.cleanup_mode;
                const snapshot = adapter.snapshot();
                debugState.current_skill = snapshot.current_skill;
                debugState.target = snapshot.current_slots?.target || '';
                debugState.output_gate = snapshot.drop_output_until_listen;
                renderDebugState();
            }
            rawSend(payload);
        },
    });

    const connect = () => {
        if (closed) return;
        status.setConnection('Harness: connecting', '#7a5b00');
        socket = new WebSocket(defaultControlUrl());
        socket.onopen = () => {
            debugState.connected = true;
            status.setConnection('Harness: ready', '#176b36');
            renderDebugState();
        };
        socket.onmessage = (message) => {
            let payload;
            try { payload = JSON.parse(message.data); } catch (_) { return; }
            if (payload.type === 'asr.transcript') {
                debugState.latest_transcript = payload.utterance || '';
                renderDebugState();
            }
            if (payload.type === 'control.intent') {
                debugState.latest_transcript = payload.utterance || debugState.latest_transcript;
                debugState.intent = payload.intent || '';
                renderDebugState();
                void adapter.handleControl(payload);
            }
        };
        socket.onerror = () => {
            status.setConnection('Harness: unavailable (native unaffected)', '#8b1e1e');
        };
        socket.onclose = () => {
            socket = null;
            debugState.connected = false;
            if (!closed) {
                status.setConnection('Harness: reconnecting', '#7a5b00');
                renderDebugState();
                reconnectTimer = setTimeout(connect, 1500);
            }
        };
    };
    connect();

    const integration = {
        enabled: true,
        adapter,
        bindSession(session) { adapter.bindSession(session); },
        async attachAudioMirror(context, source) {
            if (audioNode) return;
            await context.audioWorklet.addModule('/static/assistive_harness/audio-mirror-processor.js');
            audioNode = new AudioWorkletNode(context, 'assistive-audio-mirror', {
                processorOptions: { frameSize: 1600 },
            });
            zeroGain = context.createGain();
            zeroGain.gain.value = 0;
            source.connect(audioNode);
            audioNode.connect(zeroGain);
            zeroGain.connect(context.destination);
            audioNode.port.onmessage = (event) => {
                if (event.data?.type !== 'audio.frame') return;
                const frame = event.data.audio;
                    rawSend({
                    type: 'audio.mirror',
                    started_at_ms: Date.now() - (frame.length * 1000 / context.sampleRate),
                    sample_rate: context.sampleRate,
                    audio_b64: bytesToBase64(new Uint8Array(frame.buffer)),
                });
            };
        },
        detachAudioMirror() {
            try { audioNode?.port.postMessage({ command: 'stop' }); } catch (_) {}
            try { audioNode?.disconnect(); } catch (_) {}
            try { zeroGain?.disconnect(); } catch (_) {}
            audioNode = null;
            zeroGain = null;
        },
        mirrorFrame(jpegBase64) {
            const timestamp = Date.now();
            if (!jpegBase64 || timestamp - lastFrameAt < 1000) return;
            lastFrameAt = timestamp;
            rawSend({
                type: 'frame.shadow', frame_id: `browser_${timestamp}`,
                timestamp_ms: timestamp, jpeg_b64: jpegBase64,
            });
        },
        injectTranscript(text) {
            rawSend({ type: 'asr.inject', text });
        },
        snapshot() { return adapter.snapshot(); },
        close() {
            closed = true;
            if (reconnectTimer) clearTimeout(reconnectTimer);
            integration.detachAudioMirror();
            try { socket?.close(); } catch (_) {}
            status.root.remove();
        },
    };
    window.__assistiveHarness = integration;
    return integration;
}
