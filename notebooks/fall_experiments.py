"""Helper functions for the fall-detection experiments notebook.

Running the pose models is the slow part of each experiment. These helpers run YOLO11-pose and MediaPipe
over every clip ONCE, save their raw outputs (boxes and keypoints) to a cache file, and then *replay* the
fall logic from that cache. Changing a setting, such as the stayed-down time, then takes seconds instead
of minutes, and every experiment compares settings on exactly the same model outputs.
"""

import csv
import json
import time
from pathlib import Path

import numpy as np

from sentrycare.body_tracking import MultiPersonFallMonitor, Point, TrackerSettings, _Model
from sentrycare.fall_detection import CONFIRMED_FALL, FallStateMachine, studio_fall_config
from sentrycare.recordings import recording_frames

CADENCE = 0.2   # seconds between analysed frames = the live system's 5 frames per second
HOLD = 4.0      # seconds the last frame is repeated in the "held" view


# ----------------------------------------------------------------------------------------------
# Dataset
# ----------------------------------------------------------------------------------------------
def load_manifest(manifest_path):
    """Read the clip list written by scripts/download_urfd_samples.py."""
    manifest_path = Path(manifest_path).resolve()
    with manifest_path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    clips = []
    for row in rows:
        clips.append({"clip": Path(row["path"]).name.replace("-cam0-rgb", ""), "label": row["label"],
                      "split": row.get("split", ""), "path": manifest_path.parent / row["path"],
                      "timing": manifest_path.parent / row["timestamps"] if row.get("timestamps") else None})
    return clips


def load_fall_onsets(labels_csv, clips):
    """Timestamp (s) of the first frame labelled 'falling' (URFD label 0), per fall clip."""
    first_falling_frame = {}
    with Path(labels_csv).open(newline="", encoding="utf-8") as stream:
        for row in csv.reader(stream):
            name, frame, label = row[0], int(row[1]), int(row[2])
            if label == 0 and name not in first_falling_frame:
                first_falling_frame[name] = frame
    onsets = {}
    for clip in clips:
        if clip["label"] != "fall" or clip["clip"] not in first_falling_frame or clip["timing"] is None:
            continue
        with Path(clip["timing"]).open(newline="", encoding="utf-8") as stream:
            times = {int(r[0]): float(r[1]) / 1000 for r in csv.reader(stream) if r and r[0].isdigit()}
        # Same clock as recording_frames(): the timing CSV's milliseconds / 1000.
        onsets[clip["clip"]] = times[first_falling_frame[clip["clip"]]]
    return onsets


def sampled_frames(clip, cadence=CADENCE):
    """Frames of a clip at the live analysis rate, with their real timestamps in seconds."""
    last = None
    for frame, timestamp in recording_frames(clip["path"], clip["timing"]):
        if last is None or timestamp - last >= cadence - 1e-6:
            last = timestamp
            yield frame, timestamp


# ----------------------------------------------------------------------------------------------
# Run the models once and cache their outputs
# ----------------------------------------------------------------------------------------------
def run_models_on_clip(clip, yolo_model, mediapipe):
    """Run YOLO11-pose (with ByteTrack) and MediaPipe Pose on one clip; return their raw outputs."""
    import cv2
    tracker = MultiPersonFallMonitor(1.2, model=yolo_model)
    tracker.reset()
    record = {"clip": clip["clip"], "label": clip["label"], "split": clip["split"],
              "times": [], "shape": None, "yolo": [], "mediapipe": [], "yolo_ms": [], "mediapipe_ms": []}
    frames = list(sampled_frames(clip))
    # The "held" view: the last frame is fed again for HOLD seconds. The models are re-run on it,
    # exactly as the live system would, so trackers and smoothing behave as they really do.
    held = [(frames[-1][0], None)] * round(HOLD / CADENCE)
    with mediapipe.solutions.pose.Pose(model_complexity=1, smooth_landmarks=True,
                                       min_detection_confidence=.55, min_tracking_confidence=.55) as pose:
        for frame, timestamp in frames + held:
            record["shape"] = list(frame.shape[:2])
            if timestamp is not None:
                record["times"].append(round(timestamp, 4))
            start = time.perf_counter()
            detections = tracker.detect(frame)
            record["yolo_ms"].append((time.perf_counter() - start) * 1000)
            record["yolo"].append([[int(i), [float(v) for v in box], np.asarray(kxy).round(4).tolist(),
                                    None if kc is None else np.asarray(kc).round(3).tolist(), round(score, 3)]
                                   for i, box, kxy, kc, score in detections])
            start = time.perf_counter()
            result = pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            record["mediapipe_ms"].append((time.perf_counter() - start) * 1000)
            landmarks = result.pose_landmarks.landmark if result.pose_landmarks else None
            record["mediapipe"].append(None if landmarks is None else
                                       [[round(p.x, 4), round(p.y, 4), round(p.visibility, 3)] for p in landmarks])
    return record


