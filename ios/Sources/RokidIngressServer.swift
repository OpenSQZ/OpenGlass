import Foundation
import Network

/// Foreground-only local receiver for OpenGlassRokidSensor 0.1.3.
/// Call start/stop on the main queue; all callbacks are delivered on that queue.
final class RokidIngressServer {
    var onImage: ((Data) -> Void)?
    var onAudio: ((Data) -> Void)?
    var onStatus: ((String) -> Void)?
    var onFailure: ((String) -> Void)?
    var onAudioConnectionChanged: ((Bool) -> Void)?

    private let queue = DispatchQueue(label: "org.opensqz.rokid.ingress")
    private var listener: NWListener?
    private var clients: [UUID: RokidIngressConnection] = [:]
    private var audioClient: UUID?
    private var imageCount = 0
    private var audioPacketCount = 0
    private var epoch = UUID()
    // Read/written only on queue. epoch belongs to the main queue and only
    // filters UI callbacks; this also prevents an old listener accepting work.
    private var servingGeneration: UUID?
    private var imageDeliveryInFlight = false
    private var pendingAudioBytes = 0
    private let maximumPendingAudioBytes = 64_000 // Two seconds at 16 kHz PCM16.

    func start(port: UInt16 = 18080) throws {
        dispatchPrecondition(condition: .onQueue(.main))
        guard port > 0 else { throw RokidWireError.invalid("Invalid listen port") }
        let parameters = NWParameters.tcp
        parameters.allowLocalEndpointReuse = true
        let newListener = try NWListener(using: parameters, on: NWEndpoint.Port(rawValue: port)!)
        stop()
        let generation = UUID()
        epoch = generation
        listener = newListener
        queue.sync { servingGeneration = generation }
        newListener.stateUpdateHandler = { [weak self, weak newListener] state in
            guard let self else { return }
            switch state {
            case .ready: self.deliver(generation) { $0.onStatus?("正在接收 Rokid，端口 \(port)") }
            case .failed(let error):
                self.deliver(generation) {
                    let message = "接收服务失败：\(error.localizedDescription)"
                    $0.onStatus?(message)
                    $0.onFailure?(message)
                }
                newListener?.cancel()
            default: break
            }
        }
        newListener.newConnectionHandler = { [weak self] connection in
            guard let self, self.servingGeneration == generation, self.clients.count < 6 else {
                connection.cancel()
                return
            }
            let client = RokidIngressConnection(connection: connection, queue: self.queue)
            self.clients[client.id] = client
            client.healthJSON = { [weak self] in
                guard let self else { return "{}" }
                return "{\"service\":\"openglass-ios\",\"protocol\":1,\"image_count\":\(self.imageCount),\"audio_packets_in\":\(self.audioPacketCount),\"audio_connected\":\(self.audioClient != nil)}"
            }
            client.onImage = { [weak self] data in
                guard let self else { return }
                self.imageCount += 1
                // A busy UI must not retain an unbounded queue of full JPEGs.
                guard !self.imageDeliveryInFlight else { return }
                self.imageDeliveryInFlight = true
                self.deliver(generation, completionOnQueue: { [weak self] in
                    guard let self, self.servingGeneration == generation else { return }
                    self.imageDeliveryInFlight = false
                }) { $0.onImage?(data) }
            }
            client.onAudioOpen = { [weak self, weak client] in
                guard let self, let client else { return }
                // A reconnect replaces the previous audio source instead of interleaving PCM.
                let previous = self.audioClient
                self.audioClient = client.id
                if let previous { self.clients[previous]?.close() }
                self.deliver(generation) { $0.onAudioConnectionChanged?(true) }
            }
            client.onAudio = { [weak self, weak client] data in
                guard let self, let client, self.audioClient == client.id else { return }
                guard self.pendingAudioBytes + data.count <= self.maximumPendingAudioBytes else {
                    // End the stream explicitly instead of silently introducing
                    // holes in a recognition request while the UI is blocked.
                    client.close()
                    self.deliver(generation) { $0.onStatus?("音频处理未及时跟上，正在等待 Rokid 重连。") }
                    return
                }
                self.audioPacketCount += 1
                self.pendingAudioBytes += data.count
                self.deliver(generation, completionOnQueue: { [weak self] in
                    guard let self, self.servingGeneration == generation else { return }
                    self.pendingAudioBytes -= data.count
                }) { $0.onAudio?(data) }
            }
            client.onClosed = { [weak self, weak client] in
                guard let self, let client else { return }
                self.clients.removeValue(forKey: client.id)
                if self.audioClient == client.id {
                    self.audioClient = nil
                    self.deliver(generation) { $0.onAudioConnectionChanged?(false) }
                }
            }
            client.start()
        }
        newListener.start(queue: queue)
    }

    func stop() {
        dispatchPrecondition(condition: .onQueue(.main))
        epoch = UUID()
        listener?.cancel()
        listener = nil
        queue.sync {
            servingGeneration = nil
            let closing = Array(clients.values)
            clients.removeAll()
            audioClient = nil
            imageCount = 0
            audioPacketCount = 0
            imageDeliveryInFlight = false
            pendingAudioBytes = 0
            closing.forEach { $0.close() }
        }
        onAudioConnectionChanged?(false)
    }

    private func deliver(_ generation: UUID, completionOnQueue: (() -> Void)? = nil,
                         _ callback: @escaping (RokidIngressServer) -> Void) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if self.epoch == generation { callback(self) }
            if let completionOnQueue { self.queue.async(execute: completionOnQueue) }
        }
    }
}

