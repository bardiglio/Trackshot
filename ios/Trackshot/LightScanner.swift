import ARKit
import AVFoundation
import CoreImage
import ImageIO

/// A finished light scan, ready to be copied into each take.
struct CompletedLightScan {
    var folder: URL
    var frames: [ScanFrameInfo]
    var whiteBalanceGains: [Float]?
}

/// The exposure a frame was actually captured with, as reported by the camera.
enum FrameExposure {
    static func read(from frame: ARFrame) -> (time: Double, iso: Double)? {
        let exif = frame.exifData
        let values = exif["{Exif}"] as? [String: Any] ?? exif
        guard
            let time = (values[kCGImagePropertyExifExposureTime as String] as? NSNumber)?.doubleValue,
            let iso = (values[kCGImagePropertyExifISOSpeedRatings as String] as? [NSNumber])?.first?.doubleValue,
            time > 0, iso > 0
        else { return nil }
        return (time, iso)
    }
}

/// Photographs the room in every direction at several exposures. Blender merges
/// the photos into an HDRI, using each one's camera pose and real exposure.
///
/// The camera alternates back to normal exposure between dark brackets, so ARKit
/// keeps seeing enough to track. Use it from one serial queue.
final class LightScanner {
    static let gridColumns = 36
    static let gridRows = 18
    static let minimumFrames = 6

    private static let exposureStops: [Float] = [0, -4, -8]
    private static let stopSequence = [0, 1, 0, 2]
    /// A new exposure reaches the picture late and gradually, so each bracket
    /// waits for the picture's brightness to move and settle, within these limits.
    private static let minimumFramesPerStep = 6
    private static let maximumFramesPerStep = 30
    private static let maximumFrames = 300
    private static let photoWidth: CGFloat = 1280
    private static let minimumSeparation = cos(Float(20).degreesToRadians)
    private static let coverageRadius = cos(Float(30).degreesToRadians)

    let folder: URL
    private(set) var frames: [ScanFrameInfo] = []
    private(set) var covered = [Bool](repeating: false, count: gridColumns * gridRows)

    private let device: AVCaptureDevice?
    private let context = CIContext()
    private let cellDirections: [SIMD3<Float>]
    private var savedDirections = [[SIMD3<Float>]](repeating: [], count: exposureStops.count)
    private var baseDuration = 0.0
    private var baseISO: Float = 0
    private var framesInStep = 0
    private var step = 0
    private var previousLevel: Float = 0
    private var levelAtStepStart: Float = 0
    private var steadyFrames = 0
    private var expectsChange = false
    /// The white balance the scan was locked to, as red, green and blue gains.
    private(set) var whiteBalanceGains: [Float]?

    init(folder: URL, device: AVCaptureDevice?) {
        self.folder = folder
        self.device = device
        var directions: [SIMD3<Float>] = []
        for row in 0..<Self.gridRows {
            let elevation = (0.5 - (Float(row) + 0.5) / Float(Self.gridRows)) * .pi
            for column in 0..<Self.gridColumns {
                let azimuth = (Float(column) + 0.5) / Float(Self.gridColumns) * 2 * .pi
                directions.append(SIMD3(
                    cos(elevation) * sin(azimuth),
                    sin(elevation),
                    -cos(elevation) * cos(azimuth)
                ))
            }
        }
        cellDirections = directions
    }

    /// The share of the room's sphere that's been photographed, from 0 to 1.
    static func coverageFraction(_ covered: [Bool]) -> Double {
        var total = 0.0
        var seen = 0.0
        for row in 0..<gridRows {
            let elevation = (0.5 - (Double(row) + 0.5) / Double(gridRows)) * .pi
            for column in 0..<gridColumns {
                total += cos(elevation)
                if covered[row * gridColumns + column] { seen += cos(elevation) }
            }
        }
        return total > 0 ? seen / total : 0
    }