def build_cache(clips, cache_path, refresh=False):
    """Load cached model outputs, computing any missing clips (about 5 s per clip on a laptop CPU)."""
    cache_path = Path(cache_path)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() and not refresh else {}
    missing = [c for c in clips if c["clip"] not in cache]
    if missing:
        from sentrycare.studio import _import_mediapipe
        mediapipe, yolo_model = _import_mediapipe(), _Model.load()
        for i, clip in enumerate(missing, 1):
            cache[clip["clip"]] = run_models_on_clip(clip, yolo_model, mediapipe)
            print(f"  cached {clip['clip']:8s} ({i}/{len(missing)})", flush=True)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
    return cache


# ----------------------------------------------------------------------------------------------
# Replay the fall logic from cached outputs
# ----------------------------------------------------------------------------------------------
def _frames_with_hold(record, hold):
    """(time, index) pairs. Entries after the clip's own frames are the cached 'held' frames."""
    times = record["times"]
    pairs = [(t, i) for i, t in enumerate(times)]
    extra = min(round(hold / CADENCE), len(record["yolo"]) - len(times))
    pairs += [(times[-1] + CADENCE * (k + 1), len(times) + k) for k in range(extra)]
    return pairs


def replay_yolo(record, settings=None, hold=HOLD, confirm_seconds=1.2):
    """Replay the multi-person fall logic. Returns (first alert time or None, per-frame timeline)."""
    monitor = MultiPersonFallMonitor(confirm_seconds, settings=settings or TrackerSettings(), model=object())
    frame = np.zeros((*record["shape"], 3), np.uint8)
    alert, timeline = None, []
    for t, i in _frames_with_hold(record, hold):
        detections = [(tid, box, np.array(kxy), None if kc is None else np.array(kc), score)
                      for tid, box, kxy, kc, score in record["yolo"][i]]
        people = monitor.update(frame, t, detections=detections)
        if alert is None and any(p["new_alert"] for p in people):
            alert = t
        worst = max(people, key=lambda p: p["score"], default=None)
        timeline.append({"time": t, "people": len(people),
                         "state": max((p["state"] for p in people), key=_rank, default="NORMAL"),
                         "score": worst["score"] if worst else 0.0})
    return alert, timeline


def replay_mediapipe(record, hold=HOLD):
    """Replay the previous single-person MediaPipe pipeline. Returns the first alert time or None."""
    fsm = FallStateMachine(studio_fall_config())
    h, w = record["shape"]
    for t, i in _frames_with_hold(record, hold):
        landmarks = record["mediapipe"][i]
        points = None if landmarks is None else [Point(*p) for p in landmarks]
        if fsm.update_rich(points, h, w, t) == CONFIRMED_FALL:
            return t
    return None


_RANKS = {"NORMAL": 0, "LYING": 1, "POSSIBLE_FALL": 2, "CHECKING_DOWN": 3, "CONFIRMED_FALL": 4}


def _rank(state):
    return _RANKS.get(state, 0)


# ----------------------------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------------------------
def classification_metrics(rows, predicted_key):
    """Clip-level confusion matrix and standard metrics. A clip is 'positive' if any alert was raised."""
    tp = sum(1 for r in rows if r["label"] == "fall" and r[predicted_key] is not None)
    fn = sum(1 for r in rows if r["label"] == "fall" and r[predicted_key] is None)
    fp = sum(1 for r in rows if r["label"] == "adl" and r[predicted_key] is not None)
    tn = sum(1 for r in rows if r["label"] == "adl" and r[predicted_key] is None)
    div = lambda a, b: round(a / b, 3) if b else None
    precision, recall = div(tp, tp + fp), div(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 3) if precision and recall else 0.0
    return {"TP": tp, "FN": fn, "FP": fp, "TN": tn, "recall": recall, "precision": precision, "f1": f1,
            "specificity": div(tn, tn + fp), "accuracy": div(tp + tn, tp + tn + fp + fn)}