private final class RokidIngressConnection {
    let id = UUID()
    var onImage: ((Data) -> Void)?
    var onAudio: ((Data) -> Void)?
    var onAudioOpen: (() -> Void)?
    var onClosed: (() -> Void)?
    var healthJSON: (() -> String)?
    private let connection: NWConnection
    private let queue: DispatchQueue
    private var buffer = Data()
    private var head: RokidHTTPHead?
    private var decoder = RokidWebSocketDecoder()
    private var websocket = false
    private var closed = false
    private var responding = false
    private var deadline: DispatchSourceTimer?
    private var lastInput = Date()
    private let created = Date()

    init(connection: NWConnection, queue: DispatchQueue) {
        self.connection = connection
        self.queue = queue
    }

    func start() {
        connection.stateUpdateHandler = { [weak self] state in
            switch state {
            case .failed, .cancelled: self?.close()
            default: break
            }
        }
        connection.start(queue: queue)
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 5, repeating: 5)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let age = Date().timeIntervalSince(self.websocket ? self.lastInput : self.created)
            if age > (self.websocket ? 30 : 15) { self.close() }
        }
        deadline = timer
        timer.resume()
        receive()
    }

    func close() {
        guard !closed else { return }
        closed = true
        deadline?.cancel()
        deadline = nil
        connection.cancel()
        onClosed?()
    }

    private func receive() {
        guard !closed, !responding else { return }
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, done, error in
            guard let self, !self.closed else { return }
            if let data, !data.isEmpty {
                self.lastInput = Date()
                do {
                    if self.websocket { try self.consumeAudio(data) }
                    else { try self.consumeHTTP(data) }
                } catch {
                    if self.websocket { self.closeWebSocket(code: 1002) }
                    else { self.respond(status: "400 Bad Request", body: "Invalid Rokid request") }
                }
            }
            if error != nil || (done && !self.responding) { self.close() }
            else { self.receive() }
        }
    }

    private func consumeHTTP(_ data: Data) throws {
        buffer.append(data)
        guard buffer.count <= RokidHTTPHead.maximumImageBytes + RokidHTTPHead.maximumHeaderBytes else {
            throw RokidWireError.invalid("Image too large")
        }
        if head == nil {
            guard let parsed = try RokidHTTPHead.parse(buffer) else { return }
            head = parsed
            buffer = Data(buffer.dropFirst(parsed.bytesConsumed))
            if parsed.method == "GET", parsed.path == "/health" {
                respond(status: "200 OK", body: healthJSON?() ?? "{}", type: "application/json")
                return
            }
            if parsed.path == "/rokid/audio" {
                let accept = try parsed.webSocketAccept()
                websocket = true
                let reply = "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: \(accept)\r\n\r\n"
                connection.send(content: Data(reply.utf8), completion: .contentProcessed { [weak self] error in
                    if error != nil { self?.close() }
                })
                onAudioOpen?()
                let remainder = buffer
                buffer.removeAll(keepingCapacity: false)
                if !remainder.isEmpty { try consumeAudio(remainder) }
                return
            }
            guard parsed.method == "POST", parsed.path == "/rokid/image" else {
                respond(status: "404 Not Found", body: "Unknown route")
                return
            }
            guard let length = parsed.contentLength, length > 3, length <= RokidHTTPHead.maximumImageBytes,
                  parsed.headers["content-type"]?.lowercased().split(separator: ";").first == "image/jpeg" else {
                throw RokidWireError.invalid("Expected a bounded JPEG body")
            }
            if parsed.headers["expect"]?.lowercased() == "100-continue" {
                connection.send(content: Data("HTTP/1.1 100 Continue\r\n\r\n".utf8), completion: .contentProcessed { _ in })
            }
        }
        guard let count = head?.contentLength, buffer.count >= count else { return }
        let jpeg = Data(buffer.prefix(count))
        guard jpeg.starts(with: [0xff, 0xd8]), jpeg.suffix(2) == Data([0xff, 0xd9]) else {
            throw RokidWireError.invalid("Invalid JPEG markers")
        }
        onImage?(jpeg)
        respond(status: "200 OK", body: "{\"ok\":true}", type: "application/json")
    }

    private func consumeAudio(_ data: Data) throws {
        for event in try decoder.append(data) {
            switch event {
            case .binary(let pcm):
                guard pcm.count % 2 == 0 else { throw RokidWireError.invalid("PCM16 requires complete samples") }
                if !pcm.isEmpty { onAudio?(pcm) }
            case .ping(let payload):
                connection.send(content: RokidWebSocketDecoder.serverControlFrame(opcode: 10, payload: payload),
                                completion: .contentProcessed { [weak self] error in
                    if error != nil { self?.close() }
                })
            case .close(let payload):
                responding = true
                connection.send(content: RokidWebSocketDecoder.serverControlFrame(opcode: 8, payload: payload),
                                completion: .contentProcessed { [weak self] _ in self?.close() })
                return
            }
        }
    }

    private func closeWebSocket(code: UInt16) {
        responding = true
        let payload = Data([UInt8(code >> 8), UInt8(code & 0xff)])
        connection.send(content: RokidWebSocketDecoder.serverControlFrame(opcode: 8, payload: payload),
                        completion: .contentProcessed { [weak self] _ in self?.close() })
    }

    private func respond(status: String, body: String, type: String = "text/plain") {
        guard !responding else { return }
        responding = true
        let bytes = Data(body.utf8)
        var response = Data("HTTP/1.1 \(status)\r\nContent-Type: \(type)\r\nContent-Length: \(bytes.count)\r\nConnection: close\r\n\r\n".utf8)
        response.append(bytes)
        connection.send(content: response, completion: .contentProcessed { [weak self] _ in self?.close() })
    }
}
