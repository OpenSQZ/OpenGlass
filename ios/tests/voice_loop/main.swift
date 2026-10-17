import Foundation

var checks = 0
func check(_ condition: @autoclosure () -> Bool, _ label: String) {
    guard condition() else { fatalError("FAIL: \(label)") }
    checks += 1
}
func checkTime(_ actual: TimeInterval, _ expected: TimeInterval, _ label: String) {
    check(abs(actual - expected) < 0.000_000_001, label)
}

// Startup is opt-in, including when a late callback arrives before any start.
var startup = OpenGlassVoiceLoopPolicy()
check(!startup.wantsListening && !startup.canListen(at: 100), "startup waits for user intent")
startup.retry(now: 100)
startup.receivedUtterance()
check(!startup.canListen(at: 200), "callbacks cannot create startup intent")
startup.requestListening()
check(startup.canListen(at: 200), "explicit start opens the gate")

// Recognition failures keep a positive, bounded delay. Wakeups at the
// deadline may retry; repeated audio/frame notifications before it may not.
var silence = OpenGlassVoiceLoopPolicy()
silence.requestListening()
let expectedDelays: [TimeInterval] = [0.5, 1, 2, 4, 8, 8, 8]
var now: TimeInterval = 1_000
for (index, expected) in expectedDelays.enumerated() {
    let delay = silence.retry(now: now)
    checkTime(delay, expected, "recognition failure retry \(index + 1) uses bounded backoff")
    checkTime(silence.notBefore, now + expected, "retry sets a future deadline")
    check(!silence.canListen(at: now), "retry cannot spin immediately")
    check(!silence.canListen(at: silence.notBefore - 0.001), "retry waits through its deadline")
    check(silence.canListen(at: silence.notBefore), "retry is allowed at its deadline")
    now = silence.notBefore
}
for _ in 0..<1_000 {
    checkTime(silence.retry(now: now), 8, "repeated recognition failures stay capped at eight seconds")
    check(!silence.canListen(at: now), "capped retries still wait")
    now = silence.notBefore
}
check(silence.retryCount == 5, "retry count saturates instead of growing without bound")
silence.receivedUtterance()
check(silence.retryCount == 0 && silence.canListen(at: now), "a recognized question clears backoff")
checkTime(silence.retry(now: now), 0.5, "a later failure starts with the short delay")

// A retry timer and an echo hold are independent: whichever expires later wins.
var timing = OpenGlassVoiceLoopPolicy()
timing.requestListening()
timing.protectFromEcho(now: 10)
checkTime(timing.retry(now: 10), 0.5, "retry reports its delay independently of echo")
checkTime(timing.notBefore, 10.7, "short retry cannot shorten echo hold")
timing.receivedUtterance()
checkTime(timing.notBefore, 10.7, "successful recognition cannot clear echo hold")
timing.requestListening()
check(!timing.canListen(at: 10.6), "explicit listening request still respects echo")
check(timing.canListen(at: timing.notBefore), "echo hold opens at the boundary")
timing.retry(now: 20)
timing.retry(now: 20)
timing.protectFromEcho(now: 20)
checkTime(timing.notBefore, 21, "longer retry survives a shorter echo hold")
timing.protectFromEcho(now: 20.5)
checkTime(timing.notBefore, 21.2, "a later echo hold extends the gate")
timing.protectFromEcho(now: 20.6, delay: 0.1)
checkTime(timing.notBefore, 21.2, "a stale shorter hold cannot move the deadline backwards")
check(!timing.canListen(at: 20.7), "the first echo wakeup cannot bypass an extended hold")

// All ingress and completion paths consult the same gate. These arrivals model
// a reconnect, new frame, PCM packet and an old timer while TTS echo can remain.
var arrivals = OpenGlassVoiceLoopPolicy()
arrivals.requestListening()
arrivals.protectFromEcho(now: 30)
for arrival in [30.0, 30.1, 30.4, 30.699] {
    check(!arrivals.canListen(at: arrival), "an arrival cannot bypass the common echo gate")
}
arrivals.requestListening()
check(!arrivals.canListen(at: 30.699), "a manual restart during echo also waits")
check(arrivals.canListen(at: arrivals.notBefore), "arrivals become eligible after echo expires")

