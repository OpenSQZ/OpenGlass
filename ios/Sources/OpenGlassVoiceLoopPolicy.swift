import Foundation

/// Recovery timing and user intent for the foreground voice loop.
/// Callers supply ProcessInfo.processInfo.systemUptime for every `now` value.
struct OpenGlassVoiceLoopPolicy {
    private(set) var wantsListening = false
    private(set) var blockedReason: String?
    private(set) var retryCount = 0
    private var retryNotBefore: TimeInterval = 0
    private var echoNotBefore: TimeInterval = 0

    var notBefore: TimeInterval { max(retryNotBefore, echoNotBefore) }

    mutating func requestListening() {
        wantsListening = true
        blockedReason = nil
        receivedUtterance()
    }

    mutating func pause() {
        wantsListening = false
        blockedReason = nil
        receivedUtterance()
    }

    mutating func block(reason: String) {
        blockedReason = reason
    }

    /// Returns the retry delay, while `notBefore` also includes any echo hold.
    @discardableResult
    mutating func retry(now: TimeInterval) -> TimeInterval {
        let delays: [TimeInterval] = [0.5, 1, 2, 4, 8]
        let delay = delays[min(retryCount, delays.count - 1)]
        retryCount = min(retryCount + 1, delays.count)
        retryNotBefore = now + delay
        return delay
    }

    mutating func receivedUtterance() {
        retryCount = 0
        retryNotBefore = 0
    }

    mutating func protectFromEcho(now: TimeInterval, delay: TimeInterval = 0.7) {
        echoNotBefore = max(echoNotBefore, now + delay)
    }

    func canListen(at time: TimeInterval) -> Bool {
        wantsListening && blockedReason == nil && time >= notBefore
    }
}

/// Decides when to finalize recognition without depending on Speech or timers.
struct OpenGlassUtteranceTiming {
    enum Action: Equatable {
        case listen
        case finish
        case audioTimeout
    }

    static func action(now: TimeInterval, startedAt: TimeInterval,
                       lastPacketAt: TimeInterval, lastVoiceAt: TimeInterval?,
                       lastTranscriptAt: TimeInterval?, hasText: Bool) -> Action {
        if hasText, let lastTranscriptAt, now - lastTranscriptAt >= 2.5 {
            return .finish
        }
        if hasText, let lastVoiceAt, now - lastVoiceAt >= 1.3 {
            return .finish
        }
        if now - startedAt >= 25 {
            return .finish
        }
        if now - lastPacketAt >= 5 {
            return .audioTimeout
        }
        return .listen
    }
}
