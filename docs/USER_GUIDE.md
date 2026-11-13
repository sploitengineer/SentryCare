# User guide

This guide covers everyday use of the SentryCare dashboard. For setup, see [INSTALLATION.md](INSTALLATION.md).
For phones, IP cameras and alerts, see [CAMERA_NETWORK.md](CAMERA_NETWORK.md).

## Starting

Double-click **`Start SentryCare.cmd`**, or run `.\.venv\Scripts\python.exe -m sentrycare.web --open-browser`, then
open **http://127.0.0.1:8765**. Keep the terminal window open while using the dashboard. Wait for **Models ready** in the top bar.

## 1. Enrol a person

1. Open **Enroll a person**. Type the person's name and click **Create profile**, or select an existing profile.
2. Click **Start camera** and allow camera access. Use good, even lighting with only that person in view.
3. Choose an angle (**Front**, **Left**, **Right**, **Slightly up** or **Slightly down**) and click **Capture photo**.
   Turn the head slightly each time; a full side profile is not needed.
4. Save at least **three** clear photos. Five varied angles give the best results. You can also click **Upload photos**.
5. Click **Build profile**. This computes the person's face features and saves them locally. The profile becomes
   active immediately.

Every photo is checked before it is saved. A photo is rejected, with the reason shown, if it contains no face or
several faces, if the face is too small, or if it is blurry, too dark or overexposed. A profile shows **Update needed** after new
photos are added; build it again to include them.

## 2. Manage profiles

**Saved profiles** lists each person with their photo count and status:

| Status | Meaning |
|---|---|
| Add photos | Fewer than three accepted photos |
| Build needed | Enough photos, but features not yet built |
| Update needed | New photos added since the last build |
| Ready | Active in recognition |

Open a profile to view, add or remove photos, or delete the whole profile. Removing photos also removes their stored
features. A profile with fewer than three built photos is no longer recognised.

## 3. Live monitoring

Open **Live monitoring** and click **Start camera**.

- **Faces:** each detected face is labelled with a name, **Unknown**, or **Checking** while the identity is being
  confirmed. A name appears only after two consistent matches. The similarity value is a raw score, not a percentage of accuracy.
- **Bodies:** each person gets a numbered dashed box and skeleton coloured by state:

| State | Meaning |
|---|---|
| Normal | Standing, sitting or moving normally |
| Check posture | A possible fall has started (descent and low posture) |
| Confirming fall | Fall confirmed; waiting for the stayed-down check |
| **FALL** | The person stayed down: an alert is raised |
| Lying (no fall seen) | Lying posture without an observed fall (for example on a sofa); never alerted |

For reliable body tracking, place the camera so that people are visible from head to knees.
The video preview stays smooth even when analysis is slower than the camera.

## 4. Alerts and events

Click **Enable alerts** in the top bar once on each device that should sound an alarm. Browsers only allow sound and
notifications after a click on the page.

When a fall is confirmed, every open dashboard shows a full-screen alert with a snapshot, the camera, the person (when
recognised) and the time, and plays a siren. Click **Acknowledge** to clear it on all screens.

**Event history** shows:
- **Live alerts:** recent alerts with snapshots and who acknowledged them. **Simulate fall alert** tests the
  complete alert flow without a real fall.
- **Phone push (ntfy):** optional push notifications to phones (see [CAMERA_NETWORK.md](CAMERA_NETWORK.md#phone-push-with-ntfy-optional-works-with-the-screen-locked)).
- **Monitoring events:** the stored history of confirmed falls.

Do not stage real falls for testing; use **Simulate fall alert** or recorded footage.

## Where data is stored

| Location | Contents |
|---|---|
| `data/studio/enrollment.sqlite3` | Profiles, face features, events |
| `data/studio/images/` | Aligned face photos shown in the gallery |
| `data/models/` | Downloaded model weights |
| `data/tls/` | Self-signed certificate for network mode |

All of `data/` is excluded from Git. Enrol people only with their consent.
