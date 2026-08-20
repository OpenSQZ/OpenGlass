const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function sameSlots(left = {}, right = {}) {
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return leftKeys.length === rightKeys.length
        && leftKeys.every((key, index) => key === rightKeys[index] && String(left[key]) === String(right[key]));
}

function sessionIdentity(session) {
    const value = session?.sessionId || session?.recordingSessionId || '';
    return value ? String(value) : null;
}

export class BrowserSessionAdapter {
    constructor({
        getSession,
        startSession,
        setSystemPrompt,
        addLog = () => {},
        sendTelemetry = () => {},
        onRestartingChange = () => {},
        closeTimeoutMs = 2000,
        restartCleanupMode = 'light',
        now = () => performance.now(),
    }) {
        this.getSession = getSession;
        this.startSession = startSession;
        this.setSystemPrompt = setSystemPrompt;
        this.addLog = addLog;
        this.sendTelemetry = sendTelemetry;
        this.onRestartingChange = onRestartingChange;
        this.closeTimeoutMs = closeTimeoutMs;
        this.restartCleanupMode = restartCleanupMode === 'full' ? 'full' : 'light';
        this.now = now;
        this.generation = 0;
        this.currentSkill = 'idle_chat';
        this.currentSlots = {};
        this.dropOutputUntilListen = false;
        this.speechHoldActive = false;
        this.restartInProgress = false;
        this.pendingRestart = null;
        this.restartPromise = null;
        this.droppedOldText = 0;
        this.droppedOldAudio = 0;
        this.stopCount = 0;
        this.restartLatencies = [];
        this._boundSessions = new WeakSet();
    }

    bindSession(session) {
        if (!session || this._boundSessions.has(session)) return;
        this._boundSessions.add(session);
        const boundGeneration = this.generation;
        const originalHandleResult = session._handleResult.bind(session);
        session._handleResult = (result) => {
            const stale = boundGeneration !== this.generation || session !== this.getSession();
            if (stale || (this.dropOutputUntilListen && !result.is_listen)) {
                if (result.text) this.droppedOldText += 1;
                if (result.audio_data) this.droppedOldAudio += 1;
                return;
            }
            if (result.is_listen && this.dropOutputUntilListen && !this.speechHoldActive) {
                this.dropOutputUntilListen = false;
                session.forceListenActive = false;
                session.audioPlayer?.setOutputBlocked?.(false);
                session.onForceListenChange(false);
            }
            this.sendTelemetry({
                type: 'model.state',
                state: result.is_listen ? 'listen' : 'speak',
                text: result.text || '',
                generation: boundGeneration,
            });
            originalHandleResult(result);
        };
        this.sendTelemetry({
            type: 'session.state',
            phase: 'bound',
            generation: boundGeneration,
            skill_id: this.currentSkill,
            slots: this.currentSlots,
        });
    }

    stopSpeech(event = {}, { emitAck = true } = {}) {
        const session = this.getSession();
        this.stopCount += 1;
        this.speechHoldActive = true;
        this.dropOutputUntilListen = true;
        if (session) {
            session.audioPlayer?.setOutputBlocked?.(true);
            session.audioPlayer?.stopAll?.();
            if (session.audioPlayer?.turnActive) session.audioPlayer.endTurn();
            session.forceListenActive = true;
            session.onForceListenChange?.(true);
        }
        this.addLog(`Harness STOP (event ${event.event_id ?? '?'})`);
        const ack = {
            type: 'control.ack',
            event_id: event.event_id,
            intent: 'stop_speech',
            ok: true,
            generation: this.generation,
            dropped_old_text: this.droppedOldText,
            dropped_old_audio: this.droppedOldAudio,
        };
        if (emitAck) this.sendTelemetry(ack);
        return ack;
    }

    resumeSpeech(event = {}, { emitAck = true } = {}) {
        const session = this.getSession();
        this.speechHoldActive = false;
        this.dropOutputUntilListen = false;
        if (session) {
            session.forceListenActive = false;
            session.audioPlayer?.setOutputBlocked?.(false);
            session.onForceListenChange?.(false);
        }
        this.addLog(`Harness RESUME (event ${event.event_id ?? '?'})`);
        const ack = {
            type: 'control.ack',
            event_id: event.event_id,
            intent: 'resume_speech',
            ok: true,
            generation: this.generation,
            dropped_old_text: this.droppedOldText,
            dropped_old_audio: this.droppedOldAudio,
        };
        if (emitAck) this.sendTelemetry(ack);
        return ack;
    }

    async handleControl(event) {
        if (!event?.accepted) return { ok: false, ignored: true };
        if (event.intent === 'stop_speech') return this.stopSpeech(event);
        if (event.intent === 'resume_speech') return this.resumeSpeech(event);
        if (['reset_session', 'activate_skill', 'cancel_skill', 'return_to_chat'].includes(event.intent)) {
            return this.requestRestart(event);
        }
        return { ok: false, ignored: true };
    }

    restartSession({ skillId = 'idle_chat', systemPrompt = '', reason = 'reset', eventId = 0 } = {}) {
        return this.requestRestart({
            accepted: true, intent: 'reset_session', event_id: eventId,
            skill_id: skillId, slots: {}, system_prompt: systemPrompt, reason,
        });
    }

    activateSkill({ skillId, slots = {}, systemPrompt = '', reason = 'activate', eventId = 0 } = {}) {
        return this.requestRestart({
            accepted: true, intent: 'activate_skill', event_id: eventId,
            skill_id: skillId, slots, system_prompt: systemPrompt, reason,
        });
    }

