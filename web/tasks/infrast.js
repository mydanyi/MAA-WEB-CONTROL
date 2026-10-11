function renderInfrastGeneral(p, escapeHtml = escapeHtmlFallback) {
  const facilities = p.facilities || allFacilities();
  return `
    <div class="maaParams wideForm infrastForm">
      <span>基建模式</span><select id="paramInfrastMode">${selectOptions(INFRAST_MODES, p.mode || "常规模式", escapeHtml)}</select>
      <span>无人机用途</span><select id="paramDrone">${selectOptions(DRONE_OPTIONS, p.drone || "贸易站-龙门币", escapeHtml)}</select>
      <span class="centerText">基建工作心情阈值: ${numberValue(p.mood, 30)}% ${hint("若启用自定义换班，该字段仅针对 autofill 和使用干员编组的房间有效", escapeHtml)}</span>
      <input id="paramMood" type="range" min="0" max="100" value="${numberValue(p.mood, 30)}" />
      <div class="facilityBox">${allFacilities().map((name) => checkRow(`facility-${name}`, name, facilities.includes(name))).join("")}</div>
      <div class="twoButtons"><button type="button" data-facility-action="all">全选</button><button type="button" data-facility-action="none">清空</button></div>
    </div>
  `;
}

function renderInfrastAdvanced(p) {
  const customVisible = p.mode === "自定义基建配置" || Number(p.mode) === 10000;
  const rotationVisible = p.mode === "队列轮换" || Number(p.mode) === 20000;
  return `
    <div class="maaParams wideForm">
      ${customVisible ? renderInfrastFilePicker(p) : ""}
      ${customVisible ? '<span>方案序号（从 0 开始）</span><input id="paramInfrastPlanIndex" type="number" min="0" value="' + numberValue(p.plan_index, 0) + '" />' : ""}
      ${rotationVisible ? unsupportedLine("轮换计划", "MaaCore Infrast 没有 rotation 参数，队列轮换的具体计划请写进自定义基建配置文件。") : ""}
      ${checkRow("dorm_trust", "宿舍空余位置蹭信赖", p.dorm_trust)}
      ${checkRow("skip_entered", "不将已进驻的干员放入宿舍", p.skip_entered ?? true)}
      ${checkRow("stone_fragment", "源石碎片自动补货", p.stone_fragment ?? true)}
      ${checkRow("collect_credit", "会客室信息板收取信用", p.collect_credit ?? true)}
      ${checkRow("clue_exchange", "进行线索交流", p.clue_exchange ?? true)}
      ${checkRow("send_clue", "赠送线索", p.send_clue ?? true)}
      ${checkRow("continue_training", "训练完成后继续尝试专精当前技能", p.continue_training)}
    </div>
  `;
}

function onInfrastFileSelect(select) {
  const input = $("paramInfrastFilename");
  if (input && select.value) input.value = select.value;
}

function collectInfrastParams() {
  const params = {};
  addValue(params, "mode", "paramInfrastMode", "常规模式");
  addValue(params, "drone", "paramDrone", "贸易站-龙门币");
  addNumber(params, "mood", "paramMood", 30);
  if ($("facility-制造站")) {
    params.facilities = allFacilities().filter((name) => boolOf(`facility-${name}`));
  }
  addBool(params, "dorm_trust", "dorm_trust");
  addBool(params, "skip_entered", "skip_entered");
  addBool(params, "stone_fragment", "stone_fragment");
  addBool(params, "collect_credit", "collect_credit");
  addBool(params, "clue_exchange", "clue_exchange");
  addBool(params, "send_clue", "send_clue");
  addBool(params, "continue_training", "continue_training");
  addValue(params, "filename", "paramInfrastFilename", "");
  addNumber(params, "plan_index", "paramInfrastPlanIndex", 0);
  return params;
}


