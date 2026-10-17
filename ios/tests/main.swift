import Foundation

var checks = 0
func check(_ condition: @autoclosure () throws -> Bool, _ label: String) {
    do { guard try condition() else { fatalError("FAIL: \(label)") } }
    catch { fatalError("FAIL: \(label): \(error)") }
    checks += 1
}
func rejects(_ label: String, _ action: () throws -> Void) {
    do { try action(); fatalError("FAIL: expected rejection: \(label)") }
    catch { checks += 1 }
}
func frame(_ payload: Data, opcode: UInt8 = 2, final: Bool = true) -> Data {
    var data = Data([(final ? 0x80 : 0) | opcode])
    if payload.count < 126 { data.append(0x80 | UInt8(payload.count)) }
    else if payload.count <= 65535 {
        data.append(contentsOf: [0xfe, UInt8(payload.count >> 8), UInt8(payload.count & 0xff)])
    } else {
        data.append(0xff)
        for shift in stride(from: 56, through: 0, by: -8) {
            data.append(UInt8((UInt64(payload.count) >> shift) & 0xff))
        }
    }
    let mask: [UInt8] = [0x37, 0xfa, 0x21, 0x3d]
    data.append(contentsOf: mask)
    data.append(contentsOf: payload.enumerated().map { $0.element ^ mask[$0.offset % 4] })
    return data
}
func binary(_ events: [RokidWebSocketEvent]) -> [Data] {
    events.compactMap { if case .binary(let data) = $0 { return data }; return nil }
}

let jpegHeader = Data("POST /rokid/image HTTP/1.1\r\nHost: phone\r\nContent-Type: image/jpeg\r\nContent-Length: 1280\r\n\r\n".utf8)
for split in 0..<jpegHeader.count {
    check(try RokidHTTPHead.parse(Data(jpegHeader.prefix(split))) == nil, "HTTP waits for header boundary")
}
let head = try RokidHTTPHead.parse(jpegHeader)!
check(head.method == "POST" && head.path == "/rokid/image" && head.contentLength == 1280, "JPEG upload headers")
check(head.bytesConsumed == jpegHeader.count, "Header boundary excludes image body")
rejects("duplicate length") {
    _ = try RokidHTTPHead.parse(Data("POST / HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 9\r\n\r\n".utf8))
}
rejects("chunked body") {
    _ = try RokidHTTPHead.parse(Data("POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n".utf8))
}
rejects("large headers") { _ = try RokidHTTPHead.parse(Data(repeating: 65, count: 17000)) }
let upgrade = try RokidHTTPHead.parse(Data("GET /rokid/audio HTTP/1.1\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n".utf8))!
check(try upgrade.webSocketAccept() == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", "RFC6455 handshake example")
let badUpgrade = try RokidHTTPHead.parse(Data("GET /rokid/audio HTTP/1.1\r\nSec-WebSocket-Key: abc\r\n\r\n".utf8))!
rejects("invalid upgrade") { _ = try badUpgrade.webSocketAccept() }

// A real Rokid packet is 640 mono samples at 16kHz: 1280 bytes per 40ms.
let pcm = Data((0..<1280).map { UInt8($0 % 256) })
let encoded = frame(pcm)
for split in 0...encoded.count {
    var decoder = RokidWebSocketDecoder()
    var events = try decoder.append(Data(encoded.prefix(split)))
    events += try decoder.append(Data(encoded.dropFirst(split)))
    check(binary(events) == [pcm], "PCM preserved across TCP split \(split)")
}
var coalesced = RokidWebSocketDecoder()
check(binary(try coalesced.append(encoded + encoded)) == [pcm, pcm], "Coalesced TCP frames")
var fragmented = RokidWebSocketDecoder()
let parts = frame(Data(pcm.prefix(333)), final: false)
    + frame(Data("ping".utf8), opcode: 9)
    + frame(Data(pcm.dropFirst(333)), opcode: 0)
let fragmentEvents = try fragmented.append(parts)
check(binary(fragmentEvents) == [pcm], "Continuation and ping preserve a fragmented message")
check(fragmentEvents.count == 2, "Ping delivered independently of fragments")
var large = RokidWebSocketDecoder()
let largePayload = Data(repeating: 128, count: 65536)
check(binary(try large.append(frame(largePayload))) == [largePayload], "64-bit length")
rejects("unmasked client") { var d = RokidWebSocketDecoder(); _ = try d.append(Data([0x82, 2, 0, 0])) }
rejects("unexpected continuation") { var d = RokidWebSocketDecoder(); _ = try d.append(frame(pcm, opcode: 0)) }
rejects("text not PCM") { var d = RokidWebSocketDecoder(); _ = try d.append(frame(Data("text".utf8), opcode: 1)) }
rejects("oversized message") { var d = RokidWebSocketDecoder(); _ = try d.append(frame(Data(repeating: 0, count: 131073))) }
rejects("fragmented control") { var d = RokidWebSocketDecoder(); _ = try d.append(frame(Data([1]), opcode: 9, final: false)) }
rejects("odd close payload") { var d = RokidWebSocketDecoder(); _ = try d.append(frame(Data([1]), opcode: 8)) }
rejects("oversized fragmented message") {
    var d = RokidWebSocketDecoder()
    _ = try d.append(frame(Data(repeating: 0, count: 100000), final: false))
    _ = try d.append(frame(Data(repeating: 0, count: 100000), opcode: 0))
}
check(RokidWebSocketDecoder.serverControlFrame(opcode: 10, payload: Data([7])) == Data([0x8a, 1, 7]), "Unmasked server pong")
print("Passed \(checks) Rokid HTTP/WebSocket checks")
