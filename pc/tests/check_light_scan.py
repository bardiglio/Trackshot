"""Check HDRI stitching against a simulated room with known light values.

Run make_fake_capture.py first, then run this inside Blender:
  blender --background --factory-startup --python check_light_scan.py -- test_out

It photographs the simulated room the way the app does, with bracketed exposures
that clip, as JPEGs, and with the floor left unscanned. Then it builds a .blend
and compares both the HDRI file and a real Cycles render with the true values.
"""

import json
import os
import sys
import zipfile

import bpy
import numpy as np
from mathutils import Matrix

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import trackshot_blender as tb

folder = sys.argv[sys.argv.index("--") + 1]
scan_folder = os.path.join(folder, "scan")
os.makedirs(scan_folder, exist_ok=True)

WIDTH, HEIGHT = 640, 480
FX, FY, CX, CY = 520.0, 520.0, 332.0, 228.0
BASE_TIME, BASE_ISO = 1.0 / 60.0, 100.0
TAKE_EXPOSURE = BASE_TIME * BASE_ISO
LAMP = np.array([0.3, 0.5, 0.81]) / np.linalg.norm([0.3, 0.5, 0.81])
LAMP_PEAK = 60.0


def room_radiance(directions):
    """True radiance for Blender-axis directions: a tinted room and a bright lamp."""
    x, y, z = directions[..., 0], directions[..., 1], directions[..., 2]
    azimuth = np.arctan2(y, x)
    level = 0.25 + 0.35 * np.maximum(z, 0.0)
    colour = np.stack(
        (
            level * (1.0 + 0.5 * np.cos(azimuth)),
            level * (1.0 + 0.5 * np.sin(azimuth)),
            level * (1.0 - 0.3 * z),
        ),
        axis=-1,
    )
    angle = np.arccos(np.clip(directions @ LAMP, -1.0, 1.0))
    colour += (LAMP_PEAK * np.exp(-((angle / np.radians(6.0)) ** 2)))[..., None]
    return colour


def linear_to_srgb(values):
    values = np.clip(values, 0.0, 1.0)
    return np.where(values <= 0.0031308, values * 12.92, 1.055 * values ** (1 / 2.4) - 0.055)


def arkit_to_blender(directions):
    return np.stack(
        (directions[..., 0], -directions[..., 2], directions[..., 1]), axis=-1
    )


def pixel_rays(width, height, fx, fy, cx, cy):
    """Camera-space rays through pixel centres, top row first."""
    u, v = np.meshgrid(np.arange(width) + 0.5, np.arange(height) + 0.5)
    rays = np.stack(((u - cx) / fx, (cy - v) / fy, -np.ones_like(u)), axis=-1)
    return rays / np.linalg.norm(rays, axis=-1, keepdims=True)


def photograph(index, pose, ev):
    """Save what the phone would capture, returning its manifest entry."""
    time = BASE_TIME * 2.0**ev
    rotation = np.array(pose)[:3, :3]
    world = pixel_rays(WIDTH, HEIGHT, FX, FY, CX, CY) @ rotation.T
    captured = room_radiance(arkit_to_blender(world)) * time * BASE_ISO
    rgba = np.ones((HEIGHT, WIDTH, 4), dtype=np.float32)
    rgba[..., :3] = linear_to_srgb(captured)
    image = bpy.data.images.new(f"scan{index}", WIDTH, HEIGHT, alpha=False)
    image.pixels.foreach_set(rgba[::-1].ravel())
    name = f"scan_{index:04d}.jpg"
    image.filepath_raw = os.path.join(scan_folder, name)
    image.file_format = "JPEG"
    image.save()
    bpy.data.images.remove(image)
    return {
        "file": name,
        "width": WIDTH,
        "height": HEIGHT,
        "camera": [list(row) for row in pose],
        "intrinsics": {"fx": FX, "fy": FY, "cx": CX, "cy": CY},
        "exposureTime": time,
        "iso": BASE_ISO,
    }


frames = []
for pitch in (-35.0, 0.0, 35.0, 70.0):
    for yaw in range(0, 360, 30):
        pose = (
            Matrix.Rotation(np.radians(yaw), 4, "Y")
            @ Matrix.Rotation(np.radians(pitch), 4, "X")
        )
        for ev in (0, -4, -8):
            frames.append(photograph(len(frames), pose, ev))
pose = Matrix.Rotation(np.radians(90.0), 4, "X")
for ev in (0, -4, -8):
    frames.append(photograph(len(frames), pose, ev))

