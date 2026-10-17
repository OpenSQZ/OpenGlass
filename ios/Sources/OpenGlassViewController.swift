import UIKit
import AVFoundation
import Darwin

/// Foreground Rokid receiver with one-image, one-question local inference.
@MainActor
final class OpenGlassViewController: UIViewController, UITextFieldDelegate, AVSpeechSynthesizerDelegate {
    private let server = RokidIngressServer()
    private let model = OpenGlassModelSession()
    // The upstream downloader requires its ObservableObject for file/progress
    // bookkeeping. Never call initialize() on this download-only wrapper.
    private lazy var downloadWrapper = MTMDWrapperExample()
    private let speech = OpenGlassSpeechSession()
    private let synthesizer = AVSpeechSynthesizer()
    private var activeUtterance: AVSpeechUtterance?
    private var isRunning = false
    private var isStarting = false
    private var isAuthorizingSpeech = false
    private var isAnswering = false
    private var audioConnected = false
    private var voiceLoop = OpenGlassVoiceLoopPolicy()
    private var wantsService = false
    private var attemptedAutomaticStart = false
    private var isInBackground = false
    private var audioInterruptionActive = false
    private var listeningWakeTask: Task<Void, Never>?
    private var speechAuthorizationID = UUID()
    private var completedTurns = 0
    private var suppressAnswerSpeech = false
    private var runID = UUID()
    private var turnID = UUID()
    private var latestRawImage: Data?
    private var latestImage: UIImage?
    private var latestImageAt: Date?
    private var frameTimer: Timer?
    private var backgroundTask: UIBackgroundTaskIdentifier = .invalid
    private let maximumFrameAge: TimeInterval = 8

