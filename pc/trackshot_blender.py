"""Trackshot: turn an iPhone capture into a ready-to-render .blend file.

Run it one of three ways:

  Wait for captures from the iPhone over Wi-Fi:
    blender --background --python trackshot_blender.py -- serve --out C:/Trackshot

  Convert a capture file you copied over by hand:
    blender --background --python trackshot_blender.py -- build capture.zip out.blend

  Install as a Blender add-on, which adds File > Import > Trackshot Capture.
"""

bl_info = {
    "name": "Trackshot Capture Importer",
    "author": "Trackshot",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > Import > Trackshot Capture (.zip)",
    "description": "Builds a camera-tracked scene from a Trackshot iPhone capture",
    "category": "Import-Export",
}

import datetime
import hmac
import http.server
import json
import math
import os
import secrets
import shutil
import socket
import sys
import tempfile
import traceback
import zipfile

import addon_utils
import bpy
import numpy as np
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion, Vector

SUPPORTED_FORMAT = 1
MAX_UPLOAD_BYTES = 50 * 1024**3
HDRI_WIDTH = 2048
MIN_SCAN_FRAMES = 6

# ARKit's world is Y-up and Blender's is Z-up.
Y_UP_TO_Z_UP = Matrix.Rotation(math.radians(90.0), 4, "X")


class CaptureError(Exception):
    """The capture is damaged, or isn't something this script can read."""


def to_blender_point(x, y, z):
    return Vector((x, -z, y))


def camera_matrix(rows):
    # ARKit cameras and Blender cameras share local axes (looking down -Z with
    # +Y up), so only the world basis has to change.
    return Y_UP_TO_Z_UP @ Matrix(rows)


def object_matrix(rows):
    # Imported models and primitives are already Z-up locally, so this changes
    # the basis on both sides.
    return Y_UP_TO_Z_UP @ Matrix(rows) @ Y_UP_TO_Z_UP.inverted()


def intrinsics_to_lens(intrinsics, width, height, sensor_width):
    """Convert pinhole intrinsics in pixels to Blender lens and shift values."""
    fx, cx, cy = intrinsics["fx"], intrinsics["cx"], intrinsics["cy"]
    lens = fx / width * sensor_width
    shift_x = (width / 2.0 - cx) / width
    shift_y = (cy - height / 2.0) / width
    return lens, shift_x, shift_y


def safe_member(folder, name):
    """Return a path for a manifest filename, refusing anything outside folder."""
    if not isinstance(name, str) or not name or os.path.basename(name) != name:
        raise CaptureError(f"Bad file name in manifest: {name!r}")
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        raise CaptureError(f"Capture is missing {name}")
    return path


def safe_extract(zip_path, folder):
    """Extract a zip, refusing entries that would land outside folder."""
    root = os.path.realpath(folder)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                target = os.path.realpath(os.path.join(root, info.filename))
                if target != root and not target.startswith(root + os.sep):
                    raise CaptureError(f"Unsafe path in zip: {info.filename}")
            archive.extractall(root)
    except zipfile.BadZipFile as error:
        raise CaptureError("The file isn't a valid zip") from error


def find_capture_root(folder):
    """Find manifest.json, which may sit inside one top-level folder of the zip."""
    if os.path.isfile(os.path.join(folder, "manifest.json")):
        return folder
    for entry in os.scandir(folder):
        if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "manifest.json")):
            return entry.path
    raise CaptureError("No manifest.json found in the capture")


def load_manifest(root):
    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as file:
        manifest = json.load(file)
    if manifest.get("format") != SUPPORTED_FORMAT:
        raise CaptureError(f"Unsupported capture format: {manifest.get('format')!r}")
    if not manifest.get("frames"):
        raise CaptureError("The capture contains no frames")
    return manifest


