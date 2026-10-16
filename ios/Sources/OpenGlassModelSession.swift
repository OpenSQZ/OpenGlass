import Foundation
import Combine

/// Owns exactly one native model context. All entry points run on the main actor;
/// a busy session rejects new work instead of queueing frames or questions.
@MainActor
final class OpenGlassModelSession {
    enum Phase: Equatable {
        case unloaded, loading, ready, preparing, generating, stopping, failed
    }

    enum SessionError: LocalizedError {
        case busy, missingFiles, cancelled, restartRequired, invalidFile(String)
        var errorDescription: String? {
            switch self {
            case .busy: return "上一轮尚未结束，请稍候。"
            case .missingFiles: return "请先在“模型下载”中下载 MiniCPM-V 4.6 的 LLM 和 VPM 文件。"
            case .cancelled: return "已停止本轮。"
            case .restartRequired: return "底层推理超时，已锁定模型以保护内存。请关闭并重新打开 App。"
            case .invalidFile(let name): return "模型文件无效：\(name)。请在模型下载页重新下载。"
            }
        }
    }

    var onChange: ((Phase, String) -> Void)?
    var onOutput: ((String) -> Void)?
    private(set) var phase: Phase = .unloaded
    private(set) var isBusy = false
    private(set) var isLoaded = false
    private(set) var requiresAppRestart = false
    private var wrapper: MTMDWrapper?
    private var tokenSubscription: AnyCancellable?
    private var discardResult = false
    private var output = ""
    // A timed-out worker cannot be joined through the upstream API. Keep its
    // context alive even if UIKit later discards the view controller.
    private static var quarantinedContexts: [MTMDWrapper] = []

    static var modelFiles: [String] {
        [MiniCPMModelConst.modelv46_FileName, MiniCPMModelConst.mmprojv46_FileName]
    }

    func load() async throws {
        guard !requiresAppRestart else { throw SessionError.restartRequired }
        guard !isBusy else { throw SessionError.busy }
        if isLoaded { return }
        isBusy = true
        discardResult = false
        transition(.loading, "正在加载 MiniCPM-V 4.6…")
        defer { isBusy = false }
        do {
            let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            let files = Self.modelFiles.map { documents.appendingPathComponent($0) }
            for file in files {
                guard FileManager.default.fileExists(atPath: file.path) else { throw SessionError.missingFiles }
                // Reject obvious truncation before doing full artifact validation.
                let handle = try FileHandle(forReadingFrom: file)
                let header = try handle.read(upToCount: 4)
                try handle.close()
                guard header == Data("GGUF".utf8) else { throw SessionError.invalidFile(file.lastPathComponent) }
            }
            // The upstream download UI reports completion before its background
            // checksum finishes. A GGUF header/filename alone cannot prove that
            // these are the matching 4.6 LLM and projector. Reuse its cached
            // validation receipts, and keep first-time hashing off the UI actor.
            let checksums = [MiniCPMModelConst.modelv46_Q4_K_M_MD5,
                             MiniCPMModelConst.modelMMProjv46_MD5]
            transition(.loading, "正在校验 MiniCPM-V 4.6 模型文件…")
            let invalidFile = await Task.detached(priority: .userInitiated) {
                for (file, checksum) in zip(files, checksums) {
                    if !MBModelArtifactStore.validate(file, expectedMD5: checksum) {
                        return Optional(file.lastPathComponent)
                    }
                }
                return Optional<String>.none
            }.value
            if let invalidFile { throw SessionError.invalidFile(invalidFile) }
            try checkStopped()
            transition(.loading, "正在加载 MiniCPM-V 4.6…")
            let engine = MTMDWrapper()
            wrapper = engine
            engine.setEnableThinking(false)
            let params = MTMDParams(modelPath: files[0].path, mmprojPath: files[1].path,
                                    nPredict: 160, nCtx: 4096, nThreads: 4,
                                    temperature: 0.2, useGPU: true, mmprojUseGPU: true,
                                    warmup: false, nUbatch: 512, imageMaxSliceNums: 1)
            try await engine.initialize(with: params)
            engine.setModelVersion(46)
            engine.decodeTokenLimit = 160
            tokenSubscription = engine.$currentToken.dropFirst().sink { [weak self] token in
                guard let self, self.phase == .generating || self.phase == .stopping else { return }
                self.output += token.content
                if !self.discardResult { self.onOutput?(self.output) }
            }
            isLoaded = true
            transition(.ready, discardResult ? "模型已加载；接收已停止。" : "MiniCPM-V 4.6 已就绪")
        } catch {
            // initialize has returned; no native worker is left running on this path.
            if let engine = wrapper { await engine.reset() }
            wrapper = nil
            isLoaded = false
            transition(.failed, error.localizedDescription)
            throw error
        }
    }

