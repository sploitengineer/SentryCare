# Installation

These steps set up SentryCare on a fresh Windows 10/11 machine. Models, photos, datasets and camera settings are
local files and are not part of the repository, so each machine prepares its own.

## 1. Prerequisites

- **Python 3.11 or 3.12.** Python 3.13 is not supported because MediaPipe 0.10.21 has no build for it.
- **Git**
- About **3 GB** of free disk space (PyTorch, TensorFlow, OpenCV and model weights)
- Internet access for the first installation and model download
- Optional: [uv](https://docs.astral.sh/uv/getting-started/installation/) for the automated setup script

## 2. Get the code

```powershell
git clone https://github.com/sploitengineer/facial_reco_cctv.git
cd facial_reco_cctv
```

## 3. Install dependencies

**Automated (uv):**

```powershell
.\scripts\setup.ps1
```

The script installs Python 3.12 locally under `.runtime/` if needed, creates `.venv/`, installs the dependencies and
the `sentrycare` package, creates `configs/cameras.json` from the example, and runs the environment check.

**Standard Python:**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
Copy-Item configs/cameras.example.json configs/cameras.json
```

On Linux or macOS, use `python3.12 -m venv .venv` and `.venv/bin/python`. On Linux, add
`--extra-index-url https://download.pytorch.org/whl/cpu` to the first `pip install` to get the smaller CPU-only PyTorch build.

## 4. Check the environment

```powershell
.\.venv\Scripts\python.exe -m sentrycare.doctor --camera 0
```

**Expected:** all checks pass, including `webcam_inference`. If the webcam check fails, close other apps that use the camera
(video calls, other browser tabs) and check *Windows Settings → Privacy & security → Camera*. If your camera is not device
`0`, use another index.

## 5. Start SentryCare

Double-click **`Start SentryCare.cmd`**, or run:

```powershell
.\.venv\Scripts\python.exe -m sentrycare.web --open-browser
```

Open **http://127.0.0.1:8765**. On the first start, SentryCare downloads its model weights into `data/models/`:
YuNet, SFace and YOLO11n-pose, about 50 MB in total. Wait until the top bar shows **Models ready**.
Press **Ctrl+C** in the terminal to stop the server.

## 6. Run the tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

All tests should pass. They run offline and do not open a camera.

## Troubleshooting

| Problem | Solution |
|---|---|
| `setup.ps1` is blocked by PowerShell | Use the *Standard Python* commands, or run `Set-ExecutionPolicy -Scope Process Bypass` first. |
| *Model error* in the top bar | Check internet access for the first model download, then restart the server. |
| *Refresh the page before making changes* | The server was restarted. Reload the browser page to get a new session token. |
| Camera busy or black | Close other apps using the webcam, or pick another device in the camera selector. |
| Body tracking shows *MediaPipe · single person* | YOLO could not load. Check that `ultralytics` and `torch` are installed, then restart. |
| A phone cannot open the link | See [CAMERA_NETWORK.md](CAMERA_NETWORK.md#phone-as-a-camera-browser-no-app) and use the `/ping` test link. |
