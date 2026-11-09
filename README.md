# Trackshot

Film on your iPhone with 3D models placed in the room, then open the result in Blender as a camera-tracked scene ready to render.

**How it works:** iPhones can't write `.blend` files, so the app records the video and the camera's exact movement, then sends them to your PC. Blender on the PC builds the `.blend` file automatically.

## What you need

- **To build the app:** a free GitHub account. GitHub's cloud Macs compile it, so you don't need your own Mac.
- **To install the app:** a Windows PC, a USB cable and an Apple account. A spare Apple account is safest, because the install tool asks you to sign in with it.
- **iPhone:** iOS 17 or later. A LiDAR model (Pro) also gets a room scan, so real objects can hide models.
- **PC:** Blender 4.2 or later. Tested on 5.0. If yours isn't Blender 5.0 in the default folder, change the path in `pc/start_receiver.bat`.

## Install the app without a Mac

### 1. Build it on GitHub

1. Create a new repository on GitHub and put this folder's contents in it. Standard cloud Macs are free for **public** repositories; private ones use up a limited monthly allowance faster.
2. Open the repository's **Actions** tab. The **Build iPhone app** job starts on its own, or you can start it from there with **Run workflow**.
3. When it turns green, open that run and download **Trackshot-ipa** under **Artifacts**. Unzip it to get `Trackshot.ipa`.

If it turns red, the error is in the failed step's log.

### 2. Install it from Windows

Sideloadly is a third-party tool that isn't made by Apple. It signs the app with your Apple account, then installs it.

1. Remove the Microsoft Store versions of iTunes and iCloud if you have them, then install the versions from Apple's website.
2. Install Sideloadly from [sideloadly.io](https://sideloadly.io).
3. Connect your iPhone by USB and tap **Trust** on it.
4. Drag `Trackshot.ipa` into Sideloadly, enter your Apple account, and press **Start**.
5. On the iPhone, turn on **Settings > Privacy & Security > Developer Mode** and restart when asked.
6. If the app won't open, trust your account under **Settings > General > VPN & Device Management**.

With a free Apple account, the app stops opening after 7 days. Install it again with the same steps; your earlier captures may need sending to the PC first.

## Install the app (with a Mac)

```bash
brew install xcodegen
```

```bash
cd ios && xcodegen && open Trackshot.xcodeproj
```

In Xcode:

1. Select the **Trackshot** target and open **Signing & Capabilities**.
2. Choose your Apple account as the **Team**, and change the bundle ID from `com.example.trackshot` to something unique.
3. Plug in your iPhone, select it as the destination, and press **Run**.

With a free Apple account, the app stops opening after 7 days and needs installing again. A paid developer account avoids that.

## Use it

1. On the PC, double-click `pc/start_receiver.bat`. It shows the PC address and a pairing code. Allow it through Windows Firewall on your private network if asked.
2. In the app, tap **PC** and enter both. The phone and PC must be on the same Wi-Fi.
3. Hold the iPhone sideways, with the buttons as the app shows them. Move it slowly until tracking starts.
4. Tap a surface to place a model. Drag, pinch or twist to adjust it.
5. Press record, film, then press stop.
6. Tap **Send to PC**. The `.blend` file appears in `Documents\Trackshot`, with its video in a `_files` folder beside it. Keep them together.
7. Open the `.blend` file in Blender and render.

**Without Wi-Fi:** tap **Share** to save the zip somewhere like iCloud Drive, then copy it to your PC and do one of these:

- In Blender, install `pc/trackshot_blender.py` as an add-on, save your `.blend` file, and use **File > Import > Trackshot Capture**.
- Run the command below.

```bash
blender --background --python pc/trackshot_blender.py -- build capture.zip out.blend
```

## What's in the .blend file

| Item | Details |
|---|---|
| **Camera** | Keyframed on every frame, including lens changes from autofocus |
| **Your models** | At the position you placed them. USDZ files come across; boxes and spheres are rebuilt in Blender |
| **Room** | The LiDAR scan or detected floors, set up to catch shadows |
| **Lighting** | The room's average brightness and colour, plus a sun you can move |
| **Compositor** | Lays the render and its shadows over your video |
| **Render settings** | Cycles, 128 samples, matching the video's resolution and frame rate. Frames save to a `render` folder beside the `.blend` |

## Current limits

- **Landscape only.** Filming in one fixed orientation keeps the video and camera data aligned.
- **USDZ models only in the app.** To use another format, export it from Blender as USDZ first. You can also skip models in the app and add them in Blender afterwards.
- **No HDRI capture yet.** The lighting is only an average, so the sun's direction is a starting guess. For matched lighting, add your own HDRI to the World in Blender.
- **No audio** is recorded.
- **Models can't move during a take.** The `.blend` file stores one position for each.

## Testing the PC side

These commands make a synthetic capture and check that the built `.blend` lines up with it. Run them from the `pc` folder, with a temporary folder of your choice as `test_out`.

```bash
blender --background --factory-startup --python tests/make_fake_capture.py -- test_out
```

```bash
blender --background --factory-startup --python trackshot_blender.py -- build test_out/capture.zip test_out/built.blend
```

```bash
blender --background --factory-startup test_out/built.blend --python tests/check_blend.py -- test_out/manifest.json test_out
```
