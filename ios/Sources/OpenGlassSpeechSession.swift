import Foundation
import Speech
import AVFoundation

/// Recognizes only incoming Rokid PCM16LE, 16 kHz, mono. It never opens the
/// iPhone microphone and never falls back to Apple's cloud recognition.
@MainActor
final class OpenGlassSpeechSession {
    var onPartial: ((String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onError: ((String) -> Void)?
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
    private var startedAt = Date()
    private var lastVoiceAt: Date?
    private var lastPacketAt = Date()
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
            throw SpeechError.message("未获语音识别权限。可在系统设置中允许，或关闭语音、使用文字提问。")
        }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeIdentifier)),
              recognizer.supportsOnDeviceRecognition else {
            throw SpeechError.message("此语言的离线语音识别不可用。请在 iPhone 下载对应语言资源后重试，或使用文字提问；不会转到云端。")
        }
        guard recognizer.isAvailable else {
            throw SpeechError.message("系统离线语音识别暂不可用，请稍后重新启动，或使用文字提问。")
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
        startedAt = Date()
        lastPacketAt = Date()
        lastVoiceAt = nil
        isListening = true
        let id = sessionID
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor in
                guard let self, self.sessionID == id else { return }
                if let result {
                    let text = result.bestTranscription.formattedString
                    if !text.isEmpty && text != self.latestText { self.lastVoiceAt = Date() }
                    self.latestText = text
                    self.onPartial?(self.latestText)
                    if result.isFinal { self.complete(); return }
                }
                if let error {
                    if self.ending && !self.latestText.isEmpty {
                        self.complete()
                    } else {
                        self.stop()
                        self.onError?("离线语音识别中断：\(error.localizedDescription)。可重新启动或使用文字提问。")
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
        let now = Date()
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
        let now = Date()
        if let lastVoiceAt, now.timeIntervalSince(lastVoiceAt) >= 1.3 {
            finishUtterance()
        } else if now.timeIntervalSince(startedAt) >= 25 {
            finishUtterance()
        } else if now.timeIntervalSince(lastPacketAt) >= 5 {
            stop()
            onError?("Rokid 音频超过5秒未到达。确认连接后重新启动语音。")
        }
    }

    private func complete() {
        let text = latestText.trimmingCharacters(in: .whitespacesAndNewlines)
        stop()
        onFinal?(text)
    }

    enum SpeechError: LocalizedError {
        case message(String)
        var errorDescription: String? {
            if case .message(let message) = self { return message }
            return nil
        }
    }
}
