"""Compare the MediaPipe single-person pipeline with the YOLO11-pose multi-person pipeline.

Both run at Studio cadence (5 analysed frames per second) on the same labelled URFD clips.

Two views are reported, applied identically to fall and daily-activity (ADL) clips:
* raw  - exactly the frames in the clip.
* held - the last frame is repeated for ``--hold`` seconds afterwards. URFD clips end roughly 1-2 s
         after impact, too soon for a confirmation window plus a stayed-down check; holding models the
         person remaining where they are. ADL clips are held the same way, so people who end on a bed
         or chair are tested for false alarms equally.

This is an indicative comparison on a public research dataset, not a validated accuracy claim:
clips are not split by subject, and clips 01-02 were used during development (split "dev").
"""

import argparse
import csv
import json
from pathlib import Path
import time

import cv2

from sentrycare.body_tracking import MultiPersonFallMonitor, _Model
from sentrycare.fall_detection import CONFIRMED_FALL, FallStateMachine, studio_fall_config
from sentrycare.recordings import recording_frames


def sampled(path, timing, cadence):
    last = None
    for frame, timestamp in recording_frames(path, timing):
        if last is None or timestamp - last >= cadence - 1e-6:
            last = timestamp
            yield frame, timestamp


def run_clip(path, timing, model, mp, cadence, hold):
    frames = list(sampled(path, timing, cadence))
    if not frames:
        raise ValueError(f"No frames in {path}")
    yolo = MultiPersonFallMonitor(1.2, model=model)
    yolo.reset()
    fsm = FallStateMachine(studio_fall_config())
    out = {"frames": len(frames), "yolo": {"raw": None, "held": None}, "mediapipe": {"raw": None, "held": None},
           "max_people": 0, "yolo_ms": [], "mp_ms": []}
    last_frame, last_time = frames[-1]
    extra = [(last_frame, last_time + cadence * (i + 1)) for i in range(round(hold / cadence))]
    with mp.solutions.pose.Pose(model_complexity=1, smooth_landmarks=True,
                                min_detection_confidence=.55, min_tracking_confidence=.55) as pose:
        for index, (frame, timestamp) in enumerate(frames + extra):
            phase = "raw" if index < len(frames) else "held"
            t = time.perf_counter()
            people = yolo.update(frame, timestamp)
            out["yolo_ms"].append((time.perf_counter() - t) * 1000)
            out["max_people"] = max(out["max_people"], len(people))
            t = time.perf_counter()
            result = pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            state = fsm.update_rich(result.pose_landmarks.landmark if result.pose_landmarks else None,
                                    *frame.shape[:2], timestamp)
            out["mp_ms"].append((time.perf_counter() - t) * 1000)
            if any(p["new_alert"] for p in people):
                for key in ("raw", "held") if phase == "raw" else ("held",):
                    if out["yolo"][key] is None:
                        out["yolo"][key] = round(timestamp, 2)
            if state == CONFIRMED_FALL:
                for key in ("raw", "held") if phase == "raw" else ("held",):
                    if out["mediapipe"][key] is None:
                        out["mediapipe"][key] = round(timestamp, 2)
    return out


def confusion(clips, method, view, split=None):
    counts = {"tp": 0, "fn": 0, "fp": 0, "tn": 0}
    for clip in clips:
        if split and clip["split"] != split:
            continue
        actual = clip["label"] == "fall"
        predicted = clip[method][view] is not None
        counts[("tp" if predicted else "fn") if actual else ("fp" if predicted else "tn")] += 1
    falls, adls = counts["tp"] + counts["fn"], counts["fp"] + counts["tn"]
    counts["recall"] = round(counts["tp"] / falls, 2) if falls else None
    counts["false_alarm_rate"] = round(counts["fp"] / adls, 2) if adls else None
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", nargs="?", default="data/evaluation/urfd_extended.csv")
    parser.add_argument("--output", default="data/evaluation/multiperson_fall_comparison.json")
    parser.add_argument("--cadence", type=float, default=.2, help="Seconds between analysed frames (Studio: 0.2)")
    parser.add_argument("--hold", type=float, default=4.0, help="Seconds to hold the last frame for the 'held' view")
    args = parser.parse_args()
    cv2.setNumThreads(2)
    from sentrycare.studio import _import_mediapipe
    mp = _import_mediapipe()
    model = _Model.load()
    manifest = Path(args.manifest).resolve()
    with manifest.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    clips = []
    for row in rows:
        path = manifest.parent / row["path"]
        timing = manifest.parent / row["timestamps"] if row.get("timestamps") else None
        result = run_clip(path, timing, model, mp, args.cadence, args.hold)
        yolo_ms, mp_ms = sorted(result.pop("yolo_ms")), sorted(result.pop("mp_ms"))
        clip = {"clip": path.name, "label": row["label"], "split": row.get("split", ""), **result,
                "yolo_median_ms": round(yolo_ms[len(yolo_ms) // 2]), "mp_median_ms": round(mp_ms[len(mp_ms) // 2])}
        clips.append(clip)
        print(json.dumps(clip), flush=True)
    summary = {}
    for method in ("mediapipe", "yolo"):
        for view in ("raw", "held"):
            summary[f"{method}/{view}"] = {"all": confusion(clips, method, view),
                                           "test_only": confusion(clips, method, view, "test")}
    report = {"protocol": __doc__.strip(), "cadence_s": args.cadence, "hold_s": args.hold,
              "manifest": manifest.name, "clips": clips, "summary": summary}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=1), flush=True)


if __name__ == "__main__":
    main()