// A user pause wins over already queued retry, successful-final and TTS events.
var stopped = OpenGlassVoiceLoopPolicy()
stopped.requestListening()
stopped.retry(now: 40)
stopped.protectFromEcho(now: 40)
stopped.block(reason: "temporary test state")
stopped.pause()
check(!stopped.wantsListening && stopped.blockedReason == nil && stopped.retryCount == 0,
      "pause clears failure state while disabling intent")
checkTime(stopped.notBefore, 40.7, "pause preserves the acoustic hold")
stopped.retry(now: 41)
stopped.receivedUtterance()
stopped.protectFromEcho(now: 42)
check(!stopped.canListen(at: 1_000), "late callbacks cannot resurrect a stopped loop")
stopped.requestListening()
check(!stopped.canListen(at: 42.5), "resuming a paused loop still waits for recent echo")
check(stopped.canListen(at: stopped.notBefore), "only explicit resume restores eligibility")

// Permanent errors preserve user intent but require an explicit request to
// retry. Neither traffic, a successful old utterance nor elapsed time clears it.
var blocked = OpenGlassVoiceLoopPolicy()
blocked.requestListening()
blocked.block(reason: "offline language unavailable")
check(blocked.wantsListening, "blocking preserves intended listening")
blocked.retry(now: 50)
blocked.receivedUtterance()
blocked.protectFromEcho(now: 50)
check(blocked.blockedReason == "offline language unavailable", "late success keeps the permanent block")
check(!blocked.canListen(at: 1_000), "permanent failure does not expire with time")
blocked.requestListening()
check(blocked.blockedReason == nil && blocked.retryCount == 0, "explicit request clears the block")
check(!blocked.canListen(at: 50.6), "clearing a permanent block cannot bypass echo")
check(blocked.canListen(at: blocked.notBefore), "cleared block allows listening after echo")
blocked.pause()
blocked.block(reason: "late permission callback")
blocked.receivedUtterance()
check(!blocked.wantsListening && !blocked.canListen(at: 1_000), "a late permanent error cannot enable intent")

// Intent survives only when the caller explicitly restores it after background
// cleanup. The policy never enables itself merely because time has passed.
var foreground = OpenGlassVoiceLoopPolicy()
foreground.requestListening()
let wasListening = foreground.wantsListening
foreground.protectFromEcho(now: 60)
foreground.pause()
check(!foreground.canListen(at: 60.2), "background cleanup closes the gate")
if wasListening { foreground.requestListening() }
check(!foreground.canListen(at: 60.2), "foreground restoration preserves the echo deadline")
check(foreground.canListen(at: 61), "restored intent can resume after the deadline")
foreground.pause()
let wasPaused = foreground.wantsListening
if wasPaused { foreground.requestListening() }
check(!foreground.canListen(at: 1_000), "a previously paused loop stays paused on foreground")

// Steady environmental noise can keep the RMS voice marker fresh. A stable
// transcript still finishes promptly instead of waiting for the 25-second cap.
check(OpenGlassUtteranceTiming.action(now: 4.499, startedAt: 0,
                                    lastPacketAt: 4.499, lastVoiceAt: 4.499,
                                    lastTranscriptAt: 2, hasText: true) == .listen,
      "stable text waits the full 2.5 seconds")
check(OpenGlassUtteranceTiming.action(now: 4.5, startedAt: 0,
                                    lastPacketAt: 4.5, lastVoiceAt: 4.5,
                                    lastTranscriptAt: 2, hasText: true) == .finish,
      "stable text finishes despite continuous RMS noise")
check(OpenGlassUtteranceTiming.action(now: 4.5, startedAt: 0,
                                    lastPacketAt: 4.5, lastVoiceAt: 4.5,
                                    lastTranscriptAt: 4, hasText: true) == .listen,
      "a changed partial transcript extends the text deadline")
check(OpenGlassUtteranceTiming.action(now: 6.5, startedAt: 0,
                                    lastPacketAt: 6.5, lastVoiceAt: 6.5,
                                    lastTranscriptAt: 4, hasText: true) == .finish,
      "the updated transcript finishes when it becomes stable")

// An empty transcript must not reuse the stable-text deadline from old content.
check(OpenGlassUtteranceTiming.action(now: 10, startedAt: 0,
                                    lastPacketAt: 10, lastVoiceAt: 10,
                                    lastTranscriptAt: 0, hasText: false) == .listen,
      "old transcript time cannot finalize current empty recognition")
