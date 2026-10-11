"""在线资源增量（tasks.json）同步的独立单元测试。

聚焦 app/update_service.py::_sync_incremental_tasks 与 _resource_updated
对「合法空对象」的兼容：{} 表示当前无增量，应当覆盖已有 tasks.json 清除旧缓存；
非 dict（数组 / 坏 JSON）仍然失败并保留旧文件、清理临时文件。全部走临时目录与
伪 _download_file，不触碰真实网络与 MaaCore。

复用 tests/test_versions_update.py 的 make_service / write_json 助手，避免复制
FakeAdapter 等假对象。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.update_helpers import INCREMENTAL_TASKS_URLS

try:
    from test_versions_update import make_service, write_json
except ModuleNotFoundError:  # 以 tests 包路径导入时（如 python -m unittest tests.test_incremental_tasks）
    from tests.test_versions_update import make_service, write_json


def tasks_target(core_dir: Path) -> Path:
    return core_dir / "cache" / "resource" / "tasks" / "tasks.json"


class IncrementalTasksTest(unittest.TestCase):
    def test_nonempty_payload_writes_nested_tasks_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core_dir = root / "MAA"
            (core_dir / "resource").mkdir(parents=True)
            service = make_service(root, core_dir)

            payload = {"YW-8": {"algorithm": "JustReturn"}}
            seen_urls: list[str] = []

            def download(url: str, target: Path) -> None:
                seen_urls.append(url)
                write_json(target, payload)

            service._download_file = download
            result = service._sync_incremental_tasks(core_dir)

            target = tasks_target(core_dir)
            written = json.loads(target.read_text(encoding="utf-8"))
            leftovers = list((root / "cache").glob("tasks-*.json"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], INCREMENTAL_TASKS_URLS[0])
        self.assertEqual(Path(result["path"]), target)
        self.assertEqual(written, payload)
        self.assertEqual(seen_urls, [INCREMENTAL_TASKS_URLS[0]])
        self.assertEqual(leftovers, [])

    def test_empty_object_overwrites_previous_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core_dir = root / "MAA"
            (core_dir / "resource").mkdir(parents=True)
            target = tasks_target(core_dir)
            write_json(target, {"AccountManager": {"action": "ClickSelf"}})
            service = make_service(root, core_dir)

            def download(_url: str, target_path: Path) -> None:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text("{}", encoding="utf-8")

            service._download_file = download
            result = service._sync_incremental_tasks(core_dir)

            written = json.loads(target.read_text(encoding="utf-8"))
            leftovers = list((root / "cache").glob("tasks-*.json"))

        self.assertTrue(result["ok"])
        self.assertEqual(written, {})
        self.assertEqual(leftovers, [])

    def test_first_source_failure_falls_back_to_second(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core_dir = root / "MAA"
            (core_dir / "resource").mkdir(parents=True)
            service = make_service(root, core_dir)

            payload = {"YW-8": {"algorithm": "JustReturn"}}
            seen_urls: list[str] = []

            def download(url: str, target: Path) -> None:
                seen_urls.append(url)
                if url == INCREMENTAL_TASKS_URLS[0]:
                    raise RuntimeError("primary source down")
                write_json(target, payload)

            service._download_file = download
            result = service._sync_incremental_tasks(core_dir)

            written = json.loads(tasks_target(core_dir).read_text(encoding="utf-8"))
            leftovers = list((root / "cache").glob("tasks-*.json"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], INCREMENTAL_TASKS_URLS[1])
        self.assertEqual(written, payload)
        self.assertEqual(seen_urls, list(INCREMENTAL_TASKS_URLS))
        self.assertEqual(leftovers, [])

    def test_invalid_json_or_non_object_keeps_existing_file(self):
        cases = {"broken_json": "not-json{", "json_array": "[]"}
        for label, body in cases.items():
            with self.subTest(payload=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                core_dir = root / "MAA"
                (core_dir / "resource").mkdir(parents=True)
                target = tasks_target(core_dir)
                existing = {"keep": True}
                write_json(target, existing)
                service = make_service(root, core_dir)

                def download(_url: str, target_path: Path, _body: str = body) -> None:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(_body, encoding="utf-8")

                service._download_file = download
                result = service._sync_incremental_tasks(core_dir)

                unchanged = json.loads(target.read_text(encoding="utf-8"))
                leftovers = list((root / "cache").glob("tasks-*.json"))

            self.assertFalse(result["ok"])
            self.assertEqual(result["message"], "任务增量同步失败")
            self.assertEqual(len(result["errors"]), 2)
            self.assertEqual(unchanged, existing)
            self.assertEqual(leftovers, [])

    def test_failed_sync_result_publishes_incremental_failure_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core_dir = root / "MAA"
            (core_dir / "resource").mkdir(parents=True)
            service = make_service(root, core_dir)
            failed = {
                "ok": False,
                "path": str(tasks_target(core_dir)),
                "message": "任务增量同步失败",
                "errors": ["primary: down", "fallback: down"],
            }

            result = service._resource_updated("Official", incremental_tasks=failed)

            events = service._events.recent()
            failure_events = [event for event in events if event.type == "update.resource.incremental_tasks_failed"]
            updated_events = [event for event in events if event.type == "update.resource.updated"]

        self.assertTrue(result["ok"])
        self.assertIn("任务增量", result["message"])
        self.assertEqual(len(failure_events), 1)
        self.assertEqual(failure_events[0].level, "warning")
        self.assertEqual(failure_events[0].detail["message"], "任务增量同步失败")
        self.assertEqual(len(updated_events), 1)

    def test_empty_object_result_reports_clean_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core_dir = root / "MAA"
            (core_dir / "resource").mkdir(parents=True)
            service = make_service(root, core_dir)

            result = service._resource_updated(
                "Official",
                incremental_tasks={"ok": True, "source": INCREMENTAL_TASKS_URLS[0], "path": str(tasks_target(core_dir))},
            )

            types = [event.type for event in service._events.recent()]

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "资源已更新")
        self.assertNotIn("update.resource.incremental_tasks_failed", types)
        self.assertIn("update.resource.updated", types)


if __name__ == "__main__":
    unittest.main()
