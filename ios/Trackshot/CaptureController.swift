import ARKit
import Combine
import RealityKit
import UIKit

/// Runs the AR session, the objects placed in the room, and recording.
@MainActor
final class CaptureController: NSObject, ObservableObject, ARSessionDelegate {
    enum PlaceableModel: Equatable {
        case box
        case sphere
        case file(URL)

        var title: String {
            switch self {
            case .box: return "Box"
            case .sphere: return "Sphere"
            case .file(let url): return url.deletingPathExtension().lastPathComponent
            }
        }
    }

    private struct PlacedObject {
        var model: PlaceableModel
        var size: Float
        var anchor: AnchorEntity
        var entity: ModelEntity
        var gestures: [EntityGestureRecognizer]
    }

    private static let primitiveSize: Float = 0.2

    let arView = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)

    @Published private(set) var isRecording = false
    @Published private(set) var isSaving = false
    @Published private(set) var recordedFrames = 0
    @Published private(set) var trackingMessage: String? = "Starting camera…"
    @Published private(set) var placedCount = 0
    @Published private(set) var videoFormatLabel = ""
    @Published private(set) var fps = 30
    @Published private(set) var lastCapture: URL?
    @Published var selectedModel = PlaceableModel.box
    @Published var errorMessage: String?
    @Published private(set) var isScanningLight = false
    @Published private(set) var scanCoverage = [Bool](
        repeating: false,
        count: LightScanner.gridColumns * LightScanner.gridRows
    )
    @Published private(set) var scanPhotoCount = 0
    @Published private(set) var hasLightScan = false

    private var placed: [PlacedObject] = []
    private var completedScan: CompletedLightScan?

    // Frames arrive here. `recorder` is only touched on this queue.
    private nonisolated let recordQueue = DispatchQueue(label: "trackshot.recording")
    private nonisolated(unsafe) var recorder: TakeRecorder?
    private nonisolated(unsafe) var scanner: LightScanner?

    override init() {
        super.init()
        arView.session.delegate = self
        arView.session.delegateQueue = recordQueue
        arView.addGestureRecognizer(
            UITapGestureRecognizer(target: self, action: #selector(handleTap(_:)))
        )
    }

    func start() {
        guard ARWorldTrackingConfiguration.isSupported else {
            errorMessage = "This iPhone doesn't support AR tracking."
            return
        }
        let configuration = ARWorldTrackingConfiguration()
        configuration.planeDetection = [.horizontal, .vertical]
        configuration.environmentTexturing = .automatic
        configuration.isLightEstimationEnabled = true
        if let format = ARWorldTrackingConfiguration.recommendedVideoFormatFor4KResolution {
            configuration.videoFormat = format
        }
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) {
            configuration.sceneReconstruction = .mesh
            arView.environment.sceneUnderstanding.options.insert(.occlusion)
        }
        let format = configuration.videoFormat
        fps = format.framesPerSecond
        videoFormatLabel = "\(Int(format.imageResolution.width))×"
            + "\(Int(format.imageResolution.height)) \(format.framesPerSecond) fps"
        arView.session.run(configuration)
        UIApplication.shared.isIdleTimerDisabled = true
    }

    // MARK: Placing objects

    @objc private func handleTap(_ gesture: UITapGestureRecognizer) {
        guard !isRecording, !isSaving else { return }
        let point = gesture.location(in: arView)
        // Tapping an object that's already there means moving it, not adding one.
        guard arView.entity(at: point) == nil else { return }
        guard let hit = arView.raycast(from: point, allowing: .estimatedPlane, alignment: .any).first else {
            return
        }
        do {
            try place(selectedModel, at: hit.worldTransform)
        } catch {
            errorMessage = "Couldn't load that model: \(error.localizedDescription)"
        }
    }

    private func place(_ model: PlaceableModel, at transform: simd_float4x4) throws {
        let size = Self.primitiveSize
        let entity: ModelEntity
        let material = SimpleMaterial(color: .lightGray, isMetallic: false)
        switch model {
        case .box:
            entity = ModelEntity(mesh: .generateBox(size: size), materials: [material])
            entity.position.y = size / 2  // Rest on the surface rather than sink into it.
        case .sphere:
            entity = ModelEntity(mesh: .generateSphere(radius: size / 2), materials: [material])
            entity.position.y = size / 2
        case .file(let url):
            entity = try ModelEntity.loadModel(contentsOf: url)
        }
        entity.generateCollisionShapes(recursive: true)

        let anchor = AnchorEntity(world: transform)
        anchor.addChild(entity)
        arView.scene.addAnchor(anchor)
        let gestures = arView.installGestures([.translation, .rotation, .scale], for: entity)
        placed.append(PlacedObject(model: model, size: size, anchor: anchor, entity: entity, gestures: gestures))
        placedCount = placed.count
    }

    func undoLastPlacement() {
        guard !isRecording, let last = placed.popLast() else { return }
        last.gestures.forEach { arView.removeGestureRecognizer($0) }
        arView.scene.removeAnchor(last.anchor)
        placedCount = placed.count
    }

    /// Copies a chosen USDZ into the app, because access to the original is temporary.
    func importModel(from url: URL) {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        do {
            let manager = FileManager.default
            let folder = manager.urls(for: .documentDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Models", isDirectory: true)
            try manager.createDirectory(at: folder, withIntermediateDirectories: true)
            let target = folder.appendingPathComponent(url.lastPathComponent)
            if manager.fileExists(atPath: target.path) {
                try manager.removeItem(at: target)
            }
            try manager.copyItem(at: url, to: target)
            selectedModel = .file(target)
        } catch {
            errorMessage = "Couldn't import that file: \(error.localizedDescription)"
        }
    }

    // MARK: Light scan

    func toggleLightScan() {
        if isScanningLight {
            finishLightScan()
        } else if !isRecording && !isSaving {
            startLightScan()
        }
    }

    private func startLightScan() {
        do {
            let folder = try CaptureExporter.newFolder(prefix: "LightScan_")
            if let old = completedScan {
                try? FileManager.default.removeItem(at: old.folder)
            }
            completedScan = nil
            hasLightScan = false
            scanCoverage = scanCoverage.map { _ in false }
            scanPhotoCount = 0
            isScanningLight = true
            let device = ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera
            recordQueue.async {
                let scan = LightScanner(folder: folder, device: device)
                scan.begin()
                self.scanner = scan
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func finishLightScan() {
        isScanningLight = false
        recordQueue.async {
            guard let scan = self.scanner else { return }
            self.scanner = nil
            scan.end()
            let result = CompletedLightScan(folder: scan.folder, frames: scan.frames)
            Task { @MainActor in
                if result.frames.count >= LightScanner.minimumFrames {
                    self.completedScan = result
                    self.hasLightScan = true
                } else {
                    try? FileManager.default.removeItem(at: result.folder)
                    self.errorMessage = "The light scan was too short. Turn slowly and look all around the room."
                }
            }
        }
    }

    // MARK: Recording

    func toggleRecording() {
        if isRecording {
            stopRecording()
        } else if !isSaving && !isScanningLight {
            startRecording()
        }
    }

    private func startRecording() {
        do {
            let take = TakeRecorder(folder: try CaptureExporter.newFolder(prefix: "Take_"), fps: fps)
            recordQueue.async { self.recorder = take }
            // Objects stay put during a take: the .blend stores one position for each.
            placed.forEach { $0.gestures.forEach { $0.isEnabled = false } }
            recordedFrames = 0
            lastCapture = nil
            isRecording = true
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func stopRecording() {
        isRecording = false
        isSaving = true
        placed.forEach { $0.gestures.forEach { $0.isEnabled = true } }

        let anchors = arView.session.currentFrame?.anchors ?? []
        let lightScan = completedScan
        let snapshots = placed.enumerated().map { index, object in
            snapshot(of: object, index: index)
        }
        recordQueue.async {
            guard let take = self.recorder else {
                Task { @MainActor in self.isSaving = false }
                return
            }
            self.recorder = nil
            take.finish { result in
                DispatchQueue.global(qos: .userInitiated).async {
                    let exported = Result { () throws -> URL in
                        let (video, frames, light) = try result.get()
                        return try CaptureExporter.export(
                            folder: take.folder,
                            video: video,
                            frames: frames,
                            light: light,
                            objects: snapshots,
                            anchors: anchors,
                            lightScan: lightScan
                        )
                    }
                    Task { @MainActor in self.finishSaving(exported) }
                }
            }
        }
    }

    private func finishSaving(_ result: Result<URL, Error>) {
        isSaving = false
        switch result {
        case .success(let url): lastCapture = url
        case .failure(let error): errorMessage = error.localizedDescription
        }
    }

    private func snapshot(of object: PlacedObject, index: Int) -> CaptureExporter.PlacedSnapshot {
        let matrix = object.entity.transformMatrix(relativeTo: nil).rows
        let name = "\(object.model.title) \(index + 1)"
        switch object.model {
        case .box:
            return .init(info: ObjectInfo(name: name, kind: "box", size: object.size, matrix: matrix))
        case .sphere:
            return .init(info: ObjectInfo(name: name, kind: "sphere", size: object.size, matrix: matrix))
        case .file(let url):
            return .init(
                info: ObjectInfo(name: name, kind: "usdz", file: url.lastPathComponent, matrix: matrix),
                sourceFile: url
            )
        }
    }

    // MARK: ARSessionDelegate (called on recordQueue)

    nonisolated func session(_ session: ARSession, didUpdate frame: ARFrame) {
        if let scan = scanner, scan.process(frame) {
            let covered = scan.covered
            let count = scan.frames.count
            Task { @MainActor in
                self.scanCoverage = covered
                self.scanPhotoCount = count
            }
        }
        guard let take = recorder else { return }
        do {
            try take.append(frame)
            let count = take.frameCount
            Task { @MainActor in self.recordedFrames = count }
        } catch {
            let message = error.localizedDescription
            Task { @MainActor in
                self.errorMessage = message
                if self.isRecording { self.stopRecording() }
            }
        }
    }

    nonisolated func session(_ session: ARSession, cameraDidChangeTrackingState camera: ARCamera) {
        let message: String?
        switch camera.trackingState {
        case .normal: message = nil
        case .notAvailable: message = "Tracking isn't available"
        case .limited(.initializing): message = "Move the iPhone slowly to start tracking"
        case .limited(.relocalizing): message = "Finding your position again…"
        case .limited(.excessiveMotion): message = "Move more slowly"
        case .limited(.insufficientFeatures): message = "Point at something with more detail"
        case .limited: message = "Tracking is limited"
        }
        Task { @MainActor in self.trackingMessage = message }
    }

    nonisolated func sessionWasInterrupted(_ session: ARSession) {
        // Tracking is lost during an interruption, so end the take while it's good.
        Task { @MainActor in
            if self.isRecording { self.stopRecording() }
            if self.isScanningLight { self.finishLightScan() }
        }
    }

    nonisolated func session(_ session: ARSession, didFailWithError error: Error) {
        let message = error.localizedDescription
        Task { @MainActor in
            self.errorMessage = message
            if self.isRecording { self.stopRecording() }
        }
    }
}