    func ask(question: String, imageURL: URL?, answerLanguage: String = "zh-CN") async throws -> String {
        guard !requiresAppRestart else { throw SessionError.restartRequired }
        guard !isBusy else { throw SessionError.busy }
        guard isLoaded, let engine = wrapper else { throw SessionError.missingFiles }
        isBusy = true
        discardResult = false
        output = ""
        engine.decodeTokenLimit = 160
        transition(.preparing, imageURL == nil ? "正在处理问题…" : "正在理解最新画面…")
        defer { isBusy = false }
        do {
            // Previous turn has fully completed before clearing KV. Never call
            // upstream stopGeneration(), which publishes completion too early.
            engine.clearKVCacheForNewTurn()
            if let imageURL {
                try await engine.addImageInBackground(imageURL.path, timeoutSeconds: 180)
            }
            try checkStopped()
            let instruction = answerLanguage.hasPrefix("en")
                ? "Answer briefly in English, suitable for reading aloud, in at most 80 words. Describe only what you can confirm.\n"
                : "请用简洁中文回答，适合语音朗读，不超过100字。只描述你确实能确认的内容。\n"
            let prompt = instruction + question
            try await engine.addTextInBackground(prompt)
            try checkStopped()
            transition(.generating, "正在生成回答…")
            try await engine.startGeneration()
            // startGeneration launches an internal task, so await its terminal
            // state before letting another request touch the context.
            while engine.generationState == .generating {
                try? await Task.sleep(nanoseconds: 30_000_000)
            }
            if case .failed(let error) = engine.generationState { throw error }
            try checkStopped()
            let answer = output.trimmingCharacters(in: .whitespacesAndNewlines)
            transition(.ready, "回答完成")
            return answer
        } catch {
            if let mtmdError = error as? MTMDError, case .timeout = mtmdError {
                // The upstream watchdog can return while C++ still uses ctx.
                // Retain the wrapper, never reset/free or start a second call.
                requiresAppRestart = true
                Self.quarantinedContexts.append(engine)
                transition(.failed, SessionError.restartRequired.localizedDescription)
                throw SessionError.restartRequired
            }
            if let sessionError = error as? SessionError, case .cancelled = sessionError {
                transition(.ready, "本轮已结束，可重新启动。")
            } else {
                // The native operation has returned. Reinitializing next time
                // is safer than reusing a context after a prefill error.
                await engine.reset()
                tokenSubscription = nil
                wrapper = nil
                isLoaded = false
                transition(.failed, error.localizedDescription)
            }
            throw error
        }
    }

    func requestStop() {
        discardResult = true
        guard isBusy else { return }
        if phase == .generating {
            // A Swift-only flag read after mb_mtmd_loop returns. The next token
            // finishes normally, including native perf bookkeeping.
            wrapper?.decodeTokenLimit = 1
        }
        transition(.stopping, "正在结束本轮推理…")
    }

    /// Used before entering the download UI, which can replace model files.
    func unload() async throws {
        guard !requiresAppRestart else { throw SessionError.restartRequired }
        guard !isBusy else { throw SessionError.busy }
        if let engine = wrapper { await engine.reset() }
        wrapper = nil
        tokenSubscription = nil
        isLoaded = false
        transition(.unloaded, "模型未加载")
    }

    private func checkStopped() throws {
        if discardResult { throw SessionError.cancelled }
    }

    private func transition(_ phase: Phase, _ message: String) {
        self.phase = phase
        onChange?(phase, message)
    }
}