// /api/options 已经返回 custom_infrast 下的全部方案文件（绝对路径），不该让用户手敲路径。
function renderInfrastFilePicker(p) {
  const current = String(p.filename || p.custom_infrast_file || "");
  const files = activeClientOptions()?.infrast?.custom_files || UI_OPTIONS?.infrast?.custom_files || [];
  const options = [{ label: "（手动填写路径）", value: "" }, ...files.map(normalizeOption)];
  const known = !current || options.some((option) => String(option.value) === current);
  return `<span>自定义基建文件</span>
    <select id="paramInfrastFileSelect">${selectOptions(options, known ? current : "", escapeHtmlFallback)}</select>
    <input class="wideInput" id="paramInfrastFilename" value="${escapeHtmlFallback(current)}" placeholder="/app/data/custom_infrast/方案.json（服务器绝对路径）" />
    <button type="button" id="paramInfrastImport">导入排班表</button>
    <input id="paramInfrastFileUpload" type="file" accept=".json,application/json" hidden />`;
}

// 上传选中的排班表文件，成功后先刷新选项，再把服务器返回的绝对路径写入原任务（filename + plan_index=0）。
async function uploadInfrastFile(file) {
  if (!file || isProfileEditingLocked()) return;
  const task = selectedTask();
  if (!task || task.type !== "Infrast") return;
  // 任何 await 之前先固定目标：配置名 + 任务 id，并先把屏幕上已编辑的内容收进任务。
  const profileName = state.profile?.name;
  const taskId = task.id;
  const displayName = file.name || "排班表";
  const importButton = $("paramInfrastImport");
  const fileInput = $("paramInfrastFileUpload");
  collectTaskForm();

  try {
    if (importButton) importButton.disabled = true;
    const content = await file.text();
    const result = await api("/api/infrast/upload", {
      method: "POST",
      body: JSON.stringify({ name: file.name, content })
    });
    if (!result || !result.ok || !result.path) throw new Error("服务器未返回有效的排班表路径");
    const uploadedPath = String(result.path);
    const uploadedName = String(result.name || file.name || displayName);

    // 先刷新选项，让下拉列表包含刚上传的方案，再把它落到任务上，否则界面上仍是旧列表。
    let optionsRefreshed = true;
    try {
      const options = await api("/api/options");
      state.options = options;
      if (typeof setTaskFormOptions === "function") {
        setTaskFormOptions({ ...options, capabilities: state.capabilities });
      }
    } catch (optionsError) {
      // 选项列表刷新失败不影响已经上传文件的写入与保存。
      optionsRefreshed = false;
    }

    // 上传期间可能被自动保存替换掉 state.profile，也可能切换了选中任务/配置。
    // 每次都在最新的 state.profile 里按 id 重新定位原任务，不继续引用旧对象。
    const profile = state.profile;
    const original = profile && profile.name === profileName
      ? (profile.tasks || []).find((item) => item.id === taskId)
      : null;
    let applied = false;
    if (original && original.type === "Infrast" && !isProfileEditingLocked()) {
      // 只有原任务仍被选中时才把等待期间用户已编辑的内容收进来，避免写到别的任务上。
      const stillSelected = state.profile?.tasks?.[state.selectedTask] === original;
      if (stillSelected) collectTaskForm();
      original.params = original.params || {};
      original.params.filename = uploadedPath;
      original.params.plan_index = 0;
      applied = true;
      // 原任务仍被选中时才刷新 DOM，否则不要拿它去覆盖当前选中的任务表单。
      if (stillSelected) renderEditor();
    }
    // 只有真正写到原任务时才安排保存。
    if (applied) scheduleSave();

    if (!applied) {
      showNotice(`已上传排班表 ${uploadedName}，可从列表中选择使用。`, "info");
    } else if (!optionsRefreshed) {
      showNotice(`已导入排班表 ${uploadedName}，但选项列表刷新失败，刷新页面即可看到。`, "warning");
    } else {
      showNotice(`已导入排班表：${uploadedName}`, "info");
    }
  } catch (error) {
    showError(error);
    showNotice("导入排班表失败，已保留原有配置。", "error");
    return;
  } finally {
    if (fileInput) fileInput.value = "";
    if (importButton) importButton.disabled = false;
  }
}