    returnToChat(reason = 'return_to_chat', eventId = 0, systemPrompt = '') {
        return this.requestRestart({
            accepted: true, intent: 'return_to_chat', event_id: eventId,
            skill_id: 'idle_chat', slots: {}, system_prompt: systemPrompt, reason,
        });
    }

    getState() { return this.snapshot(); }

    requestRestart(event) {
        const requestedSkill = event.skill_id || 'idle_chat';
        const requestedSlots = event.slots || {};
        if (
            event.intent !== 'reset_session'
            && !this.restartInProgress
            && requestedSkill === this.currentSkill
            && sameSlots(requestedSlots, this.currentSlots)
        ) {
            const ack = {
                type: 'control.ack', event_id: event.event_id, intent: event.intent,
                ok: true, no_restart: true, generation: this.generation,
            };
            this.sendTelemetry(ack);
            return Promise.resolve(ack);
        }
        this.pendingRestart = event;
        if (!this.restartPromise) {
            this.restartPromise = this._restartLoop().finally(() => {
                this.restartPromise = null;
            });
        }
        return this.restartPromise;
    }

    async _restartLoop() {
        let result = null;
        while (this.pendingRestart) {
            const event = this.pendingRestart;
            this.pendingRestart = null;
            result = await this._restartOnce(event);
        }
        return result;
    }

    async _restartOnce(event) {
        const started = this.now();
        this.restartInProgress = true;
        this.onRestartingChange(true);
        this.stopSpeech({ event_id: event.event_id }, { emitAck: false });
        const oldSession = this.getSession();
        const oldSessionId = sessionIdentity(oldSession);
        this.generation += 1;
        this.sendTelemetry({
            type: 'session.state', phase: 'restart_started', generation: this.generation,
            event_id: event.event_id, old_session_id: oldSessionId,
        });
        try {
            await this._closeOldSession(oldSession, this.restartCleanupMode);
            if (event.system_prompt) this.setSystemPrompt(event.system_prompt);
            this.currentSkill = event.skill_id || 'idle_chat';
            this.currentSlots = event.slots || {};
            await this.startSession();
            const replacement = this.getSession();
            if (!replacement) throw new Error('replacement session was not created');
            this.bindSession(replacement);
            const newSessionId = sessionIdentity(replacement);
            this.speechHoldActive = false;
            this.dropOutputUntilListen = false;
            replacement.forceListenActive = false;
            replacement.audioPlayer?.setOutputBlocked?.(false);
            replacement.onForceListenChange?.(false);
            const latency = this.now() - started;
            this.restartLatencies.push(latency);
            const ack = {
                type: 'control.ack', event_id: event.event_id, intent: event.intent,
                ok: true, generation: this.generation, restart_latency_ms: latency,
                skill_id: this.currentSkill, slots: this.currentSlots,
                old_session_id: oldSessionId, new_session_id: newSessionId,
                cleanup_mode: this.restartCleanupMode,
                dropped_old_text: this.droppedOldText, dropped_old_audio: this.droppedOldAudio,
            };
            this.sendTelemetry({
                type: 'session.state', phase: 'restart_complete', generation: this.generation,
                skill_id: this.currentSkill, slots: this.currentSlots,
                old_session_id: oldSessionId, new_session_id: newSessionId,
                cleanup_mode: this.restartCleanupMode,
            });
            this.sendTelemetry(ack);
            this.addLog(`Harness ${this.currentSkill} ready (${Math.round(latency)} ms)`);
            return ack;
        } catch (error) {
            const ack = {
                type: 'control.ack', event_id: event.event_id, intent: event.intent,
                ok: false, generation: this.generation, error: String(error?.message || error),
            };
            this.sendTelemetry(ack);
            this.addLog(`Harness restart failed: ${ack.error}`);
            return ack;
        } finally {
            this.restartInProgress = false;
            this.onRestartingChange(false);
        }
    }

    async _closeOldSession(session, cleanupMode = 'light') {
        if (!session) return;
        const ws = session.ws;
        if (!ws || ws.readyState !== 1) {
            session.cleanup?.();
            return;
        }
        let settled = false;
        await Promise.race([
            new Promise((resolve) => {
                const listener = (message) => {
                    try {
                        if (JSON.parse(message.data)?.type === 'stopped') {
                            settled = true;
                            ws.removeEventListener('message', listener);
                            resolve();
                        }
                    } catch (_) {}
                };
                ws.addEventListener('message', listener);
                ws.send(JSON.stringify({ type: 'stop', cleanup_mode: cleanupMode }));
            }),
            sleep(this.closeTimeoutMs),
        ]);
        if (!settled) session.cleanup?.();
    }

    snapshot() {
        return {
            generation: this.generation,
            current_skill: this.currentSkill,
            current_slots: { ...this.currentSlots },
            drop_output_until_listen: this.dropOutputUntilListen,
            speech_hold_active: this.speechHoldActive,
            restart_in_progress: this.restartInProgress,
            pending_restart: Boolean(this.pendingRestart),
            active_session_id: sessionIdentity(this.getSession()),
            restart_cleanup_mode: this.restartCleanupMode,
            dropped_old_text: this.droppedOldText,
            dropped_old_audio: this.droppedOldAudio,
            stop_count: this.stopCount,
            restart_latencies_ms: [...this.restartLatencies],
        };
    }
}
