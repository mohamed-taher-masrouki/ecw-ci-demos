"""Offline contract and failure-path checks; no network or hardware is contacted."""

from datetime import datetime, timedelta, timezone
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github/scripts"))
import demo_console
import run_demo
import upload_firmware
import flash_firmware


def bundle(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return output.getvalue()


class RA6DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        for target in (patch.object(run_demo, "ARTIFACTS", self.directory),
                       patch.object(demo_console, "ARTIFACTS", self.directory),
                       patch.dict(os.environ, {"ECW_BACKEND_URL": "https://example.test/api",
                                               "ECW_API_KEY": "fake-offline-key"})):
            target.start()
            self.addCleanup(target.stop)

    def test_only_manual_workflows_and_shared_ra6_lock(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        for path in (root / ".github/workflows").glob("*.yml"):
            workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(set(workflow["on"]), {"workflow_dispatch"}, path.name)
        workflow = yaml.load((root / ".github/workflows/ci-job-smoke.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["concurrency"]["group"], "ecw-ra6-board-23")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(json.loads((root / "demo.json").read_text())["board_id"], 23)

    def test_job_identity_is_unique_for_repeated_runs_and_preserves_board_metadata(self):
        config = {"demo": "twister"}
        env = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "abc"}
        commands = [run_demo.command("twister", "twister.run", 290000, profile="ci_twister_test")]
        first = run_demo.payload(config, "test", commands, booking_id=99, env=env)
        second = run_demo.payload(config, "test", commands, booking_id=99, env={**env, "GITHUB_RUN_ATTEMPT": "2"})
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["metadata"]["bench"], "ci_twister_test")
        self.assertEqual(first["metadata"]["board_id"], 23)
        self.assertEqual(first["commands"][0]["attempts"], 1)
        self.assertLess(sum(c["timeout_ms"] for c in commands), first["duration_seconds"] * 1000)

    def test_booking_rejects_wrong_board_future_window_and_short_reservation(self):
        now = datetime.now(timezone.utc)
        valid = {"board_id": 23, "board_name": "FPB-RA6E2", "status": "active",
                 "start_utc": (now - timedelta(minutes=1)).isoformat(),
                 "end_utc": (now + timedelta(minutes=10)).isoformat()}
        run_demo.require_booking(valid)
        for change in ({"board_id": 24}, {"status": "confirmed"},
                       {"start_utc": (now + timedelta(minutes=1)).isoformat()},
                       {"end_utc": (now + timedelta(seconds=300)).isoformat()}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                run_demo.require_booking({**valid, **change})

    def test_existing_booking_is_not_cancelled_but_created_booking_is(self):
        demo = run_demo.Demo({"demo": "hello"})
        demo.booking_id = 99
        with patch.object(demo, "request", return_value={"status": "okay"}) as request:
            demo.release()
            request.assert_not_called()
            demo.created = True
            demo.release()
            request.assert_called_once_with("/cancel-booking", params={"booking_id": 99})

    def test_booking_with_null_name_resolves_identity_before_accepting_session(self):
        now = datetime.now(timezone.utc)
        booking = {"id": 99, "board_id": 23, "board_name": None, "status": "active",
                   "start_utc": (now - timedelta(minutes=1)).isoformat(),
                   "end_utc": (now + timedelta(minutes=10)).isoformat()}
        responses = {"/booking": booking, "/board": {"id": 23, "board_type_id": 7},
                     "/board_type": {"id": 7, "board_name": "FPB-RA6E2", "is_mcu": True}}
        demo = run_demo.Demo({"demo": "hello"})
        with patch.dict(os.environ, {"ECW_BOOKING_ID": "99"}), \
             patch.object(demo, "request", side_effect=lambda path, **kwargs: responses[path]) as api:
            demo.book()
        self.assertEqual([c.args[0] for c in api.call_args_list], ["/booking", "/board", "/board_type"])
        self.assertIsNone(json.loads((self.directory / "booking-response.json").read_text())["board_name"])
        self.assertEqual(json.loads((self.directory / "booking.json").read_text())["board_name"], "FPB-RA6E2")
        self.assertEqual(json.loads((self.directory / "board-identity.json").read_text())["board_type_id"], 7)

    def test_missing_booking_label_does_not_bypass_wrong_board_checks(self):
        for booking in ({"board_id": 24, "board_name": None},
                        {"board_id": 23, "board_name": "Another board"}):
            demo = run_demo.Demo({"demo": "hello"})
            with self.subTest(booking=booking), patch.object(demo, "request") as api:
                with self.assertRaises(RuntimeError):
                    demo.identify_booking(booking)
                api.assert_not_called()

    def test_missing_booking_label_rejects_inconsistent_board_metadata(self):
        for board, board_type in (
            ({"id": 24, "board_type_id": 7}, {}),
            ({"id": 23}, {}),
            ({"id": 23, "board_type_id": 7}, {"id": 8, "board_name": "FPB-RA6E2", "is_mcu": True}),
            ({"id": 23, "board_type_id": 7}, {"id": 7, "board_name": "Other", "is_mcu": True}),
            ({"id": 23, "board_type_id": 7}, {"id": 7, "board_name": "FPB-RA6E2", "is_mcu": False}),
        ):
            demo = run_demo.Demo({"demo": "hello"})
            with self.subTest(board=board, board_type=board_type), \
                 patch.object(demo, "request", side_effect=[board, board_type]):
                with self.assertRaises(RuntimeError):
                    demo.identify_booking({"board_id": 23, "board_name": None})

    def test_failed_job_still_collects_logs_results_and_zip(self):
        demo = run_demo.Demo({"demo": "hello"})
        demo.booking_id = 99
        def request(path, **kwargs):
            if path == "/v1/ci/jobs":
                return {"id": 55}
            if path.endswith("/results"):
                return {"status": "failed", "results": {}}
            if path.endswith("/logs"):
                return {"items": []}
            return {"status": "failed"}
        with patch.object(demo, "request", side_effect=request) as api, patch.object(run_demo, "request_bytes", return_value=bundle({"failure.txt": "failed"})):
            with self.assertRaises(RuntimeError):
                demo.job("hello", [run_demo.command("serial", "mcu.serial_log", 5000)])
        self.assertTrue((self.directory / "hello-results.json").exists())
        self.assertTrue((self.directory / "hello-logs.json").exists())
        self.assertTrue((self.directory / "hello-artifacts.zip").exists())
        self.assertEqual(api.call_count, 4)

    def test_zip_member_is_not_extracted_and_requires_unique_basename(self):
        self.assertEqual(run_demo.read_member(bundle({"../../hello.log": "hello"}), "hello.log"), b"hello")
        for data in (bundle({"else.log": "hello"}), bundle({"a/hello.log": "one", "b/hello.log": "two"})):
            with self.assertRaises(RuntimeError):
                run_demo.read_member(data, "hello.log")

    def test_hello_requires_consecutive_real_greetings(self):
        self.assertEqual(run_demo.verify_hello("\n".join(f"Hello from FPB-RA6E2! #{i}" for i in range(7, 12)))["greetings"], 5)
        for text in ("", "hello", "\n".join(f"Hello from FPB-RA6E2! #{i}" for i in (1, 1, 1, 1)),
                     "\n".join(f"Hello from FPB-RA6E2! #{i}" for i in (1, 2, 4, 5))):
            with self.assertRaises(RuntimeError):
                run_demo.verify_hello(text)

    def test_twister_rejects_api_success_without_passed_case_or_reports(self):
        metadata = {"board_id": 23}
        job = {"status": "succeeded", "metadata": metadata}
        output = {"framework": "twister", "successful": True, "exit_code": 0, "cases": {"passed": 1, "failed": 0}}
        results = {"status": "succeeded", "results": {"twister": {"success": True, "output": output}}}
        reports = bundle({"reports/twister.json": "{}", "reports/twister.xml": "<testsuites/>"})
        run_demo.verify_twister(job, results, metadata, reports)
        for change in ({"exit_code": 1}, {"successful": False}, {"cases": {"passed": 0}}, {"cases": {"passed": 1, "failed": 1}}):
            bad = copy.deepcopy(results)
            bad["results"]["twister"]["output"].update(change)
            with self.assertRaises(RuntimeError):
                run_demo.verify_twister(job, bad, metadata, reports)
        with self.assertRaises(RuntimeError):
            run_demo.verify_twister(job, results, {}, reports)
        with self.assertRaises(RuntimeError):
            run_demo.verify_twister(job, results, metadata, bundle({"log.txt": "looks good"}))

    def test_hil_accepts_complete_csv_cycle_and_rejects_stale_or_partial_samples(self):
        expected = [(15000 + i, 10000 + i, 16000 + i) for i in range(50)]
        self.assertEqual(demo_console.verify_hil(expected[7:] + expected[:7], expected)["csv_start_offset"], 7)
        for readings in ([expected[0]] * 50, expected[:49], expected[:49] + [(0, 0, 0)]):
            with self.assertRaises(RuntimeError):
                demo_console.verify_hil(readings, expected)

    def test_console_wait_handles_partial_frames_and_does_not_accept_a_stale_nonce(self):
        console = demo_console.Console(MagicMock())
        console.connection = MagicMock()
        console.connection.recv.side_effect = ["ECW_LED ON stale\nECW_", "LED ON fresh\r", "\n"]
        console.wait(r"ECW_LED ON fresh\r?\n")
        self.assertEqual(console.connection.recv.call_count, 3)
        self.assertIn("stale", console.transcript)

    def test_camera_saves_67_scheduled_frames_and_reports_duplicate_source_frames(self):
        from PIL import Image
        jpeg = io.BytesIO()
        Image.new("RGB", (8, 8), color="green").save(jpeg, format="JPEG")
        ticks = [10.0]
        camera = MagicMock()
        camera.lock = threading.Lock()
        camera.error = None
        camera.latest = (jpeg.getvalue(), 1, ticks[0])
        def sleep(duration):
            ticks[0] += duration
            camera.latest = (jpeg.getvalue(), int((ticks[0] - 10) / 0.06) + 1, ticks[0])
        report = demo_console.sample_camera(camera, clock=lambda: ticks[0], sleep=sleep)
        self.assertEqual(report["screenshots"], 67)
        self.assertEqual(report["frames"][-1]["target_ms"], 1980)
        self.assertLess(report["distinct_source_frames"], 67)
        self.assertFalse(report["fresh_frame_every_slot"])
        self.assertAlmostEqual(ticks[0] - 10, 2)
        self.assertEqual(len(list((self.directory / "frames").glob("*.jpg"))), 67)

    def test_hex_upload_is_storage_only_and_flash_is_separate(self):
        firmware = self.directory / "firmware.hex"
        firmware.write_text(":00000001FF\n")
        with patch.object(upload_firmware, "BUILD_DIR", self.directory), \
             patch.object(upload_firmware, "upload_file", return_value={"status": "okay"}) as upload, \
             patch.object(upload_firmware, "request_json", return_value={"items": [{"filename": "firmware.hex"}]}), \
             patch.object(upload_firmware, "write_github_output"), \
             patch.dict(os.environ, {"ECW_BOOKING_ID": "99", "ECW_FIRMWARE_CODE": "firmware.hex"}), \
             patch.object(flash_firmware, "request_json", return_value={"status": "okay"}) as flash:
            code = upload_firmware.main()
            self.assertEqual(code, "firmware.hex")
            upload.assert_called_once()
            flash.assert_not_called()
            flash_firmware.main()
            flash.assert_called_once()
            self.assertEqual(flash.call_args.kwargs["method"], "POST")

    def test_hil_configuration_precedes_mcu_flash_and_sensor_read(self):
        import configure_hil
        import upload_and_flash
        demo = MagicMock()
        demo.config = {"demo": "hil"}
        calls = []
        run_demo.save("hil-configuration.json", {"samples": 50, "sent_words": 150})
        with patch.object(configure_hil, "main", side_effect=lambda: calls.append("configure")), \
             patch.object(upload_and_flash, "main", side_effect=lambda: calls.append("upload-flash")), \
             patch.object(demo_console, "test_hil", side_effect=lambda d: calls.append("read")):
            run_demo.run(demo)
        self.assertEqual(calls, ["configure", "upload-flash", "read"])

    def test_twister_never_double_flashes(self):
        import upload_and_flash
        demo = MagicMock()
        demo.config = {"demo": "twister"}
        demo.job.return_value = ({}, {}, b"", {})
        with patch.object(upload_and_flash, "main") as flash, patch.object(run_demo, "verify_twister", return_value={}):
            run_demo.run(demo)
        flash.assert_not_called()
        self.assertEqual(demo.job.call_args.args[1][0]["parameters"], {"profile": "ci_twister_test"})


if __name__ == "__main__":
    unittest.main()
