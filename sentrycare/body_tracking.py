"""Multi-person body tracking for fall monitoring.

YOLO11-pose finds every person and ByteTrack keeps a stable id per person. Each tracked person gets
an independent ``FallStateMachine`` (the existing rich heuristic), fed with YOLO keypoints mapped
to MediaPipe landmark indices. Two additions target known failure modes:

* Box continuation: when keypoints drop out mid-fall but the person box is lying flat *after*
  descent was observed, the fall keeps progressing. Missing keypoints alone never start a fall.
* Stayed-down check: an alert requires the person to stay low and roughly still for
  ``stay_down_seconds``; quickly getting back up cancels it.

``Ultralytics`` is AGPL-3.0 licensed: fine for this academic project; review before any product use.
"""

from collections import deque
from dataclasses import dataclass, replace
import logging
import math
import os
from pathlib import Path
import threading

import numpy as np

from .fall_detection import (CONFIRMED_FALL, NORMAL, POSSIBLE_FALL, FallStateMachine,
                             full_body_features, studio_fall_config)
from .paths import DATA_DIR

logger = logging.getLogger("SentryCare")

# COCO-17 keypoint index -> MediaPipe-33 landmark index used by fall_detection.
COCO_TO_MP = {0: 0, 1: 2, 2: 5, 3: 7, 4: 8, 5: 11, 6: 12, 7: 13, 8: 14, 9: 15, 10: 16,
              11: 23, 12: 24, 13: 25, 14: 26, 15: 27, 16: 28}
COCO_LINES = [(5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12), (11, 12), (11, 13), (13, 15),
              (12, 14), (14, 16)]
MODEL_NAME = "yolo11n-pose.pt"
TRACKER_CONFIG = str(Path(__file__).with_name("bytetrack_sentrycare.yaml"))
CHECKING_DOWN = "CHECKING_DOWN"     # fall logic confirmed; waiting for the stayed-down check
LYING = "LYING"                     # down posture without an observed fall: shown, never alerted


@dataclass
class TrackerSettings:
    stay_down_seconds: float = 2.0      # must stay low/still this long after the fall is confirmed
    still_tolerance: float = 0.12       # max box-centre travel (fraction of frame height) while down
    rise_tolerance: float = 0.10        # box centre rising this much cancels (person got up)
    lying_aspect: float = 1.15          # box width/height above this counts as lying
    box_drop_speed: float = 0.35        # box-centre descent (frame heights/s) counted as fall motion
    forget_after: float = 3.0           # drop a track unseen for this long
    min_confidence: float = 0.2         # fallen people often score low (odd pose, partly out of frame)
    handoff_distance: float = 0.35      # max centre distance (frame fraction) to inherit a lost track
    keypoint_confidence: float = 0.5


class Point:
    __slots__ = ("x", "y", "visibility")

    def __init__(self, x, y, visibility):
        self.x, self.y, self.visibility = x, y, visibility


def keypoints_to_landmarks(xyn, conf):
    """Map 17 normalized COCO keypoints to a 33-entry MediaPipe-style landmark list."""
    landmarks = [Point(0.0, 0.0, 0.0) for _ in range(33)]
    for coco, mp_index in COCO_TO_MP.items():
        x, y = float(xyn[coco][0]), float(xyn[coco][1])
        c = float(conf[coco]) if conf is not None else 1.0
        if x == 0 and y == 0:          # Ultralytics reports undetected keypoints as (0, 0)
            c = 0.0
        landmarks[mp_index] = Point(x, y, c)
    return landmarks


