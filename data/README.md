# Local data directory

Runtime data is stored here and is excluded from version control. Nothing in this folder is shared through Git.

| Path | Contents |
|---|---|
| `models/` | Downloaded model weights (YuNet, SFace, YOLO11n-pose) |
| `studio/` | Dashboard registry (SQLite), aligned enrolment photos, alert settings |
| `tls/` | Self-signed certificate for network mode |
| `known_faces/<name>/` | Optional source photos for bulk import (3-5 per consenting person) |
| `captured_faces/<camera-id>/` | Unknown-face snapshots from the command-line worker |
| `event_logs/<camera-id>/` | Event snapshots and CSV logs from the command-line worker |
| `logs/` | Application logs, diagnostics, supervisor status and heartbeats |
| `datasets/` | Public research datasets and their attribution |
| `evaluation/` | Evaluation manifests and reports |

Set `SENTRYCARE_DATA_DIR` to use a different location.
