"use strict";

const TOKEN = document.body.dataset.token;
const state = {
  files: [],
  nextId: 1,
  jobId: null,
  polling: null,
  busy: false,
  helpReturnFocus: null,
  directoryHandle: null,
  completedJob: null,
  resultBlob: null,
  resultFileName: null,
  resultSaved: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const elements = {
  form: $("#convertForm"),
  input: $("#fileInput"),
  choose: $("#chooseFiles"),
  drop: $("#dropZone"),
  list: $("#fileList"),
  empty: $("#emptyFiles"),
  template: $("#fileTemplate"),
  button: $("#convertButton"),
  outputName: $("#outputName"),
  progress: $("#progressPanel"),
  progressBar: $("#progressBar"),
  progressTrack: $(".progress-track"),
  progressPercent: $("#progressPercent"),
  progressStage: $("#progressStage"),
  progressLog: $("#progressLog"),
  result: $("#resultPanel"),
  resultSummary: $("#resultSummary"),
  stats: $("#resultStats"),
  reviewBox: $("#reviewBox"),
  reviewMeasures: $("#reviewMeasures"),
  warningBox: $("#warningBox"),
  resultTitle: $("#resultTitle"),
  savedLocation: $("#savedLocation"),
  chooseDirectory: $("#chooseDirectory"),
  directoryName: $("#directoryName"),
  directoryHint: $("#directoryHint"),
  compatibilityError: $("#browserSupportError"),
  reselectDirectory: $("#reselectDirectory"),
  help: $("#helpDrawer"),
  scrim: $("#drawerScrim"),
  toast: $("#toastRegion"),
};

const FILE_SYSTEM_ERROR = "当前浏览器不支持直接保存到文件夹。请使用最新版 Microsoft Edge 或 Google Chrome 打开本地页面；本应用不会改用浏览器下载。";

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function toast(message, type = "info") {
  const item = document.createElement("div");
  item.className = `toast ${type === "error" ? "error" : ""}`;
  if (type === "error") item.setAttribute("role", "alert");
  item.textContent = message;
  elements.toast.append(item);
  window.setTimeout(() => item.remove(), 4200);
}

function supportsDirectorySaving() {
  return typeof window.showDirectoryPicker === "function";
}

function showDirectoryCompatibilityError() {
  elements.compatibilityError.hidden = false;
  elements.chooseDirectory.setAttribute("aria-invalid", "true");
  toast(FILE_SYSTEM_ERROR, "error");
}

function updateDirectoryDisplay() {
  if (!state.directoryHandle) {
    elements.directoryName.textContent = "尚未选择文件夹";
    elements.directoryHint.textContent = "点击后由浏览器请求写入权限";
    elements.chooseDirectory.classList.remove("is-selected");
    return;
  }
  elements.directoryName.textContent = state.directoryHandle.name || "已选择文件夹";
  elements.directoryHint.textContent = "转换完成后将直接写入这里";
  elements.chooseDirectory.classList.add("is-selected");
  elements.chooseDirectory.removeAttribute("aria-invalid");
  elements.compatibilityError.hidden = true;
}

async function ensureWritePermission(handle) {
  const options = { mode: "readwrite" };
  if (typeof handle.queryPermission === "function") {
    const current = await handle.queryPermission(options);
    if (current === "granted") return true;
  }
  if (typeof handle.requestPermission === "function") {
    return (await handle.requestPermission(options)) === "granted";
  }
  // Chromium originally granted the picker permission without exposing these
  // optional helpers.  The actual write remains the final authority.
  return true;
}

async function chooseSaveDirectory() {
  if (!supportsDirectorySaving()) {
    showDirectoryCompatibilityError();
    return null;
  }
  try {
    const handle = await window.showDirectoryPicker({
      id: "midi-to-mscz-output",
      mode: "readwrite",
      startIn: "documents",
    });
    if (!await ensureWritePermission(handle)) {
      throw new Error("没有获得该文件夹的写入权限，请重新选择并允许保存。 ");
    }
    state.directoryHandle = handle;
    updateDirectoryDisplay();
    return handle;
  } catch (error) {
    if (error?.name === "AbortError") return null;
    toast(error?.message || "无法使用这个文件夹，请重新选择。", "error");
    return null;
  }
}

async function fetchResultBlob(jobId) {
  const response = await fetch(`/api/results/${encodeURIComponent(jobId)}`, {
    headers: { "X-App-Token": TOKEN },
    cache: "no-store",
  });
  if (!response.ok) {
    let message = "无法从本机转换服务读取 MSCZ 结果。";
    try {
      const payload = await response.json();
      if (payload?.error) message = payload.error;
    } catch (_) { /* response was not JSON */ }
    throw new Error(message);
  }
  return response.blob();
}

async function acknowledgeSaved(jobId) {
  await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}/saved`, { method: "POST" });
}

async function sha256Hex(blob) {
  if (!window.crypto?.subtle) {
    throw new Error("当前浏览器无法校验保存结果，请使用最新版 Microsoft Edge 或 Google Chrome。");
  }
  const bytes = await blob.arrayBuffer();
  const digest = await window.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
}

async function writeResultFile(handle, fileName, blob, expectedSize, expectedSha256) {
  if (!handle || !await ensureWritePermission(handle)) {
    throw new Error("保存文件夹的写入权限已失效，请重新选择目录。");
  }
  if (!Number.isInteger(Number(expectedSize)) || Number(expectedSize) <= 0 || blob.size !== Number(expectedSize)) {
    throw new Error("转换结果大小校验失败，尚未写入文件夹，请重试保存。");
  }
  if (!expectedSha256 || await sha256Hex(blob) !== String(expectedSha256).toLowerCase()) {
    throw new Error("转换结果完整性校验失败，尚未写入文件夹，请重试保存。");
  }
  let existing = null;
  try {
    existing = await handle.getFileHandle(fileName);
  } catch (error) {
    if (error?.name !== "NotFoundError") throw error;
  }
  if (existing && !window.confirm(`所选文件夹中已有“${fileName}”。是否覆盖这个文件？`)) {
    throw new Error("已取消覆盖；请重新选择其他文件夹后重试保存。");
  }
  const fileHandle = existing || await handle.getFileHandle(fileName, { create: true });
  const writable = await fileHandle.createWritable();
  try {
    await writable.write(blob);
    await writable.close();
  } catch (error) {
    try { await writable.abort(); } catch (_) { /* best-effort cleanup */ }
    throw error;
  }
  const saved = await fileHandle.getFile();
  if (saved.size !== Number(expectedSize)) {
    throw new Error("保存后的文件大小校验失败，请检查磁盘空间后重试。");
  }
  if (await sha256Hex(saved) !== String(expectedSha256).toLowerCase()) {
    throw new Error("保存后的文件内容校验失败，请重新选择目录后重试。");
  }
}

function guessedStaff(name) {
  const lower = name.toLowerCase();
  if (/(piano|bass|伴奏|钢琴|低音|left|chord)/i.test(lower)) return "bass";
  if (/(vocal|voice|melody|人声|主唱|旋律|lead)/i.test(lower)) return "treble";
  const counts = state.files.reduce((acc, item) => {
    const select = item.element?.querySelector('[data-field="staff"]');
    acc[select?.value || item.staff || "treble"] += 1;
    return acc;
  }, { treble: 0, bass: 0 });
  return counts.treble <= counts.bass ? "treble" : "bass";
}

function validMidiFile(file) {
  const suffix = file.name.toLowerCase();
  return suffix.endsWith(".mid") || suffix.endsWith(".midi");
}

function addFiles(fileList) {
  const candidates = [...fileList];
  let added = 0;
  for (const file of candidates) {
    if (!validMidiFile(file)) {
      toast(`已跳过“${file.name}”：请选择 .mid 或 .midi 文件。`, "error");
      continue;
    }
    if (file.size > 32 * 1024 * 1024) {
      toast(`“${file.name}”超过 32 MB，无法添加。`, "error");
      continue;
    }
    if (state.files.length >= 8) {
      toast("一次最多处理 8 个 MIDI（每个谱表最多 4 个）。", "error");
      break;
    }
    const duplicate = state.files.some(item =>
      item.file.name === file.name && item.file.size === file.size && item.file.lastModified === file.lastModified
    );
    if (duplicate) {
      toast(`“${file.name}”已经在列表中。`);
      continue;
    }
    const staff = guessedStaff(file.name);
    const fragment = elements.template.content.cloneNode(true);
    const card = $(".file-card", fragment);
    const id = `file_${state.nextId++}`;
    card.dataset.id = id;
    $(".file-name", card).textContent = file.name;
    $(".file-size", card).textContent = `${formatSize(file.size)} · 独立声部`;
    $('[data-field="staff"]', card).value = staff;
    const removeButton = $(".remove-file", card);
    removeButton.setAttribute("aria-label", `移除 ${file.name}`);
    removeButton.addEventListener("click", () => removeFile(id));
    elements.list.append(fragment);
    state.files.push({ id, file, element: elements.list.lastElementChild, staff });
    added += 1;
  }
  elements.input.value = "";
  updateFileState();
  if (added && state.files.length === added) {
    const firstName = state.files[0].file.name.replace(/\.(?:mid|midi)$/i, "");
    elements.outputName.value = `${firstName}_标准化.mscz`;
  }
}

function removeFile(id) {
  const index = state.files.findIndex(item => item.id === id);
  if (index < 0) return;
  state.files[index].element.remove();
  state.files.splice(index, 1);
  updateFileState();
}

function updateFileState() {
  elements.empty.hidden = state.files.length > 0;
  elements.drop.querySelector("strong").textContent = state.files.length
    ? `继续添加 MIDI（当前 ${state.files.length} 个）`
    : "把 MIDI 文件拖到这里";
}

function modeChanged(groupName) {
  const selected = $(`input[name="${groupName}Mode"]:checked`)?.value || "auto";
  const prefix = groupName === "meter" ? "meter" : groupName;
  const field = $(`#${prefix}Field`);
  if (!field) return;
  const control = $("input, select", field);
  control.disabled = selected === "auto";
  field.classList.toggle("is-disabled", selected === "auto");
  if (groupName === "meter") {
    const pickup = $("#pickupBeats");
    const hint = $("#pickupHint");
    pickup.disabled = selected === "auto";
    hint.textContent = selected === "auto"
      ? "自动拍号时会读取 MIDI 明确记录的短首小节；如需手填，请先选择“手动指定”拍号。"
      : "填写首个不完整小节的长度；没有弱起填 0，例如一拍弱起填 1。";
  }
}

