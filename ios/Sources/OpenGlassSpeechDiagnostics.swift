/// Operational metadata only: never retains speech, audio or error descriptions.
struct OpenGlassSpeechDiagnostics {
    enum State: String {
        case idle, listening, finishing
    }

    private(set) var state: State = .idle
    private(set) var sessions = 0
    private(set) var acceptedSamples = 0
    private(set) var sessionSamples = 0
    private(set) var lastRMS = 0.0
    private(set) var sessionPeakRMS = 0.0
    private(set) var partialCount = 0
    private(set) var nonemptyPartialCount = 0
    private(set) var emptyFinalCount = 0
    private(set) var nonemptyFinalCount = 0
    private(set) var failureCount = 0
    private(set) var lastFailureDomain = ""
    private(set) var lastFailureCode: Int?
    private(set) var lastEndReason = "not_started"

    mutating func reset() {
        self = Self()
    }

    mutating func started() {
        state = .listening
        sessions += 1
        sessionSamples = 0
        lastRMS = 0
        sessionPeakRMS = 0
        lastEndReason = "none"
    }

    mutating func appended(samples: Int, rms: Double) {
        guard state == .listening, samples > 0 else { return }
        acceptedSamples += samples
        sessionSamples += samples
        // Invalid measurements must not introduce NaN/Infinity into diagnostics.
        lastRMS = rms.isFinite ? max(0, rms) : 0
        sessionPeakRMS = max(sessionPeakRMS, lastRMS)
    }

    mutating func partial(nonempty: Bool) {
        guard state != .idle else { return }
        partialCount += 1
        if nonempty { nonemptyPartialCount += 1 }
    }

    mutating func finishing(reason: String) {
        guard state == .listening else { return }
        state = .finishing
        lastEndReason = reason
    }

    mutating func completed(nonempty: Bool, reason: String) {
        guard state != .idle else { return }
        if nonempty { nonemptyFinalCount += 1 }
        else { emptyFinalCount += 1 }
        state = .idle
        lastEndReason = reason
    }

    mutating func failed(domain: String, code: Int, reason: String) {
        // A recognizer can fail synchronously before a session starts.
        failureCount += 1
        state = .idle
        lastFailureDomain = domain
        lastFailureCode = code
        lastEndReason = reason
    }

    mutating func stopped(reason: String) {
        state = .idle
        lastEndReason = reason
    }
}
