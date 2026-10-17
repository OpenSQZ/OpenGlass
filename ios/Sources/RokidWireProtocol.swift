import Foundation
import CryptoKit

enum RokidWireError: Error, LocalizedError {
    case invalid(String)
    var errorDescription: String? {
        if case .invalid(let message) = self { return message }
        return nil
    }
}

/// Only the two existing Rokid upload routes and a health probe are supported.
/// Parsing is incremental: TCP packet boundaries are not HTTP/WS message boundaries.
struct RokidHTTPHead: Equatable {
    static let maximumHeaderBytes = 16 * 1024
    static let maximumImageBytes = 4 * 1024 * 1024
    let method: String
    let path: String
    let headers: [String: String]
    let bytesConsumed: Int

    var contentLength: Int? {
        guard let value = headers["content-length"], !value.isEmpty,
              value.allSatisfy({ $0 >= "0" && $0 <= "9" }) else { return nil }
        return Int(value)
    }

    static func parse(_ data: Data) throws -> RokidHTTPHead? {
        guard let boundary = data.range(of: Data("\r\n\r\n".utf8)) else {
            if data.count > maximumHeaderBytes { throw RokidWireError.invalid("HTTP header too large") }
            return nil
        }
        let count = data.distance(from: data.startIndex, to: boundary.upperBound)
        guard count <= maximumHeaderBytes,
              let text = String(data: data.prefix(count - 4), encoding: .ascii) else {
            throw RokidWireError.invalid("Invalid HTTP header")
        }
        let lines = text.components(separatedBy: "\r\n")
        let request = lines[0].split(separator: " ", omittingEmptySubsequences: false)
        guard request.count == 3, request[2] == "HTTP/1.1" || request[2] == "HTTP/1.0" else {
            throw RokidWireError.invalid("Invalid request line")
        }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":"), colon != line.startIndex,
                  line.first != " ", line.first != "\t" else {
                throw RokidWireError.invalid("Invalid HTTP field")
            }
            let name = line[..<colon].lowercased()
            guard headers[name] == nil else { throw RokidWireError.invalid("Duplicate HTTP field") }
            headers[name] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        guard headers["transfer-encoding"] == nil else {
            throw RokidWireError.invalid("Chunked requests are not supported")
        }
        return RokidHTTPHead(method: String(request[0]), path: String(request[1]),
                             headers: headers, bytesConsumed: count)
    }

    func webSocketAccept() throws -> String {
        let connection = (headers["connection"] ?? "").lowercased()
            .split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        guard method == "GET", path == "/rokid/audio",
              headers["upgrade"]?.lowercased() == "websocket", connection.contains("upgrade"),
              headers["sec-websocket-version"] == "13",
              let key = headers["sec-websocket-key"], Data(base64Encoded: key)?.count == 16,
              (contentLength ?? 0) == 0 else {
            throw RokidWireError.invalid("Invalid WebSocket upgrade")
        }
        let digest = Insecure.SHA1.hash(data: Data((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").utf8))
        return Data(digest).base64EncodedString()
    }
}

enum RokidWebSocketEvent {
    case binary(Data)
    case ping(Data)
    case close(Data)
}

struct RokidWebSocketDecoder {
    static let maximumMessageBytes = 128 * 1024
    private var buffer = Data()
    private var fragmented: Data?

    mutating func append(_ data: Data) throws -> [RokidWebSocketEvent] {
        buffer.append(data)
        var events: [RokidWebSocketEvent] = []
        while buffer.count >= 2 {
            let bytes = [UInt8](buffer.prefix(14))
            let final = bytes[0] & 0x80 != 0
            let opcode = bytes[0] & 0x0f
            guard bytes[0] & 0x70 == 0, bytes[1] & 0x80 != 0 else {
                throw RokidWireError.invalid("Client frames must be masked; extensions are disabled")
            }
            var offset = 2
            var length = UInt64(bytes[1] & 0x7f)
            if length == 126 {
                guard buffer.count >= 4 else { break }
                length = UInt64(bytes[2]) << 8 | UInt64(bytes[3])
                guard length >= 126 else { throw RokidWireError.invalid("Noncanonical frame length") }
                offset = 4
            } else if length == 127 {
                guard buffer.count >= 10 else { break }
                guard bytes[2] & 0x80 == 0 else { throw RokidWireError.invalid("Invalid frame length") }
                length = bytes[2..<10].reduce(UInt64(0)) { ($0 << 8) | UInt64($1) }
                guard length >= 65536 else { throw RokidWireError.invalid("Noncanonical frame length") }
                offset = 10
            }
            guard length <= UInt64(Self.maximumMessageBytes) else {
                throw RokidWireError.invalid("Audio frame too large")
            }
            guard [0, 2, 8, 9, 10].contains(Int(opcode)) else {
                throw RokidWireError.invalid("Only binary PCM audio is supported")
            }
            if opcode >= 8 && (!final || length > 125) {
                throw RokidWireError.invalid("Invalid WebSocket control frame")
            }
            let total = offset + 4 + Int(length)
            guard buffer.count >= total else { break }
            let mask = Array(buffer.dropFirst(offset).prefix(4))
            let payload = Data(buffer.dropFirst(offset + 4).prefix(Int(length)).enumerated()
                .map { $0.element ^ mask[$0.offset % 4] })
            buffer = Data(buffer.dropFirst(total))
            switch opcode {
            case 2:
                guard fragmented == nil else { throw RokidWireError.invalid("Nested fragmented message") }
                if final { events.append(.binary(payload)) } else { fragmented = payload }
            case 0:
                guard var message = fragmented else { throw RokidWireError.invalid("Unexpected continuation") }
                guard message.count + payload.count <= Self.maximumMessageBytes else {
                    throw RokidWireError.invalid("Audio message too large")
                }
                message.append(payload)
                if final { fragmented = nil; events.append(.binary(message)) } else { fragmented = message }
            case 8:
                guard payload.count != 1 else { throw RokidWireError.invalid("Invalid close payload") }
                events.append(.close(payload))
                return events
            case 9: events.append(.ping(payload))
            default: break // pong
            }
        }
        guard buffer.count <= Self.maximumMessageBytes + 14 else {
            throw RokidWireError.invalid("Receive buffer exceeded")
        }
        return events
    }

    static func serverControlFrame(opcode: UInt8, payload: Data) -> Data {
        precondition(payload.count <= 125)
        var data = Data([0x80 | opcode, UInt8(payload.count)])
        data.append(payload)
        return data
    }
}