function openHelp(topic = null, trigger = document.activeElement) {
  state.helpReturnFocus = trigger instanceof HTMLElement ? trigger : null;
  for (const region of [$(".topbar"), $(".shell")]) region.inert = true;
  elements.help.classList.add("is-open");
  elements.help.setAttribute("aria-hidden", "false");
  elements.help.inert = false;
  elements.scrim.hidden = false;
  document.body.style.overflow = "hidden";
  if (topic) {
    window.setTimeout(() => {
      const article = $(`#help-${topic}`);
      if (!article) return;
      article.scrollIntoView({ block: "start" });
      article.classList.remove("is-highlighted");
      void article.offsetWidth;
      article.classList.add("is-highlighted");
    }, 80);
  }
  $("#closeHelp").focus();
}

function closeHelp() {
  elements.help.classList.remove("is-open");
  elements.help.setAttribute("aria-hidden", "true");
  elements.help.inert = true;
  elements.scrim.hidden = true;
  document.body.style.overflow = "";
  for (const region of [$(".topbar"), $(".shell")]) region.inert = false;
  if (state.helpReturnFocus?.isConnected) state.helpReturnFocus.focus();
  state.helpReturnFocus = null;
}

function numberValue(selector, label, minimum, maximum) {
  const control = $(selector);
  const value = Number(control.value);
  if (!Number.isFinite(value) || value < minimum || value > maximum) {
    control.focus();
    throw new Error(`${label}需要填写 ${minimum} 到 ${maximum} 之间的数字。`);
  }
  return value;
}

