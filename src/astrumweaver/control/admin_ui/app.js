"use strict";
// Every value shown here comes from a Worker or from Hugging Face, so it is
// only ever set through textContent / createTextNode, never as HTML.

const REFRESH_MS = 5000;

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function gib(mb) {
  return mb === null || mb === undefined ? "—" : `${(mb / 1024).toFixed(1)} GiB`;
}

function ago(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${Math.round(seconds)} 秒前`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} 分前`;
  return `${(seconds / 3600).toFixed(1)} 時間前`;
}

function meter(label, used, total, unitFormatter, warnAt) {
  const known = used !== null && used !== undefined && total;
  const percent = known ? Math.min(100, (used / total) * 100) : 0;
  // VRAM of a loaded model is normally near full, so only RAM gets a warning.
  const warn = warnAt !== undefined && percent >= warnAt;
  const text = known ? `${unitFormatter(used)} / ${unitFormatter(total)} (${percent.toFixed(0)}%)` : `— / ${unitFormatter(total)}`;
  const fill = el("div", { class: warn ? "fill hot" : "fill" });
  fill.style.width = `${percent}%`;
  return el("div", { class: "meter" },
    el("div", { class: "label" }, el("span", null, label), el("span", null, text)),
    el("div", { class: "track", role: "progressbar", "aria-label": label, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(percent) }, fill));
}

function renderWorker(w) {
  const host = w.host || {};
  const memUsed = host.memory_total_mb != null && host.memory_available_mb != null
    ? host.memory_total_mb - host.memory_available_mb : null;
  const model = [w.profile, w.model].filter(Boolean);
  return el("article", { class: "card" },
    el("header", null, el("strong", null, w.worker_id), el("span", { class: `badge ${w.state}` }, w.state)),
    el("dl", null,
      el("dt", null, "モデル"), el("dd", null, model.length ? model.join(" / ") : "不明"),
      el("dt", null, "バックエンド"), el("dd", null, w.backend || "不明（telemetry 未対応の Worker）"),
      el("dt", null, "コンテキスト"), el("dd", null, w.context_tokens ? `${w.context_tokens.toLocaleString()} トークン` : "—"),
      el("dt", null, "処理中"), el("dd", null, `${w.active_jobs} / ${w.max_concurrency}`),
      el("dt", null, "提供機能"), el("dd", null, w.capabilities.join(", ") || "—"),
      el("dt", null, "最終応答"), el("dd", null, ago(w.last_seen_age_s))),
    ...w.gpus.map((g) => el("div", null,
      el("div", { class: "muted" }, `${g.name} · ${g.temperature_c != null ? g.temperature_c + "℃" : "—"} · 使用率 ${g.utilization_percent != null ? g.utilization_percent + "%" : "—"}`),
      meter("VRAM", g.memory_used_mb, g.memory_total_mb, gib))),
    host.memory_total_mb ? meter("RAM", memUsed, host.memory_total_mb, gib, 95) : null,
    host.cpu_count ? el("dl", null,
      el("dt", null, "CPU"), el("dd", null, `${host.cpu_count} コア`),
      el("dt", null, "ディスク空き"), el("dd", null, gib(host.disk_free_mb))) : null,
    w.telemetry_stale
      ? el("div", { class: "stale" }, w.telemetry_age_s == null ? "稼働情報が未取得です" : `稼働情報が古い値です（${ago(w.telemetry_age_s)}）`)
      : null,
    w.telemetry_errors.length ? el("div", { class: "stale" }, w.telemetry_errors.join(" / ")) : null);
}

function renderWorkers(data) {
  const list = data.workers;
  const count = (state) => list.filter((w) => w.state === state).length;
  document.getElementById("summary").replaceChildren(
    el("span", { class: "chip" }, `online ${count("online")}`),
    el("span", { class: "chip" }, `draining ${count("draining")}`),
    el("span", { class: "chip" }, `offline ${count("offline")}`));
  const order = { online: 0, draining: 1, offline: 2 };
  document.getElementById("workers").replaceChildren(
    ...[...list].sort((a, b) => order[a.state] - order[b.state] || a.worker_id.localeCompare(b.worker_id)).map(renderWorker));
  document.getElementById("updated").textContent = `更新: ${new Date(data.generated_at).toLocaleTimeString("ja-JP")}`;
}

