"""Check HDRI stitching against a simulated room with known light values.

Run make_fake_capture.py first, then run this inside Blender:
  blender --background --factory-startup --python check_light_scan.py -- test_out

It photographs the simulated room the way the app does, with bracketed exposures
that clip, as JPEGs, and with the floor left unscanned. Then it builds a .blend
and compares both the HDRI file and a real Cycles render with the true values.

It runs twice. The second time the dark brackets are brighter than the exposure
reported for them, which is what a real iPhone does.
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

WIDTH, HEIGHT = 640, 480
FX, FY, CX, CY = 520.0, 520.0, 332.0, 228.0
BASE_TIME, BASE_ISO = 1.0 / 60.0, 100.0
TAKE_EXPOSURE = BASE_TIME * BASE_ISO
LAMP = np.array([0.3, 0.5, 0.81]) / np.linalg.norm([0.3, 0.5, 0.81])
LAMP_PEAK = 60.0
REPORTED_STOPS = (0, -4, -8)
TAKE_GAINS = (2.0, 1.0, 1.76)
failures = []


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


def photograph(scan_folder, index, pose, reported_stops, actual_stops, tint):
    """Save what the phone would capture, returning its manifest entry."""
    rotation = np.array(pose)[:3, :3]
    world = pixel_rays(WIDTH, HEIGHT, FX, FY, CX, CY) @ rotation.T
    captured = room_radiance(arkit_to_blender(world)) * TAKE_EXPOSURE * 2.0**actual_stops
    captured = captured * tint
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
        "exposureTime": BASE_TIME * 2.0**reported_stops,
        "iso": BASE_ISO,
    }


def check(label, value, limit):
    status = "ok" if value <= limit else "FAIL"
    print(f"{label}: {value:.4f} (limit {limit}) {status}", flush=True)
    if value > limit:
        failures.append(label)


def run(name, actual_stops, lamp_is_measurable, scan_gains=None):
    """Scan the room, build a .blend from it, and compare with the truth."""
    # A scan locked to a different white balance than the video sees the same
    # room with a colour cast.
    tint = np.ones(3)
    if scan_gains is not None:
        tint = np.array(scan_gains) / np.array(TAKE_GAINS)
        tint = tint / tint[1]
    scan_folder = os.path.join(folder, f"scan_{name}")
    os.makedirs(scan_folder, exist_ok=True)
    poses = [
        Matrix.Rotation(np.radians(yaw), 4, "Y") @ Matrix.Rotation(np.radians(pitch), 4, "X")
        for pitch in (-35.0, 0.0, 35.0, 70.0)
        for yaw in range(0, 360, 30)
    ]
    poses.append(Matrix.Rotation(np.radians(90.0), 4, "X"))
    frames = []
    for pose in poses:
        for reported, actual in zip(REPORTED_STOPS, actual_stops):
            frames.append(photograph(scan_folder, len(frames), pose, reported, actual, tint))

    manifest = json.load(open(os.path.join(folder, "manifest.json"), encoding="utf-8"))
    manifest["objects"] = []
    manifest["planes"] = []
    manifest["lightScan"] = frames
    manifest["light"] = {
        "ambientIntensity": 1000.0,
        "colorTemperature": 6500.0,
        "exposure": TAKE_EXPOSURE,
    }
    if scan_gains is not None:
        manifest["light"]["whiteBalanceGains"] = list(TAKE_GAINS)
        manifest["lightScanWhiteBalance"] = list(scan_gains)
    zip_path = os.path.join(folder, f"scan_{name}.zip")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(os.path.join(folder, "plate.mov"), "plate.mov")
        archive.writestr("manifest.json", json.dumps(manifest))
        for frame in frames:
            archive.write(os.path.join(scan_folder, frame["file"]), frame["file"])

    blend_path = os.path.join(folder, f"scan_{name}.blend")
    tb.convert_zip(zip_path, blend_path)
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    scene = bpy.context.scene

    texture = next(n for n in scene.world.node_tree.nodes if n.type == "TEX_ENVIRONMENT")
    image = texture.image
    if not image.filepath.startswith("//"):
        failures.append(f"{name}: HDRI path isn't relative: {image.filepath}")
    if any(o.type == "LIGHT" for o in bpy.data.objects):
        failures.append(f"{name}: the fallback sun should be gone")

    # Compare the saved HDRI file with the true room, in the scanned directions only.
    width, height = image.size
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    hdri = pixels.reshape(height, width, 4)[::-1, :, :3]
    blender = arkit_to_blender(tb.equirect_directions(width, height)).astype(np.float64)
    truth = room_radiance(blender) * TAKE_EXPOSURE
    lamp_angle = np.arccos(np.clip(blender @ LAMP, -1.0, 1.0))
    elevation = np.arcsin(np.clip(blender[..., 2], -1.0, 1.0))
    room_area = (elevation > np.radians(-40.0)) & (lamp_angle > np.radians(25.0))
    room_error = np.abs(hdri - truth)[room_area] / truth[room_area]
    check(f"{name}: room, median error", float(np.median(room_error)), 0.05)
    check(f"{name}: room, 99th percentile error", float(np.percentile(room_error, 99)), 0.25)

    lamp_area = lamp_angle < np.radians(25.0)
    if lamp_is_measurable:
        energy_error = abs(hdri[lamp_area].sum() / truth[lamp_area].sum() - 1.0)
        check(f"{name}: lamp, total energy error", float(energy_error), 0.10)
        peak_row, peak_column = np.unravel_index(
            np.argmax(np.where(lamp_area, hdri[..., 1], 0.0)), hdri.shape[:2]
        )
        peak_direction = blender[peak_row, peak_column]
        check(
            f"{name}: lamp, direction error in degrees",
            float(np.degrees(np.arccos(np.clip(peak_direction @ LAMP, -1.0, 1.0)))),
            2.0,
        )
    else:
        # The lamp clips in every photo, so only a lower bound can be known: the
        # level at which the darkest bracket clips.
        clip_level = 0.9 / 2.0 ** min(actual_stops)
        if hdri[lamp_area].max() < clip_level:
            failures.append(f"{name}: the clipped lamp is dimmer than its lower bound")
    floor = blender[..., 2] < -0.98
    if not np.all(np.isfinite(hdri)) or hdri[floor].min() <= 0.0:
        failures.append(f"{name}: unscanned floor wasn't filled")

    # Other programs read the file without Blender's settings, so the values on
    # disk have to be the linear ones.
    raw = bpy.data.images.load(bpy.path.abspath(image.filepath), check_existing=False)
    raw.colorspace_settings.name = "Non-Color"
    raw_pixels = np.empty(width * height * 4, dtype=np.float32)
    raw.pixels.foreach_get(raw_pixels)
    on_disk = raw_pixels.reshape(height, width, 4)[::-1, :, :3]
    check(f"{name}: file on disk differs from scene values", float(np.abs(on_disk - hdri).max()), 1e-3)

    # Render through the real camera with the real world shader. This catches any
    # disagreement with how Blender maps an HDRI onto directions.
    camera = manifest["frames"][0]
    scene.frame_set(1)
    scene.render.film_transparent = False
    scene.render.use_compositing = False
    scene.cycles.samples = 16
    scene.cycles.use_denoising = False
    scene.render.filepath = os.path.join(folder, f"scan_{name}_render.exr")
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

    rotation = np.array(camera["camera"])[:3, :3]
    intrinsics = camera["intrinsics"]
    rays = pixel_rays(
        render_width, render_height,
        intrinsics["fx"], intrinsics["fy"], intrinsics["cx"], intrinsics["cy"],
    ) @ rotation.T
    view = arkit_to_blender(rays)
    expected = room_radiance(view) * TAKE_EXPOSURE
    away_from_lamp = np.degrees(np.arccos(np.clip(view @ LAMP, -1.0, 1.0))) > 25.0
    render_error = np.abs(rendered - expected)[away_from_lamp] / expected[away_from_lamp]
    check(f"{name}: Cycles render, median error", float(np.median(render_error)), 0.05)


run("honest", actual_stops=(0, -4, -8), lamp_is_measurable=True)
run(
    "misreported",
    actual_stops=(0, -0.5, -2),
    lamp_is_measurable=False,
    scan_gains=(1.6, 1.0, 2.2),
)

if failures:
    print("FAILED: " + "; ".join(failures), flush=True)
    sys.exit(1)
print("ALL HDRI CHECKS PASSED", flush=True)
