const $ = (selector) => document.querySelector(selector);
const histories = new Map();
let refreshTimer = null;

const fmt = {
  int: (value) => value == null ? "—" : new Intl.NumberFormat("zh-CN").format(Math.round(value)),
  one: (value) => value == null ? "—" : Number(value).toFixed(1),
  mib: (value) => value == null ? "—" : `${(value / 1024).toFixed(1)} GiB`,
  time: (value) => value ? new Date(value).toLocaleTimeString("zh-CN", {hour12: false}) : "—",
};

function esc(value) {
  return String(value ?? "—").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
}

function settingRows(items) {
  return items.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join("");
}

function pushHistory(gpu) {
  const key = gpu.uuid;
  const values = histories.get(key) || [];
  values.push({util: gpu.gpu_util_pct || 0, power: gpu.power_draw_w || 0});
  if (values.length > 60) values.shift();
  histories.set(key, values);
}

function drawSpark(canvas, values, maxValue, color) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 300;
  const height = canvas.clientHeight || 54;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = values.length === 1 ? width : index / (values.length - 1) * width;
    const y = height - Math.min(value / maxValue, 1) * (height - 5) - 2;
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function renderGpus(gpus, processes, expectedIndex) {
  gpus.forEach(pushHistory);
  const processByGpu = new Map();
  processes.forEach((proc) => processByGpu.set(proc.gpu_uuid, (processByGpu.get(proc.gpu_uuid) || 0) + 1));
  $("#gpu-grid").innerHTML = gpus.map((gpu) => {
    const memPct = gpu.memory_total_mib ? gpu.memory_used_mib / gpu.memory_total_mib * 100 : 0;
    return `<article class="gpu-card ${gpu.index === expectedIndex ? "active" : ""}">
      <div class="gpu-head"><div><span class="gpu-index">GPU ${esc(gpu.index)}</span><div class="gpu-name">${esc(gpu.name)}</div></div><div class="gpu-temp">${fmt.int(gpu.temperature_c)}°</div></div>
      <div class="gauge-row"><label>GPU utilization</label><strong>${fmt.int(gpu.gpu_util_pct)}%</strong><div class="bar"><span style="width:${Math.min(gpu.gpu_util_pct || 0,100)}%"></span></div></div>
      <div class="gauge-row"><label>Memory</label><strong>${fmt.mib(gpu.memory_used_mib)} / ${fmt.mib(gpu.memory_total_mib)}</strong><div class="bar"><span style="width:${Math.min(memPct,100)}%"></span></div></div>
      <div class="gpu-stats"><div><span>Power</span><strong>${fmt.one(gpu.power_draw_w)} / ${fmt.one(gpu.power_limit_w)} W</strong></div><div><span>Persistence</span><strong>${esc(gpu.persistence_mode)}</strong></div><div><span>Processes</span><strong>${processByGpu.get(gpu.uuid) || 0}</strong></div></div>
      <canvas class="spark" data-gpu="${esc(gpu.uuid)}" aria-label="GPU utilization trend"></canvas>
    </article>`;
  }).join("");
  requestAnimationFrame(() => {
    document.querySelectorAll("canvas.spark").forEach((canvas) => {
      const values = (histories.get(canvas.dataset.gpu) || []).map((item) => item.util);
      drawSpark(canvas, values, 100, "#71e6d1");
    });
  });
}

function compareConfig(info, expected, gpu) {
  const checks = [
    [info.served_model_name, expected.model], [info.dtype, expected.dtype],
    [info.quantization, expected.quantization], [info.context_length, expected.context_length],
    [info.tp_size, expected.tp_size], [info.mem_fraction_static, expected.mem_fraction_static],
    [info.max_running_requests, expected.max_running_requests], [info.reasoning_parser, expected.reasoning_parser],
    [info.tool_call_parser, expected.tool_call_parser], [info.speculative_num_steps, expected.speculative_num_steps],
    [info.speculative_num_draft_tokens, expected.speculative_num_draft_tokens],
    [info.disable_radix_cache, expected.disable_radix_cache], [gpu?.power_limit_w, expected.power_limit_w]
  ];
  return checks.every(([actual, wanted]) => actual === wanted);
}