    private let statusLabel = UILabel()
    private let modelLabel = UILabel()
    private let connectionLabel = UILabel()
    private let frameLabel = UILabel()
    private let routeLabel = UILabel()
    private let preview = UIImageView()
    private let addressView = UITextView()
    private let transcriptLabel = UILabel()
    private let answerView = UITextView()
    private let questionField = UITextField()
    private let serviceButton = UIButton(type: .system)
    private let askButton = UIButton(type: .system)
    private let listenButton = UIButton(type: .system)
    private let stopTurnButton = UIButton(type: .system)
    private let voiceSwitch = UISwitch()
    private let speakSwitch = UISwitch()
    private let rotationControl = UISegmentedControl(items: ["0°", "90°", "180°", "270°"])
    private let languageControl = UISegmentedControl(items: ["中文", "English"])
    private var downloadItem: UIBarButtonItem!

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "OpenGlass · iPhone"
        view.backgroundColor = .systemBackground
        buildUI()
        connectCallbacks()
        synthesizer.delegate = self
        NotificationCenter.default.addObserver(self, selector: #selector(enteredBackground),
                                               name: UIApplication.didEnterBackgroundNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(enteredForeground),
                                               name: UIApplication.willEnterForegroundNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(audioInterrupted),
                                               name: AVAudioSession.interruptionNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(updateAudioRoute),
                                               name: AVAudioSession.routeChangeNotification, object: nil)
        statusLabel.text = "准备就绪。保持 App 在前台，点击启动。"
        modelLabel.text = "固定模型：MiniCPM-V 4.6 · 本机推理"
        connectionLabel.text = "Rokid 尚未连接"
        answerView.text = "回答会显示在这里。"
        frameTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshFrameAge()
                self?.refreshAddresses()
                self?.reconcileAutomaticLoop()
            }
        }
        refreshAddresses()
        refreshFrameAge()
        updateAudioRoute()
        updateControls()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        refreshAddresses()
        if !attemptedAutomaticStart {
            attemptedAutomaticStart = true
            let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            let hasModelFiles = OpenGlassModelSession.modelFiles.allSatisfy {
                FileManager.default.fileExists(atPath: documents.appendingPathComponent($0).path)
            }
            if hasModelFiles {
                wantsService = true
                if voiceSwitch.isOn { voiceLoop.requestListening() }
                reconcileAutomaticLoop()
            } else {
                statusLabel.text = "首次使用请在“模型下载”中准备 MiniCPM-V 4.6，然后启动接收。"
            }
        }
        UIApplication.shared.isIdleTimerDisabled = isRunning
    }

    deinit {
        frameTimer?.invalidate()
        listeningWakeTask?.cancel()
        NotificationCenter.default.removeObserver(self)
    }

    private func buildUI() {
        downloadItem = UIBarButtonItem(title: "模型下载", style: .plain, target: self, action: #selector(showDownloads))
        navigationItem.rightBarButtonItem = downloadItem
        let scroll = UIScrollView()
        scroll.keyboardDismissMode = .interactive
        scroll.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scroll)
        let stack = UIStackView()
        stack.axis = .vertical
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        scroll.addSubview(stack)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: view.keyboardLayoutGuide.topAnchor),
            stack.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor, constant: -18),
            stack.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor, constant: 16),
            stack.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor, constant: -24),
            stack.widthAnchor.constraint(equalTo: scroll.frameLayoutGuide.widthAnchor, constant: -36)
        ])
        for label in [statusLabel, modelLabel, connectionLabel, frameLabel, routeLabel, transcriptLabel] {
            label.numberOfLines = 0
            label.font = .preferredFont(forTextStyle: .subheadline)
            label.adjustsFontForContentSizeCategory = true
        }
        statusLabel.font = .preferredFont(forTextStyle: .headline)
        modelLabel.textColor = .secondaryLabel
        frameLabel.textColor = .secondaryLabel
        routeLabel.textColor = .secondaryLabel
        stack.addArrangedSubview(statusLabel)
        stack.addArrangedSubview(modelLabel)
        configureButton(serviceButton, title: "启动接收", action: #selector(toggleService), filled: true)
        stack.addArrangedSubview(serviceButton)
        addressView.isEditable = false
        addressView.isScrollEnabled = false
        addressView.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        addressView.backgroundColor = .secondarySystemBackground
        addressView.layer.cornerRadius = 8
        addressView.accessibilityLabel = "iPhone 接收地址"
        stack.addArrangedSubview(addressView)
        let addressRow = UIStackView()
        addressRow.axis = .horizontal
        addressRow.spacing = 12
        let copy = UIButton(type: .system)
        configureButton(copy, title: "复制地址", action: #selector(copyAddress))
        let refresh = UIButton(type: .system)
        configureButton(refresh, title: "刷新地址", action: #selector(refreshAddresses))
        addressRow.addArrangedSubview(copy)
        addressRow.addArrangedSubview(refresh)
        stack.addArrangedSubview(addressRow)
        stack.addArrangedSubview(connectionLabel)
        preview.contentMode = .scaleAspectFit
        preview.backgroundColor = .secondarySystemBackground
        preview.layer.cornerRadius = 12
        preview.clipsToBounds = true
        preview.accessibilityLabel = "最新 Rokid 画面"
        preview.heightAnchor.constraint(equalToConstant: 220).isActive = true
        stack.addArrangedSubview(preview)
        stack.addArrangedSubview(frameLabel)
        let savedRotation = UserDefaults.standard.object(forKey: "OpenGlass.imageRotation") as? Int ?? 270
        rotationControl.selectedSegmentIndex = [0, 90, 180, 270].firstIndex(of: savedRotation) ?? 3
        rotationControl.addTarget(self, action: #selector(rotationChanged), for: .valueChanged)
        rotationControl.accessibilityLabel = "画面顺时针旋转"
        stack.addArrangedSubview(row("画面旋转", rotationControl))
        voiceSwitch.isOn = UserDefaults.standard.object(forKey: "OpenGlass.voiceEnabled") as? Bool ?? true
        voiceSwitch.addTarget(self, action: #selector(voiceChanged), for: .valueChanged)
        stack.addArrangedSubview(row("Rokid 离线语音提问", voiceSwitch))
        languageControl.selectedSegmentIndex = UserDefaults.standard.integer(forKey: "OpenGlass.speechLanguage")
        if !(0...1).contains(languageControl.selectedSegmentIndex) { languageControl.selectedSegmentIndex = 0 }
        languageControl.addTarget(self, action: #selector(languageChanged), for: .valueChanged)
        stack.addArrangedSubview(row("识别 / 播报语言", languageControl))
        speakSwitch.isOn = UserDefaults.standard.object(forKey: "OpenGlass.speakAnswers") as? Bool ?? true
        speakSwitch.addTarget(self, action: #selector(speakChanged), for: .valueChanged)
        stack.addArrangedSubview(row("朗读回答", speakSwitch))
        stack.addArrangedSubview(routeLabel)
        configureButton(listenButton, title: "恢复自动语音", action: #selector(toggleListening))
        stack.addArrangedSubview(listenButton)
        transcriptLabel.text = "启动后自动听取 Rokid 语音。停顿约1.3秒后提问，朗读结束会自动听下一句。"
        stack.addArrangedSubview(transcriptLabel)
        questionField.borderStyle = .roundedRect
        questionField.placeholder = "输入问题，例如：我面前有什么？"
        questionField.returnKeyType = .send
        questionField.delegate = self
        questionField.accessibilityLabel = "文字问题"
        questionField.heightAnchor.constraint(greaterThanOrEqualToConstant: 44).isActive = true
        stack.addArrangedSubview(questionField)
        configureButton(askButton, title: "用最新画面提问", action: #selector(askManually), filled: true)
        configureButton(stopTurnButton, title: "停止本轮 / 暂停语音", action: #selector(stopCurrentTurn))
        stack.addArrangedSubview(askButton)
        stack.addArrangedSubview(stopTurnButton)
        answerView.isEditable = false
        answerView.isScrollEnabled = false
        answerView.font = .preferredFont(forTextStyle: .body)
        answerView.backgroundColor = .secondarySystemBackground
        answerView.layer.cornerRadius = 8
        answerView.heightAnchor.constraint(greaterThanOrEqualToConstant: 130).isActive = true
        answerView.accessibilityLabel = "模型回答"
        stack.addArrangedSubview(answerView)
        let note = UILabel()
        note.numberOfLines = 0
        note.font = .preferredFont(forTextStyle: .footnote)
        note.textColor = .secondaryLabel
        note.text = "Rokid 可通过同一局域网或 Windows 双 USB 中继连接。首次使用请先下载模型。此 App 的模型文件与原 MiniCPM 示例分开保存。进入后台会停止接收和语音；回到前台会恢复此前运行状态。主动停止后不会自动重启。"
        stack.addArrangedSubview(note)
    }

    private func row(_ title: String, _ control: UIView) -> UIStackView {
        let label = UILabel()
        label.text = title
        label.font = .preferredFont(forTextStyle: .subheadline)
        label.numberOfLines = 0
        let stack = UIStackView(arrangedSubviews: [label, control])
        stack.axis = .horizontal
        stack.spacing = 12
        stack.alignment = .center
        control.setContentHuggingPriority(.required, for: .horizontal)
        return stack
    }

    private func configureButton(_ button: UIButton, title: String, action: Selector, filled: Bool = false) {
        var configuration = filled ? UIButton.Configuration.filled() : UIButton.Configuration.tinted()
        configuration.title = title
        configuration.cornerStyle = .medium
        button.configuration = configuration
        button.heightAnchor.constraint(greaterThanOrEqualToConstant: 44).isActive = true
        button.addTarget(self, action: action, for: .touchUpInside)
    }

    private func connectCallbacks() {
        server.onImage = { [weak self] data in
            guard let self, self.isRunning, let decoded = UIImage(data: data) else { return }
            self.latestRawImage = data
            self.latestImage = self.normalizedImage(decoded)
            self.latestImageAt = Date()
            self.preview.image = self.latestImage
            self.refreshFrameAge()
            self.reconcileAutomaticLoop()
        }
        server.onAudio = { [weak self] data in
            guard let self else { return }
            self.reconcileAutomaticLoop()
            guard self.isRunning, !self.isInBackground, !self.audioInterruptionActive,
                  !self.isAnswering, !self.model.isBusy, self.activeUtterance == nil,
                  self.voiceLoop.canListen(at: self.monotonicNow) else { return }
            self.speech.appendPCM16LE(data)
        }
        server.onStatus = { [weak self] status in self?.connectionLabel.text = status }
        server.onFailure = { [weak self] message in
            self?.stopService(message: "接收服务失败：\(message)")
        }
        server.onAudioConnectionChanged = { [weak self] connected in
            guard let self else { return }
            self.audioConnected = connected
            if !connected {
                self.speech.stop()
                if self.isRunning { self.statusLabel.text = "Rokid 音频已断开，等待重新连接。" }
            } else {
                self.reconcileAutomaticLoop()
            }
            self.updateControls()
        }
        model.onChange = { [weak self] _, message in
            self?.modelLabel.text = message
            self?.updateControls()
        }
        model.onOutput = { [weak self] output in self?.answerView.text = output }
        speech.onPartial = { [weak self] text in self?.transcriptLabel.text = text }
        speech.onStatus = { [weak self] message in
            self?.statusLabel.text = message
            self?.updateControls()
        }
        speech.onError = { [weak self] failure in
            self?.handleSpeechFailure(failure)
        }
        speech.onFinal = { [weak self] text in
            guard let self, self.isRunning, !self.isInBackground,
                  self.voiceLoop.wantsListening, self.voiceLoop.blockedReason == nil else { return }
            if text.isEmpty {
                self.retryListening(message: "未识别到问题，继续等待语音。")
            } else {
                self.voiceLoop.receivedUtterance()
                self.questionField.text = text
                self.submit(text, fromSpeech: true)
            }
            self.updateControls()
        }
    }

    @objc private func toggleService() {
        if wantsService || isRunning || isStarting {
            stopService(message: "接收已停止，点击启动可恢复。")
            return
        }
        guard !model.requiresAppRestart else { return }
        wantsService = true
        if voiceSwitch.isOn { voiceLoop.requestListening() } else { voiceLoop.pause() }
        reconcileAutomaticLoop()
    }

    private func startServiceIfPossible() {
        guard wantsService, !isInBackground, !isRunning, !isStarting,
              !model.isBusy, !isAnswering, !model.requiresAppRestart else { return }
        isStarting = true
        isRunning = true
        completedTurns = 0
        runID = UUID()
        let id = runID
        latestRawImage = nil
        latestImage = nil
        latestImageAt = nil
        preview.image = nil
        do {
            try server.start(port: 18080)
        } catch {
            stopService(message: "无法启动接收：\(error.localizedDescription)")
            return
        }
        UIApplication.shared.isIdleTimerDisabled = true
        statusLabel.text = "接收已启动，正在准备模型…"
        refreshAddresses()
        refreshFrameAge()
        updateControls()
        Task {
            guard isRunning, runID == id, !isInBackground else { return }
            do {
                try await model.load()
                guard isRunning, runID == id else { updateControls(); return }
                statusLabel.text = voiceLoop.wantsListening
                    ? "等待 Rokid 画面和音频；也可以文字提问。"
                    : "接收已启动；自动语音保持关闭或暂停，仍可文字提问。"
                if voiceSwitch.isOn, voiceLoop.wantsListening, voiceLoop.blockedReason == nil {
                    let authorizationID = UUID()
                    speechAuthorizationID = authorizationID
                    isAuthorizingSpeech = true
                    let authorized = await OpenGlassSpeechSession.authorize()
                    guard isRunning, runID == id else { updateControls(); return }
                    if speechAuthorizationID == authorizationID {
                        isAuthorizingSpeech = false
                        if !authorized, voiceLoop.wantsListening {
                            handleSpeechFailure(.permissionDenied)
                        }
                    }
                }
                isStarting = false
                reconcileAutomaticLoop()
            } catch {
                if runID == id { stopService(message: error.localizedDescription) }
            }
            updateControls()
            endBackgroundTaskIfIdle()
        }
    }

    private func stopService(message: String, preservingIntent: Bool = false) {
        if !preservingIntent {
            wantsService = false
            voiceLoop.pause()
        }
        isRunning = false
        isStarting = false
        isAuthorizingSpeech = false
        speechAuthorizationID = UUID()
        cancelListeningWake()
        suppressAnswerSpeech = true
        runID = UUID()
        turnID = UUID()
        speech.stop()
        stopSpeaking()
        server.stop()
        audioConnected = false
        model.requestStop()
        UIApplication.shared.isIdleTimerDisabled = false
        statusLabel.text = model.isBusy ? "已停止接收和语音；正在结束本轮推理…" : message
        updateControls()
    }

    @objc private func stopCurrentTurn() {
        turnID = UUID()
        voiceLoop.pause()
        cancelListeningWake()
        isAuthorizingSpeech = false
        speechAuthorizationID = UUID()
        suppressAnswerSpeech = true
        speech.stop()
        stopSpeaking()
        model.requestStop()
        statusLabel.text = model.isBusy ? "正在结束本轮推理…" : "语音已暂停，点击“恢复自动语音”可恢复。"
        updateControls()
    }

    @objc private func askManually() {
        questionField.resignFirstResponder()
        submit(questionField.text ?? "")
    }

    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        askManually()
        return true
    }

    private func submit(_ text: String, fromSpeech: Bool = false) {
        let question = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty else { statusLabel.text = "请先输入或说出问题。"; return }
        guard isRunning, !isInBackground, !isStarting, model.isLoaded, !model.isBusy, !isAnswering else {
            statusLabel.text = "请先启动接收，并等待当前模型任务结束。"
            return
        }
        guard question.count <= 500 else {
            if fromSpeech { retryListening(message: "问题超过500字，请简短地重新说一次。") }
            else { statusLabel.text = "问题请控制在500字以内。" }
            return
        }
        guard let image = latestImage, let receivedAt = latestImageAt,
              Date().timeIntervalSince(receivedAt) <= maximumFrameAge else {
            speech.stop()
            statusLabel.text = "本句未提交：没有8秒内的新画面。收到新画面后会自动继续听，请重新说出问题。"
            updateControls()
            return
        }
        speech.stop()
        stopSpeaking()
        isAnswering = true
        suppressAnswerSpeech = false
        answerView.text = ""
        statusLabel.text = "已暂停收音，正在本机理解画面…"
        let id = runID
        turnID = UUID()
        let currentTurnID = turnID
        updateControls()
        Task {
            guard isRunning, runID == id, turnID == currentTurnID, !isInBackground else {
                isAnswering = false
                updateControls()
                reconcileAutomaticLoop()
                return
            }
            var imageURL: URL?
            do {
                let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
                    .appendingPathComponent("OpenGlassFrames", isDirectory: true)
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                let file = folder.appendingPathComponent(UUID().uuidString + ".jpg")
                guard let jpeg = image.jpegData(compressionQuality: 0.85) else {
                    throw OpenGlassModelSession.SessionError.invalidFile("当前画面")
                }
                try jpeg.write(to: file, options: .atomic)
                imageURL = file
                let answer = try await model.ask(question: question, imageURL: file, answerLanguage: localeIdentifier)
                isAnswering = false
                if isRunning, runID == id, turnID == currentTurnID {
                    completedTurns += 1
                    answerView.text = answer.isEmpty ? "模型没有返回可朗读的文字，请换个问题重试。" : answer
                    if !suppressAnswerSpeech, speakSwitch.isOn, !answer.isEmpty {
                        speak(answer)
                    } else {
                        statusLabel.text = "回答完成。"
                        resumeListeningAfterEchoDelay()
                    }
                }
            } catch {
                isAnswering = false
                if isRunning, runID == id {
                    if model.requiresAppRestart || !model.isLoaded {
                        stopService(message: error.localizedDescription)
                    } else if turnID == currentTurnID, voiceLoop.wantsListening {
                        retryListening(message: error.localizedDescription)
                    }
                }
            }
            // A timed-out native prefill may still read its file and context.
            if !model.requiresAppRestart, let imageURL { try? FileManager.default.removeItem(at: imageURL) }
            updateControls()
            endBackgroundTaskIfIdle()
            reconcileAutomaticLoop()
        }
    }

    private var localeIdentifier: String { languageControl.selectedSegmentIndex == 1 ? "en-US" : "zh-CN" }
    private var monotonicNow: TimeInterval { ProcessInfo.processInfo.systemUptime }
    private var hasFreshFrame: Bool {
        guard latestImage != nil, let date = latestImageAt else { return false }
        return Date().timeIntervalSince(date) <= maximumFrameAge
    }

    /// All timer, frame, connection and audio callbacks go through the same
    /// admission gate. None can bypass a retry or the post-TTS echo deadline.
    private func reconcileAutomaticLoop() {
        guard !isInBackground else { return }
        if !isRunning { startServiceIfPossible() }
        startListeningIfPossible()
    }

    private func startListeningIfPossible() {
        guard isRunning, !isInBackground, !audioInterruptionActive,
              !isStarting, !isAuthorizingSpeech, !isAnswering, !model.isBusy, model.isLoaded,
              !model.requiresAppRestart, voiceSwitch.isOn, voiceLoop.wantsListening,
              voiceLoop.blockedReason == nil, audioConnected, hasFreshFrame,
              activeUtterance == nil, !speech.isActive else { return }
        guard voiceLoop.canListen(at: monotonicNow) else {
            scheduleListeningWake()
            return
        }
        do {
            try speech.start(localeIdentifier: localeIdentifier)
        } catch let failure as OpenGlassSpeechSession.Failure {
            handleSpeechFailure(failure)
        } catch {
            retryListening(message: error.localizedDescription)
        }
        updateControls()
    }

    private func handleSpeechFailure(_ failure: OpenGlassSpeechSession.Failure) {
        guard isRunning, voiceLoop.wantsListening else { return }
        speech.stop()
        if failure.requiresUserAction {
            voiceLoop.block(reason: failure.localizedDescription)
            cancelListeningWake()
            statusLabel.text = failure.localizedDescription
            updateControls()
        } else {
            retryListening(message: failure.localizedDescription)
        }
    }

    private func retryListening(message: String) {
        guard isRunning, !isInBackground, voiceLoop.wantsListening,
              voiceLoop.blockedReason == nil else { return }
        speech.stop()
        let delay = voiceLoop.retry(now: monotonicNow)
        statusLabel.text = "\(message) \(delay.formatted())秒后自动重试。"
        scheduleListeningWake()
        updateControls()
    }

    private func cancelListeningWake() {
        listeningWakeTask?.cancel()
        listeningWakeTask = nil
    }

    private func scheduleListeningWake() {
        guard listeningWakeTask == nil, isRunning, !isInBackground,
              voiceLoop.wantsListening, voiceLoop.blockedReason == nil else { return }
        let delay = voiceLoop.notBefore - monotonicNow
        guard delay > 0 else { return }
        let id = runID
        listeningWakeTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
            catch { return }
            guard let self, !Task.isCancelled, self.runID == id else { return }
            self.listeningWakeTask = nil
            self.reconcileAutomaticLoop()
        }
    }

    @objc private func toggleListening() {
        if speech.isListening { speech.finishUtterance(); updateControls(); return }
        if speech.isFinishing { return }
        guard isRunning, !isAuthorizingSpeech else { return }
        voiceSwitch.isOn = true
        UserDefaults.standard.set(true, forKey: "OpenGlass.voiceEnabled")
        voiceLoop.requestListening()
        isAuthorizingSpeech = true
        let id = runID
        let authorizationID = UUID()
        speechAuthorizationID = authorizationID
        updateControls()
        Task {
            let authorized = await OpenGlassSpeechSession.authorize()
            guard isRunning, runID == id, speechAuthorizationID == authorizationID else { return }
            isAuthorizingSpeech = false
            if authorized {
                if !audioConnected { statusLabel.text = "等待 Rokid 音频连接…" }
                else if !hasFreshFrame { statusLabel.text = "等待新画面，随后自动听取语音。" }
                reconcileAutomaticLoop()
            } else {
                handleSpeechFailure(.permissionDenied)
            }
            updateControls()
        }
    }

    @objc private func voiceChanged() {
        UserDefaults.standard.set(voiceSwitch.isOn, forKey: "OpenGlass.voiceEnabled")
        if voiceSwitch.isOn {
            if isRunning { toggleListening() }
        } else {
            speech.stop()
            voiceLoop.pause()
            cancelListeningWake()
            isAuthorizingSpeech = false
            speechAuthorizationID = UUID()
            statusLabel.text = "自动语音已关闭，仍可文字提问。"
        }
        updateControls()
    }

    @objc private func languageChanged() {
        UserDefaults.standard.set(languageControl.selectedSegmentIndex, forKey: "OpenGlass.speechLanguage")
        speech.stop()
        if voiceLoop.wantsListening { voiceLoop.requestListening() }
        reconcileAutomaticLoop()
    }

    @objc private func speakChanged() {
        UserDefaults.standard.set(speakSwitch.isOn, forKey: "OpenGlass.speakAnswers")
        if !speakSwitch.isOn {
            stopSpeaking()
            resumeListeningAfterEchoDelay()
        }
    }

    private func speak(_ text: String) {
        speech.stop()
        do {
            let audio = AVAudioSession.sharedInstance()
            try audio.setCategory(.playback, mode: .spokenAudio, options: [])
            try audio.setActive(true)
            let utterance = AVSpeechUtterance(string: text)
            utterance.voice = AVSpeechSynthesisVoice(language: localeIdentifier)
            utterance.rate = AVSpeechUtteranceDefaultSpeechRate
            activeUtterance = utterance
            statusLabel.text = "正在朗读；收音已暂停。"
            updateAudioRoute()
            synthesizer.speak(utterance)
        } catch {
            activeUtterance = nil
            statusLabel.text = "朗读不可用：\(error.localizedDescription)。回答已保留在屏幕。"
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            resumeListeningAfterEchoDelay()
        }
        updateControls()
    }

    private func stopSpeaking() {
        if activeUtterance != nil || synthesizer.isSpeaking {
            voiceLoop.protectFromEcho(now: monotonicNow)
        }
        // Invalidate identity before stopSpeaking can dispatch didCancel.
        activeUtterance = nil
        synthesizer.stopSpeaking(at: .immediate)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            guard let self, self.activeUtterance === utterance else { return }
            self.activeUtterance = nil
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            self.statusLabel.text = "朗读完成。"
            self.resumeListeningAfterEchoDelay()
            self.updateControls()
        }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            guard let self, self.activeUtterance === utterance else { return }
            self.activeUtterance = nil
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            self.statusLabel.text = "朗读被中断，稍后自动继续听取语音。"
            self.resumeListeningAfterEchoDelay()
            self.updateControls()
        }
    }

    private func resumeListeningAfterEchoDelay() {
        voiceLoop.protectFromEcho(now: monotonicNow)
        scheduleListeningWake()
        reconcileAutomaticLoop()
    }

    @objc private func rotationChanged() {
        UserDefaults.standard.set(rotationControl.selectedSegmentIndex * 90, forKey: "OpenGlass.imageRotation")
        if let data = latestRawImage, let image = UIImage(data: data) {
            latestImage = normalizedImage(image)
            preview.image = latestImage
        }
    }

    private func normalizedImage(_ image: UIImage) -> UIImage {
        let quarterTurns = rotationControl.selectedSegmentIndex
        let radians = CGFloat(quarterTurns) * .pi / 2
        let original = image.size
        let swapped = quarterTurns % 2 != 0
        let width = swapped ? original.height : original.width
        let height = swapped ? original.width : original.height
        let scale = min(1, 1024 / max(width, height))
        let size = CGSize(width: max(1, width * scale), height: max(1, height * scale))
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        return UIGraphicsImageRenderer(size: size, format: format).image { context in
            context.cgContext.translateBy(x: size.width / 2, y: size.height / 2)
            context.cgContext.rotate(by: radians)
            image.draw(in: CGRect(x: -original.width * scale / 2, y: -original.height * scale / 2,
                                  width: original.width * scale, height: original.height * scale))
        }
    }

    private func refreshFrameAge() {
        if let date = latestImageAt {
            let age = max(0, Date().timeIntervalSince(date))
            frameLabel.text = age <= maximumFrameAge
                ? "最新画面：\(Int(age)) 秒前 · 提问时使用这一张"
                : "画面已过期（\(Int(age))秒前），等待 Rokid 新画面。"
            frameLabel.textColor = age <= maximumFrameAge ? .secondaryLabel : .systemOrange
        } else {
            frameLabel.text = "尚未收到 Rokid 画面"
            frameLabel.textColor = .secondaryLabel
        }
    }

    private func updateControls() {
        let busy = model.isBusy || isAnswering
        serviceButton.configuration?.title = wantsService || isRunning || isStarting ? "停止接收" : "启动接收"
        serviceButton.isEnabled = wantsService || isRunning || (!busy && !model.requiresAppRestart)
        askButton.isEnabled = isRunning && !isStarting && model.isLoaded && !busy && !model.requiresAppRestart
        listenButton.configuration?.title = speech.isFinishing ? "正在结束语句…"
            : (speech.isListening ? "结束语句并提问" : "恢复自动语音")
        listenButton.isEnabled = isRunning && !isStarting && !busy && activeUtterance == nil
            && !model.requiresAppRestart && !speech.isFinishing && !isAuthorizingSpeech
        stopTurnButton.isEnabled = isRunning || busy || activeUtterance != nil
        downloadItem.isEnabled = !busy && !model.requiresAppRestart
        languageControl.isEnabled = !busy && activeUtterance == nil
        rotationControl.isEnabled = !busy
        voiceSwitch.isEnabled = !isStarting
        if isRunning, let reason = voiceLoop.blockedReason { statusLabel.text = reason }
        if model.requiresAppRestart { statusLabel.text = OpenGlassModelSession.SessionError.restartRequired.localizedDescription }
        let phase: String
        if !isRunning || isInBackground { phase = "paused" }
        else if busy { phase = "thinking" }
        else if activeUtterance != nil { phase = "speaking" }
        else if speech.isActive { phase = "listening" }
        else if !voiceLoop.wantsListening || voiceLoop.blockedReason != nil { phase = "paused" }
        else { phase = "waiting" }
        server.setConversationStatus(phase: phase, completedTurns: completedTurns)
    }

    @objc private func showDownloads() {
        guard !model.isBusy, !isAnswering, !model.requiresAppRestart else { return }
        stopService(message: "模型下载完成后，返回本页点击启动。")
        Task {
            do {
                try await model.unload()
                navigationController?.pushViewController(MBV46ModelDetailViewController(with: downloadWrapper), animated: true)
            } catch {
                statusLabel.text = error.localizedDescription
            }
            updateControls()
        }
    }

    @objc private func refreshAddresses() {
        let addresses = localIPv4Addresses()
        let endpoints = addresses.isEmpty
            ? "等待 Wi-Fi / 热点地址；USB 中继仍可使用。"
            : addresses.map { "http://\($0):18080" }.joined(separator: "\n")
        addressView.text = "无线接收：\(RokidIngressServer.serviceName)\n\(endpoints)\n让眼镜加入此 iPhone 热点或同一 Wi-Fi，选择自动连接。"
    }

    @objc private func copyAddress() {
        if let first = localIPv4Addresses().first {
            UIPasteboard.general.string = "http://\(first):18080"
            statusLabel.text = "接收地址已复制。把 Rokid 采集端目标设为这个地址。"
        }
    }

    private func localIPv4Addresses() -> [String] {
        var first: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&first) == 0, let first else { return [] }
        defer { freeifaddrs(first) }
        var addresses: [(String, String)] = []
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let current = cursor {
            let interface = current.pointee
            defer { cursor = interface.ifa_next }
            guard let address = interface.ifa_addr, address.pointee.sa_family == UInt8(AF_INET),
                  interface.ifa_flags & UInt32(IFF_UP) != 0,
                  interface.ifa_flags & UInt32(IFF_LOOPBACK) == 0 else { continue }
            let name = String(cString: interface.ifa_name)
            guard name == "en0" || name.hasPrefix("bridge") || name.hasPrefix("ap") else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            if getnameinfo(address, socklen_t(address.pointee.sa_len), &host, socklen_t(host.count),
                           nil, 0, NI_NUMERICHOST) == 0 {
                addresses.append((name, String(cString: host)))
            }
        }
        return addresses.sorted { $0.0 < $1.0 }.map { $0.1 }
    }

    @objc private func updateAudioRoute() {
        let names = AVAudioSession.sharedInstance().currentRoute.outputs.map { $0.portName }.joined(separator: "、")
        routeLabel.text = "朗读输出：\(names.isEmpty ? "iPhone 系统音频" : names)。可在系统中选择蓝牙眼镜。"
    }

    @objc private func enteredBackground() {
        isInBackground = true
        stopService(message: "已进入后台。回到前台后恢复此前运行状态。", preservingIntent: true)
        if model.isBusy, backgroundTask == .invalid {
            backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "FinishOpenGlassTurn") { [weak self] in
                Task { @MainActor in
                    guard let self else { return }
                    self.model.requestStop()
                    self.endBackgroundTask()
                }
            }
            Task {
                while model.isBusy && backgroundTask != .invalid {
                    try? await Task.sleep(nanoseconds: 200_000_000)
                }
                endBackgroundTask()
            }
        }
    }

    @objc private func enteredForeground() {
        isInBackground = false
        // Background suspension deactivated our audio session; iOS may omit
        // its matching interruption-ended notification while suspended.
        audioInterruptionActive = false
        refreshAddresses()
        if wantsService {
            statusLabel.text = model.isBusy ? "正在结束上一轮推理，随后自动恢复接收。" : "已回到前台，正在恢复接收。"
            reconcileAutomaticLoop()
        } else {
            statusLabel.text = "已回到前台，接收保持停止。"
        }
        updateControls()
    }

    @objc private func audioInterrupted(_ notification: Notification) {
        guard let rawType = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: rawType) else { return }
        switch type {
        case .began:
            audioInterruptionActive = true
            speech.stop()
            stopSpeaking()
            if isRunning { statusLabel.text = "系统音频暂时中断，恢复后继续自动语音。" }
        case .ended:
            audioInterruptionActive = false
            resumeListeningAfterEchoDelay()
        @unknown default: break
        }
        updateControls()
    }

    private func endBackgroundTaskIfIdle() {
        if !model.isBusy { endBackgroundTask() }
    }

    private func endBackgroundTask() {
        guard backgroundTask != .invalid else { return }
        UIApplication.shared.endBackgroundTask(backgroundTask)
        backgroundTask = .invalid
    }
}