function collectConfiguration() {
  if (!state.files.length) {
    elements.choose.focus();
    throw new Error("请先添加至少一个 MIDI 文件。");
  }
  const staffCounts = { treble: 0, bass: 0 };
  const files = state.files.map(item => {
    const card = item.element;
    const staff = $('[data-field="staff"]', card).value;
    staffCounts[staff] += 1;
    return {
      upload_id: item.id,
      name: item.file.name,
      staff,
      octaves: Number($('[data-field="octaves"]', card).value),
      semitones: Number($('[data-field="semitones"]', card).value),
      velocity_min: Number($('[data-field="velocity"]', card).value),
      offset_beats: Number($('[data-field="offset"]', card).value),
    };
  });
  if (staffCounts.treble > 4 || staffCounts.bass > 4) {
    throw new Error("每个谱表最多安排 4 个 MIDI。请调整高音 / 低音谱表分配。");
  }
  for (const row of files) {
    if (!Number.isInteger(row.octaves) || row.octaves < -3 || row.octaves > 3) throw new Error("八度调整应在 −3 到 +3 之间。");
    if (!Number.isInteger(row.semitones) || row.semitones < -11 || row.semitones > 11) throw new Error("额外移调应在 −11 到 +11 个半音之间。");
    if (!Number.isInteger(row.velocity_min) || row.velocity_min < 1 || row.velocity_min > 127) throw new Error("力度阈值应在 1 到 127 之间。");
    if (!Number.isFinite(row.offset_beats) || row.offset_beats < -128 || row.offset_beats > 128) throw new Error("时间对齐应在 −128 到 +128 个四分音符拍之间。");
  }

  const bpmMode = $('input[name="bpmMode"]:checked').value;
  const meterMode = $('input[name="meterMode"]:checked').value;
  const keyMode = $('input[name="keyMode"]:checked').value;
  const outputName = elements.outputName.value.trim() || "标准化乐谱.mscz";
  return {
    version: 1,
    output_name: outputName.toLowerCase().endsWith(".mscz") ? outputName : `${outputName}.mscz`,
    files,
    settings: {
      bpm_mode: bpmMode,
      bpm: bpmMode === "auto" ? null : numberValue("#bpm", "BPM", 20, 400),
      meter_mode: meterMode,
      time_signature: $("#timeSignature").value,
      key_mode: keyMode,
      key_fifths: Number($("#keyFifths").value),
      smallest_note: Number($("#smallestNote").value),
      pickup_beats: meterMode === "manual" ? numberValue("#pickupBeats", "弱起拍长度", 0, 32) : 0,
      title: $("#title").value,
      composer: $("#composer").value,
      auto_latency: $("#autoLatency").checked,
      auto_trim: $("#autoTrim").checked,
      detect_arpeggios: $("#detectArpeggios").checked,
      detect_tuplets: $("#detectTuplets").checked,
      detect_grace_notes: $("#detectGrace").checked,
      detect_swing: $("#detectSwing").checked,
      detect_ornaments: $("#detectOrnaments").checked,
    },
  };
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-App-Token", TOKEN);
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const type = response.headers.get("Content-Type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    throw new Error(payload?.error || `请求失败（${response.status}）`);
  }
  return payload;
}