class PersonTrack:
    def __init__(self, track_id, config, settings):
        self.id = track_id
        self.fsm = FallStateMachine(config)
        self.settings = settings
        self.last_seen = None
        self.last_center = None              # (x, y) normalized, for hand-off after an id switch
        self.centers = deque(maxlen=6)        # (time, centre_y normalized)
        self.down_since = None                # when the FSM reached CONFIRMED_FALL
        self.down_anchor = None               # box centre when confirmation happened
        self.alerted = False
        self.state = NORMAL
        self.lying = False
        self.descent_seen = False             # a drop was observed during the current down episode

    def update(self, box_norm, landmarks, frame_h, frame_w, now):
        s = self.settings
        x1, y1, x2, y2 = box_norm
        width, height = (x2 - x1) * frame_w, (y2 - y1) * frame_h
        self.lying = height > 0 and width / height >= s.lying_aspect
        center_y = (y1 + y2) / 2
        self.last_center = ((x1 + x2) / 2, center_y)
        self.centers.append((now, center_y))
        if len(self.centers) >= 2:
            (t0, c0), (t1, c1) = self.centers[0], self.centers[-1]
            if t1 - t0 > 0 and (c1 - c0) / (t1 - t0) >= s.box_drop_speed:
                self.fsm.note_fall_motion(now)
        # Exactly one state-machine update per frame: keypoints when usable, otherwise the box.
        if landmarks is not None and full_body_features(landmarks, frame_h, frame_w, self.fsm.config) is not None:
            state = self.fsm.update_rich(landmarks, frame_h, frame_w, now)
        else:
            state = self.fsm.update_box_evidence(self.lying, now)
        self.last_seen = now
        return self._stayed_down(state, center_y, now)

    def _stayed_down(self, fsm_state, center_y, now):
        s = self.settings
        # Remember for the whole episode whether a descent was seen. Descent evidence only lasts a
        # couple of seconds, but a fall that was seen happening stays a fall until the person recovers.
        if fsm_state == NORMAL:
            self.descent_seen = False
        elif self.fsm.fall_motion_active_at(now):
            self.descent_seen = True
        if fsm_state != NORMAL and not self.descent_seen:
            # Down/horizontal posture but no descent was observed (e.g. already lying on a sofa, or
            # entered view on the floor). Not alerted as a fall; shown as LYING for review.
            self.down_since = self.down_anchor = None
            self.state = LYING
            return self.state
        if fsm_state != CONFIRMED_FALL:
            self.down_since = self.down_anchor = None
            self.state = fsm_state
            if fsm_state == NORMAL:
                self.alerted = False
            return self.state
        if self.down_since is None:
            self.down_since, self.down_anchor = now, center_y
        if self.down_anchor - center_y > s.rise_tolerance:      # box centre rose: getting up
            self.fsm.reset()
            self.down_since = self.down_anchor = None
            self.state = NORMAL
            self.alerted = False
            return self.state
        if abs(center_y - self.down_anchor) > s.still_tolerance:  # big movement: restart the wait
            self.down_since, self.down_anchor = now, center_y
        self.state = CONFIRMED_FALL if now - self.down_since >= s.stay_down_seconds else CHECKING_DOWN
        return self.state

    def missing(self, now):
        """Called when the tracker did not report this person in a frame."""
        if self.state in (CONFIRMED_FALL, CHECKING_DOWN):
            return self.state                       # keep showing; never escalate on absence
        return self.fsm.update(False, now, pose_missing=True)


class _Model:
    """Lazily loads YOLO11-pose once; each camera session gets its own tracker state."""
    lock = threading.Lock()
    weights = None

    @classmethod
    def path(cls):
        return DATA_DIR / "models" / MODEL_NAME

    @classmethod
    def load(cls):
        os.environ.setdefault("YOLO_VERBOSE", "False")
        from ultralytics import YOLO, settings as yolo_settings
        try:
            yolo_settings.update({"sync": False})     # no analytics or crash reports leave this computer
        except Exception:
            pass
        path = cls.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            logger.info("Downloading %s (about 6 MB) to %s", MODEL_NAME, path)
            # Ultralytics downloads official assets to the given path when missing.
        return YOLO(str(path), task="pose")


