# Fall detection: multi-person pipeline (YOLO11-pose)

## What changed

| | Before (MediaPipe) | Now (YOLO11-pose, default) |
|---|---|---|
| People per camera | 1 | Many, each with a stable track id (ByteTrack) |
| Body points lost mid-fall | Fall logic stalls after 0.25 s | The person box continues the fall, but only after an observed drop |
| Detector loses person on the floor | New person, history lost | Track hand-off: a nearby new track inherits the lost track's fall history |
| Lying down without falling | Could be confirmed as a fall | Shown as **Lying (no fall seen)**, never alerted |
| Sitting/bending then getting up | Confirms after 1.2 s down | **Stayed-down check**: must stay low and still 2 s after confirmation; rising cancels |
| Who fell | Name only if one face overlapped the single body | Per person: a recognised face inside that person's box |

MediaPipe remains as an automatic fallback when YOLO cannot load. Set `SENTRYCARE_POSE=mediapipe` to force it,
or `SENTRYCARE_POSE=yolo` to fail loudly instead of falling back.

## How a fall is decided (per person)

1. **YOLO11-pose** returns a box and 17 keypoints for every person. ByteTrack keeps ids stable
   (`sentrycare/bytetrack_sentrycare.yaml`, tuned for about 5 analysed frames per second and low-confidence fallen people).
2. Keypoints are mapped to the MediaPipe indices used by the existing rich heuristic in `fall_detection.py`:
   torso angle, low position, head below hips and descent speed.
3. **Descent** can come from keypoint velocity or from the person box dropping quickly (≥ 0.35 frame heights/s).
4. If keypoints are unusable, a **wide (lying) box** counts as "down" only while descent evidence is active.
   A missing pose or a lying box alone never starts or advances a fall.
5. The state machine confirms after 1.2 s of down evidence (`CHECKING_DOWN` on screen). The **stayed-down check**
   then needs 2 s more with the box centre roughly still (≤ 12 % of frame height of movement).
   If the centre rises by more than 10 %, the person got up and the fall is cancelled.
6. Then `CONFIRMED_FALL`. That person produces exactly one alert until they return to normal.

States shown on screen: Normal · Check posture · Confirming fall · **FALL** · Lying (no fall seen).

Trade-off to state in the report: someone who falls *out of view* and appears already on the floor is
shown as Lying, not alerted, because no drop was seen. A separate "lying too long" alert would cover this case.

## Performance

YOLO11n-pose on CPU takes about 110–130 ms per frame on the development laptop (PyTorch CPU). The first
frame takes about 10 s, so Studio warms the model up while it loads. Analysis is shared between cameras.

## Licence and privacy

Ultralytics (YOLO) is **AGPL-3.0**: fine for this academic project, but review before any commercial use.
Studio turns off Ultralytics analytics (`sync=False`). Weights are downloaded once to the ignored `data/models/` folder.

## Reproducing the benchmark

```powershell
.\.venv\Scripts\python.exe scripts/download_urfd_samples.py --falls 15 --adls 15
.\.venv\Scripts\python.exe scripts/benchmark_multiperson_falls.py
```

## Benchmark results (2026-11-14)

URFD, 15 fall + 15 daily-activity (ADL) clips, camera 0, 5 analysed frames/s, CPU.
"Held" repeats each clip's last frame for 4 s for both falls and ADLs (see the script docstring).
"Test" excludes clips 01–02, which were used during development (13 + 13 clips).

| Pipeline | View | Falls caught (all) | Falls caught (test) | False alarms (all) | False alarms (test) |
|---|---|---|---|---|---|
| MediaPipe (before) | raw | 4/15 | 3/13 | 0/15 | 0/13 |
| MediaPipe (before) | held | 8/15 (53 %) | 7/13 (54 %) | 1/15 | 1/13 |
| **YOLO multi-person (now)** | raw | 0/15 | 0/13 | 0/15 | 0/13 |
| **YOLO multi-person (now)** | held | **11/15 (73 %)** | **9/13 (69 %)** | **0/15** | **0/13** |

How to read this:

- With the person given time on the floor (held), the new pipeline catches more falls (9 vs 7 of 13 test falls)
  and raised no false alarms (MediaPipe raised one).
- **Raw 0/15 is by design, not a failure:** the stayed-down check needs about 3.2 s on the floor (1.2 s confirmation + 2 s still),
  and URFD clips end sooner. The cost is about 2 s more alert latency than MediaPipe, traded for fewer false alarms.
- Still missed by YOLO: fall-05, 09, 12, 13. Further tuning should use separate development clips, not these test clips.
- These are small numbers on one public dataset with a single camera angle: indicative, not validated accuracy.
  Clips are not split by subject. Real rooms, lighting and camera heights will differ.
- Median processing per frame: YOLO about 85–115 ms, MediaPipe about 40–60 ms.

Per-clip results: `data/evaluation/multiperson_fall_comparison.json` (local, ignored by Git).