function showProgress() {
  elements.result.hidden = true;
  elements.progress.hidden = false;
  elements.progressBar.style.width = "4%";
  elements.progressTrack.setAttribute("aria-valuenow", "4");
  elements.progressPercent.textContent = "4%";
  elements.progressStage.textContent = "正在安全接收文件";
  elements.progressLog.replaceChildren();
  elements.button.disabled = true;
  elements.progress.scrollIntoView({ behavior: "smooth", block: "center" });
}

function updateProgress(job) {
  const percent = Math.max(0, Math.min(100, Number(job.progress) || 0));
  elements.progressBar.style.width = `${percent}%`;
  elements.progressTrack.setAttribute("aria-valuenow", String(percent));
  elements.progressPercent.textContent = `${Math.round(percent)}%`;
  elements.progressStage.textContent = job.stage || "正在转换";
  const messages = Array.isArray(job.messages) ? job.messages : [];
  elements.progressLog.replaceChildren(...messages.map(message => {
    const li = document.createElement("li");
    li.textContent = message;
    return li;
  }));
}

function addStat(label, value) {
  const chip = document.createElement("span");
  chip.className = "stat-chip";
  const number = document.createElement("b");
  number.textContent = String(value ?? 0);
  chip.append(number, document.createTextNode(label));
  elements.stats.append(chip);
}

