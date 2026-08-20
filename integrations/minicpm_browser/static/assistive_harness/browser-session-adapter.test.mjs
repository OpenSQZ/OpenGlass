import assert from 'node:assert/strict';
import test from 'node:test';

import { BrowserSessionAdapter } from './browser-session-adapter.js';

let fakeSessionSequence = 0;

class FakeSocket {
    constructor(session) {
        this.readyState = 1;
        this.session = session;
        this.listeners = new Set();
    }
    addEventListener(type, listener) { if (type === 'message') this.listeners.add(listener); }
    removeEventListener(type, listener) { if (type === 'message') this.listeners.delete(listener); }
    send(value) {
        const message = JSON.parse(value);
        if (message.type !== 'stop') return;
        this.lastStopMessage = message;
        queueMicrotask(() => {
            this.session.cleanup();
            for (const listener of [...this.listeners]) listener({ data: JSON.stringify({ type: 'stopped' }) });
        });
    }
}

function fakeSession(onCleanup = () => {}) {
    const counters = { stopAll: 0, endTurn: 0, originalResults: 0, forceChanges: [], outputBlocks: [] };
    const session = {
        counters,
        sessionId: `fake_${++fakeSessionSequence}`,
        forceListenActive: false,
        audioPlayer: {
            turnActive: true,
            stopAll() { counters.stopAll += 1; },
            setOutputBlocked(blocked) { counters.outputBlocks.push(Boolean(blocked)); },
            endTurn() { counters.endTurn += 1; this.turnActive = false; },
        },
        onForceListenChange(active) { counters.forceChanges.push(active); },
        _handleResult() { counters.originalResults += 1; },
        cleanup() { this.ws.readyState = 3; onCleanup(this); },
    };
    session.ws = new FakeSocket(session);
    return session;
}

function harnessFixture() {
    let active = null;
    let prompt = '';
    const telemetry = [];
    const adapter = new BrowserSessionAdapter({
        getSession: () => active,
        startSession: async () => {
            active = fakeSession((session) => { if (active === session) active = null; });
            return active;
        },
        setSystemPrompt: (value) => { prompt = value; },
        sendTelemetry: (value) => telemetry.push(value),
        closeTimeoutMs: 20,
    });
    const createInitial = () => {
        active = fakeSession((session) => { if (active === session) active = null; });
        adapter.bindSession(active);
        return active;
    };
    return { adapter, createInitial, get active() { return active; }, get prompt() { return prompt; }, telemetry };
}

test('STOP holds output and RESUME releases it 20/20', () => {
    const fixture = harnessFixture();
    const session = fixture.createInitial();
    for (let index = 1; index <= 20; index += 1) {
        const ack = fixture.adapter.stopSpeech({ event_id: index });
        assert.equal(ack.ok, true);
        session._handleResult({ is_listen: false, text: `stale-${index}`, audio_data: 'old' });
        assert.equal(session.counters.originalResults, index - 1);
        session._handleResult({ is_listen: true });
        assert.equal(session.counters.originalResults, index);
        assert.equal(fixture.adapter.snapshot().speech_hold_active, true);
        assert.equal(fixture.adapter.snapshot().drop_output_until_listen, true);
        const resumed = fixture.adapter.resumeSpeech({ event_id: 100 + index });
        assert.equal(resumed.ok, true);
        assert.equal(fixture.adapter.snapshot().speech_hold_active, false);
        assert.equal(fixture.adapter.snapshot().drop_output_until_listen, false);
    }
    assert.equal(session.counters.stopAll, 20);
    assert.equal(session.counters.outputBlocks.filter(Boolean).length, 20);
    assert.equal(session.counters.outputBlocks.filter((value) => !value).length, 20);
    assert.equal(fixture.adapter.snapshot().stop_count, 20);
    assert.equal(fixture.adapter.snapshot().dropped_old_text, 20);
    assert.equal(fixture.adapter.snapshot().dropped_old_audio, 20);
});

test('handleControl routes the resume_speech intent', async () => {
    const fixture = harnessFixture();
    const session = fixture.createInitial();
    fixture.adapter.stopSpeech({ event_id: 1 });
    const ack = await fixture.adapter.handleControl({
        accepted: true, intent: 'resume_speech', event_id: 2,
    });
    assert.equal(ack.ok, true);
    assert.equal(ack.intent, 'resume_speech');
    assert.equal(session.forceListenActive, false);
    assert.equal(fixture.adapter.snapshot().speech_hold_active, false);
});