    /// Freezes white balance and starts bracketing from the current exposure.
    func begin() {
        guard let device else { return }
        baseDuration = device.exposureDuration.seconds
        baseISO = device.iso
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            // Without this, colours would shift between photos of the same wall.
            if device.isWhiteBalanceModeSupported(.locked) {
                let gains = device.deviceWhiteBalanceGains
                whiteBalanceGains = [gains.redGain, gains.greenGain, gains.blueGain]
                device.whiteBalanceMode = .locked
            }
            applyStops(Self.exposureStops[0], to: device)
        } catch {
            // Without control of the camera, auto exposure still gives a usable scan.
        }
    }

    /// Hands the camera back to automatic control.
    func end() {
        guard let device else { return }
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            if device.isExposureModeSupported(.continuousAutoExposure) {
                device.exposureMode = .continuousAutoExposure
            }
            if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                device.whiteBalanceMode = .continuousAutoWhiteBalance
            }
        } catch {
            // The next AR session restores automatic control anyway.
        }
    }

    /// Returns true when the frame added a photo to the scan.
    func process(_ frame: ARFrame) -> Bool {
        let level = Self.meanLevel(of: frame.capturedImage)
        defer { previousLevel = level }
        framesInStep += 1
        let isSteady = abs(level - previousLevel) <= 0.03 * max(level, previousLevel, 1)
        steadyFrames = isSteady ? steadyFrames + 1 : 0
        let hasMoved = !expectsChange
            || abs(level - levelAtStepStart) > 0.15 * max(levelAtStepStart, 1)
        let hasSettled = framesInStep >= Self.minimumFramesPerStep && steadyFrames >= 3 && hasMoved
        guard hasSettled || framesInStep >= Self.maximumFramesPerStep else { return false }

        framesInStep = 0
        steadyFrames = 0
        let stopIndex = Self.stopSequence[step]
        step = (step + 1) % Self.stopSequence.count
        defer {
            moveToNextExposure()
            levelAtStepStart = level
            expectsChange = true
        }
        return savePhotoIfNeeded(frame, stopIndex: stopIndex, level: level)
    }

    /// The picture's average brightness from 0 to 255, from a sparse sample.
    private static func meanLevel(of buffer: CVPixelBuffer) -> Float {
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddressOfPlane(buffer, 0) else { return 0 }
        let width = CVPixelBufferGetWidthOfPlane(buffer, 0)
        let height = CVPixelBufferGetHeightOfPlane(buffer, 0)
        let rowBytes = CVPixelBufferGetBytesPerRowOfPlane(buffer, 0)
        let pixels = base.assumingMemoryBound(to: UInt8.self)
        var sum = 0
        var count = 0
        for y in stride(from: 0, to: height, by: 16) {
            for x in stride(from: 0, to: width, by: 16) {
                sum += Int(pixels[y * rowBytes + x])
                count += 1
            }
        }
        return count > 0 ? Float(sum) / Float(count) : 0
    }

    private func savePhotoIfNeeded(_ frame: ARFrame, stopIndex: Int, level: Float) -> Bool {
        guard frames.count < Self.maximumFrames else { return false }
        // The dark brackets can make ARKit report too little detail for a moment.
        // Its pose is still good then, and rejecting those frames would lose
        // exactly the photos that record the bright lights.
        switch frame.camera.trackingState {
        case .normal, .limited(.insufficientFeatures): break
        default: return false
        }

        let axis = frame.camera.transform.columns.2
        let forward = -simd_normalize(SIMD3(axis.x, axis.y, axis.z))
        let isNewView = savedDirections[stopIndex].allSatisfy {
            simd_dot($0, forward) < Self.minimumSeparation
        }
        guard isNewView, let exposure = FrameExposure.read(from: frame) else { return false }

        let name = String(format: "scan_%04d.jpg", frames.count + 1)
        guard let photo = writePhoto(frame, named: name) else { return false }

        frames.append(ScanFrameInfo(
            file: name,
            width: photo.width,
            height: photo.height,
            camera: frame.camera.transform.rows,
            intrinsics: photo.intrinsics,
            exposureTime: exposure.time,
            iso: exposure.iso,
            level: level
        ))
        savedDirections[stopIndex].append(forward)
        if stopIndex == 0 {
            for (index, direction) in cellDirections.enumerated()
            where simd_dot(direction, forward) > Self.coverageRadius {
                covered[index] = true
            }
        }
        return true
    }

    private func writePhoto(
        _ frame: ARFrame,
        named name: String
    ) -> (width: Int, height: Int, intrinsics: Intrinsics)? {
        let image = CIImage(cvPixelBuffer: frame.capturedImage)
        let sourceWidth = image.extent.width
        let sourceHeight = image.extent.height
        let width = min(Self.photoWidth, sourceWidth)
        let height = (sourceHeight * width / sourceWidth).rounded()
        let scaleX = width / sourceWidth
        let scaleY = height / sourceHeight
        let scaled = image.transformed(by: CGAffineTransform(scaleX: scaleX, y: scaleY))

        guard
            let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
            let data = context.jpegRepresentation(
                of: scaled,
                colorSpace: colorSpace,
                options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.92]
            )
        else { return nil }
        do {
            try data.write(to: folder.appendingPathComponent(name))
        } catch {
            return nil
        }

        var intrinsics = Intrinsics(frame.camera.intrinsics)
        intrinsics.fx *= Float(scaleX)
        intrinsics.cx *= Float(scaleX)
        intrinsics.fy *= Float(scaleY)
        intrinsics.cy *= Float(scaleY)
        return (Int(width), Int(height), intrinsics)
    }

    private func moveToNextExposure() {
        guard let device else { return }
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            applyStops(Self.exposureStops[Self.stopSequence[step]], to: device)
        } catch {
            // This bracket just gets skipped. The frame's real exposure is recorded.
        }
    }

    /// Darkens by shortening the shutter first, then lowering ISO. On a real
    /// iPhone, lowering ISO alone left the picture as bright as before.
    private func applyStops(_ stops: Float, to device: AVCaptureDevice) {
        guard device.isExposureModeSupported(.custom), baseDuration > 0, baseISO > 0 else { return }
        let format = device.activeFormat
        let shortest = format.minExposureDuration.seconds
        let longest = format.maxExposureDuration.seconds
        let wanted = baseDuration * Double(pow(2, stops))
        let duration = max(shortest, min(longest, wanted))
        // Whatever the shutter couldn't cover is left to ISO.
        let remaining = Float(wanted / duration)
        let iso = max(format.minISO, min(format.maxISO, baseISO * remaining))
        device.setExposureModeCustom(
            duration: CMTime(seconds: duration, preferredTimescale: 1_000_000),
            iso: iso,
            completionHandler: nil
        )
    }
}

private extension Float {
    var degreesToRadians: Float { self * .pi / 180 }
}
