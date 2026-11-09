"""Create a synthetic capture zip in the format the iPhone app writes.

Run inside Blender:
  blender --background --python make_fake_capture.py -- out_folder

The plate shows a red marker at a known ARKit world point, seen through a known
camera path, with an off-centre principal point. If the importer gets axes, lens
or shift wrong, a CG sphere placed at that point won't sit on the marker.
"""

import json
import math
import os
import sys
import zipfile

import bpy
from mathutils import Matrix, Vector

out = sys.argv[sys.argv.index("--") + 1]
os.makedirs(out, exist_ok=True)

WIDTH, HEIGHT, FPS, COUNT = 640, 480, 30, 24
FX, FY, CX, CY = 520.0, 520.0, 332.0, 228.0
MARKER = (0.1, 0.15, -1.0)  # ARKit world: Y up, camera starts looking down -Z


def arkit_camera(index):
    """Camera pose in ARKit world coordinates: a slow slide and small pan."""
    t = index / (COUNT - 1)
    angle = math.radians(-6.0 + 12.0 * t)
    pose = Matrix.Translation((-0.3 + 0.6 * t, 0.2, 0.5)) @ Matrix.Rotation(angle, 4, "Y")
    return pose


def project(point, camera):
    """Pinhole projection using ARKit conventions, giving pixels from the top left."""
    local = camera.inverted() @ Vector(point)
    u = FX * (local.x / -local.z) + CX
    v = CY - FY * (local.y / -local.z)
    return u, v


def matrix_rows(matrix):
    return [list(row) for row in matrix]


# Paint the plate directly, so it's independent of the code under test.
scene = bpy.context.scene
scene.render.resolution_x, scene.render.resolution_y = WIDTH, HEIGHT
frames = []
frame_paths = []
for index in range(COUNT):
    camera = arkit_camera(index)
    u, v = project(MARKER, camera)
    image = bpy.data.images.new(f"plate{index}", WIDTH, HEIGHT, alpha=False)
    pixels = [0.0] * (WIDTH * HEIGHT * 4)
    for y in range(HEIGHT):
        from_top = HEIGHT - 1 - y
        for x in range(WIDTH):
            at = (y * WIDTH + x) * 4
            shade = 0.22 if (x // 40 + y // 40) % 2 else 0.3
            if (x - u) ** 2 + (from_top - v) ** 2 < 36:
                pixels[at:at + 4] = [1.0, 0.0, 0.0, 1.0]
            else:
                pixels[at:at + 4] = [shade, shade, shade, 1.0]
    image.pixels = pixels
    path = os.path.join(out, f"plate_{index + 1:04d}.png")
    image.filepath_raw = path
    image.file_format = "PNG"
    image.save()
    frame_paths.append(path)
    frames.append(
        {
            "t": index / FPS,
            "camera": matrix_rows(camera),
            "intrinsics": {"fx": FX, "fy": FY, "cx": CX, "cy": CY},
            "expected_marker_px": [u, v],
        }
    )

# A PNG sequence named .mov wouldn't load, so encode a real video with Blender.
sequence = scene.sequence_editor_create()
strips = sequence.strips if hasattr(sequence, "strips") else sequence.sequences
for index, path in enumerate(frame_paths):
    strip = strips.new_image(f"f{index}", path, 1, index + 1)
    strip.blend_type = "REPLACE"
    strip.colorspace_settings.name = "sRGB"
scene.frame_start, scene.frame_end = 1, COUNT
scene.render.fps = FPS
scene.render.use_sequencer = True
scene.view_settings.view_transform = "Standard"
settings = scene.render.image_settings
if hasattr(settings, "media_type"):
    settings.media_type = "VIDEO"
settings.file_format = "FFMPEG"
scene.render.ffmpeg.format = "QUICKTIME"
scene.render.ffmpeg.codec = "H264"
scene.render.ffmpeg.constant_rate_factor = "PERC_LOSSLESS"
plate_path = os.path.join(out, "plate.mov")
scene.render.filepath = plate_path
bpy.ops.render.render(animation=True)
rendered = [
    os.path.join(out, name)
    for name in os.listdir(out)
    if name.startswith("plate") and name.endswith(".mov")
]
if plate_path not in rendered:
    os.replace(rendered[0], plate_path)

manifest = {
    "format": 1,
    "app": "fake-capture",
    "video": {"file": "plate.mov", "width": WIDTH, "height": HEIGHT, "fps": FPS},
    "frames": frames,
    "objects": [
        {
            "name": "Marker Ball",
            "kind": "sphere",
            "size": 0.1,
            "matrix": matrix_rows(Matrix.Translation(MARKER)),
        }
    ],
    "planes": [
        {
            "matrix": matrix_rows(Matrix.Translation((0.1, 0.1, -1.0))),
            "width": 2.0,
            "depth": 2.0,
        }
    ],
    "roomMesh": None,
    "light": {"ambientIntensity": 1000.0, "colorTemperature": 6500.0},
}
with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as file:
    json.dump(manifest, file)

zip_path = os.path.join(out, "capture.zip")
with zipfile.ZipFile(zip_path, "w") as archive:
    archive.write(plate_path, "capture/plate.mov")
    archive.write(os.path.join(out, "manifest.json"), "capture/manifest.json")
print(f"Wrote {zip_path}", flush=True)
