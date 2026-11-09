import Foundation

/// Sends a capture to the Trackshot receiver running on the PC.
enum CaptureUploader {
    enum UploadError: LocalizedError {
        case badAddress
        case rejected(String)

        var errorDescription: String? {
            switch self {
            case .badAddress: return "Check the PC address in Settings, for example 192.168.1.20:8765."
            case .rejected(let reason): return reason
            }
        }
    }

    private struct Reply: Decodable {
        var blend: String?
        var error: String?
    }

    /// Returns the path of the .blend file on the PC.
    static func send(_ file: URL, address: String, code: String) async throws -> String {
        guard let url = URL(string: "http://\(address)/upload"), url.host != nil else {
            throw UploadError.badAddress
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(code, forHTTPHeaderField: "X-Trackshot-Code")
        request.setValue("application/zip", forHTTPHeaderField: "Content-Type")

        // The PC only replies after it has built the .blend, which can take minutes.
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 900
        configuration.timeoutIntervalForResource = 3600
        let session = URLSession(configuration: configuration)
        defer { session.finishTasksAndInvalidate() }

        let (data, response) = try await session.upload(for: request, fromFile: file)
        let reply = try? JSONDecoder().decode(Reply.self, from: data)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200, let blend = reply?.blend else {
            throw UploadError.rejected(reply?.error ?? "The PC replied with error \(status).")
        }
        return blend
    }
}