function render(payload) {
  const {remote, local} = payload;
  const info = remote.server_info || {};
  const container = remote.container || {};
  const expected = local.expected;
  const targetGpu = remote.gpus.find((gpu) => gpu.index === expected.gpu_index);
  const model = remote.models?.data?.[0];
  const healthy = payload.ok && remote.service_ok && container.status === "running";
  const matched = remote.service_ok && compareConfig(info, expected, targetGpu);

  $("#connection-pill").className = `pill ${healthy ? "live" : "error"}`;
  $("#connection-pill").innerHTML = `<i></i>${healthy ? "实时运行" : "状态异常"}`;
  $("#model-title").textContent = model?.id || expected.model;
  $("#service-summary").textContent = healthy
    ? `服务健康。${remote.host} 上的 ${model?.id || expected.model} 正通过 SGLang 提供原生工具协议推理。`
    : `服务未完全就绪：${remote.service_error || container.error || "请检查详细状态"}`;
  $("#remote-host").textContent = remote.host;
  $("#latency").textContent = `${payload.latency_ms} ms`;
  $("#updated-at").textContent = fmt.time(remote.collected_at);
  $("#framework").textContent = `SGLang ${info.version || "—"}`;
  $("#frontend").textContent = info.frontend || "python";
  $("#context-length").textContent = fmt.int(info.context_length);
  $("#concurrency").textContent = fmt.int(info.max_running_requests);
  $("#tp-size").textContent = fmt.int(info.tp_size);
  $("#alert").classList.toggle("hidden", healthy);
  $("#alert").textContent = healthy ? "" : (remote.service_error || container.error || "远程服务状态异常");

  renderGpus(remote.gpus || [], remote.processes || [], expected.gpu_index);

  $("#config-match").className = `status-tag ${matched ? "ok" : "bad"}`;
  $("#config-match").textContent = matched ? "CONFIG MATCH" : "CONFIG DRIFT";
  $("#engine-settings").innerHTML = settingRows([
    ["模型", info.served_model_name], ["数据类型", info.dtype], ["量化", info.quantization ?? "none"],
    ["上下文", `${fmt.int(info.context_length)} tokens`], ["Tensor Parallel", info.tp_size],
    ["显存比例", info.mem_fraction_static], ["最大并发", info.max_running_requests],
    ["Reasoning parser", info.reasoning_parser], ["Tool parser", info.tool_call_parser],
    ["Speculative algorithm", info.speculative_algorithm], ["MTP steps", info.speculative_num_steps],
    ["Draft tokens", info.speculative_num_draft_tokens], ["Radix cache", info.disable_radix_cache ? "disabled" : "enabled"],
    ["KV cache dtype", info.kv_cache_dtype], ["模型容量", model ? `${fmt.int(model.max_model_len)} tokens` : "—"]
  ]);

  $("#container-status").className = `status-tag ${container.status === "running" ? "ok" : "bad"}`;
  $("#container-status").textContent = String(container.status || "unknown").toUpperCase();
  $("#runtime-settings").innerHTML = settingRows([
    ["容器", container.name], ["镜像", container.image], ["状态", container.status],
    ["启动时间", container.started_at], ["重启策略", container.restart_policy || "none"],
    ["Endpoint", local.endpoint], ["采样默认来源", info.sampling_defaults],
    ["Preferred sampling", info.preferred_sampling_params ? JSON.stringify(info.preferred_sampling_params) : "—"],
    ["主机负载", (remote.system?.load_average || []).map((v) => Number(v).toFixed(2)).join(" / ")],
    ["主机可用内存", fmt.mib(remote.system?.memory_available_mib)], ["磁盘可用", `${remote.system?.disk_free_gib ?? "—"} GiB`]
  ]);

  $("#sampling-table").innerHTML = Object.entries(local.sampling_profiles).map(([name, profile]) =>
    `<tr><td>${esc(name)}</td><td>${esc(profile.purpose)}</td><td>${esc(profile.temperature)}</td><td>${esc(profile.top_p)}</td><td>${esc(profile.top_k)}</td><td>${esc(profile.min_p)}</td></tr>`
  ).join("");

  const gpuIndex = new Map(remote.gpus.map((gpu) => [gpu.uuid, gpu.index]));
  $("#process-table").innerHTML = remote.processes.length ? remote.processes.map((proc) =>
    `<tr><td>${esc(gpuIndex.get(proc.gpu_uuid))}</td><td>${esc(proc.pid)}</td><td>${esc(proc.process_name)}</td><td>${fmt.mib(proc.used_memory_mib)}</td></tr>`
  ).join("") : `<tr><td colspan="4" class="subtle">没有计算进程</td></tr>`;

  $("#integrity-settings").innerHTML = settingRows([
    ["组合版本", local.manifest?.bundle_version], ["Manifest SHA256", local.manifest_sha256],
    ["源码提交", local.source_commit], ["SSH 主机", local.ssh_host],
    ["配置校验", matched ? "actual = expected" : "drift detected"]
  ]);
  $("#container-command").textContent = (container.command || []).join(" ");
  $("#raw-json").textContent = JSON.stringify(payload, null, 2);
  $("#refresh-note").textContent = `每 ${local.refresh_seconds} 秒刷新`;
}

function renderFailure(payload) {
  $("#connection-pill").className = "pill error";
  $("#connection-pill").innerHTML = "<i></i>连接失败";
  $("#alert").classList.remove("hidden");
  $("#alert").textContent = payload?.error || "无法连接本地监控服务";
  $("#updated-at").textContent = new Date().toLocaleTimeString("zh-CN", {hour12: false});
}

async function refresh(force = false) {
  const button = $("#refresh-button");
  button.disabled = true;
  try {
    const response = await fetch(`/api/status${force ? "?force=1" : ""}`, {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw payload;
    render(payload);
    const seconds = payload.local.refresh_seconds || 5;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, seconds * 1000);
  } catch (error) {
    renderFailure(error);
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 8000);
  } finally {
    button.disabled = false;
  }
}

$("#refresh-button").addEventListener("click", () => refresh(true));
window.addEventListener("resize", () => {
  document.querySelectorAll("canvas.spark").forEach((canvas) => {
    const values = (histories.get(canvas.dataset.gpu) || []).map((item) => item.util);
    drawSpark(canvas, values, 100, "#71e6d1");
  });
});
refresh(true);