async function refreshWorkers() {
  const banner = document.getElementById("workers-error");
  try {
    const response = await fetch("/admin/api/workers", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderWorkers(await response.json());
    banner.hidden = true;
  } catch (error) {
    banner.textContent = `Worker の状態を取得できません（${error.message}）。自動で再試行します。`;
    banner.hidden = false;
  }
}

const VERDICTS = {
  fits_gpu: "載る（VRAM）", hybrid: "載る可能性あり（併用・低速）", too_large: "載らない", unknown: "不明",
};

// Cells carry their column name (data-label) so a narrow screen can stack each
// row as a small card instead of squeezing the columns.
function dataTable(headers, rows, nowrapColumns) {
  return el("table", null,
    el("thead", null, el("tr", null, headers.map((h) => el("th", null, h)))),
    el("tbody", null, rows.map((row) => el("tr", null, row.map((cell, i) =>
      el("td", { "data-label": headers[i], class: nowrapColumns.includes(i) ? "nowrap" : null }, cell))))));
}

function chatFirst(f) { return (f.capabilities || []).includes("llm.chat") ? 1 : 0; }
function roleOf(f) {
  const caps = f.capabilities || [];
  if (caps.includes("llm.chat")) return "チャット";
  if (caps.some((c) => c.startsWith("text.embed") || c.startsWith("decision"))) return "埋め込み・判定";
  return caps.join(", ") || "—";
}

function renderCheck(r) {
  const out = [];
  out.push(el("p", { class: r.ok ? "banner ok" : "banner error" },
    r.ok ? "このモデルは配備の候補にできます（重みの検証は未実施）。" : "このままでは配備できません。下の項目を確認してください。"));
  out.push(el("div", { class: "card" }, el("h2", null, "検証結果"),
    el("ul", { class: "checks" }, r.checks.map((c) =>
      el("li", null, el("span", { class: `mark ${c.status}` }, c.status === "pass" ? "OK" : c.status === "warn" ? "注意" : "NG"), el("span", null, c.message)))),
    el("dl", null,
      el("dt", null, "リポジトリ"), el("dd", null, r.repo),
      el("dt", null, "コミット"), el("dd", null, r.commit || "—"),
      el("dt", null, "ライセンス"), el("dd", null, r.license || "未宣言"),
      r.gguf ? [el("dt", null, "アーキテクチャ"), el("dd", null, r.gguf.architecture || "—"),
                el("dt", null, "学習時の文脈長"), el("dd", null, r.gguf.context_length ? r.gguf.context_length.toLocaleString() : "—")] : null)));
  if (r.files.length) {
    const selected = new Set(r.selected.map((f) => f.path));
    out.push(el("div", { class: "card" }, el("h2", null, `GGUF ファイル（${r.files.length}）`),
      dataTable(["選択", "ファイル", "サイズ", "SHA-256"], r.files.map((f) => [
        selected.has(f.path) ? "▶" : "", f.path, gib(f.size_bytes / 1048576), f.sha256 ? f.sha256.slice(0, 16) + "…" : "—"]), [0, 2, 3])));
  }
  if (r.fit.length) {
    const rows = [...r.fit]
      .sort((x, y) => chatFirst(y) - chatFirst(x) || x.worker_id.localeCompare(y.worker_id))
      .map((f) => [f.worker_id, roleOf(f), f.backend || "—", gib(f.vram_total_mb), gib(f.ram_total_mb),
        el("span", null, el("strong", null, VERDICTS[f.verdict] || f.verdict), el("div", { class: "muted" }, f.note))]);
    out.push(el("div", { class: "card" }, el("h2", null, `Worker ごとの見込み（重み ${gib(r.weights_mb)}、目安）`),
      dataTable(["Worker", "役割", "バックエンド", "VRAM", "RAM", "判定"], rows, [0, 1, 2, 3, 4])));
  }
  if (r.plan.length) {
    out.push(el("div", { class: "card" }, el("h2", null, "配備の手順（手動）"), el("ol", { class: "plan" }, r.plan.map((s) => el("li", null, s)))));
  }
  document.getElementById("check-result").replaceChildren(...out);
}

async function submitCheck(event) {
  event.preventDefault();
  const button = document.getElementById("check-button");
  const banner = document.getElementById("check-error");
  const url = document.getElementById("hf-url").value.trim();
  button.disabled = true; button.textContent = "検証中…"; banner.hidden = true;
  try {
    const response = await fetch("/admin/api/model-check", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ url }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    renderCheck(body);
  } catch (error) {
    banner.textContent = error.message;
    banner.hidden = false;
    document.getElementById("check-result").replaceChildren();
  } finally {
    button.disabled = false; button.textContent = "検証する";
  }
}

function selectTab(name) {
  for (const id of ["workers", "model"]) {
    const active = id === name;
    document.getElementById(`tab-${id}`).setAttribute("aria-selected", String(active));
    document.getElementById(`view-${id}`).hidden = !active;
  }
  history.replaceState(null, "", `#${name}`);
}

document.getElementById("tab-workers").addEventListener("click", () => selectTab("workers"));
document.getElementById("tab-model").addEventListener("click", () => selectTab("model"));
document.getElementById("check-form").addEventListener("submit", submitCheck);
selectTab(location.hash === "#model" ? "model" : "workers");
refreshWorkers();
setInterval(() => { if (!document.hidden) refreshWorkers(); }, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshWorkers(); });
