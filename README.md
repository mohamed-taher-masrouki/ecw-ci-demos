# RA6 blinky CI demo

Branch: `demo/ra6-blinky-camera`. Physical board: **FPB-RA6E2**, ECW board **23**, Zephyr target `fpb_ra6e2`, console 115200 baud.

## Run manually

1. Add repository secret **ECW_API_KEY** containing your ECW API token.
2. Open **Actions → ECW CI Job Smoke → Run workflow**, select `demo/ra6-blinky-camera`, and start it. The displayed workflow name on this branch is **RA6 manual demo**. No pull request or push triggers hardware work.
3. Leave `booking_id` empty to create a CI booking (cancelled after testing), or supply your active RA6 booking (preserved after testing). Close any other terminal using that booking before starting.
4. Download the run artifact for firmware, provenance, requests, job metadata, logs and test evidence. On the blinky branch, open `artifacts/camera.html` for the screenshot gallery.

Defaults: `ECW_BACKEND_URL=https://plovdiv.edgebench.io/api`, `ECW_CONSOLE_URL=wss://plovdiv.edgebench.io`. Optional repository variables with these names override the endpoints. RA6 board identity is checked before programming; it cannot be changed by a repository variable. Auto-booking waits up to 15 minutes; the active session must have over six minutes left.

The auto-booking API can return the short remainder of a half-hour slot. If that leaves six minutes or less, the demo requests one ten-minute extension through the normal booking API and checks the booking again. Conflicts, limits, and maintenance rules still apply. A supplied `booking_id` is never extended or cancelled by the demo. Expired and cancelled bookings stop polling immediately.

## Blinky test lifecycle

![RA6 blinky lifecycle and capture timing](docs/flow.svg)

The test builds and programs the RA6, checks that its LED-control command is acknowledged, and records a two-second camera sequence. Hardware access goes through the booked ECW session.

| Phase | What happens | Required result |
| --- | --- | --- |
| 1. Build | Compile `app/` for `fpb_ra6e2` using pinned Zephyr v4.2.0 and SDK 0.17.2. | `build/firmware.hex` and SHA256 provenance are produced. |
| 2. Book | Create a CI booking, or use the supplied `booking_id`; wait for activation. | The booking is for board 23, FPB-RA6E2, with more than six minutes remaining. |
| 3. Upload | POST the HEX to `/upload`, then check the upload manifest. | The exact stored filename appears in `/v1/artifacts/my-uploads`. Upload only stores the file on NFS. |
| 4. Flash | Separately POST that stored filename to `/v1/artifacts/my-uploads`. | The backend reports successful programming before the test proceeds. |
| 5. Connect | Open `/video/{booking_id}` and wait for the first JPEG. Request a console ticket, connect over WebSocket, then wait for the RM connection banner. | Both camera and serial console are ready before starting the LED. |
| 6. Start | Send `led blink <nonce>` through the authenticated console. | Receive the matching `ECW_LED BLINK 50 <nonce>` acknowledgement. |
| 7. Capture | Start the capture clock after acknowledgement. Save the latest camera frame at 0, 30, 60, …, 1980 ms; finish the window at 2000 ms. | Save 67 JPEGs, their actual timestamps, source frame numbers, ages and SHA256 values. |
| 8. Check and stop | Write the report/gallery, validate the JPEGs, then send `led off <nonce>`. | No stream error, at least two received source frame numbers, and a matching OFF acknowledgement for a successful run. |
| 9. Close and release | Close the console and camera stream. Cancel a booking created by this run; preserve a supplied booking. | Release acknowledgement is saved when the run owns the booking. GitHub publishes the available evidence even after failure. |

### Two independent clocks

| Activity | Schedule | Meaning |
| --- | --- | --- |
| RA6 LED | Toggle every **50 ms** | Nominally 50 ms ON + 50 ms OFF: a 100 ms full cycle, or 10 Hz. The firmware controls this timing. |
| Screenshot files | Save every **30 ms** for **2 seconds** | 67 save slots: `0..1980 ms`. The last 20 ms completes the capture window. The Python client schedules saves against a monotonic clock. |
| Camera source | Its own frame rate | One continuous authenticated MJPEG connection supplies frames in parallel. Each save uses the latest received frame. |

The 30 ms interval is a **file-save target**, not a guarantee of a new camera exposure at every slot. If the camera or network supplies frames more slowly, several JPEG files can use the same received frame. The report records this through `source_sequence`, `frame_age_ms`, `captured_ms` and the frame hash. `fresh_frame_every_slot` tells you whether every save used a different received frame number.

The backend OpenCV fallback is capped at 15 fps; direct MJPEG passthrough can run faster. A new source sequence number means another JPEG arrived, not that its pixels or the LED state changed. The timing drawing is schematic: the test does not measure physical LED edges or automatically classify ON/OFF pixels.

The two-second window measures screenshot collection. Connection setup, flashing, JPEG validation and cleanup add to the total workflow time. The LED remains in blink mode until the post-capture checks finish and the stop command is sent.

### Evidence to inspect

Download the workflow artifact and open `artifacts/camera.html` locally. It displays the screenshots with their target time, source frame number and frame age.

| File | Evidence |
| --- | --- |
| `artifacts/frames/frame-000.jpg` … `frame-066.jpg` | The 67 scheduled camera screenshots. |
| `artifacts/verification.json` | Requested timing, actual save timestamps, source frame counts, frame ages and hashes. |
| `artifacts/console.log` | Connection banner and LED command acknowledgements. |
| `artifacts/summary.json` | Overall demo result. |
| `artifacts/error.json` | Error details when the demo fails. |
| `artifacts/booking.json`, `reservation.json`, `release.json` | Booking identity and cleanup evidence; `release.json` applies to bookings created by the run. |
| `build/provenance.json` | Firmware checksum and build revision. |

A successful run confirms command acknowledgements, a progressing camera stream and valid JPEG files. Review the images to confirm the LED is visible and blinking.

If capture or validation fails, cleanup still attempts to turn the LED off and closes both connections. The original failure is retained if the OFF command also fails. Available logs and partial screenshots are published by the workflow's final artifact step.

## Build locally

Use Python 3.12, west, Zephyr SDK 0.17.2 (ARM toolchain) and the pinned west manifest. In a fresh parent directory:

```sh
python -m pip install -r requirements-ci.txt
west init -l .
west update --narrow -o=--depth=1
python -m pip install -r ../zephyr/scripts/requirements.txt
west zephyr-export
west sdk install --version 0.17.2 --toolchains arm-zephyr-eabi
export ZEPHYR_BASE="$(realpath ../zephyr)"
bash .github/user/build/build.sh
python -m unittest discover -s tests -v
```

`/upload` stores `build/firmware.hex` on NFS. A separate POST to `/v1/artifacts/my-uploads` flashes that exact stored filename. This blinky branch does not select or configure a HIL sensor. Cleanup and evidence publishing run on failure; force-stopping a runner can still interrupt cleanup, and the booking eventually expires.

All four demo branches share a repository-wide RA6 concurrency group. Other repositories or interactive users are still governed by ECW booking and RM ownership.

## Sources

Adapted from [nxp-zephyr-projects](https://github.com/DevHeadsCommunity/nxp-zephyr-projects/tree/7f4af26), retaining its Zephyr v4.2.0 basis and CMake application layout. The west manifest fixes its original remote declarations and targets RA6. Firmware and wiring are inspired by `ECW_Lab/ecw_demos/fpb-ra6e2`; CI API helpers come from the ECW public CI template. No backend changes are required.

See [the demo flow](docs/flow.svg). Offline checks and build success are separate from a passing physical-board run.
