"""Multi-person fall tracking logic, driven by synthetic detections (no model download needed)."""

import unittest

import numpy as np

from sentrycare.body_tracking import (CHECKING_DOWN, LYING, MultiPersonFallMonitor, TrackerSettings,
                                      keypoints_to_landmarks, summarize)
from sentrycare.fall_detection import CONFIRMED_FALL, NORMAL, POSSIBLE_FALL

FRAME = np.zeros((480, 640, 3), np.uint8)
NO_MODEL = object()


def standing(cx=.5, top=.15):
    """Upright COCO keypoints (normalized) and a tall box."""
    xy = np.array([[cx, top], [cx - .01, top - .01], [cx + .01, top - .01], [cx - .02, top], [cx + .02, top],
                   [cx - .06, top + .12], [cx + .06, top + .12], [cx - .08, top + .25], [cx + .08, top + .25],
                   [cx - .08, top + .35], [cx + .08, top + .35], [cx - .04, top + .40], [cx + .04, top + .40],
                   [cx - .04, top + .58], [cx + .04, top + .58], [cx - .04, top + .75], [cx + .04, top + .75]])
    box = [cx - .1, top - .03, cx + .1, top + .78]
    return box, xy, np.full(17, .9)


def lying(cx=.5, y=.85):
    """Horizontal body near the floor: head on the left, feet on the right."""
    xs = np.linspace(cx - .3, cx + .3, 17)
    xy = np.array([[xs[0], y], [xs[0], y - .01], [xs[0], y + .01], [xs[0], y - .01], [xs[0], y + .01],
                   [cx - .18, y - .03], [cx - .18, y + .03], [cx - .1, y - .05], [cx - .1, y + .05],
                   [cx - .02, y - .05], [cx - .02, y + .05], [cx + .02, y - .03], [cx + .02, y + .03],
                   [cx + .15, y - .03], [cx + .15, y + .03], [cx + .28, y - .03], [cx + .28, y + .03]])
    box = [cx - .32, y - .07, cx + .32, y + .07]
    return box, xy, np.full(17, .9)


def det(track_id, pose, conf=None):
    box, xy, kc = pose
    return (track_id, box, xy, kc if conf is None else conf, .9)


def run(monitor, script, start=0.0, step=.2):
    """script: list of detection lists, one per frame at 5 Hz. Returns per-frame people lists."""
    out = []
    for i, detections in enumerate(script):
        out.append(monitor.update(FRAME, start + i * step, detections=detections))
    return out


def monitor(**settings):
    return MultiPersonFallMonitor(1.2, settings=TrackerSettings(**settings), model=NO_MODEL)


def fall_script(track_id=1, cx=.5, after=20, lost_frames=0, new_id=None):
    frames = [[det(track_id, standing(cx))] for _ in range(5)]
    frames.append([det(track_id, standing(cx, top=.35))])          # dropping fast
    frames.append([det(track_id, standing(cx, top=.55))])
    frames += [[] for _ in range(lost_frames)]                        # detector lost the person
    frames += [[det(new_id or track_id, lying(cx))] for _ in range(after)]
    return frames


def states(history, track_id):
    return [next((p["state"] for p in people if p["track_id"] == track_id), None) for people in history]


class KeypointMappingTests(unittest.TestCase):
    def test_coco_to_mediapipe_indices(self):
        xy = np.array([[i / 20, i / 40] for i in range(17)])
        conf = np.linspace(.1, .9, 17)
        lm = keypoints_to_landmarks(xy, conf)
        self.assertEqual(len(lm), 33)
        self.assertAlmostEqual(lm[11].x, xy[5][0])     # left shoulder
        self.assertAlmostEqual(lm[24].y, xy[12][1])    # right hip
        self.assertAlmostEqual(lm[28].visibility, conf[16])
        self.assertEqual(lm[1].visibility, 0)          # MediaPipe-only landmark stays invisible
        xy[0] = [0, 0]
        self.assertEqual(keypoints_to_landmarks(xy, conf)[0].visibility, 0)   # undetected keypoint