class MultiPersonFallMonitor:
    """Per-camera-session multi-person fall monitor."""

    def __init__(self, confirm_seconds=1.2, settings=None, model=None):
        self.settings = settings or TrackerSettings()
        self.config = studio_fall_config(confirm_seconds)
        self.model = model or _Model.load()
        self.tracks = {}

    def reset(self):
        self.tracks.clear()
        predictor = getattr(self.model, "predictor", None)
        for tracker in getattr(predictor, "trackers", None) or []:
            tracker.reset()

    def detect(self, frame):
        """Return [(track_id, box_xyxy_norm, keypoints_xyn(17x2), keypoint_conf(17), score)]."""
        h, w = frame.shape[:2]
        results = self.model.track(frame, persist=True, tracker=TRACKER_CONFIG, conf=self.settings.min_confidence,
                                   imgsz=640, verbose=False, classes=[0],
                                   project=str(DATA_DIR / "logs" / "yolo"), name="track", exist_ok=True)
        out = []
        if not results:
            return out
        r = results[0]
        if r.boxes is None or r.boxes.id is None or r.keypoints is None:
            return out
        ids = r.boxes.id.int().tolist()
        boxes = r.boxes.xyxyn.cpu().numpy()
        scores = r.boxes.conf.cpu().numpy()
        kxy = r.keypoints.xyn.cpu().numpy()
        kconf = r.keypoints.conf.cpu().numpy() if r.keypoints.conf is not None else None
        for i, track_id in enumerate(ids):
            out.append((track_id, boxes[i].tolist(), kxy[i], None if kconf is None else kconf[i], float(scores[i])))
        return out

    def update(self, frame, now, detections=None):
        h, w = frame.shape[:2]
        detections = self.detect(frame) if detections is None else detections
        seen, people = set(), []
        for track_id, box, kxy, kconf, score in detections:
            track = self.tracks.get(track_id)
            if track is None:
                track = self._handoff(track_id, box, seen, now) or PersonTrack(track_id, self.config, self.settings)
                self.tracks[track_id] = track
            landmarks = keypoints_to_landmarks(kxy, kconf)
            state = track.update(box, landmarks, h, w, now)
            seen.add(track_id)
            people.append({
                "track_id": track_id, "box": [round(v, 4) for v in box], "confidence": round(score, 2),
                "state": state, "pose_usable": track.fsm.last_pose_usable, "lying": track.lying,
                "score": round(track.fsm.last_score, 2), "signals": track.fsm.last_breakdown,
                "keypoints": [[round(float(kxy[k][0]), 4), round(float(kxy[k][1]), 4),
                               round(float(kconf[k]) if kconf is not None else 1.0, 2)] for k in range(17)],
                "new_alert": False})
            if state == CONFIRMED_FALL and not track.alerted:
                track.alerted = True
                people[-1]["new_alert"] = True
        for track_id in list(self.tracks):
            if track_id in seen:
                continue
            track = self.tracks[track_id]
            if track.last_seen is not None and now - track.last_seen > self.settings.forget_after:
                del self.tracks[track_id]
            else:
                track.missing(now)
        return people


    def _handoff(self, track_id, box, seen, now):
        """A person who falls often changes box shape so much that the tracker issues a new id.

        Give the new id the history of a nearby person who just disappeared while a fall was in
        progress, so descent evidence is not lost. Only tracks unseen in this frame qualify.
        """
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        best, best_distance = None, self.settings.handoff_distance
        for old_id, old in self.tracks.items():
            if old_id in seen or old.last_center is None or now - (old.last_seen or now) > self.settings.forget_after:
                continue
            if old.fsm.state == NORMAL and not old.fsm.fall_motion_active_at(now):
                continue
            distance = math.hypot(cx - old.last_center[0], cy - old.last_center[1])
            if distance <= best_distance:
                best, best_distance = old_id, distance
        if best is None:
            return None
        track = self.tracks.pop(best)
        track.id = track_id
        return track


RANK = {NORMAL: 0, LYING: 0.5, POSSIBLE_FALL: 1, CHECKING_DOWN: 2, CONFIRMED_FALL: 3}


def summarize(people):
    """Overall state for one camera: the most severe person."""
    if not people:
        return {"state": NORMAL, "pose_usable": False, "people": 0}
    worst = max(people, key=lambda p: RANK.get(p["state"], 0))
    return {"state": worst["state"], "pose_usable": any(p["pose_usable"] for p in people), "people": len(people),
            "score": worst["score"], "signals": worst["signals"]}


def yolo_available():
    try:
        import ultralytics  # noqa: F401
        return True
    except Exception:
        return False
