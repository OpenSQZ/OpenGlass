import Foundation

/// In-memory PCM received only after endAudio, for one empty-result handoff.
/// Audio already appended to a recognition request must never enter this buffer.
struct OpenGlassSpeechHandoffBuffer {
    enum Phase: Equatable {
        case idle, collecting, ready
    }

    private(set) var phase: Phase = .idle
    private let capacity: Int
    private var bytes = Data()
    private var readyAt: TimeInterval?

    var byteCount: Int { bytes.count }

    func hasReadyAudio(at now: TimeInterval) -> Bool {
        guard phase == .ready, !bytes.isEmpty, let readyAt else { return false }
        return now >= readyAt && now - readyAt <= 0.5
    }

    init(capacity: Int = 70_400) {
        let nonnegativeCapacity = max(0, capacity)
        self.capacity = nonnegativeCapacity - nonnegativeCapacity % 2
    }

    mutating func begin() {
        clear()
        phase = .collecting
    }

    mutating func append(_ data: Data) {
        guard phase == .collecting, bytes.count < capacity else { return }
        // Preserve the start of an utterance instead of replacing it with its tail.
        bytes.append(contentsOf: data.prefix(capacity - bytes.count))
    }

    mutating func complete(empty: Bool, now: TimeInterval) {
        guard empty, phase == .collecting else { clear(); return }
        phase = .ready
        readyAt = now
    }

    mutating func takeReadyAudio(now: TimeInterval) -> Data {
        guard phase == .ready else { return Data() }
        guard hasReadyAudio(at: now) else { clear(); return Data() }
        let audio = bytes
        clear()
        return audio
    }

    mutating func clear() {
        phase = .idle
        bytes.removeAll(keepingCapacity: false)
        readyAt = nil
    }
}
