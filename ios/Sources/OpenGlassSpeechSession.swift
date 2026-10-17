import Foundation
import Speech
import AVFoundation

/// Recognizes only incoming Rokid PCM16LE, 16 kHz, mono. It never opens the
/// iPhone microphone and never falls back to Apple's cloud recognition.
@MainActor
final class OpenGlassSpeechSession {
    var onPartial: ((String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onError: ((Failure) -> Void)?
    var onStatus: ((String) -> Void)?
    private(set) var isListening = false
    private(set) var diagnostics = OpenGlassSpeechDiagnostics()
    var isActive: Bool { request != nil }
    var isFinishing: Bool { ending }
    var hasPendingHandoffAudio: Bool {
        handoff.hasReadyAudio(at: ProcessInfo.processInfo.systemUptime)
    }
    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var timer: Timer?
    private var finishTimer: Timer?
    private var sessionID = UUID()
    private var latestText = ""
    private var startedAt = ProcessInfo.processInfo.systemUptime
    private var lastVoiceAt: TimeInterval?
    private var lastTranscriptAt: TimeInterval?
    private var lastPacketAt = ProcessInfo.processInfo.systemUptime
    private var pendingByte: UInt8?
    private var handoff = OpenGlassSpeechHandoffBuffer()
    private var ending = false
    private let format = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                      sampleRate: 16_000, channels: 1, interleaved: false)!

    static func authorize() async -> Bool {
        if SFSpeechRecognizer.authorizationStatus() == .authorized { return true }
        return await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
    }

    func start(localeIdentifier: String) throws {
        // Only a normal empty final can prepare this one-shot handoff. Taking
        // it before cleanup preserves new speech received after endAudio.
        let carriedAudio = handoff.takeReadyAudio(now: ProcessInfo.processInfo.systemUptime)
        stop(reason: "restart")
        guard SFSpeechRecognizer.authorizationStatus() == .authorized else {
            recordFailure(.permissionDenied)
            throw Failure.permissionDenied
        }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeIdentifier)),
              recognizer.supportsOnDeviceRecognition else {
            recordFailure(.languageUnavailable)
            throw Failure.languageUnavailable
        }
        guard recognizer.isAvailable else {
            recordFailure(.temporarilyUnavailable)
            throw Failure.temporarilyUnavailable
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.taskHint = .dictation
        if #available(iOS 16.0, *) { request.addsPunctuation = true }
        self.recognizer = recognizer
        self.request = request
        latestText = ""
        ending = false
        startedAt = ProcessInfo.processInfo.systemUptime
        lastPacketAt = startedAt
        lastVoiceAt = nil
        lastTranscriptAt = nil
        isListening = true
        diagnostics.started()
        logDiagnostics(event: "started")
        let id = sessionID
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor in
                guard let self, self.sessionID == id else { return }
                if let result {
                    let text = result.bestTranscription.formattedString
                    if !text.isEmpty && text != self.latestText {
                        self.lastVoiceAt = ProcessInfo.processInfo.systemUptime
                        self.lastTranscriptAt = self.lastVoiceAt
                    }
                    self.latestText = text
                    if !result.isFinal {
                        self.diagnostics.partial(nonempty: !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    self.onPartial?(self.latestText)
                    if result.isFinal {
                        self.complete(reason: "native_final", allowHandoff: error == nil)
                        return
                    }
                }
                if let error {
                    if self.ending && !self.latestText.isEmpty {
                        self.complete(reason: "finalization_error", allowHandoff: false)
                    } else {
                        let failure: Failure
                        if SFSpeechRecognizer.authorizationStatus() != .authorized {
                            failure = .permissionDenied
                        } else if self.recognizer?.supportsOnDeviceRecognition == false {
                            failure = .languageUnavailable
                        } else {
                            failure = .recognitionInterrupted(error.localizedDescription)
                        }
                        self.stop(reason: "recognizer_error")
                        self.recordFailure(failure, underlying: error)
                        self.onError?(failure)
                    }
                }
            }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        if !carriedAudio.isEmpty { appendPCM16LE(carriedAudio) }
        onStatus?("正在听 Rokid 麦克风（离线识别）")
    }

    func appendPCM16LE(_ data: Data) {
        guard !data.isEmpty else { return }
        if ending {
            // This is only the short endAudio/final-result window. Controller
            // gates still discard all inference, TTS and user-paused audio.
            handoff.append(data)
            return
        }
        guard isListening, let request else { return }
        // Preserve a split sample across websocket messages. At most one byte
        // is retained; audio received during inference/TTS is simply dropped.
        var bytes = [UInt8]()
        bytes.reserveCapacity(data.count + 1)
        if let pendingByte { bytes.append(pendingByte) }
        bytes.append(contentsOf: data)
        pendingByte = bytes.count.isMultiple(of: 2) ? nil : bytes.removeLast()
        let frames = bytes.count / 2
        guard frames > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)),
              let samples = buffer.floatChannelData?[0] else { return }
        buffer.frameLength = AVAudioFrameCount(frames)
        var squareSum: Float = 0
        for index in 0..<frames {
            let bits = UInt16(bytes[index * 2]) | (UInt16(bytes[index * 2 + 1]) << 8)
            let sample = Float(Int16(bitPattern: bits)) / 32768
            samples[index] = sample
            squareSum += sample * sample
        }
        let now = ProcessInfo.processInfo.systemUptime
        lastPacketAt = now
        let rms = sqrt(squareSum / Float(frames))
        if rms > 0.012 { lastVoiceAt = now }
        request.append(buffer)
        diagnostics.appended(samples: frames, rms: Double(rms))
    }

    /// Stop accepting audio and give the recognizer time to finalize its text.
    func finishUtterance(reason: String = "manual") {
        guard isListening, !ending else { return }
        ending = true
        isListening = false
        handoff.begin()
        // A split sample's first byte has not reached the old request. Keep it
        // ahead of the post-endAudio stream so the next request stays aligned.
        if let pendingByte { handoff.append(Data([pendingByte])) }
        pendingByte = nil
        diagnostics.finishing(reason: reason)
        logDiagnostics(event: "finishing")
        timer?.invalidate()
        timer = nil
        request?.endAudio()
        onStatus?("正在结束语句…")
        let id = sessionID
        finishTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.sessionID == id else { return }
                self.complete(reason: "finish_timeout")
            }
        }
    }

    func stop(reason: String = "cancelled") {
        cancelRecognition(reason: reason, preservingHandoff: false)
    }

    private func cancelRecognition(reason: String, preservingHandoff: Bool) {
        if !preservingHandoff { handoff.clear() }
        let wasActive = diagnostics.state != .idle
        // No-op cleanup after a final/error must not replace its useful cause.
        if wasActive { diagnostics.stopped(reason: reason) }
        sessionID = UUID()
        isListening = false
        ending = false
        timer?.invalidate()
        timer = nil
        finishTimer?.invalidate()
        finishTimer = nil
        request?.endAudio()
        task?.cancel()
        task = nil
        request = nil
        recognizer = nil
        pendingByte = nil
        if wasActive { logDiagnostics(event: "stopped") }
    }

    /// Reset only when beginning a new receiver run, never between ASR retries.
    func resetDiagnostics() {
        diagnostics.reset()
        logDiagnostics(event: "reset")
    }

    private func tick() {
        guard isListening else { return }
        let now = ProcessInfo.processInfo.systemUptime
        let action = OpenGlassUtteranceTiming.action(now: now, startedAt: startedAt,
            lastPacketAt: lastPacketAt, lastVoiceAt: lastVoiceAt,
            lastTranscriptAt: lastTranscriptAt, hasText: containsWords(latestText))
        switch action {
        case .finish: finishUtterance(reason: "timer_finish")
        case .audioTimeout:
            stop(reason: "audio_timeout")
            recordFailure(.audioTimeout)
            onError?(.audioTimeout)
        case .listen: break
        }
    }

    private func complete(reason: String, allowHandoff: Bool = true) {
        let candidate = latestText.trimmingCharacters(in: .whitespacesAndNewlines)
        // Punctuation-only final results are not questions. A normal empty
        // result lets the controller rearm immediately when fresh PCM is ready.
        let text = containsWords(candidate) ? candidate : ""
        let preserveHandoff = text.isEmpty && allowHandoff
        handoff.complete(empty: preserveHandoff, now: ProcessInfo.processInfo.systemUptime)
        diagnostics.completed(nonempty: !text.isEmpty, reason: reason)
        cancelRecognition(reason: reason, preservingHandoff: preserveHandoff)
        logDiagnostics(event: "final")
        onFinal?(text)
    }

    private func containsWords(_ text: String) -> Bool {
        text.unicodeScalars.contains { CharacterSet.alphanumerics.contains($0) }
    }

    private func recordFailure(_ failure: Failure, underlying: Error? = nil) {
        let reason: String
        let localCode: Int
        switch failure {
        case .permissionDenied: reason = "permission_denied"; localCode = 1
        case .languageUnavailable: reason = "language_unavailable"; localCode = 2
        case .temporarilyUnavailable: reason = "recognizer_unavailable"; localCode = 3
        case .audioTimeout: reason = "audio_timeout"; localCode = 4
        case .recognitionInterrupted: reason = "recognizer_error"; localCode = 5
        }
        let nativeError = underlying.map { $0 as NSError }
        diagnostics.failed(domain: nativeError?.domain ?? "OpenGlassSpeech",
                           code: nativeError?.code ?? localCode, reason: reason)
        logDiagnostics(event: "failed")
    }

    /// State transitions only: never log PCM, recognition text, or error descriptions.
    private func logDiagnostics(event: String) {
        let d = diagnostics
        let summary = "OpenGlassASR event=\(event) state=\(d.state.rawValue) session=\(d.sessions)"
            + " accepted_samples=\(d.acceptedSamples) session_samples=\(d.sessionSamples)"
            + " rms=\(String(format: "%.5f", d.lastRMS)) peak_rms=\(String(format: "%.5f", d.sessionPeakRMS))"
            + " partials=\(d.partialCount) nonempty_partials=\(d.nonemptyPartialCount)"
            + " empty_finals=\(d.emptyFinalCount) nonempty_finals=\(d.nonemptyFinalCount)"
            + " failures=\(d.failureCount) failure_domain=\(d.lastFailureDomain)"
            + " failure_code=\(d.lastFailureCode.map(String.init) ?? "none") end=\(d.lastEndReason)"
        NSLog("%@", summary)
    }

    enum Failure: LocalizedError {
        case permissionDenied, languageUnavailable, temporarilyUnavailable, audioTimeout
        case recognitionInterrupted(String)

        var requiresUserAction: Bool {
            switch self {
            case .permissionDenied, .languageUnavailable: return true
            default: return false
            }
        }
        var errorDescription: String? {
            switch self {
            case .permissionDenied:
                return "未获语音识别权限。请在系统设置中允许后点击“恢复自动语音”；文字提问仍可使用。"
            case .languageUnavailable:
                return "此语言的离线识别资源不可用。请下载资源或切换语言后恢复；不会转到云端。"
            case .temporarilyUnavailable:
                return "系统离线识别暂不可用。"
            case .audioTimeout:
                return "Rokid 音频暂时没有到达。"
            case .recognitionInterrupted(let message):
                return "离线识别暂时中断：\(message)"
            }
        }
    }
}
