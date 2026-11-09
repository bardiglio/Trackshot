import RealityKit
import SwiftUI
import UniformTypeIdentifiers

private struct ARViewContainer: UIViewRepresentable {
    let arView: ARView

    func makeUIView(context: Context) -> ARView { arView }
    func updateUIView(_ uiView: ARView, context: Context) {}
}

struct ContentView: View {
    private enum SendState: Equatable {
        case idle
        case sending
        case sent(String)
        case failed(String)
    }

    @StateObject private var controller = CaptureController()
    @AppStorage("pcAddress") private var pcAddress = ""
    @AppStorage("pairingCode") private var pairingCode = ""
    @State private var showImporter = false
    @State private var showSettings = false
    @State private var sendState = SendState.idle

    var body: some View {
        ZStack {
            ARViewContainer(arView: controller.arView).ignoresSafeArea()
            HStack(alignment: .center) {
                modelControls
                Spacer()
                centerStatus
                Spacer()
                recordControls
            }
            .padding()
        }
        .onAppear { controller.start() }
        .fileImporter(isPresented: $showImporter, allowedContentTypes: [.usdz]) { result in
            if case .success(let url) = result { controller.importModel(from: url) }
        }
        .sheet(isPresented: $showSettings) { settingsSheet }
        .alert(
            "Something went wrong",
            isPresented: Binding(
                get: { controller.errorMessage != nil },
                set: { if !$0 { controller.errorMessage = nil } }
            ),
            actions: { Button("OK") {} },
            message: { Text(controller.errorMessage ?? "") }
        )
        .onChange(of: controller.lastCapture) { sendState = .idle }
    }

    // MARK: Left side

    private var modelControls: some View {
        VStack(alignment: .leading, spacing: 12) {
            Menu {
                Button("Box") { controller.selectedModel = .box }
                Button("Sphere") { controller.selectedModel = .sphere }
                Divider()
                Button("Import USDZ file…") { showImporter = true }
            } label: {
                Label(controller.selectedModel.title, systemImage: "cube")
            }
            Button {
                controller.undoLastPlacement()
            } label: {
                Label("Undo", systemImage: "arrow.uturn.backward")
            }
            .disabled(controller.placedCount == 0)
            Button {
                controller.toggleLightScan()
            } label: {
                Label(
                    controller.hasLightScan ? "Rescan light" : "Scan light",
                    systemImage: controller.hasLightScan ? "checkmark.circle.fill" : "sun.max"
                )
            }
            Button {
                showSettings = true
            } label: {
                Label("PC", systemImage: "desktopcomputer")
            }
        }
        .buttonStyle(.bordered)
        .tint(.white)
        .opacity(controller.isRecording || controller.isScanningLight ? 0.3 : 1)
        .disabled(controller.isRecording || controller.isSaving || controller.isScanningLight)
    }

    // MARK: Light scan

    private var lightScanPanel: some View {
        VStack(spacing: 8) {
            Text("Stay in one spot and turn slowly. Look up, down and all around until the map fills in.")
                .font(.footnote.weight(.medium))
                .multilineTextAlignment(.center)
                .foregroundStyle(.white)
            Canvas { context, size in
                let columns = LightScanner.gridColumns
                let rows = LightScanner.gridRows
                let cellWidth = size.width / CGFloat(columns)
                let cellHeight = size.height / CGFloat(rows)
                for row in 0..<rows {
                    for column in 0..<columns where controller.scanCoverage[row * columns + column] {
                        let cell = CGRect(
                            x: CGFloat(column) * cellWidth,
                            y: CGFloat(row) * cellHeight,
                            width: cellWidth,
                            height: cellHeight
                        )
                        context.fill(Path(cell), with: .color(.yellow))
                    }
                }
            }
            .frame(width: 216, height: 108)
            .background(.white.opacity(0.15))
            .accessibilityLabel("Map of the directions scanned so far")
            Text(String(
                format: "%.0f%% of the room · %d photos",
                LightScanner.coverageFraction(controller.scanCoverage) * 100,
                controller.scanPhotoCount
            ))
            .font(.caption2)
            .foregroundStyle(.white)
            Button("Finish scan") { controller.toggleLightScan() }
                .buttonStyle(.borderedProminent)
        }
        .padding(12)
        .background(.black.opacity(0.6), in: RoundedRectangle(cornerRadius: 12))
    }