def setup_render(scene, manifest):
    video = manifest["video"]
    scene.frame_start = 1
    scene.frame_end = len(manifest["frames"])
    scene.frame_current = 1
    scene.render.fps = round(video["fps"])
    scene.render.fps_base = 1.0
    scene.render.resolution_x = video["width"]
    scene.render.resolution_y = video["height"]
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.use_compositing = True
    scene.render.filepath = "//render/frame_"
    if not hasattr(scene, "cycles"):
        addon_utils.enable("cycles")
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 128
    scene.cycles.use_denoising = True
    # The plate is already display-ready, so tone mapping would wash it out.
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"


def build_camera(scene, manifest, clip):
    video = manifest["video"]
    width, height = video["width"], video["height"]

    data = bpy.data.cameras.new("iPhone Camera")
    data.sensor_fit = "HORIZONTAL"
    data.sensor_width = 36.0
    data.clip_start = 0.01
    data.clip_end = 1000.0
    camera = bpy.data.objects.new("iPhone Camera", data)
    scene.collection.objects.link(camera)
    scene.camera = camera

    data.show_background_images = True
    background = data.background_images.new()
    background.source = "MOVIE_CLIP"
    background.clip = clip
    background.alpha = 1.0

    camera.rotation_mode = "QUATERNION"
    previous = None
    for index, frame in enumerate(manifest["frames"]):
        number = index + 1
        location, rotation, _ = camera_matrix(frame["camera"]).decompose()
        if previous is not None:
            # Keeps the shortest rotation path, avoiding a spin between frames.
            rotation.make_compatible(previous)
        previous = rotation.copy()
        camera.location = location
        camera.rotation_quaternion = rotation
        camera.keyframe_insert("location", frame=number)
        camera.keyframe_insert("rotation_quaternion", frame=number)

        lens, shift_x, shift_y = intrinsics_to_lens(
            frame["intrinsics"], width, height, data.sensor_width
        )
        data.lens = lens
        data.shift_x = shift_x
        data.shift_y = shift_y
        data.keyframe_insert("lens", frame=number)
        data.keyframe_insert("shift_x", frame=number)
        data.keyframe_insert("shift_y", frame=number)
    return camera


def build_primitive(scene, entry):
    size = float(entry.get("size", 0.2))
    kind = entry["kind"]
    before = set(bpy.data.objects)
    if kind == "box":
        bpy.ops.mesh.primitive_cube_add(size=size)
    elif kind == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(radius=size / 2.0, segments=48, ring_count=24)
        bpy.ops.object.shade_smooth()
    else:
        raise CaptureError(f"Unknown object kind: {kind!r}")
    new_objects = list(set(bpy.data.objects) - before)
    material = bpy.data.materials.new("Placeholder")
    material.diffuse_color = (0.75, 0.75, 0.78, 1.0)
    for obj in new_objects:
        obj.data.materials.append(material)
    return new_objects


def build_objects(scene, manifest, root):
    for entry in manifest.get("objects", []):
        if entry["kind"] == "usdz":
            before = set(bpy.data.objects)
            bpy.ops.wm.usd_import(filepath=safe_member(root, entry["file"]))
            new_objects = list(set(bpy.data.objects) - before)
        else:
            new_objects = build_primitive(scene, entry)

        pivot = bpy.data.objects.new(entry.get("name", "Object"), None)
        scene.collection.objects.link(pivot)
        pivot.matrix_world = object_matrix(entry["matrix"])
        for obj in new_objects:
            if obj.parent is None:
                obj.parent = pivot


def mark_shadow_catcher(scene, obj):
    scene.collection.objects.link(obj)
    obj.is_shadow_catcher = True
    obj.display_type = "WIRE"


def read_obj(path):
    vertices, faces = [], []
    with open(path, encoding="ascii") as file:
        for line in file:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "v":
                vertices.append(to_blender_point(*map(float, parts[1:4])))
            elif parts[0] == "f":
                faces.append([int(token.split("/")[0]) - 1 for token in parts[1:]])
    return vertices, faces