check(OpenGlassUtteranceTiming.action(now: 10, startedAt: 0,
                                    lastPacketAt: 10, lastVoiceAt: 10,
                                    lastTranscriptAt: nil, hasText: true) == .listen,
      "text without a stability timestamp does not finish prematurely")
check(OpenGlassUtteranceTiming.action(now: 24.999, startedAt: 0,
                                    lastPacketAt: 24.999, lastVoiceAt: nil,
                                    lastTranscriptAt: nil, hasText: false) == .listen,
      "silent packets keep empty recognition alive before the duration cap")
check(OpenGlassUtteranceTiming.action(now: 25, startedAt: 0,
                                    lastPacketAt: 25, lastVoiceAt: nil,
                                    lastTranscriptAt: nil, hasText: false) == .finish,
      "continuous silence finishes at 25 seconds")
check(OpenGlassUtteranceTiming.action(now: 1.299, startedAt: 0,
                                    lastPacketAt: 1.299, lastVoiceAt: 0,
                                    lastTranscriptAt: nil, hasText: false) == .listen,
      "voice silence without text does not end an utterance early")
check(OpenGlassUtteranceTiming.action(now: 1.3, startedAt: 0,
                                    lastPacketAt: 1.3, lastVoiceAt: 0,
                                    lastTranscriptAt: nil, hasText: false) == .listen,
      "empty recognition survives the 1.3-second voice silence boundary")
check(OpenGlassUtteranceTiming.action(now: 24.999, startedAt: 0,
                                    lastPacketAt: 24.999, lastVoiceAt: 0,
                                    lastTranscriptAt: nil, hasText: false) == .listen,
      "an old RMS voice marker cannot end empty recognition before its cap")
check(OpenGlassUtteranceTiming.action(now: 25, startedAt: 0,
                                    lastPacketAt: 25, lastVoiceAt: 0,
                                    lastTranscriptAt: nil, hasText: false) == .finish,
      "empty recognition with an old voice marker still reaches its cap")
check(OpenGlassUtteranceTiming.action(now: 1.299, startedAt: 0,
                                    lastPacketAt: 1.299, lastVoiceAt: 0,
                                    lastTranscriptAt: 0.5, hasText: true) == .listen,
      "recognized text waits the full 1.3 seconds of voice silence")
check(OpenGlassUtteranceTiming.action(now: 1.3, startedAt: 0,
                                    lastPacketAt: 1.3, lastVoiceAt: 0,
                                    lastTranscriptAt: 0.5, hasText: true) == .finish,
      "recognized text finishes after 1.3 seconds of voice silence")

// A stopped transport times out earlier than an otherwise empty utterance.
check(OpenGlassUtteranceTiming.action(now: 4.999, startedAt: 0,
                                    lastPacketAt: 0, lastVoiceAt: nil,
                                    lastTranscriptAt: nil, hasText: false) == .listen,
      "packet gap waits the full five seconds")
check(OpenGlassUtteranceTiming.action(now: 5, startedAt: 0,
                                    lastPacketAt: 0, lastVoiceAt: nil,
                                    lastTranscriptAt: nil, hasText: false) == .audioTimeout,
      "missing packets time out at five seconds")
check(OpenGlassUtteranceTiming.action(now: 5, startedAt: 0,
                                    lastPacketAt: 0, lastVoiceAt: 5,
                                    lastTranscriptAt: 0, hasText: true) == .finish,
      "stable transcript finalization takes precedence over a packet timeout")
check(OpenGlassUtteranceTiming.action(now: 5, startedAt: 0,
                                    lastPacketAt: 0, lastVoiceAt: 0,
                                    lastTranscriptAt: nil, hasText: false) == .audioTimeout,
      "an empty transcript with an old voice marker still detects missing packets")
check(OpenGlassUtteranceTiming.action(now: 5, startedAt: 0,
                                    lastPacketAt: 0, lastVoiceAt: 0,
                                    lastTranscriptAt: nil, hasText: true) == .finish,
      "recognized text can finalize voice silence before a packet timeout")
check(OpenGlassUtteranceTiming.action(now: 25, startedAt: 0,
                                    lastPacketAt: 0, lastVoiceAt: nil,
                                    lastTranscriptAt: nil, hasText: false) == .finish,
      "duration cap finalization takes precedence over a packet timeout")