function renderResult(job, saved, saveError = null) {
  const result = job.result || {};
  elements.progress.hidden = true;
  elements.result.hidden = false;
  elements.result.classList.toggle("has-save-error", !saved);
  state.resultSaved = saved;
  $(".result-icon").textContent = saved ? "✓" : "!";
  elements.resultTitle.textContent = saved ? "乐谱已经保存" : "乐谱已生成，但还没有保存";
  elements.stats.replaceChildren();
  addStat("原始音符", result.notes_seen);
  addStat("输出音符", result.final_notes || result.notes_kept);
  addStat("删除弱音", result.velocity_filtered);
  addStat("连音", result.tuplet_count);
  addStat("琶音", result.arpeggio_count);
  addStat("倚音", result.grace_count);
  if (Number(result.tempo_bpm) > 0) addStat(" BPM", Number(result.tempo_bpm).toFixed(3).replace(/\.?0+$/, ""));
  if (Array.isArray(result.time_signature) && result.time_signature.length === 2) {
    addStat("识别拍号", `${result.time_signature[0]}/${result.time_signature[1]}`);
  }
  if (result.key_name) addStat("识别调号", result.key_name);
  if (result.tempo_change_count) addStat("次速度变化", result.tempo_change_count);
  if (result.time_signature_change_count) addStat("次临时变拍", result.time_signature_change_count);
  if (result.key_change_count) addStat("次调号变化", result.key_change_count);
  if (Number(result.pickup_beats) > 0) addStat("拍弱起", result.pickup_beats);
  const outputName = result.output_name || "标准化乐谱.mscz";
  elements.resultSummary.textContent = saved
    ? `“${outputName}”已写入所选文件夹，可以在 MuseScore 中继续编辑。`
    : `“${outputName}”已经生成，但写入文件夹失败：${saveError?.message || "请重新选择目录后保存。"}`;
  elements.savedLocation.textContent = saved
    ? `保存位置：${state.directoryHandle?.name || "已授权文件夹"}  ›  ${outputName}`
    : "转换结果仍暂存在本机服务中，可重新选择目录后再次保存。";
  const review = Array.isArray(result.review_measures) ? result.review_measures : [];
  elements.reviewBox.hidden = review.length === 0;
  elements.reviewMeasures.textContent = review.length ? `第 ${review.join("、")} 小节` : "";
  const warnings = Array.isArray(result.warnings) ? result.warnings.filter(Boolean) : [];
  elements.warningBox.hidden = warnings.length === 0;
  elements.warningBox.replaceChildren(...warnings.map(text => {
    const line = document.createElement("div");
    line.textContent = text;
    return line;
  }));
  elements.button.disabled = false;
  state.busy = false;
  elements.result.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function saveCompletedJob(job) {
  state.completedJob = job;
  const result = job.result || {};
  const outputName = result.output_name || "标准化乐谱.mscz";
  elements.progressStage.textContent = "正在写入所选文件夹";
  elements.progressPercent.textContent = "100%";
  elements.progressBar.style.width = "100%";
  try {
    if (!state.resultBlob || state.resultFileName !== outputName) {
      state.resultBlob = await fetchResultBlob(state.jobId);
      state.resultFileName = outputName;
    }
    if (Number(result.output_size) && state.resultBlob.size !== Number(result.output_size)) {
      throw new Error("本机结果读取不完整，请重试保存。");
    }
    if (result.output_sha256 && await sha256Hex(state.resultBlob) !== String(result.output_sha256).toLowerCase()) {
      throw new Error("本机结果校验失败，请重试保存。");
    }
    await writeResultFile(
      state.directoryHandle,
      outputName,
      state.resultBlob,
      result.output_size,
      result.output_sha256,
    );
    try {
      await acknowledgeSaved(state.jobId);
      renderResult(job, true);
      state.resultBlob = null;
      state.completedJob = null;
    } catch (cleanupError) {
      renderResult(job, true);
      toast("文件已成功保存，但临时文件清理确认失败；退出应用时仍会自动清理。", "error");
    }
  } catch (error) {
    renderResult(job, false, error);
    toast(`未能保存：${error?.message || "请重新选择目录。"}`, "error");
  }
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const job = await apiFetch(`/api/jobs/${encodeURIComponent(state.jobId)}`);
    updateProgress(job);
    if (job.status === "done") {
      window.clearTimeout(state.polling);
      await saveCompletedJob(job);
      return;
    }
    if (job.status === "error") {
      throw new Error(job.error || "转换失败，请检查 MIDI 文件。 ");
    }
    state.polling = window.setTimeout(pollJob, 700);
  } catch (error) {
    window.clearTimeout(state.polling);
    elements.button.disabled = false;
    state.busy = false;
    elements.progress.hidden = true;
    toast(error.message || "无法读取转换进度。", "error");
  }
}

