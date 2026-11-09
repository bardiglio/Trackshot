import ARKit
import AVFoundation

/// Writes the clean camera video and the matching camera data for one take.
///
/// Each video frame is stamped by its index rather than its capture time, and a
/// frame the encoder can't accept is dropped from both the video and the data.
/// Frame N of the video therefore always matches camera pose N in Blender, even
/// if the phone stutters. Call everything on one serial queue.
final class TakeRecorder {
    enum RecorderError: LocalizedError {
        case writerFailed(String)
        case noFrames

        var errorDescription: String? {
            switch self {
            case .writerFailed(let reason): return "The video couldn't be saved: \(reason)"
            case .noFrames: return "The take has no frames."
            }
        }
    }

    static let videoFileName = "plate.mov"

    let folder: URL
    private let fps: Int
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var frames: [FrameInfo] = []
    private var width = 0
    private var height = 0
    private var intensitySum = 0.0
    private var temperatureSum = 0.0
    private var lightSamples = 0

    var frameCount: Int { frames.count }

    init(folder: URL, fps: Int) {
        self.folder = folder
        self.fps = fps
    }

    func append(_ frame: ARFrame) throws {
        let buffer = frame.capturedImage
        if writer == nil {
            try startWriter(width: CVPixelBufferGetWidth(buffer), height: CVPixelBufferGetHeight(buffer))
        }
        guard let writer, let input, let adaptor else { return }
        if writer.status == .failed {
            throw RecorderError.writerFailed(writer.error?.localizedDescription ?? "unknown error")
        }
        guard input.isReadyForMoreMediaData else { return }

        let time = CMTime(value: CMTimeValue(frames.count), timescale: CMTimeScale(fps))
        guard adaptor.append(buffer, withPresentationTime: time) else {
            throw RecorderError.writerFailed(writer.error?.localizedDescription ?? "unknown error")
        }
        frames.append(FrameInfo(
            t: frame.timestamp,
            camera: frame.camera.transform.rows,
            intrinsics: Intrinsics(frame.camera.intrinsics)
        ))
        if let estimate = frame.lightEstimate {
            intensitySum += Double(estimate.ambientIntensity)
            temperatureSum += Double(estimate.ambientColorTemperature)
            lightSamples += 1
        }
    }

    /// Closes the video and hands back everything except the scene contents.
    func finish(completion: @escaping (Result<(VideoInfo, [FrameInfo], LightInfo), Error>) -> Void) {
        guard let writer, let input, !frames.isEmpty else {
            writer?.cancelWriting()
            completion(.failure(RecorderError.noFrames))
            return
        }
        input.markAsFinished()
        let video = VideoInfo(file: Self.videoFileName, width: width, height: height, fps: fps)
        let samples = max(lightSamples, 1)
        let light = lightSamples == 0
            ? LightInfo(ambientIntensity: 1000, colorTemperature: 6500)
            : LightInfo(
                ambientIntensity: intensitySum / Double(samples),
                colorTemperature: temperatureSum / Double(samples)
            )
        let frames = self.frames
        writer.finishWriting {
            if writer.status == .completed {
                completion(.success((video, frames, light)))
            } else {
                let reason = writer.error?.localizedDescription ?? "unknown error"
                completion(.failure(RecorderError.writerFailed(reason)))
            }
        }
    }

    private func startWriter(width: Int, height: Int) throws {
        self.width = width
        self.height = height
        let url = folder.appendingPathComponent(Self.videoFileName)
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        let bitRate = Int(Double(width * height * fps) * 0.15)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: bitRate],
        ])
        input.expectsMediaDataInRealTime = true
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: input,
            sourcePixelBufferAttributes: nil
        )
        writer.add(input)
        guard writer.startWriting() else {
            throw RecorderError.writerFailed(writer.error?.localizedDescription ?? "unknown error")
        }
        writer.startSession(atSourceTime: .zero)
        self.writer = writer
        self.input = input
        self.adaptor = adaptor
    }
}
