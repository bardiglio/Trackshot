"""Check a built .blend against the numbers the fake capture was made from.

Run inside Blender:
  blender --background built.blend --python check_blend.py -- manifest.json out_folder

Compares where Blender's camera projects the marker with where the plate actually
shows it, for every frame, then renders one frame through the compositor.
"""

import json
import os
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

args = sys.argv[sys.argv.index("--") + 1 :]
manifest = json.load(open(args[0], encoding="utf-8"))
out = args[1]

scene = bpy.context.scene
camera = scene.camera
ball = bpy.data.objects["Marker Ball"]
width, height = scene.render.resolution_x, scene.render.resolution_y
failures = []

assert scene.frame_end == len(manifest["frames"]), "wrong frame range"
assert (width, height) == (640, 480), "wrong resolution"

worst = 0.0
for index, frame in enumerate(manifest["frames"]):
    scene.frame_set(index + 1)
    point = world_to_camera_view(scene, camera, ball.matrix_world.translation)
    u = point.x * width
    v = (1.0 - point.y) * height
    expected_u, expected_v = frame["expected_marker_px"]
    worst = max(worst, abs(u - expected_u), abs(v - expected_v))
print(f"Worst marker reprojection error: {worst:.4f} px", flush=True)
if worst > 0.5:
    failures.append(f"reprojection error too high: {worst}")

floors = [o for o in bpy.data.objects if o.name.startswith("Floor")]
if not floors or not all(o.is_shadow_catcher for o in floors):
    failures.append("floor isn't a shadow catcher")
elif floors[0].data.polygons[0].normal.z < 0.99:
    failures.append("floor normal doesn't point up")

clip = next(iter(bpy.data.movieclips), None)
if clip is None or not clip.filepath.startswith("//"):
    failures.append(f"video path isn't relative: {clip and clip.filepath}")
elif clip.frame_duration != len(manifest["frames"]):
    failures.append(f"video has {clip.frame_duration} frames, expected {len(manifest['frames'])}")

scene.frame_set(12)
scene.cycles.samples = 16
scene.cycles.use_denoising = False
scene.render.filepath = os.path.join(out, "check_frame.png")
settings = scene.render.image_settings
if hasattr(settings, "media_type"):
    settings.media_type = "IMAGE"
settings.file_format = "PNG"
bpy.ops.render.render(write_still=True)

if failures:
    print("FAILED: " + "; ".join(failures), flush=True)
    sys.exit(1)
print("ALL CHECKS PASSED", flush=True)
