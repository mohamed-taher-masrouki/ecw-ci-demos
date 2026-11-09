# RA6 twister CI demo

Branch: `demo/ra6-zephyr-twister`. Physical board: **FPB-RA6E2**, ECW board **23**, Zephyr target `fpb_ra6e2`, console 115200 baud.

## Run manually

1. Add repository secret **ECW_API_KEY** containing your ECW API token.
2. Open **Actions → ECW CI Job Smoke → Run workflow**, select `demo/ra6-zephyr-twister`, and start it. The displayed workflow name on this branch is **RA6 manual demo**. No pull request or push triggers hardware work.
3. Leave `booking_id` empty to create a CI booking (cancelled after testing), or supply your active RA6 booking (preserved after testing). Close any other terminal using that booking before starting.
4. Download the run artifact for firmware, provenance, requests, job metadata, logs and test evidence. On the blinky branch, open `artifacts/camera.html` for the screenshot gallery.

Defaults: `ECW_BACKEND_URL=https://plovdiv.edgebench.io/api`, `ECW_CONSOLE_URL=wss://plovdiv.edgebench.io`. Optional repository variables with these names override the endpoints. RA6 board identity is checked before programming; it cannot be changed by a repository variable. Auto-booking waits up to 15 minutes; the active session must have over six minutes left.

## What this branch tests

Build-only check on GitHub → book RA6 → twister.run trusted ci_twister_test profile → backend Twister builds and flashes through pytest/RM → identify firmware, five nonce echoes, increasing uptime → JSON/JUnit reports → release



Production currently exposes the trusted admin-installed `ci_twister_test` profile for RA6, using upstream Zephyr v4.4.0. Other ECW deployments must install that reviewed profile first. It runs its reviewed server-side source; GitHub does not upload arbitrary Python to the backend. `app/` mirrors that suite for inspection and local build-only checks. The GitHub build is not a second hardware flash. Runtime firmware is built from the installed profile, and may use the server's pinned Zephyr version rather than the GitHub build's v4.2.0. The job must return exit code 0, a passed case, preserved metadata, Twister JSON and JUnit XML. No ztest/direct-serial shortcut is used.

The profile's separate Python environment must also provide the dependencies of the deployed backend release, pinned by that release's `uv.lock`. Recheck this after a backend upgrade: pytest loads the backend HIL plugin before any hardware test starts. A missing backend dependency appears in the JUnit report as a pytest plugin import error, even when the Zephyr build passed. During qualification, the runner needed `maxminddb==3.2.0`, matching the deployed release. Install runner dependencies on the server; adding them to the GitHub build environment does not repair the server environment.

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

`/upload` stores `build/firmware.hex` on NFS. A separate POST to `/v1/artifacts/my-uploads` flashes that exact stored filename. HIL preparation is completed before MCU upload/flash. Cleanup and evidence publishing run on failure; force-stopping a runner can still interrupt cleanup, and the booking eventually expires.

All four demo branches share a repository-wide RA6 concurrency group. Other repositories or interactive users are still governed by ECW booking and RM ownership.

## Sources

Adapted from [nxp-zephyr-projects](https://github.com/DevHeadsCommunity/nxp-zephyr-projects/tree/7f4af26), retaining its Zephyr v4.2.0 basis and CMake application layout. The west manifest fixes its original remote declarations and targets RA6. Firmware and wiring are inspired by `ECW_Lab/ecw_demos/fpb-ra6e2`; CI API helpers come from the ECW public CI template. No backend changes are required.

See [the demo flow](docs/flow.svg). Offline checks and build success are separate from a passing physical-board run.
