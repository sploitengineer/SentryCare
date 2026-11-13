# Camera network and phone cameras (demo mode)

Stream a phone camera, or any network camera, to the SentryCare dashboard on the laptop. The laptop
does all detection. Anyone on the same Wi-Fi who has the PIN can watch.

## Start (easiest: from the UI)

Start Studio normally, open **Camera network**, and click **Turn on phone access**. Studio starts an
HTTPS listener on port 8766 and shows a QR code, the link and a new PIN. Scan the QR code with the phone and follow
the numbered steps. **Phone can't open the link?** offers a `/ping` test page: if it shows *Connected*, the
network works. Turning access off stops the listener and signs phones out.

## Start (command line alternative)

```powershell
.\.venv\Scripts\python.exe -m sentrycare.web --lan            # random 6-digit PIN
.\.venv\Scripts\python.exe -m sentrycare.web --lan --pin 1357 # fixed PIN for a demo
```

The terminal prints the Wi-Fi address, the phone link (`https://<laptop-ip>:8765/camera`) and the PIN.
Windows may ask to allow Python through the firewall. Allow it on **private** networks only.

## Phone as a camera (browser, no app)

1. Connect the phone to the same Wi-Fi and open the `/camera` link.
2. A certificate warning appears once because the certificate is self-signed. Choose **Advanced → Proceed**.
3. Enter the PIN, name the camera, pick the back or front lens, then tap **Start streaming**.
4. On the laptop, open **Camera network** and select the camera. The video shows face names, the skeleton and the fall state.

Keep the phone page open with the screen on. The page requests a screen wake lock where the browser supports it.

## Phone or IP camera as a stream (app)

Android "IP Webcam" or a similar app shows an address such as `http://192.168.1.23:8080`. On the laptop,
add the camera under **Add a stream camera** using that address plus `/video`. RTSP URLs work too, and `0` is the
laptop webcam. Only the laptop itself can add stream URLs. Remote viewers cannot make the server open URLs.
Any credentials in a URL are hidden in the UI.

## How it works

- Each source keeps only its newest frame. One background worker analyses the newest frame of each source,
  so slow analysis drops frames instead of building a delay.
- The dashboard receives an MJPEG stream: the newest frame with the newest analysis drawn on top.
- Fall events are stored with the camera name in **Event history**.
- Up to 4 cameras. Analysis is shared, so more cameras means fewer analysed frames per camera.

## Security limits

- HTTPS uses a self-signed certificate stored under the ignored `data/tls/` folder.
- Remote clients need the PIN, which gives them a session cookie. After 5 wrong attempts that client is locked out for 60 s.
- Requests from the laptop itself skip the PIN.
- This is the Flask development server: for a trusted Wi-Fi demo only. Never use it on public networks or the internet.

## Fall alerts

Every confirmed fall, from any camera, creates an alert. It is sent instantly to every open dashboard,
on the laptop and on phones that opened the dashboard link.

- **On each screen:** a full-screen red alert with the snapshot, camera, person and time, plus a repeating siren,
  a flashing tab title, a system notification and vibration on Android.
- **Turn it on per device:** click **Enable alerts** in the top bar once. Browsers only play sound and show
  notifications after a click.
- **Acknowledge** on any screen clears the alert everywhere and records who acknowledged it.
- **Event history → Live alerts** lists recent alerts. **Simulate fall alert** rehearses the demo using the
  selected camera's current image. Simulating is only available on the laptop.
- Alerts and snapshots are kept in memory only (newest 50). The SQLite event history keeps the durable record.

### Phone push with ntfy (optional, works with the screen locked)

1. Install the free **ntfy** app on each caretaker phone.
2. In **Event history → Phone push**, click **Generate** for a random topic, tick **Send push notifications**, then **Save**.
3. In the ntfy app, tap **+** and subscribe to the same topic.
4. Press **Send test push**.

Push is off by default. It sends a short text to the ntfy server (ntfy.sh unless you set your own),
never images. The person's name is sent only when you tick that option. Anyone who knows the topic
can read the alerts, so treat it like a password. Settings are stored in the ignored `data/studio/` folder.
