# Evaluation

This document reports how SentryCare was tested, the results, and how to reproduce them.
All results come from a laptop CPU at the live system's analysis rate of **5 frames per second**.

## 1. Fall detection

### Dataset

[UR Fall Detection Dataset (URFD)](https://fenix.ur.edu.pl/~mkepski/ds/uf.html), camera 0 RGB sequences with their
official frame timestamps: **15 fall clips** (`fall-01`–`fall-15`) and **15 activities of daily living**
(`adl-01`–`adl-15`), which include walking, sitting, crouching and lying down on a bed.

- **Test set:** clips 03–15 of each type (13 + 13). Clips 01–02 were used while developing the method and are reported
  separately as "all".
- URFD clips end roughly 1–2 s after the fall. This is too short for a confirmation window plus a stayed-down check, so
  results are reported in two views:
  - **raw:** the clip as recorded.
  - **held:** the last frame is repeated for 4 s, representing the person remaining in place. This is applied identically
    to fall and daily-activity clips, so people who end lying on a bed are tested for false alarms in the same way.

### Results

| Pipeline | View | Falls detected (test) | False alarms (test) | Falls detected (all) | False alarms (all) |
|---|---|---|---|---|---|
| MediaPipe, single person (previous) | raw | 3 / 13 | 0 / 13 | 4 / 15 | 0 / 15 |
| MediaPipe, single person (previous) | held | 7 / 13 (54 %) | 1 / 13 | 8 / 15 (53 %) | 1 / 15 |
| YOLO11-pose, multi-person (current) | raw | 0 / 13 | 0 / 13 | 0 / 15 | 0 / 15 |
| **YOLO11-pose, multi-person (current)** | **held** | **9 / 13 (69 %)** | **0 / 13** | **11 / 15 (73 %)** | **0 / 15** |

Median processing time per frame: YOLO11-pose about 85–115 ms; MediaPipe about 40–60 ms.

### Interpretation

- Given time on the floor, the current pipeline detects more falls than the previous one and raised no false alarms.
- The current pipeline detects no falls in the *raw* view by design. Its stayed-down check needs about 3.2 s of the person
  remaining down (1.2 s confirmation + 2 s), which is longer than the recorded clips last after the fall. This adds about 2 s of alert
  latency in exchange for fewer false alarms on sitting, bending and similar movements.
- Four test falls are still missed (`fall-05`, `fall-09`, `fall-12`, `fall-13`).
- **Limitations:** this is a small sample from one dataset with one camera viewpoint. Clips are not separated by
  subject, and real rooms, lighting and camera heights differ. These figures are indicative and are not a validated accuracy claim.

### Reproduce

```powershell
.\.venv\Scripts\python.exe scripts\download_urfd_samples.py --falls 15 --adls 15
.\.venv\Scripts\python.exe scripts\benchmark_multiperson_falls.py
```

Per-clip results, including first-alert times and people per clip, are written to
`data/evaluation/multiperson_fall_comparison.json`. URFD is licensed CC BY-NC-SA 4.0 for non-commercial use; the
download script records the attribution alongside the data.

## 2. Face recognition

Face recognition uses pretrained YuNet and SFace models; no model training is involved. The checks below verify the
matching logic and thresholds on locally enrolled profiles. They are **not** a population-scale benchmark.

| Check | Result |
|---|---|
| Leave-one-photo-out matching: each photo of a complete profile matched against that person's other photos | 9 / 9 correctly identified |
| Unknown-person rejection: each person's photos tested with that person removed from the gallery | 9 / 9 correctly rejected as *Unknown* |
| Enrolment quality checks on 15 supplied photos | 12 accepted; 3 rejected (face too small, or several faces) |

Recognition requires a cosine similarity of at least 0.45, a margin of 0.06 over the next identity, and two consistent
checks before a name is shown. A larger evaluation with held-out, consented images under varied lighting and angles
is planned future work.

## 3. Automated tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

103 tests run offline in about 10 seconds without a camera. They cover:

| Area | Examples |
|---|---|
| Fall logic | Confirmation and recovery timing, missing poses, stayed-down cancellation, lying without descent, two independent people, track hand-off after detector loss |
| Face pipeline | Enrolment quality rejection, group-photo rejection, identity confirmation, unknown handling |
| Camera sources | Latest-frame analysis, busy-worker retry, stream reconnect and release, URL validation and credential redaction |
| Alerts | Broadcast, acknowledgement, bounded history, failed-push reporting, ntfy privacy (no names unless enabled) |
| Network mode | PIN login and lockout, host checks, host-only settings, HTTPS listener start and stop |
| Supervisor | Worker restarts, watchdog, shutdown |
| Repository privacy | Photos, biometric data, databases, keys and camera configurations cannot be committed |

## 4. End-to-end checks

- **Network mode:** HTTPS listener, PIN login, phone-camera upload, annotated MJPEG stream and `/ping` test page
  checked over the laptop's Wi-Fi address. A phone camera streamed to the dashboard successfully.
- **Alerts:** a simulated fall raised the alert on two dashboards at once; acknowledging on one cleared both.
- **Dashboard:** all pages render without layout overflow or script errors at desktop (1440 px) and phone (390 px) widths.
