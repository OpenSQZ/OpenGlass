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
    var isActive: Bool { request != nil }
    var isFinishing: Bool { ending }
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
        stop()
        guard SFSpeechRecognizer.authorizationStatus() == .authorized else {
            throw Failure.permissionDenied
        }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeIdentifier)),
              recognizer.supportsOnDeviceRecognition else {
            throw Failure.languageUnavailable
        }
        guard recognizer.isAvailable else {
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
                    self.onPartial?(self.latestText)
                    if result.isFinal { self.complete(); return }
                }
                if let error {
                    if self.ending && !self.latestText.isEmpty {
                        self.complete()
                    } else {
                        let failure: Failure
                        if SFSpeechRecognizer.authorizationStatus() != .authorized {
                            failure = .permissionDenied
                        } else if self.recognizer?.supportsOnDeviceRecognition == false {
                            failure = .languageUnavailable
                        } else {
                            failure = .recognitionInterrupted(error.localizedDescription)
                        }
                        self.stop()
                        self.onError?(failure)
                    }
                }
            }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        onStatus?("正在听 Rokid 麦克风（离线识别）")
    }

    func appendPCM16LE(_ data: Data) {
        guard isListening, !ending, let request, !data.isEmpty else { return }
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
        if sqrt(squareSum / Float(frames)) > 0.012 { lastVoiceAt = now }
        request.append(buffer)
    }

    /// Stop accepting audio and give the recognizer time to finalize its text.
    func finishUtterance() {
        guard isListening, !ending else { return }
        ending = true
        isListening = false
        timer?.invalidate()
        timer = nil
        request?.endAudio()
        onStatus?("正在结束语句…")
        let id = sessionID
        finishTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.sessionID == id else { return }
                self.complete()
            }
        }
    }

    func stop() {
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
    }

    private func tick() {
        guard isListening else { return }
        let now = ProcessInfo.processInfo.systemUptime
        let action = OpenGlassUtteranceTiming.action(now: now, startedAt: startedAt,
            lastPacketAt: lastPacketAt, lastVoiceAt: lastVoiceAt,
            lastTranscriptAt: lastTranscriptAt, hasText: !latestText.isEmpty)
        switch action {
        case .finish: finishUtterance()
        case .audioTimeout:
            stop()
            onError?(.audioTimeout)
        case .listen: break
        }
    }

    private func complete() {
        let candidate = latestText.trimmingCharacters(in: .whitespacesAndNewlines)
        // Punctuation-only final results are not questions. Silence and failed
        // recognition finish normally and let the controller schedule a retry.
        let hasWords = candidate.unicodeScalars.contains { CharacterSet.alphanumerics.contains($0) }
        let text = hasWords ? candidate : ""
        stop()
        onFinal?(text)
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