class MultiPersonFallTests(unittest.TestCase):
    def test_fall_needs_stay_down_then_alerts_once(self):
        m = monitor(stay_down_seconds=2.0)
        history = run(m, fall_script(after=25))
        seq = states(history, 1)
        self.assertEqual(seq[0], NORMAL)
        self.assertIn(POSSIBLE_FALL, seq)
        self.assertIn(CHECKING_DOWN, seq)
        first_checking = seq.index(CHECKING_DOWN)
        first_confirmed = seq.index(CONFIRMED_FALL)
        self.assertGreaterEqual((first_confirmed - first_checking) * .2, 2.0 - 1e-6)
        alerts = [p for people in history for p in people if p["new_alert"]]
        self.assertEqual(len(alerts), 1)

    def test_fall_stays_a_fall_after_descent_evidence_expires(self):
        # Descent evidence is short-lived. Once a drop was seen, the episode must still alert and must not
        # be relabelled "Lying (no fall seen)" when that evidence expires during the stayed-down wait.
        m = monitor(stay_down_seconds=2.0)
        script = fall_script(after=45)
        history = []
        for i, detections in enumerate(script):
            history.append(m.update(FRAME, i * .2, detections=detections))
            track = m.tracks.get(1)
            if track and track.state == CHECKING_DOWN:
                track.fsm._fall_candidate_latched = False     # simulate the evidence lapsing
                track.fsm._fall_motion_until = None
        seq = states(history, 1)
        self.assertIn(CONFIRMED_FALL, seq)
        self.assertNotIn(LYING, seq[seq.index(CHECKING_DOWN):])
        self.assertEqual(seq[-1], CONFIRMED_FALL)
        self.assertEqual(sum(p["new_alert"] for people in history for p in people), 1)

    def test_getting_up_cancels_before_alert(self):
        m = monitor(stay_down_seconds=2.0)
        script = fall_script(after=8) + [[det(1, standing())] for _ in range(15)]
        history = run(m, script)
        self.assertNotIn(CONFIRMED_FALL, states(history, 1))
        self.assertFalse(any(p["new_alert"] for people in history for p in people))
        self.assertEqual(states(history, 1)[-1], NORMAL)

    def test_slow_lying_down_without_descent_is_not_a_fall(self):
        m = monitor(lying_alert_seconds=1e9)
        script = [[det(1, lying())] for _ in range(30)]               # e.g. already lying on a sofa
        history = run(m, script)
        self.assertNotIn(CONFIRMED_FALL, states(history, 1))
        self.assertNotIn(CHECKING_DOWN, states(history, 1))
        self.assertEqual(states(history, 1)[-1], LYING)
        self.assertFalse(any(p["new_alert"] for people in history for p in people))

    def test_two_people_tracked_independently(self):
        m = monitor(stay_down_seconds=1.0)
        frames = []
        for frame in fall_script(track_id=1, cx=.3, after=20):
            frames.append(frame + [det(2, standing(.75))])
        history = run(m, frames)
        self.assertIn(CONFIRMED_FALL, states(history, 1))
        self.assertTrue(all(s == NORMAL for s in states(history, 2)))
        alerts = [p["track_id"] for people in history for p in people if p["new_alert"]]
        self.assertEqual(alerts, [1])
        self.assertEqual(summarize(history[-1])["state"], CONFIRMED_FALL)
        self.assertEqual(summarize(history[-1])["people"], 2)

    def test_keypoints_lost_but_lying_box_continues_fall(self):
        m = monitor(stay_down_seconds=1.0)
        no_points = np.zeros(17)
        frames = [[det(1, standing())] for _ in range(5)]
        frames += [[det(1, standing(top=.35))], [det(1, standing(top=.55))]]
        frames += [[det(1, lying(), conf=no_points)] for _ in range(20)]   # body points unusable on the floor
        history = run(m, frames)
        self.assertIn(CONFIRMED_FALL, states(history, 1))
        self.assertTrue(any(p["signals"].get("box_lying") for people in history for p in people))

    def test_person_found_lying_too_long_is_alerted_once(self):
        m = monitor()
        history = run(m, [[det(1, lying())] for _ in range(50)])      # 10 s lying, no fall seen
        self.assertEqual(states(history, 1)[0], LYING)
        self.assertEqual(states(history, 1)[-1], CONFIRMED_FALL)
        self.assertEqual(sum(p["new_alert"] for people in history for p in people), 1)

    def test_lying_box_without_descent_never_starts_a_fall(self):
        m = monitor()
        no_points = np.zeros(17)
        history = run(m, [[det(1, lying(), conf=no_points)] for _ in range(30)])
        self.assertNotIn(CONFIRMED_FALL, states(history, 1))
        self.assertNotIn(POSSIBLE_FALL, states(history, 1))

    def test_id_switch_after_detector_loss_keeps_fall_history(self):
        m = monitor(stay_down_seconds=1.0)
        history = run(m, fall_script(track_id=1, lost_frames=4, new_id=7, after=20))
        self.assertIn(CONFIRMED_FALL, states(history, 7))
        self.assertNotIn(1, m.tracks)

    def test_disappearing_person_is_never_alerted(self):
        m = monitor(stay_down_seconds=1.0)
        frames = [[det(1, standing())] for _ in range(5)] + [[] for _ in range(30)]
        history = run(m, frames)
        self.assertFalse(any(p["new_alert"] for people in history for p in people))
        self.assertEqual(m.tracks, {})                                  # forgotten after forget_after

    def test_summary_empty(self):
        self.assertEqual(summarize([])["state"], NORMAL)


if __name__ == "__main__":
    unittest.main()