manifest = json.load(open(os.path.join(folder, "manifest.json"), encoding="utf-8"))
manifest["objects"] = []
manifest["planes"] = []
manifest["lightScan"] = frames
manifest["light"] = {
    "ambientIntensity": 1000.0,
    "colorTemperature": 6500.0,
    "exposure": TAKE_EXPOSURE,
}
zip_path = os.path.join(folder, "scan_capture.zip")
with zipfile.ZipFile(zip_path, "w") as archive:
    archive.write(os.path.join(folder, "plate.mov"), "plate.mov")
    archive.writestr("manifest.json", json.dumps(manifest))
    for frame in frames:
        archive.write(os.path.join(scan_folder, frame["file"]), frame["file"])

blend_path = os.path.join(folder, "scan_built.blend")
tb.convert_zip(zip_path, blend_path)
bpy.ops.wm.open_mainfile(filepath=blend_path)
scene = bpy.context.scene
failures = []


def check(label, value, limit):
    status = "ok" if value <= limit else "FAIL"
    print(f"{label}: {value:.4f} (limit {limit}) {status}", flush=True)
    if value > limit:
        failures.append(label)


texture = next(n for n in scene.world.node_tree.nodes if n.type == "TEX_ENVIRONMENT")
image = texture.image
if not image.filepath.startswith("//"):
    failures.append(f"HDRI path isn't relative: {image.filepath}")
if any(o.type == "LIGHT" for o in bpy.data.objects):
    failures.append("the fallback sun should be gone")

# Compare the saved HDRI file with the true room, in the scanned directions only.
width, height = image.size
pixels = np.empty(width * height * 4, dtype=np.float32)
image.pixels.foreach_get(pixels)
hdri = pixels.reshape(height, width, 4)[::-1, :, :3]
arkit = tb.equirect_directions(width, height)
blender = arkit_to_blender(arkit).astype(np.float64)
truth = room_radiance(blender) * TAKE_EXPOSURE
lamp_angle = np.arccos(np.clip(blender @ LAMP, -1.0, 1.0))
elevation = np.arcsin(np.clip(blender[..., 2], -1.0, 1.0))
room_area = (elevation > np.radians(-40.0)) & (lamp_angle > np.radians(25.0))
room_error = np.abs(hdri - truth)[room_area] / truth[room_area]
check("Room, median error", float(np.median(room_error)), 0.05)
check("Room, 99th percentile error", float(np.percentile(room_error, 99)), 0.25)

lamp_area = lamp_angle < np.radians(25.0)
energy_found = hdri[lamp_area].sum()
energy_true = truth[lamp_area].sum()
check("Lamp, total energy error", abs(energy_found / energy_true - 1.0), 0.10)
peak_row, peak_column = np.unravel_index(
    np.argmax(np.where(lamp_area, hdri[..., 1], 0.0)), hdri.shape[:2]
)
peak_direction = blender[peak_row, peak_column]
check(
    "Lamp, direction error in degrees",
    float(np.degrees(np.arccos(np.clip(peak_direction @ LAMP, -1.0, 1.0)))),
    2.0,
)
floor = blender[..., 2] < -0.98
if not np.all(np.isfinite(hdri)) or hdri[floor].min() <= 0.0:
    failures.append("unscanned floor wasn't filled")

# Render through the real camera with the real world shader. This catches any
# disagreement with how Blender maps an HDRI onto directions.
cameras = manifest["frames"][0]
scene.frame_set(1)
scene.render.film_transparent = False
scene.render.use_compositing = False
scene.cycles.samples = 16
scene.cycles.use_denoising = False
scene.render.filepath = os.path.join(folder, "scan_render.exr")
settings = scene.render.image_settings
if hasattr(settings, "media_type"):
    settings.media_type = "IMAGE"
settings.file_format = "OPEN_EXR"
bpy.ops.render.render(write_still=True)
rendered_image = bpy.data.images.load(scene.render.filepath)
render_width, render_height = rendered_image.size
pixels = np.empty(render_width * render_height * 4, dtype=np.float32)
rendered_image.pixels.foreach_get(pixels)
rendered = pixels.reshape(render_height, render_width, 4)[::-1, :, :3]

rotation = np.array(cameras["camera"])[:3, :3]
intrinsics = cameras["intrinsics"]
rays = pixel_rays(
    render_width, render_height,
    intrinsics["fx"], intrinsics["fy"], intrinsics["cx"], intrinsics["cy"],
) @ rotation.T
view = arkit_to_blender(rays)
expected = room_radiance(view) * TAKE_EXPOSURE
away_from_lamp = np.degrees(np.arccos(np.clip(view @ LAMP, -1.0, 1.0))) > 25.0
render_error = np.abs(rendered - expected)[away_from_lamp] / expected[away_from_lamp]
check("Cycles render, median error", float(np.median(render_error)), 0.05)

if failures:
    print("FAILED: " + "; ".join(failures), flush=True)
    sys.exit(1)
print("ALL HDRI CHECKS PASSED", flush=True)