// Diagnostics record only accepted samples and callback outcomes. An idle or
// finishing session cannot claim that late audio reached the recognizer.
let checksBeforeDiagnostics = checks
var diagnostics = OpenGlassSpeechDiagnostics()
check(diagnostics.state == .idle && diagnostics.sessions == 0, "diagnostics start idle")
check(diagnostics.lastEndReason == "not_started" && diagnostics.lastFailureCode == nil,
      "initial diagnostics have no failure or end event")
diagnostics.appended(samples: 640, rms: 0.13)
diagnostics.partial(nonempty: true)
diagnostics.finishing(reason: "late_finish")
diagnostics.completed(nonempty: false, reason: "late_final")
check(diagnostics.acceptedSamples == 0 && diagnostics.sessionSamples == 0,
      "idle audio is not counted as accepted")
check(diagnostics.partialCount == 0 && diagnostics.emptyFinalCount == 0,
      "idle callbacks are not recognition results")
check(diagnostics.state == .idle && diagnostics.lastEndReason == "not_started",
      "late callbacks cannot create a session")

diagnostics.started()
check(diagnostics.state == .listening && diagnostics.sessions == 1,
      "starting records one listening session")
check(diagnostics.lastEndReason == "none", "starting clears the previous end reason")
diagnostics.appended(samples: 640, rms: 0.13)
diagnostics.appended(samples: 320, rms: 0.05)
check(diagnostics.acceptedSamples == 960 && diagnostics.sessionSamples == 960,
      "accepted sample totals count each appended buffer")
checkTime(diagnostics.lastRMS, 0.05, "last RMS follows the latest accepted buffer")
checkTime(diagnostics.sessionPeakRMS, 0.13, "session peak retains the loudest buffer")
diagnostics.appended(samples: 0, rms: 1)
diagnostics.appended(samples: -1, rms: 1)
check(diagnostics.acceptedSamples == 960 && diagnostics.sessionSamples == 960,
      "empty and invalid sample counts are ignored")
checkTime(diagnostics.lastRMS, 0.05, "ignored buffers do not change the last RMS")
diagnostics.partial(nonempty: false)
diagnostics.partial(nonempty: true)
check(diagnostics.partialCount == 2 && diagnostics.nonemptyPartialCount == 1,
      "partial diagnostics distinguish empty callbacks")
diagnostics.finishing(reason: "silence")
check(diagnostics.state == .finishing && diagnostics.lastEndReason == "silence",
      "finishing records the utterance boundary")
diagnostics.finishing(reason: "duplicate_finish")
check(diagnostics.lastEndReason == "silence", "duplicate finishing preserves the original reason")
diagnostics.appended(samples: 640, rms: 0.9)
check(diagnostics.acceptedSamples == 960 && diagnostics.sessionSamples == 960,
      "audio after endAudio is not counted as accepted")
checkTime(diagnostics.lastRMS, 0.05, "finishing ignores late RMS values")
diagnostics.partial(nonempty: true)
check(diagnostics.partialCount == 3 && diagnostics.nonemptyPartialCount == 2,
      "recognition may report a partial while finishing")
diagnostics.completed(nonempty: false, reason: "empty_final")
check(diagnostics.state == .idle && diagnostics.emptyFinalCount == 1,
      "empty final completes one active session")
check(diagnostics.lastEndReason == "empty_final", "final records its end reason")
diagnostics.completed(nonempty: false, reason: "duplicate_final")
diagnostics.completed(nonempty: true, reason: "stale_final")
check(diagnostics.emptyFinalCount == 1 && diagnostics.nonemptyFinalCount == 0,
      "duplicate and stale finals cannot count a session twice")
check(diagnostics.lastEndReason == "empty_final", "late final cannot overwrite the completed reason")

// Invalid RMS values are sanitized independently of the accepted sample count.
diagnostics.started()
check(diagnostics.sessions == 2 && diagnostics.sessionSamples == 0 && diagnostics.acceptedSamples == 960,
      "a new session resets its sample count but preserves the cumulative total")
check(diagnostics.lastRMS == 0 && diagnostics.sessionPeakRMS == 0,
      "a new session resets both RMS measurements")
check(diagnostics.partialCount == 3 && diagnostics.emptyFinalCount == 1,
      "callback totals survive session boundaries")
diagnostics.appended(samples: 10, rms: .nan)
check(diagnostics.lastRMS == 0 && diagnostics.sessionPeakRMS == 0,
      "NaN RMS cannot poison diagnostic values")
diagnostics.appended(samples: 10, rms: .infinity)
check(diagnostics.lastRMS == 0 && diagnostics.sessionPeakRMS == 0,
      "infinite RMS cannot poison diagnostic values")
