"""Regression tests for MAA-FnOS issue 12: custom infrastructure schedule import.

These tests pin the behaviour of ``POST /api/infrast/upload`` and the absolute
custom-infrast listing in ``GET /api/options``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import create_api_router
from app.events import EventBus
from app.mapper import task_to_append_call
from app.models import TaskDefinition
from app.runner import DryRunMaaAdapter, MaaRunnerService
from app.storage import ProfileStore


# A minimal-but-ordinary MAA custom infrast file: a top-level object with a
# non-empty ``plans`` array whose entries are objects containing rooms.
VALID_PLAN = {
    "plans": [
        {
            "name": "默认方案",
            "rooms": {"manufacture": [{"operators": ["Castle-3"], "sort": True}]},
        }
    ]
}


class InfrastApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self._tmp.name)
        self.core = self.project_root / "maa"
        builtin_dir = self.core / "resource" / "custom_infrast"
        builtin_dir.mkdir(parents=True)
        (builtin_dir / "builtin.json").write_text(self._plan_text(), encoding="utf-8")
        (self.project_root / "data").mkdir()
        (self.project_root / "data" / "adapter.json").write_text(
            json.dumps({"adapter": "official", "core_dir": str(self.core)}), encoding="utf-8"
        )
        self.upload_dir = self.project_root / "data" / "custom_infrast"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────

    def _new_client(self) -> TestClient:
        store = ProfileStore(self.project_root / "data" / "profiles")
        events = EventBus()
        runner = MaaRunnerService(DryRunMaaAdapter(), events)
        app = FastAPI()
        app.include_router(create_api_router(store, runner, events, project_root=self.project_root))
        return TestClient(app)

    @staticmethod
    def _plan_text() -> str:
        return json.dumps(VALID_PLAN, ensure_ascii=False)

    @staticmethod
    def _upload(client: TestClient, name: str, content: str):
        return client.post("/api/infrast/upload", json={"name": name, "content": content})

    @staticmethod
    def _custom_files(options: dict) -> list[dict]:
        return options["infrast"]["custom_files"]

    # ── upload happy paths ─────────────────────────────────────────────

    def test_upload_plan_stores_absolute_path_and_options_list_it(self):
        client = self._new_client()

        response = self._upload(client, "schedule.json", self._plan_text())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["name"], "schedule.json")
        saved = Path(payload["path"])
        self.assertTrue(saved.is_absolute())
        self.assertEqual(saved.parent.resolve(), self.upload_dir.resolve())
        self.assertTrue(saved.exists())

        options = client.get("/api/options").json()
        values = [item["value"] for item in self._custom_files(options)]
        self.assertIn(payload["path"], values)

    def test_upload_accepts_utf8_bom(self):
        client = self._new_client()

        response = self._upload(client, "bom.json", "\ufeff" + self._plan_text())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(Path(payload["path"]).exists())

    def test_upload_appends_json_extension_and_replaces_same_name(self):
        client = self._new_client()

        first = self._upload(client, "no_extension", self._plan_text())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["name"], "no_extension.json")

        replacement = {"plans": [{"name": "替换", "rooms": {"trading": []}}]}
        second = self._upload(client, "no_extension", json.dumps(replacement, ensure_ascii=False))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["path"], first.json()["path"])
        stored = json.loads(Path(second.json()["path"]).read_text(encoding="utf-8"))
        self.assertEqual(stored, replacement)

    def test_upload_lowercases_json_extension_so_options_list_it(self):
        client = self._new_client()

        response = self._upload(client, "PLAN.JSON", self._plan_text())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        # options only scans ``*.json`` (case-sensitive on Linux), so the
        # extension is normalized while the caller's base name is preserved.
        self.assertEqual(payload["name"], "PLAN.json")
        self.assertTrue(payload["name"].endswith(".json"))
        values = [item["value"] for item in self._custom_files(client.get("/api/options").json())]
        self.assertIn(payload["path"], values)

    def test_upload_sanitizes_windows_and_posix_directory_components(self):
        client = self._new_client()

        cases = [
            ("..\\..\\evil.json", "evil.json"),
            ("../../evil.json", "evil.json"),
            ("/etc/passwd.json", "passwd.json"),
            ("C:\\Windows\\System32\\evil.json", "evil.json"),
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                response = self._upload(client, name, self._plan_text())
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["name"], expected)
                saved = Path(payload["path"])
                self.assertEqual(saved.parent.resolve(), self.upload_dir.resolve())
                self.assertTrue(saved.exists())

    def test_upload_empty_or_dot_names_fall_back_to_infrast_json(self):
        client = self._new_client()

        for name in ("", ".", "..", "../", "..\\"):
            with self.subTest(name=name):
                response = self._upload(client, name, self._plan_text())
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["name"], "infrast.json")
                saved = Path(payload["path"])
                self.assertEqual(saved.parent.resolve(), self.upload_dir.resolve())
                self.assertTrue(saved.exists())

    # ── upload rejections ──────────────────────────────────────────────

    def test_upload_rejects_malformed_and_invalid_plans(self):
        client = self._new_client()

        cases = {
            "malformed": "not json at all",
            "root-list": json.dumps([{"plans": []}]),
            "root-string": json.dumps("plans"),
            "root-number": json.dumps(7),
            "missing-plans": json.dumps({"rooms": {}}),
            "plans-null": json.dumps({"plans": None}),
            "plans-empty": json.dumps({"plans": []}),
            "plans-object": json.dumps({"plans": {"a": {"rooms": {}}}}),
            "plan-not-object": json.dumps({"plans": ["x"]}),
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                response = self._upload(client, "attempt.json", content)
                self.assertEqual(response.status_code, 400, label)
                self.assertFalse((self.upload_dir / "attempt.json").exists(), label)
        self.assertEqual(list(self.upload_dir.glob("*.json")), [])

    # ── options listing ────────────────────────────────────────────────

    def test_options_values_are_absolute_in_top_level_and_by_client(self):
        client = self._new_client()
        uploaded = self._upload(client, "schedule.json", self._plan_text()).json()

        options = client.get("/api/options").json()

        top_level = self._custom_files(options)
        by_client = options["by_client"]["Official"]["infrast"]["custom_files"]
        self.assertTrue(top_level)
        self.assertTrue(by_client)
        for item in [*top_level, *by_client]:
            self.assertTrue(Path(item["value"]).is_absolute(), item)
        self.assertIn(uploaded["path"], [item["value"] for item in top_level])
        self.assertIn(uploaded["path"], [item["value"] for item in by_client])

    def test_options_keep_builtin_nested_filename_in_label(self):
        nested = self.core / "resource" / "custom_infrast" / "sub" / "night.json"
        nested.parent.mkdir(parents=True)
        nested.write_text(self._plan_text(), encoding="utf-8")
        client = self._new_client()

        files = self._custom_files(client.get("/api/options").json())

        nested_items = [item for item in files if Path(item["value"]).name == "night.json"]
        self.assertEqual(len(nested_items), 1)
        self.assertEqual(nested_items[0]["label"], str(Path("sub") / "night.json"))
        self.assertTrue(Path(nested_items[0]["value"]).is_absolute())
        self.assertEqual(Path(nested_items[0]["value"]).resolve(), nested.resolve())

    def test_options_lists_upload_after_fresh_router_and_client(self):
        first_client = self._new_client()
        uploaded = self._upload(first_client, "schedule.json", self._plan_text()).json()

        # A brand new store/runner/router/client must still see the on-disk upload.
        second_client = self._new_client()
        with second_client:
            options = second_client.get("/api/options").json()

        values = [item["value"] for item in self._custom_files(options)]
        self.assertIn(uploaded["path"], values)

    # ── profile + mapper preservation ──────────────────────────────────

    def test_profile_roundtrip_and_mapper_preserve_filename_and_plan_index(self):
        client = self._new_client()
        uploaded = self._upload(client, "schedule.json", self._plan_text()).json()
        filename = uploaded["path"]

        profile = {
            "name": "daily",
            "tasks": [
                {
                    "id": "infrast",
                    "type": "Infrast",
                    "enabled": True,
                    "name": "基建换班",
                    "params": {
                        "mode": "自定义基建配置",
                        "filename": filename,
                        "plan_index": 0,
                    },
                }
            ],
        }
        self.assertEqual(client.put("/api/profiles/daily", json=profile).status_code, 200)

        loaded = client.get("/api/profiles/daily").json()
        task = next(item for item in loaded["tasks"] if item["type"] == "Infrast")
        self.assertEqual(task["params"]["filename"], filename)
        self.assertTrue(Path(task["params"]["filename"]).is_absolute())
        self.assertEqual(task["params"]["plan_index"], 0)

        call = task_to_append_call(
            TaskDefinition(
                id="infrast",
                type="Infrast",
                params={"mode": "自定义基建配置", "filename": filename, "plan_index": "0"},
            )
        )
        self.assertEqual(call.params["filename"], filename)
        self.assertEqual(call.params["plan_index"], 0)
        self.assertEqual(call.params["mode"], 10000)


if __name__ == "__main__":
    unittest.main()
