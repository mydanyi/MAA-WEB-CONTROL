import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import create_api_router
from app.events import EventBus
from app.logs import MaaLogService
from app.models import AdbConfig, Profile
from app.runner import DryRunMaaAdapter, MaaRunnerService
from app.storage import ProfileStore


class RedroidApiTest(unittest.TestCase):
    def test_redroid_status_reports_running_container(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            events = EventBus()
            logs = MaaLogService(events)
            runner = MaaRunnerService(DryRunMaaAdapter(), events, logs)
            app = FastAPI()
            app.include_router(create_api_router(store, runner, events, logs))

            with patch("app.api.subprocess.run") as run:
                run.return_value.stdout = "running|true\n"
                run.return_value.stderr = ""
                run.return_value.returncode = 0
                response = TestClient(app).get("/api/redroid/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["container"], "redroid")
        self.assertIn("running", payload["message"])

    def test_redroid_status_handles_missing_container(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            events = EventBus()
            logs = MaaLogService(events)
            runner = MaaRunnerService(DryRunMaaAdapter(), events, logs)
            app = FastAPI()
            app.include_router(create_api_router(store, runner, events, logs))

            with patch("app.api.subprocess.run") as run:
                run.return_value.stdout = ""
                run.return_value.stderr = "Error: No such object: redroid\n"
                run.return_value.returncode = 1
                response = TestClient(app).get("/api/redroid/status")

        payload = response.json()
        self.assertFalse(payload["available"])
        self.assertIn("不存在", payload["message"])

    def test_redroid_status_handles_missing_docker_command(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            events = EventBus()
            logs = MaaLogService(events)
            runner = MaaRunnerService(DryRunMaaAdapter(), events, logs)
            app = FastAPI()
            app.include_router(create_api_router(store, runner, events, logs))

            with patch("app.api.subprocess.run", side_effect=FileNotFoundError()):
                response = TestClient(app).get("/api/redroid/status")

        payload = response.json()
        self.assertFalse(payload["available"])
        self.assertIn("docker", payload["message"])


class AdbApiTest(unittest.TestCase):
    def test_adb_devices_reports_configured_profile_device(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            events = EventBus()
            logs = MaaLogService(events)
            runner = MaaRunnerService(DryRunMaaAdapter(), events, logs)
            profile = Profile(name="daily", adb=AdbConfig(address="127.0.0.1:5555", adb_path="adb"))
            store.save(profile)
            runner._profile = profile
            app = FastAPI()
            app.include_router(create_api_router(store, runner, events, logs))
            output = "List of devices attached\n127.0.0.1:5555\tdevice\n"

            with patch("app.api.subprocess.run") as run:
                run.return_value.stdout = output
                run.return_value.stderr = ""
                run.return_value.returncode = 0
                response = TestClient(app).get("/api/adb/devices")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["message"], "ADB 已连接：127.0.0.1:5555")
        self.assertEqual(payload["devices"], ["127.0.0.1:5555 (device)"])

    def test_adb_test_screenshot_includes_connection_benchmark(self):
        class ScreenshotAdapter:
            is_connected = True  # 模拟已连接状态（未连接时会先走自动重连分支）
            screenshot_benchmark = {
                "kind": "screenshot",
                "method": "LDExtras",
                "cost": 41,
                "alternatives": [{"method": "RawByNc", "cost": "931"}],
            }

            async def get_image(self):
                return b"png"

        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            events = EventBus()
            logs = MaaLogService(events)
            runner = MaaRunnerService(ScreenshotAdapter(), events, logs)
            app = FastAPI()
            app.include_router(create_api_router(store, runner, events, logs))
            response = TestClient(app).post("/api/adb/test-screenshot")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["size"], 3)
        self.assertEqual(payload["benchmark"]["method"], "LDExtras")
        self.assertEqual(payload["benchmark"]["alternatives"][0]["cost"], "931")

    def test_adb_test_screenshot_reconnects_when_not_connected(self):
        """未连接时应先用当前 profile 触发一次 connect，而不是直接报「未连接」。

        回归 BUG-012：connect 失败后 _asst 不可用，用户切换触控模式后点
        「测试截图」恒报未连接，形成"换了模式也连不上"的假象。
        """

        class ReconnectAdapter:
            def __init__(self):
                self.is_connected = False
                self.connect_calls = 0

            async def connect(self, profile):
                self.connect_calls += 1
                self.is_connected = True
                return True

            async def get_image(self):
                return b"png"

        adapter = ReconnectAdapter()
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            store.save(Profile(name="daily", adb=AdbConfig(address="127.0.0.1:5555")))
            events = EventBus()
            logs = MaaLogService(events)
            runner = MaaRunnerService(adapter, events, logs)
            app = FastAPI()
            app.include_router(create_api_router(store, runner, events, logs))
            response = TestClient(app).post("/api/adb/test-screenshot")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(adapter.connect_calls, 1)

    def test_adb_test_screenshot_reports_connect_failure_reason(self):
        """自动重连失败时要把失败原因带出来（如触控模式不可用），不能含糊。"""

        class FailingAdapter:
            is_connected = False

            async def connect(self, profile):
                return False

            async def get_image(self):
                return b"png"

        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            store.save(Profile(name="daily", adb=AdbConfig(address="127.0.0.1:5555")))
            events = EventBus()
            logs = MaaLogService(events)
            runner = MaaRunnerService(FailingAdapter(), events, logs)
            app = FastAPI()
            app.include_router(create_api_router(store, runner, events, logs))
            response = TestClient(app).post("/api/adb/test-screenshot")

        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("连接失败", payload["message"])
        self.assertIn("触控模式", payload["message"])

    def test_adb_test_screenshot_uses_the_named_profile(self):
        """配置不止一份时，用前端传来的档案名去连，别报「没有可用的任务档案」。

        回归：设置页有 shoucai / shualizhi 两份配置，点「截图测试」拿到的是
        "没有可用的任务档案"，用户看到的是"换了触控模式也连不上"。
        """
        seen = []

        class RecordingAdapter:
            def __init__(self):
                self.is_connected = False

            async def connect(self, profile):
                seen.append(profile.name)
                self.is_connected = True
                return True

            async def get_image(self):
                return b"png"

        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            store.save(Profile(name="daily-shoucai", adb=AdbConfig(address="127.0.0.1:5555")))
            store.save(Profile(name="daily-shualizhi", adb=AdbConfig(address="127.0.0.1:5556")))
            events = EventBus()
            logs = MaaLogService(events)
            runner = MaaRunnerService(RecordingAdapter(), events, logs)
            app = FastAPI()
            app.include_router(create_api_router(store, runner, events, logs))
            response = TestClient(app).post("/api/adb/test-screenshot?profile_name=daily-shualizhi")

        self.assertTrue(response.json()["ok"])
        self.assertEqual(seen, ["daily-shualizhi"])

    def test_adb_test_screenshot_still_connects_without_a_named_profile(self):
        """没传档案名、配置又不止一份时也要能连：随便挑一份，别把用户堵在门口。"""
        class AnyProfileAdapter:
            def __init__(self):
                self.is_connected = False
                self.connected_profile = None

            async def connect(self, profile):
                self.connected_profile = profile.name
                self.is_connected = True
                return True

            async def get_image(self):
                return b"png"

        adapter = AnyProfileAdapter()
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            store.save(Profile(name="daily-shoucai", adb=AdbConfig(address="127.0.0.1:5555")))
            store.save(Profile(name="daily-shualizhi", adb=AdbConfig(address="127.0.0.1:5556")))
            events = EventBus()
            logs = MaaLogService(events)
            runner = MaaRunnerService(adapter, events, logs)
            app = FastAPI()
            app.include_router(create_api_router(store, runner, events, logs))
            response = TestClient(app).post("/api/adb/test-screenshot")

        self.assertTrue(response.json()["ok"])
        self.assertIn(adapter.connected_profile, {"daily-shoucai", "daily-shualizhi"})


if __name__ == "__main__":
    unittest.main()