diagnostics.appended(samples: 10, rms: -0.2)
check(diagnostics.lastRMS == 0 && diagnostics.sessionPeakRMS == 0,
      "negative RMS is clamped to zero")
diagnostics.appended(samples: 10, rms: 0.2)
diagnostics.appended(samples: 10, rms: -.infinity)
check(diagnostics.lastRMS == 0 && diagnostics.sessionPeakRMS == 0.2,
      "invalid later RMS cannot erase the finite peak")
check(diagnostics.sessionSamples == 50 && diagnostics.acceptedSamples == 1_010,
      "invalid measurements do not discard accepted sample counts")
diagnostics.completed(nonempty: true, reason: "recognized")
check(diagnostics.state == .idle && diagnostics.nonemptyFinalCount == 1 && diagnostics.emptyFinalCount == 1,
      "nonempty final can complete directly from listening")

// Synchronous start failures are visible even when no task became active.
diagnostics.failed(domain: "test.recognizer", code: 42, reason: "start_failed")
check(diagnostics.state == .idle && diagnostics.failureCount == 1 && diagnostics.sessions == 2,
      "a synchronous failure counts without inventing a session")
check(diagnostics.lastFailureDomain == "test.recognizer" && diagnostics.lastFailureCode == 42 &&
      diagnostics.lastEndReason == "start_failed", "failure records only domain, code and reason")
diagnostics.started()
check(diagnostics.failureCount == 1 && diagnostics.lastFailureDomain == "test.recognizer" &&
      diagnostics.lastFailureCode == 42, "starting preserves the most recent failure metadata")
check(diagnostics.sessions == 3 && diagnostics.lastEndReason == "none",
      "retry starts another session with no end reason")
diagnostics.appended(samples: 100, rms: 0.4)
diagnostics.failed(domain: "test.runtime", code: -7, reason: "recognition_failed")
check(diagnostics.state == .idle && diagnostics.failureCount == 2 && diagnostics.sessionSamples == 100,
      "asynchronous failure preserves accepted samples for that session")
check(diagnostics.lastFailureDomain == "test.runtime" && diagnostics.lastFailureCode == -7,
      "a newer failure replaces previous error metadata")
diagnostics.completed(nonempty: true, reason: "late_failure_final")
check(diagnostics.nonemptyFinalCount == 1 && diagnostics.lastEndReason == "recognition_failed",
      "a final arriving after failure is ignored")

diagnostics.started()
diagnostics.appended(samples: 200, rms: 0.1)
diagnostics.partial(nonempty: true)
diagnostics.stopped(reason: "audio_disconnected")
check(diagnostics.state == .idle && diagnostics.lastEndReason == "audio_disconnected",
      "explicit stop records its reason")
check(diagnostics.sessions == 4 && diagnostics.sessionSamples == 200 && diagnostics.acceptedSamples == 1_310,
      "explicit stop preserves sample and session totals")
check(diagnostics.partialCount == 4 && diagnostics.nonemptyPartialCount == 3 &&
      diagnostics.failureCount == 2, "stopping does not change callback totals")
diagnostics.completed(nonempty: true, reason: "late_stop_final")
diagnostics.appended(samples: 640, rms: 1)
diagnostics.partial(nonempty: true)
check(diagnostics.nonemptyFinalCount == 1 && diagnostics.emptyFinalCount == 1 &&
      diagnostics.acceptedSamples == 1_310 && diagnostics.partialCount == 4,
      "stop rejects late final, partial and audio callbacks")
check(diagnostics.lastEndReason == "audio_disconnected", "late callbacks preserve the stop reason")
diagnostics.stopped(reason: "user_pause")
check(diagnostics.lastEndReason == "user_pause" && diagnostics.sessions == 4,
      "an idle explicit stop can update its reason without changing counts")

diagnostics.reset()
check(diagnostics.state == .idle && diagnostics.sessions == 0 && diagnostics.acceptedSamples == 0 &&
      diagnostics.sessionSamples == 0, "reset clears state and sample totals")
check(diagnostics.lastRMS == 0 && diagnostics.sessionPeakRMS == 0, "reset clears RMS values")
check(diagnostics.partialCount == 0 && diagnostics.nonemptyPartialCount == 0 &&
      diagnostics.emptyFinalCount == 0 && diagnostics.nonemptyFinalCount == 0 && diagnostics.failureCount == 0,
      "reset clears every callback counter")