    // MARK: Middle

    private var centerStatus: some View {
        VStack(spacing: 8) {
            if let message = controller.trackingMessage {
                banner(message)
            }
            if controller.isScanningLight {
                lightScanPanel
            } else if controller.trackingMessage != nil {
                EmptyView()
            } else if controller.isRecording {
                banner(String(format: "%.1f s", Double(controller.recordedFrames) / Double(max(controller.fps, 1))))
            } else if controller.isSaving {
                banner("Saving capture…")
            } else if controller.lastCapture == nil && controller.placedCount == 0 {
                banner("Tap a surface to place your model")
            }
            Spacer()
            if controller.lastCapture != nil {
                sendControls
            }
        }
    }

    private var sendControls: some View {
        VStack(spacing: 6) {
            HStack {
                Button {
                    send()
                } label: {
                    Label("Send to PC", systemImage: "paperplane.fill")
                }
                .buttonStyle(.borderedProminent)
                .disabled(sendState == .sending)
                if let capture = controller.lastCapture {
                    ShareLink(item: capture) {
                        Label("Share", systemImage: "square.and.arrow.up")
                    }
                    .buttonStyle(.bordered)
                    .tint(.white)
                }
            }
            switch sendState {
            case .idle:
                EmptyView()
            case .sending:
                banner("Sending and building the .blend file…")
            case .sent(let path):
                banner("Saved on your PC: \(path)")
            case .failed(let reason):
                banner(reason)
            }
        }
    }

    private func banner(_ text: String) -> some View {
        Text(text)
            .font(.footnote.weight(.medium))
            .multilineTextAlignment(.center)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(.black.opacity(0.6), in: Capsule())
            .foregroundStyle(.white)
    }

    private func send() {
        guard let capture = controller.lastCapture else { return }
        guard !pcAddress.isEmpty, !pairingCode.isEmpty else {
            showSettings = true
            return
        }
        sendState = .sending
        Task {
            do {
                let path = try await CaptureUploader.send(
                    capture,
                    address: pcAddress.trimmingCharacters(in: .whitespacesAndNewlines),
                    code: pairingCode.trimmingCharacters(in: .whitespacesAndNewlines)
                )
                sendState = .sent(path)
            } catch {
                sendState = .failed(error.localizedDescription)
            }
        }
    }

    // MARK: Right side

    private var recordControls: some View {
        VStack(spacing: 8) {
            Button {
                controller.toggleRecording()
            } label: {
                ZStack {
                    Circle().stroke(.white, lineWidth: 4).frame(width: 72, height: 72)
                    if controller.isRecording {
                        RoundedRectangle(cornerRadius: 6).fill(.red).frame(width: 28, height: 28)
                    } else {
                        Circle().fill(.red).frame(width: 58, height: 58)
                    }
                }
            }
            .accessibilityLabel(controller.isRecording ? "Stop recording" : "Start recording")
            .disabled(controller.isSaving || controller.isScanningLight)
            Text(controller.videoFormatLabel)
                .font(.caption2)
                .foregroundStyle(.white)
                .shadow(radius: 2)
        }
    }

    // MARK: Settings

    private var settingsSheet: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("192.168.1.20:8765", text: $pcAddress)
                        .keyboardType(.numbersAndPunctuation)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Pairing code", text: $pairingCode)
                        .keyboardType(.numberPad)
                } header: {
                    Text("Your PC")
                } footer: {
                    Text("Double-click start_receiver.bat on your PC. It shows both of these. "
                        + "The code changes each time you start it, and both devices need to be on the same Wi-Fi.")
                }
            }
            .navigationTitle("Send to PC")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                Button("Done") { showSettings = false }
            }
        }
    }
}
