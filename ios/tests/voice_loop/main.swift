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

// Silent recognition failures keep a positive, bounded delay. Wakeups at the
// deadline may retry; repeated audio/frame notifications before it may not.
var silence = OpenGlassVoiceLoopPolicy()
silence.requestListening()
let expectedDelays: [TimeInterval] = [0.5, 1, 2, 4, 8, 8, 8]
var now: TimeInterval = 1_000
for (index, expected) in expectedDelays.enumerated() {
    let delay = silence.retry(now: now)
    checkTime(delay, expected, "silence retry \(index + 1) uses bounded backoff")
    checkTime(silence.notBefore, now + expected, "retry sets a future deadline")
    check(!silence.canListen(at: now), "retry cannot spin immediately")
    check(!silence.canListen(at: silence.notBefore - 0.001), "retry waits through its deadline")
    check(silence.canListen(at: silence.notBefore), "retry is allowed at its deadline")
    now = silence.notBefore
}
for _ in 0..<1_000 {
    checkTime(silence.retry(now: now), 8, "long silence stays capped at eight seconds")
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
      "voice silence waits the full 1.3 seconds")
check(OpenGlassUtteranceTiming.action(now: 1.3, startedAt: 0,
                                    lastPacketAt: 1.3, lastVoiceAt: 0,
                                    lastTranscriptAt: nil, hasText: false) == .finish,
      "silence after a voice marker ends even an empty utterance")

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
                                    lastTranscriptAt: nil, hasText: false) == .finish,
      "voice silence finalization takes precedence over a packet timeout")
check(OpenGlassUtteranceTiming.action(now: 25, startedAt: 0,
                                    lastPacketAt: 0, lastVoiceAt: nil,
                                    lastTranscriptAt: nil, hasText: false) == .finish,
      "duration cap finalization takes precedence over a packet timeout")

print("Passed \(checks) OpenGlass voice-loop policy checks")