test('RESET completes 20/20 with new generations and drops old output', async () => {
    const fixture = harnessFixture();
    fixture.createInitial();
    for (let index = 1; index <= 20; index += 1) {
        const old = fixture.active;
        const ack = await fixture.adapter.handleControl({
            accepted: true, intent: 'reset_session', event_id: index,
            skill_id: 'idle_chat', slots: {}, system_prompt: `idle-${index}`,
        });
        assert.equal(ack.ok, true);
        assert.equal(ack.generation, index);
        assert.equal(ack.old_session_id, old.sessionId);
        assert.equal(ack.new_session_id, fixture.active.sessionId);
        assert.notEqual(ack.old_session_id, ack.new_session_id);
        assert.equal(ack.cleanup_mode, 'light');
        assert.equal(old.ws.lastStopMessage.cleanup_mode, 'light');
        assert.equal(fixture.adapter.snapshot().active_session_id, ack.new_session_id);
        old._handleResult({ is_listen: false, text: 'late', audio_data: 'late-audio' });
        assert.equal(old.counters.originalResults, 0);
    }
    assert.equal(fixture.adapter.snapshot().generation, 20);
    assert.equal(fixture.prompt, 'idle-20');
    assert.equal(fixture.adapter.snapshot().dropped_old_text, 20);
    assert.equal(fixture.adapter.snapshot().dropped_old_audio, 20);
});

test('find/read skill round trips complete 20/20 without duplicate native results', async () => {
    const fixture = harnessFixture();
    fixture.createInitial();
    for (let index = 1; index <= 20; index += 1) {
        const find = index % 2 === 1;
        const skill = find ? 'find_object' : 'read_text';
        const slots = find ? { target: `物体${index}` } : {};
        const ack = await fixture.adapter.handleControl({
            accepted: true, intent: 'activate_skill', event_id: 100 + index,
            skill_id: skill, slots, system_prompt: `${skill}-${index}`,
        });
        assert.equal(ack.ok, true);
        assert.equal(ack.cleanup_mode, 'light');
        assert.equal(fixture.prompt, `${skill}-${index}`);
        fixture.active._handleResult({ is_listen: false, text: `native-${index}` });
        assert.equal(fixture.active.counters.originalResults, 1);
    }
    assert.equal(fixture.adapter.snapshot().generation, 20);
    assert.equal(fixture.adapter.snapshot().current_skill, 'read_text');
});

test('same skill and same slots do not restart', async () => {
    const fixture = harnessFixture();
    fixture.createInitial();
    await fixture.adapter.handleControl({
        accepted: true, intent: 'activate_skill', event_id: 1,
        skill_id: 'find_object', slots: { target: '手机' }, system_prompt: 'phone',
    });
    const generation = fixture.adapter.snapshot().generation;
    const ack = await fixture.adapter.handleControl({
        accepted: true, intent: 'activate_skill', event_id: 2,
        skill_id: 'find_object', slots: { target: '手机' }, system_prompt: 'phone',
    });
    assert.equal(ack.no_restart, true);
    assert.equal(fixture.adapter.snapshot().generation, generation);
});

test('RESET timeout force-cleans the old websocket and still recovers', async () => {
    let active = fakeSession((session) => { if (active === session) active = null; });
    active.ws.send = () => {};
    const adapter = new BrowserSessionAdapter({
        getSession: () => active,
        startSession: async () => {
            active = fakeSession((session) => { if (active === session) active = null; });
            return active;
        },
        setSystemPrompt() {}, closeTimeoutMs: 5,
    });
    adapter.bindSession(active);
    const ack = await adapter.restartSession({systemPrompt: 'idle', eventId: 9});
    assert.equal(ack.ok, true);
    assert.equal(ack.generation, 1);
});

test('STOP remains immediate while a restart is waiting for close', async () => {
    const fixture = harnessFixture();
    fixture.createInitial();
    fixture.active.ws.send = () => {};
    fixture.adapter.closeTimeoutMs = 25;
    const restart = fixture.adapter.activateSkill({
        skillId: 'read_text', systemPrompt: 'read', eventId: 1,
    });
    const stop = fixture.adapter.stopSpeech({event_id: 2});
    assert.equal(stop.ok, true);
    assert.equal(fixture.adapter.snapshot().drop_output_until_listen, true);
    const recovered = await restart;
    assert.equal(recovered.ok, true);
    assert.equal(fixture.adapter.snapshot().speech_hold_active, false);
    assert.equal(fixture.adapter.snapshot().drop_output_until_listen, false);
});