def build_environment(scene, manifest, root):
    """Add the room as a shadow catcher: the LiDAR mesh, or detected floors."""
    room = manifest.get("roomMesh")
    if room:
        vertices, faces = read_obj(safe_member(root, room))
        mesh = bpy.data.meshes.new("Room Scan")
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        mark_shadow_catcher(scene, bpy.data.objects.new("Room Scan", mesh))
        return

    for index, plane in enumerate(manifest.get("planes", [])):
        half_x, half_z = plane["width"] / 2.0, plane["depth"] / 2.0
        matrix = Matrix(plane["matrix"])
        corners = [(-half_x, -half_z), (half_x, -half_z), (half_x, half_z), (-half_x, half_z)]
        points = []
        for x, z in corners:
            world = matrix @ Vector((x, 0.0, z))
            points.append(to_blender_point(world.x, world.y, world.z))
        mesh = bpy.data.meshes.new(f"Floor {index + 1}")
        # Reversed winding so the normal points up.
        mesh.from_pydata(points, [], [[3, 2, 1, 0]])
        mesh.update()
        mark_shadow_catcher(scene, bpy.data.objects.new(f"Floor {index + 1}", mesh))


def kelvin_to_rgb(kelvin):
    """Tanner Helland's approximation, returned as linear-ish 0-1 RGB."""
    t = max(1000.0, min(40000.0, kelvin)) / 100.0
    red = 255.0 if t <= 66 else 329.698727446 * (t - 60) ** -0.1332047592
    green = (
        99.4708025861 * math.log(t) - 161.1195681661
        if t <= 66
        else 288.1221695283 * (t - 60) ** -0.0755148492
    )
    if t >= 66:
        blue = 255.0
    elif t <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(t - 10) - 305.0447927307
    return tuple(max(0.0, min(255.0, value)) / 255.0 for value in (red, green, blue))


def srgb_to_linear(values):
    return np.where(
        values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4
    ).astype(np.float32)


def read_scan_image(path):
    """Load a scan photo as sRGB-encoded floats, with the top row first."""
    image = bpy.data.images.load(path)
    try:
        width, height = image.size
        if width == 0 or height == 0:
            raise CaptureError(f"Couldn't read scan photo {os.path.basename(path)}")
        pixels = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
    finally:
        bpy.data.images.remove(image)
    return pixels.reshape(height, width, 4)[::-1, :, :3]


def equirect_directions(width, height):
    """World direction of each panorama pixel, in Blender's and ARKit's axes.

    Follows the mapping Blender's Environment Texture node uses.
    """
    u = (np.arange(width, dtype=np.float32) + 0.5) / width
    v = 1.0 - (np.arange(height, dtype=np.float32) + 0.5) / height
    azimuth = (0.5 - u) * 2.0 * np.pi
    elevation = (v - 0.5) * np.pi
    x = np.cos(elevation)[:, None] * np.cos(azimuth)[None, :]
    y = np.cos(elevation)[:, None] * np.sin(azimuth)[None, :]
    z = np.broadcast_to(np.sin(elevation)[:, None], x.shape)
    # Inverse of to_blender_point: ARKit (x, y, z) is Blender (x, -z, y).
    return np.stack((x, z, -y), axis=-1).astype(np.float32)


