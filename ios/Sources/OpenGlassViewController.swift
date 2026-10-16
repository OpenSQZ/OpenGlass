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
    private var speechPaused = false
    private var suppressAnswerSpeech = false
    private var runID = UUID()
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
            Task { @MainActor in self?.refreshFrameAge() }
        }
        refreshAddresses()
        refreshFrameAge()
        updateAudioRoute()
        updateControls()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        refreshAddresses()
        UIApplication.shared.isIdleTimerDisabled = isRunning
    }

    deinit {
        frameTimer?.invalidate()
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
        configureButton(listenButton, title: "开始语音", action: #selector(toggleListening))
        stack.addArrangedSubview(listenButton)
        transcriptLabel.text = "识别文字会显示在这里。停顿约1.3秒，或点“结束语句”提问。"
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
        note.text = "Rokid 可通过同一局域网或 Windows 双 USB 中继连接。首次使用请先下载模型。此 App 的模型文件与原 MiniCPM 示例分开保存。进入后台会停止接收和语音，回到前台后请重新启动。"
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
        }
        server.onAudio = { [weak self] data in
            guard let self, self.isRunning, !self.isAnswering, self.activeUtterance == nil,
                  !self.speechPaused else { return }
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
                self.startListeningIfPossible()
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
        speech.onError = { [weak self] message in
            guard let self else { return }
            self.speechPaused = true
            self.statusLabel.text = message
            self.updateControls()
        }
        speech.onFinal = { [weak self] text in
            guard let self, self.isRunning, !self.speechPaused else { return }
            if text.isEmpty {
                self.statusLabel.text = "未识别到问题，继续等待语音。"
                self.resumeListeningAfterEchoDelay()
            } else {
                self.questionField.text = text
                self.submit(text)
            }
            self.updateControls()
        }
    }

    @objc private func toggleService() {
        if isRunning || isStarting { stopService(message: "接收已停止，点击启动可恢复。"); return }
        guard !model.isBusy, !model.requiresAppRestart else { return }
        isStarting = true
        isRunning = true
        speechPaused = false
        runID = UUID()
        let id = runID
        latestRawImage = nil
        latestImage = nil
        latestImageAt = nil
        preview.image = nil
        do {
            try server.start(port: 18080)
        } catch {
            isRunning = false
            isStarting = false
            statusLabel.text = "无法启动接收：\(error.localizedDescription)"
            updateControls()
            return
        }
        UIApplication.shared.isIdleTimerDisabled = true
        statusLabel.text = "接收已启动，正在准备模型…"
        refreshAddresses()
        refreshFrameAge()
        updateControls()
        Task {
            do {
                try await model.load()
                guard isRunning, runID == id else { updateControls(); return }
                statusLabel.text = "等待 Rokid 画面和音频；也可以文字提问。"
                if voiceSwitch.isOn {
                    let authorized = await OpenGlassSpeechSession.authorize()
                    guard isRunning, runID == id else { updateControls(); return }
                    if !authorized {
                        speechPaused = true
                        statusLabel.text = "未获语音识别权限。可用文字提问，或在系统设置中允许。"
                    }
                }
                isStarting = false
                startListeningIfPossible()
            } catch {
                if runID == id { stopService(message: error.localizedDescription) }
            }
            isStarting = false
            updateControls()
        }
    }

    private func stopService(message: String) {
        isRunning = false
        isStarting = false
        isAuthorizingSpeech = false
        speechPaused = true
        suppressAnswerSpeech = true
        runID = UUID()
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
        speechPaused = true
        suppressAnswerSpeech = true
        speech.stop()
        stopSpeaking()
        model.requestStop()
        statusLabel.text = model.isBusy ? "正在结束本轮推理…" : "语音已暂停，点击“开始语音”可恢复。"
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

    private func submit(_ text: String) {
        let question = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty else { statusLabel.text = "请先输入或说出问题。"; return }
        guard isRunning, !isStarting, model.isLoaded, !model.isBusy, !isAnswering else {
            statusLabel.text = "请先启动接收，并等待当前模型任务结束。"
            return
        }
        guard question.count <= 500 else { statusLabel.text = "问题请控制在500字以内。"; return }
        guard let image = latestImage, let receivedAt = latestImageAt,
              Date().timeIntervalSince(receivedAt) <= maximumFrameAge else {
            speech.stop()
            speechPaused = true
            statusLabel.text = "没有8秒内的新画面。请恢复 Rokid 图像连接后，再次提问。"
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
        updateControls()
        Task {
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
                if isRunning, runID == id {
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
                    statusLabel.text = error.localizedDescription
                    // Errors are explicit; do not restart recognition in a loop.
                    speechPaused = true
                    if model.requiresAppRestart || !model.isLoaded {
                        stopService(message: error.localizedDescription)
                    }
                }
            }
            // A timed-out native prefill may still read its file and context.
            if !model.requiresAppRestart, let imageURL { try? FileManager.default.removeItem(at: imageURL) }
            updateControls()
            endBackgroundTaskIfIdle()
        }
    }

    private var localeIdentifier: String { languageControl.selectedSegmentIndex == 1 ? "en-US" : "zh-CN" }

    private func startListeningIfPossible() {
        guard isRunning, !isStarting, !isAuthorizingSpeech, !isAnswering, !model.isBusy, model.isLoaded,
              !model.requiresAppRestart, voiceSwitch.isOn, !speechPaused,
              audioConnected, activeUtterance == nil, !speech.isActive else { return }
        do {
            try speech.start(localeIdentifier: localeIdentifier)
        } catch {
            speechPaused = true
            statusLabel.text = error.localizedDescription
        }
        updateControls()
    }

    @objc private func toggleListening() {
        if speech.isListening { speech.finishUtterance(); updateControls(); return }
        if speech.isFinishing { return }
        guard isRunning, !model.isBusy, !isAnswering, !isAuthorizingSpeech else { return }
        voiceSwitch.isOn = true
        UserDefaults.standard.set(true, forKey: "OpenGlass.voiceEnabled")
        speechPaused = false
        isAuthorizingSpeech = true
        let id = runID
        Task {
            let authorized = await OpenGlassSpeechSession.authorize()
            guard isRunning, runID == id else { return }
            isAuthorizingSpeech = false
            if authorized {
                if !audioConnected { statusLabel.text = "等待 Rokid 音频连接…" }
                startListeningIfPossible()
            } else {
                speechPaused = true
                statusLabel.text = "未获语音识别权限，请在系统设置中允许；文字提问仍可使用。"
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
            speechPaused = true
        }
        updateControls()
    }

    @objc private func languageChanged() {
        UserDefaults.standard.set(languageControl.selectedSegmentIndex, forKey: "OpenGlass.speechLanguage")
        speech.stop()
        startListeningIfPossible()
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
            speechPaused = true
            statusLabel.text = "朗读不可用：\(error.localizedDescription)。回答已保留在屏幕。"
        }
        updateControls()
    }

    private func stopSpeaking() {
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
            self.speechPaused = true
            self.statusLabel.text = "朗读被中断，点击“开始语音”可继续。"
            self.updateControls()
        }
    }

    private func resumeListeningAfterEchoDelay() {
        let id = runID
        Task {
            try? await Task.sleep(nanoseconds: 700_000_000)
            guard isRunning, runID == id else { return }
            startListeningIfPossible()
        }
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
        serviceButton.configuration?.title = isRunning || isStarting ? "停止接收" : "启动接收"
        serviceButton.isEnabled = isRunning || (!busy && !model.requiresAppRestart)
        askButton.isEnabled = isRunning && !isStarting && model.isLoaded && !busy && !model.requiresAppRestart
        listenButton.configuration?.title = speech.isFinishing ? "正在结束语句…" : (speech.isListening ? "结束语句并提问" : "开始语音")
        listenButton.isEnabled = isRunning && !isStarting && !busy && activeUtterance == nil
            && !model.requiresAppRestart && !speech.isFinishing && !isAuthorizingSpeech
        stopTurnButton.isEnabled = isRunning || busy || activeUtterance != nil
        downloadItem.isEnabled = !busy && !model.requiresAppRestart
        languageControl.isEnabled = !busy && activeUtterance == nil
        rotationControl.isEnabled = !busy
        voiceSwitch.isEnabled = !isStarting
        if model.requiresAppRestart { statusLabel.text = OpenGlassModelSession.SessionError.restartRequired.localizedDescription }
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
        addressView.text = addresses.isEmpty ? "暂无局域网 IPv4 地址。可使用 Windows 双 USB 中继，或连接 Wi-Fi 后刷新。"
            : addresses.map { "http://\($0):18080" }.joined(separator: "\n")
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
            guard let address = interface.ifa_addr, address.pointee.sa_family == UInt8(AF_INET) else { continue }
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
        stopService(message: "已进入后台并停止。回到前台后点击启动。")
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
        refreshAddresses()
        statusLabel.text = model.isBusy ? "正在结束上一轮推理，请稍候。" : "已回到前台，点击“启动接收”恢复。"
        updateControls()
    }

    @objc private func audioInterrupted(_ notification: Notification) {
        guard let type = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              type == AVAudioSession.InterruptionType.began.rawValue else { return }
        speechPaused = true
        speech.stop()
        stopSpeaking()
        statusLabel.text = "系统音频被打断；恢复后点击“开始语音”，或使用文字提问。"
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
