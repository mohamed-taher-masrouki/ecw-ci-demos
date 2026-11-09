"""Run one manually dispatched RA6 demo through existing booking-scoped APIs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import time
import uuid
import zipfile

from ecw_client import request_bytes, request_json

ARTIFACTS = Path("artifacts")
TERMINAL = {"succeeded", "failed", "cancelled", "invalid"}


def save(name, value):
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def require_booking(booking, *, remaining=360):
    now = datetime.now(timezone.utc)
    if not (
        booking.get("board_id") == 23
        and booking.get("board_name") == "FPB-RA6E2"
        and booking.get("status") == "active"
        and stamp(booking["start_utc"]) <= now
        and (stamp(booking["end_utc"]) - now).total_seconds() > remaining
    ):
        raise RuntimeError("Need an active, owned board 23 FPB-RA6E2 booking with >360s remaining")


def payload(config, label, commands, *, booking_id, env):
    run_id = env.get("GITHUB_RUN_ID") or uuid.uuid4().hex
    attempt = env.get("GITHUB_RUN_ATTEMPT", "1")
    return {
        "booking_id": booking_id,
        "request_id": f"ra6-{config['demo']}-{run_id}-{attempt}-{label}",
        "name": f"RA6 {config['demo']} {label}",
        "duration_seconds": 300 if config["demo"] == "twister" else 30,
        "metadata": {
            "bench": "ci_twister_test" if config["demo"] == "twister" else "ecw-ci-demos",
            "demo": config["demo"], "board_id": 23, "board_name": "FPB-RA6E2",
            "platform": "fpb_ra6e2", "repository": env.get("GITHUB_REPOSITORY", "local"),
            "commit_sha": env.get("GITHUB_SHA", "local"), "ci_run_id": run_id,
            "ci_run_attempt": attempt, "branch": env.get("GITHUB_REF_NAME", "local"),
        },
        "commands": commands,
    }


def read_member(bundle, basename):
    """Read exactly one bounded member; never extract a server ZIP onto the runner."""
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        matches = [i for i in archive.infolist() if Path(i.filename).name == basename]
        if len(matches) != 1 or matches[0].file_size > 16 * 1024 * 1024:
            raise RuntimeError(f"Missing, duplicate or oversized artifact: {basename}")
        return archive.read(matches[0])


def verify_hello(text):
    counts = [int(n) for n in re.findall(r"Hello from FPB-RA6E2! #(\d+)", text)]
    if len(counts) < 4 or any(b != a + 1 for a, b in zip(counts, counts[1:])):
        raise RuntimeError("Five-second capture must contain >=4 consecutive RA6 greetings")
    return {"greetings": len(counts), "first": counts[0], "last": counts[-1]}


def verify_twister(job, results, metadata, bundle):
    output = results.get("results", {}).get("twister", {}).get("output") or {}
    if not (
        job.get("metadata") == metadata and job.get("status") == "succeeded"
        and results.get("status") == "succeeded"
        and results.get("results", {}).get("twister", {}).get("success") is True
        and output.get("framework") == "twister" and output.get("successful") is True
        and output.get("exit_code") == 0 and output.get("cases", {}).get("passed", 0) > 0
        and not output.get("cases", {}).get("failed", 0)
    ):
        raise RuntimeError("Twister must report a passing hardware case and preserve job metadata")
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = archive.namelist()
    if not any(n.endswith("twister.json") for n in names) or not any(n.endswith(".xml") for n in names):
        raise RuntimeError("Missing Twister JSON or JUnit XML reports")
    return output


class Demo:
    def __init__(self, config):
        self.config = config
        self.base = os.environ["ECW_BACKEND_URL"].rstrip("/")
        self.key = os.environ["ECW_API_KEY"]
        if not self.base.startswith("https://") or not self.key:
            raise RuntimeError("Set an HTTPS ECW_BACKEND_URL and the ECW_API_KEY secret")
        self.booking_id = None
        self.created = False
        self.board_identity = None

    def request(self, path, **kwargs):
        return request_json(self.base + path, self.key, **kwargs)

    def identify_booking(self, booking):
        """Resolve the optional booking label through the existing board APIs."""
        if booking.get("board_id") != 23:
            raise RuntimeError("Booking is not the expected RA6 board 23")
        if booking.get("board_name") is not None:
            if booking["board_name"] != "FPB-RA6E2":
                raise RuntimeError("Booking is not the expected RA6 board 23")
            return booking
        if self.board_identity is None:
            board = self.request("/board", params={"board_id": 23})
            board_type_id = board.get("board_type_id")
            if board.get("id") != 23 or not isinstance(board_type_id, int):
                raise RuntimeError("Could not verify the booked RA6 board identity")
            board_type = self.request("/board_type", params={"board_type_id": board_type_id})
            if (board_type.get("id") != board_type_id
                    or board_type.get("board_name") != "FPB-RA6E2"
                    or board_type.get("is_mcu") is not True):
                raise RuntimeError("Booked board metadata does not identify FPB-RA6E2")
            self.board_identity = {"board_id": 23, "board_type_id": board_type_id,
                                   "board_name": board_type["board_name"],
                                   "source": "/board -> /board_type"}
            save("board-identity.json", self.board_identity)
        return {**booking, "board_name": self.board_identity["board_name"]}

    def book(self):
        explicit = os.environ.get("ECW_BOOKING_ID", "").strip()
        if explicit:
            self.booking_id = int(explicit)
        else:
            booking = self.request("/auto-bookings", method="POST", params={"board_name": "FPB-RA6E2"})
            self.booking_id = int(booking["id"])
            self.created = True
        save("reservation.json", {"id": self.booking_id, "created_by_run": self.created})
        os.environ["ECW_BOOKING_ID"] = str(self.booking_id)
        deadline = time.monotonic() + 900
        while True:
            booking = self.request("/booking", params={"booking_id": self.booking_id})
            save("booking-response.json", booking)
            booking = self.identify_booking(booking)
            save("booking.json", booking)
            if booking.get("status") == "active" and stamp(booking["start_utc"]) <= datetime.now(timezone.utc):
                require_booking(booking)
                return
            if booking.get("status") in {"cancelled", "cleanedup", "failed"} or time.monotonic() >= deadline:
                raise RuntimeError("RA6 booking did not become active within 15 minutes")
            print(f"Waiting for RA6 booking {self.booking_id}: {booking.get('status')}", flush=True)
            time.sleep(5)

    def job(self, label, commands):
        spec = payload(self.config, label, commands, booking_id=self.booking_id, env=os.environ)
        save(f"{label}-request.json", spec)
        created = self.request("/v1/ci/jobs", method="POST", body=spec, timeout=30)
        save(f"{label}-created.json", created)
        job_id = int(created["id"])
        root = f"/v1/ci/jobs/{job_id}"
        deadline = time.monotonic() + 360
        job, collected, errors = created, {}, []
        try:
            while True:
                job = self.request(root)
                save(f"{label}-job.json", job)
                if job.get("status") in TERMINAL:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("CI job did not terminate within six minutes")
                time.sleep(1)
        finally:
            for endpoint in ("results", "logs", "artifacts.zip"):
                try:
                    if endpoint.endswith(".zip"):
                        bundle = request_bytes(self.base + root + "/" + endpoint, self.key, timeout=60)
                        (ARTIFACTS / f"{label}-artifacts.zip").write_bytes(bundle)
                        collected[endpoint] = bundle
                    else:
                        collected[endpoint] = self.request(root + "/" + endpoint)
                        save(f"{label}-{endpoint}.json", collected[endpoint])
                except Exception as error:
                    errors.append(f"{endpoint}: {type(error).__name__}")
            save(f"{label}-collection-errors.json", errors)
        if errors:
            raise RuntimeError("Required CI evidence could not be collected: " + ", ".join(errors))
        results = collected["results"]
        if (job.get("status") != "succeeded" or job.get("metadata") != spec["metadata"]
                or results.get("status") != "succeeded"
                or any(results.get("results", {}).get(c["id"], {}).get("success") is not True for c in commands)):
            raise RuntimeError(f"{label} CI job failed or returned incomplete results; see artifacts")
        return job, results, collected["artifacts.zip"], spec["metadata"]

    def release(self):
        # A caller-supplied booking belongs to the caller and is never cancelled.
        if self.created and self.booking_id is not None:
            result = self.request("/cancel-booking", params={"booking_id": self.booking_id})
            save("release.json", result)
            if result.get("status") != "okay":
                raise RuntimeError("Booking release was not acknowledged")


def command(identifier, action, timeout_ms, **parameters):
    return {"id": identifier, "name": identifier, "command": action, "attempts": 1,
            "timeout_ms": timeout_ms, "parameters": parameters}


def run(demo):
    import configure_hil
    import upload_and_flash

    config = demo.config
    if config["demo"] == "hil":
        os.environ["HIL_SENSOR_NAME"] = "BME280"
        os.environ["HIL_CSV_FILE"] = "bme280.csv"
        configure_hil.main()
        report = json.loads((ARTIFACTS / "hil-configuration.json").read_text())
        if report.get("samples") != 50 or report.get("sent_words") != 150:
            raise RuntimeError("HIL demo requires exactly 50 samples / 150 verified words")
    if config["demo"] != "twister":
        upload_and_flash.main()
    # Twister owns build -> flash -> pytest -> RM. Do not also flash from this client.
    if config["demo"] == "hello":
        _, results, bundle, _ = demo.job("hello", [command("serial", "mcu.serial_log", 5000, filename="hello.log")])
        output = results["results"]["serial"].get("output") or {}
        if output.get("duration_ms") != 5000:
            raise RuntimeError("Serial capture did not report the requested five-second duration")
        log = read_member(bundle, "hello.log")
        (ARTIFACTS / "hello.log").write_bytes(log)
        save("verification.json", verify_hello(log.decode("utf-8", errors="replace")))
    elif config["demo"] == "twister":
        job, results, bundle, metadata = demo.job("twister", [command(
            "twister", "twister.run", 290000, profile="ci_twister_test")])
        save("verification.json", verify_twister(job, results, metadata, bundle))
    else:
        from demo_console import test_blinky, test_hil
        if config["demo"] == "blinky":
            test_blinky(demo)
        else:
            test_hil(demo)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path("demo.json").read_text())
    demo = Demo(config)
    if args.release_only:
        path = ARTIFACTS / "reservation.json"
        if path.exists():
            reservation = json.loads(path.read_text())
            demo.booking_id, demo.created = reservation["id"], reservation["created_by_run"]
            demo.release()
        return
    result = {"demo": config["demo"], "status": "failed"}
    try:
        # Validate local inputs before reserving real hardware.
        if config["demo"] != "twister" and not Path("build/firmware.hex").is_file():
            raise RuntimeError("Build build/firmware.hex before reserving hardware")
        if config["demo"] in {"hil", "blinky"}:
            from demo_console import console_base
            console_base()
        demo.book()
        run(demo)
        result["status"] = "passed"
    finally:
        try:
            demo.release()
        finally:
            save("summary.json", result)
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as output:
                    output.write(f"### RA6 {config['demo']}: {result['status']}\n\nDownload the run's evidence artifact.\n")


if __name__ == "__main__":
    try:
        main()
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code in (None, 0):
            raise
        key = os.environ.get("ECW_API_KEY", "")
        message = str(error).replace(key, "[redacted]") if key else str(error)
        save("error.json", {"type": type(error).__name__, "message": message})
        print(f"Demo failed: {message}", flush=True)
        raise SystemExit(1) from None
