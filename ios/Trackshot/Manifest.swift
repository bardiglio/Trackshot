import simd

/// The capture format read by pc/trackshot_blender.py. Keep the two in step, and
/// raise `format` on both sides for any breaking change.
struct Manifest: Encodable {
    var format = 1
    var app = "Trackshot"
    var video: VideoInfo
    var frames: [FrameInfo]
    var objects: [ObjectInfo]
    var planes: [PlaneInfo]
    var roomMesh: String?
    var light: LightInfo
    var lightScan: [ScanFrameInfo]?
}

struct VideoInfo: Encodable {
    var file: String
    var width: Int
    var height: Int
    var fps: Int
}

struct FrameInfo: Encodable {
    var t: Double
    var camera: [[Float]]
    var intrinsics: Intrinsics
}

struct Intrinsics: Encodable {
    var fx: Float
    var fy: Float
    var cx: Float
    var cy: Float

    init(_ matrix: simd_float3x3) {
        fx = matrix.columns.0.x
        fy = matrix.columns.1.y
        cx = matrix.columns.2.x
        cy = matrix.columns.2.y
    }
}

struct ObjectInfo: Encodable {
    var name: String
    /// "box", "sphere" or "usdz".
    var kind: String
    var size: Float?
    var file: String?
    var matrix: [[Float]]
}

struct PlaneInfo: Encodable {
    var matrix: [[Float]]
    var width: Float
    var depth: Float
}

struct LightInfo: Encodable {
    var ambientIntensity: Double
    var colorTemperature: Double
    /// The take's typical exposure time multiplied by ISO. Blender uses it to match
    /// the HDRI's brightness to the video.
    var exposure: Double?
}

/// One photo from the light scan. Its intrinsics are scaled to the photo's size.
struct ScanFrameInfo: Encodable {
    var file: String
    var width: Int
    var height: Int
    var camera: [[Float]]
    var intrinsics: Intrinsics
    var exposureTime: Double
    var iso: Double
}

extension simd_float4x4 {
    /// Row-major, unlike simd's column-major storage.
    var rows: [[Float]] {
        (0..<4).map { row in
            [columns.0[row], columns.1[row], columns.2[row], columns.3[row]]
        }
    }
}
