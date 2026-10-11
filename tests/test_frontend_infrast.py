"""Frontend regression tests for MAA-FnOS issue 12: custom infrast schedule import.

Covered interfaces
------------------
* ``renderInfrastFilePicker(p)`` renders a button ``id="paramInfrastImport"`` and
  a file input ``id="paramInfrastFileUpload"`` with a JSON ``accept``.
* ``uploadInfrastFile(file)`` uploads the file text to ``/api/infrast/upload``,
  then refreshes UI options *before* writing the returned server path into the
  selected Infrast task (``params.filename`` + ``params.plan_index = 0``), so the
  picker rendered by that upload already lists the new path as selected. It
  schedules the existing save, resets the picker and leaves the old filename
  intact on failure. It must not corrupt an unrelated task when the user
  switches selection (or the whole profile) while the upload is still pending,
  and must keep the uploaded path even when the options refresh fails.

The harness runs the real ``web/tasks/index.js``, ``web/tasks/infrast.js`` and
``web/views/basement/taskEditor.js`` in a Node ``vm`` context. Only the network
``api`` call, notices and the render/save hooks are mocked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path


SERVER_PATH = "/srv/maa/data/custom_infrast/schedule.json"


HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const UPLOAD_MODE = "__UPLOAD_MODE__";
const SERVER_PATH = "/srv/maa/data/custom_infrast/schedule.json";

const elements = {};
function makeElement(id, props) {
  return Object.assign({
    id,
    value: "",
    type: "text",
    checked: false,
    disabled: false,
    innerHTML: "",
    className: "",
    classList: { add() {}, remove() {}, toggle() {} },
    dataset: {},
    setAttribute() {},
    closest() { return null; },
    querySelectorAll() { return []; },
  }, props || {});
}
elements["paramInfrastFileUpload"] = makeElement("paramInfrastFileUpload", { type: "file", value: "schedule.json" });
elements["taskEditor"] = makeElement("taskEditor");

const calls = [];
const notices = [];
// ``render`` counts hook invocations; the picker fields capture what the real
// ``renderInfrastFilePicker`` would render *at render time* (not re-rendered
// afterwards by the test) plus the options that were live at that moment.
const hooks = { save: 0, render: 0, lastPickerHtml: null, renderOptions: [] };
let optionsCalls = 0;
let uploadResolve = null;

const FRESH_OPTIONS = {
  resource: { default_client: "Official", clients: [{ label: "官服", value: "Official" }] },
  by_client: {
    Official: {
      infrast: { custom_files: [{ label: "schedule.json", value: SERVER_PATH }] },
    },
  },
  infrast: { custom_files: [{ label: "schedule.json", value: SERVER_PATH }] },
};

function fakeFile(name, payload) {
  return {
    name: name,
    type: "application/json",
    size: 128,
    text: async () => JSON.stringify(payload),
  };
}

const context = {
  console,
  state: {
    profile: {
      name: "daily",
      adb: { client_type: "Official" },
      tasks: [
        {
          id: "infrast-a",
          type: "Infrast",
          enabled: true,
          name: "基建换班",
          params: { mode: "自定义基建配置", filename: "old.json" },
          strategy: {},
        },
        {
          id: "infrast-b",
          type: "Infrast",
          enabled: true,
          name: "基建换班2",
          params: { mode: "常规模式" },
          strategy: {},
        },
      ],
    },
    selectedTask: 0,
    options: null,
    capabilities: null,
    saveTimer: null,
    settingMode: "advanced",
  },
  document: {
    getElementById: (id) => elements[id] || null,
    querySelectorAll: () => [],
  },
  // Production code looks DOM nodes up through the global ``$`` helper.
  $: (id) => elements[id] || null,
  isProfileEditingLocked: () => false,
  isRunnerBusy: () => false,
  closeTaskMenus: () => {},
  closeTaskContextMenu: () => {},
  addLocalLog: () => {},
  showError: (error) => {
    notices.push({ level: "error", message: error && error.message ? error.message : String(error) });
  },
  showNotice: (message, level) => {
    notices.push({ level: level || "info", message: String(message) });
  },
  api: async function (path, init) {
    calls.push({
      path: path,
      method: (init && init.method) || "GET",
      body: init && init.body ? init.body : null,
    });
    if (path === "/api/infrast/upload") {
      if (UPLOAD_MODE === "error") throw new Error("network down");
      if (UPLOAD_MODE === "pending") {
        return new Promise((resolve) => { uploadResolve = resolve; });
      }
      return { ok: true, name: "schedule.json", path: SERVER_PATH };
    }
    if (path === "/api/options") {
      optionsCalls += 1;
      if (UPLOAD_MODE === "options_error") throw new Error("options endpoint down");
      return FRESH_OPTIONS;
    }
    return {};
  },
  loadOptions: async function () {
    const options = await context.api("/api/options");
    context.state.options = options;
    if (typeof setTaskFormOptions === "function") {
      setTaskFormOptions(Object.assign({}, options, { capabilities: context.state.capabilities }));
    }
    return options;
  },
  __elements: elements,
  __calls: calls,
  __notices: notices,
  __hooks: hooks,
  // The test body below runs inside this same VM context, so it must reach the
  // real production bindings. Expose the outer helpers under their plain names.
  elements: elements,
  calls: calls,
  notices: notices,
  hooks: hooks,
  fakeFile: fakeFile,
  FRESH_OPTIONS: FRESH_OPTIONS,
  SERVER_PATH: SERVER_PATH,
  setTimeout,
  clearTimeout,
  JSON,
  encodeURIComponent,
};

// ``api`` mutates these outer bindings, so expose them as live getters instead
// of snapshotting their current values into the context.
Object.defineProperty(context, "optionsCalls", { get: () => optionsCalls });
Object.defineProperty(context, "uploadResolve", { get: () => uploadResolve });

vm.createContext(context);
for (const file of [
  "web/tasks/index.js",
  "web/tasks/infrast.js",
  "web/views/basement/taskEditor.js",
]) {
  vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file });
}

// Replace the heavy render/save hooks with counters so they can be observed.
vm.runInContext("scheduleSave = function () { __hooks.save += 1; };", context);
vm.runInContext(`
renderEditor = function () {
  __hooks.render += 1;
  // Record what the real picker renders *right now*, using the currently
  // selected task's params and the options that are live at render time, so the
  // tests can prove the auto-render already shows the freshly uploaded path.
  const task = state.profile && Array.isArray(state.profile.tasks)
    ? state.profile.tasks[state.selectedTask]
    : null;
  const params = task && task.params ? task.params : {};
  const clientOptions = typeof activeClientOptions === "function" ? activeClientOptions() : null;
  __hooks.renderOptions = (clientOptions && clientOptions.infrast && clientOptions.infrast.custom_files)
    || (UI_OPTIONS && UI_OPTIONS.infrast && UI_OPTIONS.infrast.custom_files)
    || [];
  __hooks.lastPickerHtml = renderInfrastFilePicker(
    Object.assign({ mode: params.mode, filename: params.filename }, params)
  );
};
`, context);
vm.runInContext("renderTasks = function () { __hooks.render += 1; };", context);
vm.runInContext("renderAll = function () { __hooks.render += 1; };", context);
vm.runInContext("collectTaskForm = function () {};", context);

// The body is injected as a JSON string literal and evaluated with
// ``vm.runInContext`` so that its identifiers resolve to the production
// functions living in this context rather than to the outer Node scope.
const body = __BODY__;
const result = vm.runInContext("(async () => {\n" + body + "\n})()", context);
result.then((value) => {
  console.log(JSON.stringify(value === undefined ? null : value));
}).catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""


def run_frontend(mode: str, body: str) -> dict:
    script = HARNESS.replace("__UPLOAD_MODE__", mode).replace("__BODY__", json.dumps(textwrap.dedent(body)))
    completed = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
        timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout.strip().splitlines()[-1])


class FrontendInfrastImportTest(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is not available")

    def test_picker_renders_import_button_and_file_input(self):
        result = run_frontend(
            "success",
            """
            setTaskFormOptions({
              resource: { default_client: "Official", clients: [{ label: "官服", value: "Official" }] },
              by_client: {
                Official: {
                  infrast: {
                    custom_files: [
                      { label: "sub/night.json", value: "/srv/maa/resource/custom_infrast/sub/night.json" },
                    ],
                  },
                },
              },
              infrast: { custom_files: [{ label: "plan.json", value: "/srv/maa/resource/custom_infrast/plan.json" }] },
            });
            return {
              html: renderInfrastFilePicker({
                mode: "自定义基建配置",
                filename: "/srv/maa/resource/custom_infrast/plan.json",
              }),
            };
            """,
        )

        html = result["html"]
        self.assertIn('id="paramInfrastImport"', html)
        self.assertIn('id="paramInfrastFileUpload"', html)
        self.assertIn('type="file"', html)
        self.assertIn("json", html.lower())
        # The existing select/manual path inputs must not regress.
        self.assertIn('id="paramInfrastFileSelect"', html)
        self.assertIn('id="paramInfrastFilename"', html)

    def test_upload_sets_filename_plan_index_and_refreshes_options(self):
        result = run_frontend(
            "success",
            """
            if (typeof uploadInfrastFile !== "function") {
              throw new Error("uploadInfrastFile is not implemented");
            }
            const taskA = state.profile.tasks[0];
            const taskB = state.profile.tasks[1];
            await uploadInfrastFile(fakeFile("schedule.json", { plans: [{ name: "默认方案", rooms: { manufacture: [] } }] }));
            const uploadCall = calls.find((call) => call.path === "/api/infrast/upload") || {};
            const optionsIndex = calls.findIndex((call) => call.path === "/api/options");
            const uploadIndex = calls.findIndex((call) => call.path === "/api/infrast/upload");
            return {
              filename: taskA.params.filename === undefined ? null : taskA.params.filename,
              planIndex: taskA.params.plan_index === undefined ? null : taskA.params.plan_index,
              taskBFilename: taskB.params.filename === undefined ? null : taskB.params.filename,
              saveCount: hooks.save,
              renderCount: hooks.render,
              optionsCalls: optionsCalls,
              callPaths: calls.map((call) => call.path),
              uploadBeforeOptions: uploadIndex !== -1 && optionsIndex !== -1 && uploadIndex < optionsIndex,
              uploadMethod: uploadCall.method || null,
              uploadBody: uploadCall.body || null,
              fileInputValue: elements["paramInfrastFileUpload"].value,
              autoPickerHtml: hooks.lastPickerHtml,
              autoRenderOptionValues: (hooks.renderOptions || []).map(
                (option) => (option && typeof option === "object") ? option.value : option
              ),
              noticeMessages: notices.map((notice) => notice.message),
              noticeLevels: notices.map((notice) => notice.level),
            };
            """,
        )

        self.assertEqual(result["filename"], SERVER_PATH)
        self.assertEqual(result["planIndex"], 0)
        self.assertIsNone(result["taskBFilename"])
        self.assertGreaterEqual(result["saveCount"], 1)
        self.assertIn("/api/infrast/upload", result["callPaths"])
        self.assertIn("/api/options", result["callPaths"])
        self.assertGreaterEqual(result["optionsCalls"], 1)
        self.assertEqual(result["uploadMethod"], "POST")
        self.assertIsNotNone(result["uploadBody"])
        self.assertIn("schedule.json", result["uploadBody"])
        self.assertIn("plans", result["uploadBody"])
        # Picker reset so the same file can be picked twice.
        self.assertEqual(result["fileInputValue"], "")
        # The options must be refreshed *before* the rendered picker is produced,
        # so the auto-render (renderEditor called by uploadInfrastFile) already
        # contains the freshly uploaded path as the selected option.
        self.assertTrue(result["uploadBeforeOptions"])
        self.assertGreaterEqual(result["renderCount"], 1)
        self.assertIsNotNone(result["autoPickerHtml"])
        self.assertIn(SERVER_PATH, result["autoRenderOptionValues"])
        self.assertIn(f'value="{SERVER_PATH}" selected', result["autoPickerHtml"])
        self.assertIn('id="paramInfrastImport"', result["autoPickerHtml"])
        # Success notice shows the returned name, never the internal server path.
        self.assertTrue(any("schedule.json" in message for message in result["noticeMessages"]))
        self.assertFalse(any(SERVER_PATH in message for message in result["noticeMessages"]))
        self.assertNotIn("error", result["noticeLevels"])

    def test_upload_failure_keeps_old_filename_and_notifies(self):
        result = run_frontend(
            "error",
            """
            if (typeof uploadInfrastFile !== "function") {
              throw new Error("uploadInfrastFile is not implemented");
            }
            const taskA = state.profile.tasks[0];
            let threw = null;
            try {
              await uploadInfrastFile(fakeFile("schedule.json", { plans: [{ name: "默认方案", rooms: {} }] }));
            } catch (error) {
              threw = String(error && error.message ? error.message : error);
            }
            return {
              threw: threw,
              filename: taskA.params.filename === undefined ? null : taskA.params.filename,
              planIndex: taskA.params.plan_index === undefined ? null : taskA.params.plan_index,
              notices: notices.map((notice) => notice.level),
              callPaths: calls.map((call) => call.path),
            };
            """,
        )

        self.assertEqual(result["filename"], "old.json")
        self.assertIsNone(result["planIndex"])
        self.assertIn("/api/infrast/upload", result["callPaths"])
        self.assertGreaterEqual(len(result["notices"]), 1)

    def test_pending_upload_does_not_touch_switched_task(self):
        result = run_frontend(
            "pending",
            """
            if (typeof uploadInfrastFile !== "function") {
              throw new Error("uploadInfrastFile is not implemented");
            }
            const taskA = state.profile.tasks[0];
            const taskB = state.profile.tasks[1];
            const pending = uploadInfrastFile(fakeFile("schedule.json", { plans: [{ name: "默认方案", rooms: {} }] }));
            for (let i = 0; i < 50 && uploadResolve === null; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 1));
            }
            const reachedNetwork = uploadResolve !== null;
            // User switches to another task while the upload is still in flight.
            state.selectedTask = 1;
            if (uploadResolve) {
              uploadResolve({ ok: true, name: "schedule.json", path: SERVER_PATH });
            }
            await pending;
            return {
              reachedNetwork: reachedNetwork,
              taskAFilename: taskA.params.filename === undefined ? null : taskA.params.filename,
              taskBHasFilename: Object.prototype.hasOwnProperty.call(taskB.params, "filename"),
              taskBPlanIndex: taskB.params.plan_index === undefined ? null : taskB.params.plan_index,
            };
            """,
        )

        self.assertTrue(result["reachedNetwork"])
        self.assertFalse(result["taskBHasFilename"])
        self.assertIsNone(result["taskBPlanIndex"])

    def test_pending_upload_does_not_touch_task_of_replaced_profile(self):
        result = run_frontend(
            "pending",
            """
            if (typeof uploadInfrastFile !== "function") {
              throw new Error("uploadInfrastFile is not implemented");
            }
            const pending = uploadInfrastFile(fakeFile("schedule.json", { plans: [{ name: "默认方案", rooms: {} }] }));
            for (let i = 0; i < 50 && uploadResolve === null; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 1));
            }
            const reachedNetwork = uploadResolve !== null;
            // Auto-save can replace the whole profile while the upload is in flight.
            state.profile = {
              name: "night",
              adb: { client_type: "Official" },
              tasks: [
                {
                  id: "infrast-b",
                  type: "Infrast",
                  enabled: true,
                  name: "其他基建",
                  params: { mode: "常规模式" },
                  strategy: {},
                },
              ],
            };
            state.selectedTask = 0;
            if (uploadResolve) {
              uploadResolve({ ok: true, name: "schedule.json", path: SERVER_PATH });
            }
            await pending;
            const other = state.profile.tasks[0];
            return {
              reachedNetwork: reachedNetwork,
              profileName: state.profile.name,
              otherHasFilename: Object.prototype.hasOwnProperty.call(other.params, "filename"),
              otherPlanIndex: other.params.plan_index === undefined ? null : other.params.plan_index,
            };
            """,
        )

        self.assertTrue(result["reachedNetwork"])
        self.assertEqual(result["profileName"], "night")
        self.assertFalse(result["otherHasFilename"])
        self.assertIsNone(result["otherPlanIndex"])

    def test_options_refresh_failure_still_stores_path_and_saves(self):
        result = run_frontend(
            "options_error",
            """
            if (typeof uploadInfrastFile !== "function") {
              throw new Error("uploadInfrastFile is not implemented");
            }
            const taskA = state.profile.tasks[0];
            await uploadInfrastFile(fakeFile("schedule.json", { plans: [{ name: "默认方案", rooms: {} }] }));
            return {
              filename: taskA.params.filename === undefined ? null : taskA.params.filename,
              planIndex: taskA.params.plan_index === undefined ? null : taskA.params.plan_index,
              saveCount: hooks.save,
              callPaths: calls.map((call) => call.path),
              optionsCalls: optionsCalls,
              noticeLevels: notices.map((notice) => notice.level),
            };
            """,
        )

        # The upload itself succeeded; the failed option refresh must not stop
        # the path/plan_index from landing on the task nor the pending save.
        self.assertEqual(result["filename"], SERVER_PATH)
        self.assertEqual(result["planIndex"], 0)
        self.assertGreaterEqual(result["saveCount"], 1)
        self.assertIn("/api/options", result["callPaths"])
        self.assertGreaterEqual(result["optionsCalls"], 1)
        # A warning (list is stale), never an error: the import worked.
        self.assertIn("warning", result["noticeLevels"])
        self.assertNotIn("error", result["noticeLevels"])

    def test_pending_upload_updates_latest_task_when_same_name_profile_replaced(self):
        result = run_frontend(
            "pending",
            """
            if (typeof uploadInfrastFile !== "function") {
              throw new Error("uploadInfrastFile is not implemented");
            }
            const staleTask = state.profile.tasks[0];
            const pending = uploadInfrastFile(fakeFile("schedule.json", { plans: [{ name: "默认方案", rooms: {} }] }));
            for (let i = 0; i < 50 && uploadResolve === null; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 1));
            }
            const reachedNetwork = uploadResolve !== null;
            // Auto-save swaps in a brand new profile object that keeps the same
            // name and task id, so the upload must write to the *latest* task
            // rather than the stale reference captured before the await.
            state.profile = {
              name: "daily",
              adb: { client_type: "Official" },
              tasks: [
                {
                  id: "infrast-a",
                  type: "Infrast",
                  enabled: true,
                  name: "基建换班",
                  params: { mode: "自定义基建配置", filename: "old.json" },
                  strategy: {},
                },
              ],
            };
            state.selectedTask = 0;
            if (uploadResolve) {
              uploadResolve({ ok: true, name: "schedule.json", path: SERVER_PATH });
            }
            await pending;
            const freshTask = state.profile.tasks[0];
            return {
              reachedNetwork: reachedNetwork,
              freshFilename: freshTask.params.filename === undefined ? null : freshTask.params.filename,
              freshPlanIndex: freshTask.params.plan_index === undefined ? null : freshTask.params.plan_index,
              staleFilename: staleTask.params.filename === undefined ? null : staleTask.params.filename,
              stalePlanIndex: staleTask.params.plan_index === undefined ? null : staleTask.params.plan_index,
              saveCount: hooks.save,
            };
            """,
        )

        self.assertTrue(result["reachedNetwork"])
        # The replaced-in task object receives the upload ...
        self.assertEqual(result["freshFilename"], SERVER_PATH)
        self.assertEqual(result["freshPlanIndex"], 0)
        # ... and the orphaned old object is never mutated.
        self.assertEqual(result["staleFilename"], "old.json")
        self.assertIsNone(result["stalePlanIndex"])
        self.assertGreaterEqual(result["saveCount"], 1)


if __name__ == "__main__":
    unittest.main()