def add_scan_frame(frame, root, directions, totals, weights, clipped):
    """Accumulate one scan photo's radiance into the panorama buffers."""
    encoded = read_scan_image(safe_member(root, frame["file"]))
    linear = srgb_to_linear(encoded)
    height, width = encoded.shape[:2]

    intrinsics = frame["intrinsics"]
    rotation = np.array(frame["camera"], dtype=np.float32)[:3, :3]
    local = directions @ rotation  # Same as applying the inverse rotation.
    in_front = local[..., 2] < -1e-3
    rows, columns = np.nonzero(in_front)
    local = local[rows, columns]
    depth = -local[:, 2]
    px = intrinsics["fx"] * local[:, 0] / depth + intrinsics["cx"]
    py = intrinsics["cy"] - intrinsics["fy"] * local[:, 1] / depth
    # Image coordinates here run through pixel centres.
    px -= 0.5
    py -= 0.5
    inside = (px >= 0) & (px < width - 1) & (py >= 0) & (py < height - 1)
    rows, columns, px, py = rows[inside], columns[inside], px[inside], py[inside]
    if rows.size == 0:
        return

    left, top = px.astype(np.int32), py.astype(np.int32)
    fx, fy = (px - left)[:, None], (py - top)[:, None]
    sampled = (
        linear[top, left] * (1 - fx) * (1 - fy)
        + linear[top, left + 1] * fx * (1 - fy)
        + linear[top + 1, left] * (1 - fx) * fy
        + linear[top + 1, left + 1] * fx * fy
    )
    brightest = encoded[top + np.rint(fy[:, 0]).astype(np.int32),
                        left + np.rint(fx[:, 0]).astype(np.int32)].max(axis=1)

    # Radiance is independent of exposure, which is what makes the brackets merge.
    radiance = sampled / float(frame["exposureTime"] * frame["iso"])

    trust_highlights = np.clip((0.95 - brightest) / 0.10, 0.0, 1.0)
    trust_shadows = np.clip(brightest / 0.10, 0.02, 1.0)
    edge = np.minimum.reduce((px, width - 1 - px, py, height - 1 - py))
    trust_edges = np.clip(edge / (0.1 * width), 0.05, 1.0)
    weight = trust_highlights * trust_shadows * trust_edges

    np.add.at(totals, (rows, columns), radiance * weight[:, None])
    np.add.at(weights, (rows, columns), weight)
    # A light still clipped at the darkest exposure is at least this bright.
    saturated = trust_highlights == 0.0
    clipped[rows[saturated], columns[saturated]] = np.maximum(
        clipped[rows[saturated], columns[saturated]], radiance[saturated]
    )