check(diagnostics.lastFailureDomain.isEmpty && diagnostics.lastFailureCode == nil &&
      diagnostics.lastEndReason == "not_started", "reset clears failure metadata and the end reason")
print("Passed \(checks - checksBeforeDiagnostics) OpenGlass speech-diagnostics checks")

// Only audio that arrives after endAudio can be handed to the next request.
// A normal nonempty result, explicit stop or error discards the handoff.
let checksBeforeHandoff = checks
let handoffNow: TimeInterval = 10
let firstPCM = Data([0, 1, 2, 3])
let secondPCM = Data([4, 5, 6, 7])
var handoff = OpenGlassSpeechHandoffBuffer(capacity: 8)
check(handoff.phase == .idle && handoff.byteCount == 0 && !handoff.hasReadyAudio(at: handoffNow),
      "handoff begins idle and empty")
handoff.append(firstPCM)
check(handoff.byteCount == 0 && handoff.takeReadyAudio(now: handoffNow).isEmpty,
      "audio before finishing cannot enter the handoff")
handoff.begin()
check(handoff.phase == .collecting && !handoff.hasReadyAudio(at: handoffNow),
      "begin creates an empty collecting phase")
handoff.append(firstPCM)
check(handoff.byteCount == 4 && !handoff.hasReadyAudio(at: handoffNow),
      "collected bytes are not ready until the empty final")
check(handoff.takeReadyAudio(now: handoffNow).isEmpty && handoff.byteCount == 4 && handoff.phase == .collecting,
      "taking before completion cannot drain collecting audio")
handoff.complete(empty: true, now: handoffNow)
check(handoff.phase == .ready && handoff.hasReadyAudio(at: handoffNow) && handoff.byteCount == 4,
      "empty final makes collected audio available")
handoff.append(secondPCM)
check(handoff.byteCount == 4, "audio after completion cannot enter the handoff")
check(handoff.takeReadyAudio(now: handoffNow) == firstPCM, "empty final hands off the original PCM bytes")
check(handoff.phase == .idle && handoff.byteCount == 0 && !handoff.hasReadyAudio(at: handoffNow),
      "taking ready audio clears state and storage")
check(handoff.takeReadyAudio(now: handoffNow).isEmpty, "an empty final can only be drained once")
handoff.append(secondPCM)
check(handoff.byteCount == 0, "audio after draining cannot start another handoff")

handoff.begin()
handoff.append(Data([0, 1, 2]))
handoff.append(Data([3, 4, 5, 6, 7, 8, 9]))
handoff.append(Data([10, 11]))
check(handoff.byteCount == 8, "collecting never exceeds the byte capacity")
handoff.complete(empty: true, now: handoffNow)
check(handoff.takeReadyAudio(now: handoffNow) == Data([0, 1, 2, 3, 4, 5, 6, 7]),
      "overflow retains the stream prefix across packet boundaries")

var oddCapacity = OpenGlassSpeechHandoffBuffer(capacity: 5)
oddCapacity.begin()
oddCapacity.append(Data([0, 1, 2]))
oddCapacity.append(Data([3, 4, 5]))
check(oddCapacity.byteCount == 4, "odd capacity rounds down to a complete PCM16 sample")
oddCapacity.complete(empty: true, now: handoffNow)
check(oddCapacity.takeReadyAudio(now: handoffNow) == firstPCM,
      "odd packet boundaries preserve prefix order and sample alignment")
var tinyCapacity = OpenGlassSpeechHandoffBuffer(capacity: 1)
tinyCapacity.begin()
tinyCapacity.append(firstPCM)
tinyCapacity.complete(empty: true, now: handoffNow)
check(tinyCapacity.phase == .ready && tinyCapacity.byteCount == 0 && !tinyCapacity.hasReadyAudio(at: handoffNow),
      "sub-sample capacity can complete without retaining a partial sample")
check(tinyCapacity.takeReadyAudio(now: handoffNow).isEmpty && tinyCapacity.phase == .idle,
      "an empty ready buffer can be drained and cleared")
var negativeCapacity = OpenGlassSpeechHandoffBuffer(capacity: -3)
negativeCapacity.begin()
negativeCapacity.append(firstPCM)
check(negativeCapacity.byteCount == 0, "negative capacity is safely treated as zero")
var defaultCapacity = OpenGlassSpeechHandoffBuffer()
defaultCapacity.begin()
defaultCapacity.append(Data(repeating: 9, count: 70_403))
check(defaultCapacity.byteCount == 70_400, "default capacity holds at most 2.2 seconds of PCM16")
defaultCapacity.clear()