async function submitConversion(event) {
  event.preventDefault();
  if (state.busy) return;
  if (!supportsDirectorySaving()) {
    showDirectoryCompatibilityError();
    return;
  }
  let configuration;
  try {
    configuration = collectConfiguration();
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  if (!state.directoryHandle) {
    const selected = await chooseSaveDirectory();
    if (!selected) {
      toast("尚未选择保存文件夹，转换没有开始。", "error");
      return;
    }
  } else {
    try {
      if (!await ensureWritePermission(state.directoryHandle)) {
        state.directoryHandle = null;
        updateDirectoryDisplay();
        toast("保存权限已失效，请重新选择文件夹。", "error");
        return;
      }
    } catch (error) {
      state.directoryHandle = null;
      updateDirectoryDisplay();
      toast(error?.message || "无法确认文件夹写入权限，请重新选择。", "error");
      return;
    }
  }
  state.busy = true;
  state.completedJob = null;
  state.resultBlob = null;
  state.resultFileName = null;
  state.resultSaved = false;
  showProgress();
  const formData = new FormData();
  formData.append("configuration", JSON.stringify(configuration));
  for (const item of state.files) formData.append(item.id, item.file, item.file.name);
  try {
    const response = await apiFetch("/api/jobs", { method: "POST", body: formData });
    state.jobId = response.job_id;
    await pollJob();
  } catch (error) {
    elements.progress.hidden = true;
    elements.button.disabled = false;
    state.busy = false;
    toast(error.message || "无法开始转换。", "error");
  }
}

function bindEvents() {
  elements.choose.addEventListener("click", event => {
    event.stopPropagation();
    elements.input.click();
  });
  elements.drop.addEventListener("click", event => {
    if (!event.target.closest("button")) elements.input.click();
  });
  elements.input.addEventListener("change", () => addFiles(elements.input.files));
  for (const name of ["dragenter", "dragover"]) {
    elements.drop.addEventListener(name, event => {
      event.preventDefault();
      elements.drop.classList.add("is-dragging");
    });
  }
  for (const name of ["dragleave", "drop"]) {
    elements.drop.addEventListener(name, event => {
      event.preventDefault();
      elements.drop.classList.remove("is-dragging");
    });
  }
  elements.drop.addEventListener("drop", event => addFiles(event.dataTransfer.files));

  for (const group of ["bpm", "meter", "key"]) {
    $$(`input[name="${group}Mode"]`).forEach(control => control.addEventListener("change", () => modeChanged(group)));
  }
  document.addEventListener("click", event => {
    const helpButton = event.target.closest("[data-help]");
    if (helpButton) {
      event.preventDefault();
      openHelp(helpButton.dataset.help, helpButton);
    }
  });
  $("#openHelp").addEventListener("click", event => openHelp(null, event.currentTarget));
  $("#closeHelp").addEventListener("click", closeHelp);
  elements.scrim.addEventListener("click", closeHelp);
  document.addEventListener("keydown", event => {
    if (!elements.help.classList.contains("is-open")) return;
    if (event.key === "Escape") {
      closeHelp();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = $$('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])', elements.help)
      .filter(node => !node.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  elements.form.addEventListener("submit", submitConversion);
  elements.chooseDirectory.addEventListener("click", chooseSaveDirectory);
  elements.reselectDirectory.addEventListener("click", async () => {
    const selected = await chooseSaveDirectory();
    if (selected && state.completedJob && !state.resultSaved) {
      elements.result.hidden = true;
      elements.progress.hidden = false;
      await saveCompletedJob(state.completedJob);
    }
  });
  $("#newConversion").addEventListener("click", () => {
    if (state.completedJob && !state.resultSaved) {
      toast("请先把已生成的乐谱保存成功，或退出应用清除临时结果。", "error");
      return;
    }
    elements.result.hidden = true;
    $("#filesHeading").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("#exitApp").addEventListener("click", async () => {
    if (state.busy) {
      toast("转换仍在进行，请等待完成后再退出，以便完整清理临时文件。", "error");
      return;
    }
    try {
      await apiFetch("/api/shutdown", { method: "POST" });
    } catch (error) {
      toast(error?.message || "暂时无法退出，请稍后重试。", "error");
      return;
    }
    document.body.innerHTML = '<main style="min-height:100vh;display:grid;place-items:center;padding:24px;background:#f4f0e7;color:#17201d;font-family:Segoe UI,Microsoft YaHei UI,sans-serif;text-align:center"><div><div style="font-size:48px">✓</div><h1>谱净已退出</h1><p style="color:#5d6762">临时文件已经清理，现在可以关闭这个标签页。</p></div></main>';
  });
}

bindEvents();
updateFileState();
updateDirectoryDisplay();
if (!supportsDirectorySaving()) {
  elements.compatibilityError.hidden = false;
  elements.chooseDirectory.disabled = true;
  elements.button.disabled = true;
  elements.directoryName.textContent = "当前浏览器不支持选择文件夹";
  elements.directoryHint.textContent = "请改用最新版 Microsoft Edge 或 Google Chrome";
}