def fill_holes(image, known):
    """Fill unscanned directions from their surroundings, coarsest detail first."""
    levels = [(image * known[..., None], known.astype(np.float32))]
    while min(levels[-1][1].shape) > 2:
        colour, weight = levels[-1]
        height, width = weight.shape
        colour = colour[: height // 2 * 2, : width // 2 * 2]
        weight = weight[: height // 2 * 2, : width // 2 * 2]
        levels.append((
            colour.reshape(height // 2, 2, width // 2, 2, 3).sum(axis=(1, 3)),
            weight.reshape(height // 2, 2, width // 2, 2).sum(axis=(1, 3)),
        ))

    colour, weight = levels[-1]
    filled = colour.sum(axis=(0, 1)) / max(weight.sum(), 1e-6)
    filled = np.broadcast_to(filled, colour.shape).astype(np.float32)
    for colour, weight in reversed(levels[:-1]):
        height, width = weight.shape
        upsampled = np.repeat(np.repeat(filled, 2, axis=0), 2, axis=1)
        upsampled = np.pad(
            upsampled,
            ((0, height - upsampled.shape[0]), (0, width - upsampled.shape[1]), (0, 0)),
            mode="edge",
        )
        # Rolling wraps left to right, as the panorama does.
        smooth = sum(np.roll(upsampled, shift, axis=1) for shift in (-1, 0, 1)) / 3.0
        smooth[1:-1] = (smooth[:-2] + smooth[1:-1] + smooth[2:]) / 3.0
        known_here = weight > 1e-6
        safe_weight = np.where(known_here, weight, 1.0)[..., None]
        filled = np.where(known_here[..., None], colour / safe_weight, smooth).astype(np.float32)
    return filled


def stitch_hdri(frames, root, width=HDRI_WIDTH):
    """Merge the bracketed scan photos into one radiance panorama."""
    height = width // 2
    directions = equirect_directions(width, height)
    totals = np.zeros((height, width, 3), dtype=np.float32)
    weights = np.zeros((height, width), dtype=np.float32)
    clipped = np.zeros((height, width, 3), dtype=np.float32)
    for frame in frames:
        add_scan_frame(frame, root, directions, totals, weights, clipped)

    measured = weights > 1e-4
    known = measured | (clipped.max(axis=2) > 0)
    image = np.where(
        measured[..., None], totals / np.maximum(weights, 1e-4)[..., None], clipped
    )
    return fill_holes(image, known), float(known.mean())


def save_hdri(pixels, path):
    height, width = pixels.shape[:2]
    image = bpy.data.images.new(
        "Room HDRI", width, height, alpha=False, float_buffer=True
    )
    # This has to happen before saving: Blender encodes the file in this space.
    for name in ("Linear Rec.709", "Linear"):
        try:
            image.colorspace_settings.name = name
            break
        except TypeError:
            continue
    rgba = np.ones((height, width, 4), dtype=np.float32)
    rgba[..., :3] = pixels
    image.pixels.foreach_set(rgba[::-1].ravel())
    image.filepath_raw = path
    image.file_format = "OPEN_EXR"
    image.save()
    return image


def build_hdri_world(scene, manifest, root):
    """Light the scene from the phone's light scan, matched to the video's exposure."""
    frames = manifest["lightScan"]
    pixels, coverage = stitch_hdri(frames, root)

    # The video recorded radiance times this exposure. Multiplying by it makes a
    # surface lit by this HDRI come out as bright as the same surface in the video.
    light = manifest.get("light") or {}
    # Without the take's exposure, the scan's brightest bracket is the closest
    # thing to it: that bracket uses the camera's normal exposure.
    exposure = light.get("exposure") or float(
        np.max([frame["exposureTime"] * frame["iso"] for frame in frames])
    )
    image = save_hdri(pixels * float(exposure), os.path.join(root, "room_hdri.exr"))

    world = bpy.data.worlds.new("Room HDRI")
    if world.node_tree is None:  # New worlds already have nodes from Blender 5.0
        world.use_nodes = True
    nodes = world.node_tree.nodes
    background = next(n for n in nodes if n.type == "BACKGROUND")
    background.inputs["Strength"].default_value = 1.0
    texture = nodes.new("ShaderNodeTexEnvironment")
    texture.image = image
    texture.location = (background.location.x - 320, background.location.y)
    world.node_tree.links.new(texture.outputs["Color"], background.inputs["Color"])
    scene.world = world
    print(f"Light scan covered {coverage:.0%} of the room", flush=True)


def build_lighting(scene, manifest, root):
    """Use the phone's light scan if there is one, otherwise a simple estimate.

    ARKit's estimate has no direction, so the sun's angle is only a starting
    point when there's no scan.
    """
    if len(manifest.get("lightScan") or []) >= MIN_SCAN_FRAMES:
        build_hdri_world(scene, manifest, root)
        return

    light = manifest.get("light") or {}
    strength = float(light.get("ambientIntensity", 1000.0)) / 1000.0
    color = kelvin_to_rgb(float(light.get("colorTemperature", 6500.0)))

    world = bpy.data.worlds.new("Room Light")
    if world.node_tree is None:  # New worlds already have nodes from Blender 5.0
        world.use_nodes = True
    background = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    background.inputs["Color"].default_value = (*color, 1.0)
    # Split so ambient plus sun keeps a mid-grey object below clipping, and the
    # sun is strong enough to cast a visible shadow.
    background.inputs["Strength"].default_value = 0.4 * strength
    scene.world = world

    sun_data = bpy.data.lights.new("Key Sun (adjust me)", "SUN")
    sun_data.energy = 1.5 * strength
    sun_data.color = color
    sun_data.angle = math.radians(5.0)
    sun = bpy.data.objects.new("Key Sun (adjust me)", sun_data)
    sun.rotation_euler = (math.radians(50.0), 0.0, math.radians(30.0))
    scene.collection.objects.link(sun)


def socket_named(sockets, *names):
    for name in names:
        if name in sockets:
            return sockets[name]
    raise CaptureError(f"Compositor socket not found, tried: {names}")


def build_compositor(scene, clip):
    """Lay the rendered CG and its shadows over the original video."""
    if hasattr(scene, "compositing_node_group"):  # Blender 5.0 and newer
        tree = bpy.data.node_groups.new("Trackshot Composite", "CompositorNodeTree")
        scene.compositing_node_group = tree
        tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        output = tree.nodes.new("NodeGroupOutput")
    else:
        scene.use_nodes = True
        tree = scene.node_tree
        tree.nodes.clear()
        output = tree.nodes.new("CompositorNodeComposite")

    plate = tree.nodes.new("CompositorNodeMovieClip")
    plate.clip = clip
    layers = tree.nodes.new("CompositorNodeRLayers")
    layers.scene = scene
    over = tree.nodes.new("CompositorNodeAlphaOver")

    plate.location = (-500, 200)
    layers.location = (-500, -200)
    over.location = (-100, 0)
    output.location = (200, 0)

    inputs = [socket for socket in over.inputs if socket.type == "RGBA"]
    tree.links.new(plate.outputs["Image"], inputs[0])
    tree.links.new(layers.outputs["Image"], inputs[1])
    tree.links.new(over.outputs[0], output.inputs[0])


def build_scene(root):
    """Replace the current scene's contents with the capture in root."""
    manifest = load_manifest(root)
    scene = bpy.context.scene
    setup_render(scene, manifest)
    clip = bpy.data.movieclips.load(safe_member(root, manifest["video"]["file"]))
    clip.frame_start = 1
    scene.active_clip = clip
    build_camera(scene, manifest, clip)
    build_objects(scene, manifest, root)
    build_environment(scene, manifest, root)
    build_lighting(scene, manifest, root)
    build_compositor(scene, clip)
    return manifest


def convert_zip(zip_path, blend_path):
    """Build blend_path from a capture zip, with the video in a folder beside it."""
    blend_path = os.path.abspath(blend_path)
    stem = os.path.splitext(os.path.basename(blend_path))[0]
    assets = os.path.join(os.path.dirname(blend_path), stem + "_files")
    for existing in (blend_path, assets):
        if os.path.exists(existing):
            raise CaptureError(f"Already exists, not overwriting: {existing}")
    os.makedirs(assets)
    try:
        safe_extract(zip_path, assets)
        root = find_capture_root(assets)
        bpy.ops.wm.read_factory_settings(use_empty=True)
        manifest = build_scene(root)
        # Saving first lets the video path be stored relative to the .blend file.
        bpy.ops.wm.save_as_mainfile(filepath=blend_path)
        bpy.ops.file.make_paths_relative()
        bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    except Exception:
        shutil.rmtree(assets, ignore_errors=True)
        if os.path.exists(blend_path):
            os.remove(blend_path)
        raise
    finally:
        # The second save leaves a backup of a file this function just created.
        if os.path.exists(blend_path + "1"):
            os.remove(blend_path + "1")
    return manifest


def unique_blend_path(folder):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    attempt = 1
    while True:
        suffix = "" if attempt == 1 else f"_{attempt}"
        path = os.path.join(folder, f"Trackshot_{stamp}{suffix}.blend")
        if not os.path.exists(path) and not os.path.exists(path[:-6] + "_files"):
            return path
        attempt += 1


def lan_address():
    """Find the address other devices reach this PC on. Sends no traffic."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def make_handler(out_folder, token):
    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            supplied = self.headers.get("X-Trackshot-Code", "")
            return hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))

        def do_GET(self):
            if self.path != "/ping":
                return self.reply(404, {"error": "Not found"})
            if not self.authorized():
                return self.reply(403, {"error": "Wrong pairing code"})
            self.reply(200, {"ok": True})

        def do_POST(self):
            if self.path != "/upload":
                return self.reply(404, {"error": "Not found"})
            if not self.authorized():
                return self.reply(403, {"error": "Wrong pairing code"})
            try:
                remaining = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.reply(411, {"error": "Content-Length required"})
            if remaining <= 0 or remaining > MAX_UPLOAD_BYTES:
                return self.reply(413, {"error": "Upload is empty or too large"})

            blend_path = unique_blend_path(out_folder)
            handle, zip_path = tempfile.mkstemp(suffix=".zip")
            try:
                with os.fdopen(handle, "wb") as file:
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            return self.reply(400, {"error": "Upload was cut off"})
                        file.write(chunk)
                        remaining -= len(chunk)
                print(f"Received capture, building {blend_path}", flush=True)
                manifest = convert_zip(zip_path, blend_path)
                print(f"Done: {blend_path}", flush=True)
                self.reply(200, {"blend": blend_path, "frames": len(manifest["frames"])})
            except CaptureError as error:
                print(f"Rejected capture: {error}", flush=True)
                self.reply(422, {"error": str(error)})
            except Exception as error:
                # A malformed capture must not take the receiver down.
                traceback.print_exc()
                self.reply(500, {"error": f"Couldn't build the .blend: {error}"})
            finally:
                os.remove(zip_path)

        def log_message(self, *args):
            pass

    return Handler


def serve(out_folder, port=8765):
    os.makedirs(out_folder, exist_ok=True)
    out_folder = os.path.abspath(out_folder)
    code = f"{secrets.randbelow(1000000):06d}"
    server = http.server.HTTPServer(("0.0.0.0", port), make_handler(out_folder, code))
    print("", flush=True)
    print("Trackshot is waiting for captures. In the iPhone app, enter:", flush=True)
    print(f"  PC address:   {lan_address()}:{port}", flush=True)
    print(f"  Pairing code: {code}", flush=True)
    print(f".blend files will be saved in {out_folder}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class TRACKSHOT_OT_import(bpy.types.Operator, ImportHelper):
    """Build a camera-tracked scene from a Trackshot capture"""

    bl_idname = "import_scene.trackshot"
    bl_label = "Import Trackshot Capture"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".zip"
    filter_glob: bpy.props.StringProperty(default="*.zip", options={"HIDDEN"})

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save your .blend file first, the video is stored beside it")
            return {"CANCELLED"}
        stem = os.path.splitext(os.path.basename(self.filepath))[0]
        assets = bpy.path.abspath(f"//{stem}_files")
        if os.path.exists(assets):
            self.report({"ERROR"}, f"Already exists, not overwriting: {assets}")
            return {"CANCELLED"}
        try:
            os.makedirs(assets)
            safe_extract(self.filepath, assets)
            manifest = build_scene(find_capture_root(assets))
        except Exception as error:
            shutil.rmtree(assets, ignore_errors=True)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Imported {len(manifest['frames'])} frames")
        return {"FINISHED"}


def menu_item(self, context):
    self.layout.operator(TRACKSHOT_OT_import.bl_idname, text="Trackshot Capture (.zip)")


def register():
    bpy.utils.register_class(TRACKSHOT_OT_import)
    bpy.types.TOPBAR_MT_file_import.append(menu_item)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_item)
    bpy.utils.unregister_class(TRACKSHOT_OT_import)


def main(argv):
    if argv[:1] == ["build"] and len(argv) == 3:
        manifest = convert_zip(argv[1], argv[2])
        print(f"Saved {os.path.abspath(argv[2])} ({len(manifest['frames'])} frames)", flush=True)
    elif argv[:2] == ["serve", "--out"] and len(argv) == 3:
        serve(argv[2])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    try:
        main(args)
    except CaptureError as error:
        print(f"Error: {error}", flush=True)
        sys.exit(1)
