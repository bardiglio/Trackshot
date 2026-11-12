import ARKit
import Foundation
import Metal

/// Turns a finished take into the single zip file that gets sent to the PC.
enum CaptureExporter {
    /// A snapshot of one placed object, taken while the take is being stopped.
    struct PlacedSnapshot {
        var info: ObjectInfo
        var sourceFile: URL?
    }

    static var capturesFolder: URL {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return documents.appendingPathComponent("Captures", isDirectory: true)
    }

    static func newFolder(prefix: String) throws -> URL {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent(prefix + formatter.string(from: Date()), isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder
    }

    /// Writes the manifest and any models, zips the folder, then deletes it.
    static func export(
        folder: URL,
        video: VideoInfo,
        frames: [FrameInfo],
        light: LightInfo,
        objects: [PlacedSnapshot],
        anchors: [ARAnchor],
        lightScan: CompletedLightScan?
    ) throws -> URL {
        let manager = FileManager.default
        defer { try? manager.removeItem(at: folder) }

        for object in objects {
            if let source = object.sourceFile, let name = object.info.file {
                let target = folder.appendingPathComponent(name)
                if !manager.fileExists(atPath: target.path) {
                    try manager.copyItem(at: source, to: target)
                }
            }
        }

        // Copied rather than moved, because later takes reuse the same scan.
        for frame in lightScan?.frames ?? [] {
            if let scanFolder = lightScan?.folder {
                try manager.copyItem(
                    at: scanFolder.appendingPathComponent(frame.file),
                    to: folder.appendingPathComponent(frame.file)
                )
            }
        }

        let meshes = anchors.compactMap { $0 as? ARMeshAnchor }
        var roomMesh: String?
        if !meshes.isEmpty {
            roomMesh = "room.obj"
            try writeOBJ(meshes, to: folder.appendingPathComponent("room.obj"))
        }

        let manifest = Manifest(
            video: video,
            frames: frames,
            objects: objects.map(\.info),
            // A LiDAR room scan already includes the floor.
            planes: meshes.isEmpty ? horizontalPlanes(in: anchors) : [],
            roomMesh: roomMesh,
            light: light,
            lightScan: lightScan?.frames,
            lightScanWhiteBalance: lightScan?.whiteBalanceGains
        )
        let data = try JSONEncoder().encode(manifest)
        try data.write(to: folder.appendingPathComponent("manifest.json"))

        try manager.createDirectory(at: capturesFolder, withIntermediateDirectories: true)
        let zipURL = capturesFolder.appendingPathComponent(folder.lastPathComponent + ".zip")
        try zip(folder, to: zipURL)
        return zipURL
    }

    private static func horizontalPlanes(in anchors: [ARAnchor]) -> [PlaneInfo] {
        anchors.compactMap { $0 as? ARPlaneAnchor }
            .filter { $0.alignment == .horizontal }
            .map { plane in
                // The plane's centre and rotation are relative to its anchor.
                var offset = matrix_identity_float4x4
                offset.columns.3 = SIMD4(plane.center, 1)
                let rotation = simd_float4x4(
                    simd_quatf(angle: plane.planeExtent.rotationOnYAxis, axis: SIMD3(0, 1, 0))
                )
                return PlaneInfo(
                    matrix: (plane.transform * offset * rotation).rows,
                    width: plane.planeExtent.width,
                    depth: plane.planeExtent.height
                )
            }
    }

    /// Writes all the LiDAR mesh pieces into one OBJ file, in world coordinates.
    private static func writeOBJ(_ meshes: [ARMeshAnchor], to url: URL) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        let handle = try FileHandle(forWritingTo: url)
        defer { try? handle.close() }

        var vertexOffset = 1  // OBJ indices start at 1.
        for mesh in meshes {
            let geometry = mesh.geometry
            let vertices = geometry.vertices
            let faces = geometry.faces
            guard faces.bytesPerIndex == 4, faces.indexCountPerPrimitive == 3 else { continue }

            var text = ""
            let vertexStart = vertices.buffer.contents().advanced(by: vertices.offset)
            for index in 0..<vertices.count {
                let values = vertexStart.advanced(by: index * vertices.stride)
                    .assumingMemoryBound(to: Float.self)
                let world = mesh.transform * SIMD4(values[0], values[1], values[2], 1)
                text += "v \(world.x) \(world.y) \(world.z)\n"
            }
            let indices = faces.buffer.contents().assumingMemoryBound(to: UInt32.self)
            for face in 0..<faces.count {
                let a = Int(indices[face * 3]) + vertexOffset
                let b = Int(indices[face * 3 + 1]) + vertexOffset
                let c = Int(indices[face * 3 + 2]) + vertexOffset
                text += "f \(a) \(b) \(c)\n"
            }
            try handle.write(contentsOf: Data(text.utf8))
            vertexOffset += vertices.count
        }
    }

    private static func zip(_ folder: URL, to destination: URL) throws {
        // Asking to read a folder "for uploading" makes iOS produce a zip of it.
        var coordinationError: NSError?
        var copyError: Error?
        NSFileCoordinator().coordinate(
            readingItemAt: folder,
            options: [.forUploading],
            error: &coordinationError
        ) { zipped in
            do {
                try FileManager.default.copyItem(at: zipped, to: destination)
            } catch {
                copyError = error
            }
        }
        if let error = coordinationError ?? copyError { throw error }
    }
}