handoff.begin()
handoff.append(firstPCM)
handoff.complete(empty: false, now: handoffNow)
check(handoff.phase == .idle && handoff.byteCount == 0 && handoff.takeReadyAudio(now: handoffNow).isEmpty,
      "a nonempty final discards buffered audio before inference or TTS")
handoff.append(secondPCM)
check(handoff.byteCount == 0, "nonempty completion rejects late audio")
handoff.begin()
handoff.append(firstPCM)
handoff.clear()
handoff.complete(empty: true, now: handoffNow)
check(handoff.phase == .idle && handoff.takeReadyAudio(now: handoffNow).isEmpty,
      "stop or error before finalization prevents a late empty final from reviving audio")
handoff.begin()
handoff.append(firstPCM)
handoff.complete(empty: true, now: handoffNow)
handoff.clear()
check(handoff.phase == .idle && handoff.byteCount == 0 && handoff.takeReadyAudio(now: handoffNow).isEmpty,
      "stop or error also discards audio already ready for handoff")

handoff.begin()
handoff.append(firstPCM)
handoff.begin()
check(handoff.phase == .collecting && handoff.byteCount == 0,
      "starting a new collection discards an unfinished old collection")
handoff.append(secondPCM)
handoff.complete(empty: true, now: handoffNow)
check(handoff.takeReadyAudio(now: handoffNow) == secondPCM, "a new round never replays the previous round's PCM")
handoff.begin()
handoff.append(firstPCM)
handoff.complete(empty: true, now: handoffNow)
handoff.begin()
handoff.append(secondPCM)
handoff.complete(empty: true, now: handoffNow)
check(handoff.takeReadyAudio(now: handoffNow) == secondPCM, "begin also isolates an old ready handoff from a new round")
handoff.begin()
handoff.append(firstPCM)
handoff.complete(empty: true, now: handoffNow)
handoff.complete(empty: false, now: handoffNow)
check(handoff.takeReadyAudio(now: handoffNow).isEmpty && handoff.phase == .idle,
      "a nonempty outcome invalidates a previously ready handoff")

handoff.begin()
handoff.append(Data([0x34]))
handoff.append(Data([0x12]))
handoff.complete(empty: true, now: handoffNow)
check(handoff.takeReadyAudio(now: handoffNow) == Data([0x34, 0x12]),
      "two half-sample packets concatenate without losing either byte")
handoff.begin()
handoff.append(Data([0x34]))
handoff.complete(empty: true, now: handoffNow)
check(handoff.takeReadyAudio(now: handoffNow) == Data([0x34]),
      "an incomplete final sample is preserved for the next live packet")

handoff.begin()
handoff.append(firstPCM)
handoff.complete(empty: true, now: handoffNow)
check(handoff.hasReadyAudio(at: handoffNow + 0.499), "handoff remains ready within its short transfer window")
check(handoff.hasReadyAudio(at: handoffNow + 0.5), "handoff is valid at the half-second deadline")
check(!handoff.hasReadyAudio(at: handoffNow + 0.501), "handoff expires after the half-second deadline")
check(handoff.takeReadyAudio(now: handoffNow + 0.501).isEmpty && handoff.phase == .idle && handoff.byteCount == 0,
      "delayed frame or admission gate cannot replay expired PCM")
handoff.begin()
handoff.append(secondPCM)
handoff.complete(empty: true, now: handoffNow + 1)
check(!handoff.hasReadyAudio(at: handoffNow), "a timestamp before completion cannot make a handoff ready")
check(handoff.takeReadyAudio(now: handoffNow + 1.5) == secondPCM,
      "new completion owns a fresh deadline and can drain exactly at it")
handoff.begin()
handoff.append(firstPCM)
handoff.complete(empty: true, now: handoffNow)
handoff.clear()
check(!handoff.hasReadyAudio(at: handoffNow + 0.1) && handoff.takeReadyAudio(now: handoffNow + 0.1).isEmpty,
      "clear invalidates an unexpired ready handoff")
print("Passed \(checks - checksBeforeHandoff) OpenGlass speech-handoff checks")
print("Passed \(checks) OpenGlass voice-loop, diagnostics and handoff checks")
