const state = {
  token: localStorage.getItem("dockerops_token") || "",
  username: localStorage.getItem("dockerops_user") || "",
  takeover: false,
  platform: "generic",
  tab: "overview",
  needsSetup: false,
};

const $ = (sel) => document.querySelector(sel);

function authHeaders() {
  const h = { "Content-Type": "application/json" };
  if (state.token) h.Authorization = `Bearer ${state.token}`;
  return h;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { ...authHeaders(), ...(opts.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.detail || data.message || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function fmtTime(ts) {
  if (!ts) return "-";
  const n = Number(ts);
  if (n > 1e12) return new Date(n / 1e6).toLocaleString();
  if (n > 1e10) return new Date(n).toLocaleString();
  return new Date(n * 1000).toLocaleString();
}

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1048576) return `${(v / 1024).toFixed(1)} KB`;
  if (v < 1073741824) return `${(v / 1048576).toFixed(1)} MB`;
  return `${(v / 1073741824).toFixed(2)} GB`;
}

function scoreClass(score) {
  if (score >= 75) return "ok";
  if (score >= 50) return "warn";
  return "bad";
}

function pillClass(text) {
  return `pill ${(text || "").toLowerCase()}`;
}

function managerPill(manager, label) {
  const m = manager || "third_party";
  const text = label || ({ compose: "Compose", unraid: "Unraid", third_party: "三方" }[m] || m);
  return `<span class="pill mgr-${m}">${escapeHtml(text)}</span>`;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setAuthUI() {
  const el = $("#auth-state");
  if (state.needsSetup) {
    el.textContent = "需要首次设置";
    $("#btn-login").textContent = "设置管理员";
  } else if (state.token) {
    el.textContent = `已登录：${state.username || "user"}`;
    $("#btn-login").textContent = "退出";
  } else {
    el.textContent = "未登录（只读）";
    $("#btn-login").textContent = "登录";
  }
  const tb = $("#takeover-badge");
  if (state.takeover) {
    tb.textContent = "接管: 开";
    tb.className = "badge ok";
  } else {
    tb.textContent = "接管: 关";
    tb.className = "badge warn";
  }
  const pb = $("#platform-badge");
  const map = { unraid: "Unraid", fnos: "飞牛", generic: "通用" };
  pb.textContent = `平台: ${map[state.platform] || state.platform}`;
  pb.className = `badge platform-${state.platform || "generic"}`;
}

function requireLogin() {
  if (!state.token) {
    alert("写操作需要先登录");
    $("#login-dialog").showModal();
    return false;
  }
  return true;
}

function requireTakeover(action) {
  if (!state.takeover) {
    alert(`${action} 需要开启完整接管：DOCKEROPS_TAKEOVER_ENABLED=true，并挂载 rw docker.sock。`);
    return false;
  }
  return true;
}

function actionBtn(text, fn, { danger = false, disabled = false } = {}) {
  const b = document.createElement("button");
  b.className = `btn small${danger ? " danger" : ""}`;
  b.textContent = text;
  b.disabled = disabled;
  b.addEventListener("click", fn);
  return b;
}

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll("#main-tabs .tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    const panels = (p.dataset.panel || "").split(/\s+/);
    p.hidden = !panels.includes(name);
  });
  if (["images", "networks", "volumes", "system"].includes(name)) {
    loadResources(name);
  }
}

async function checkSetup() {
  try {
    const st = await api("/api/auth/status");
    state.needsSetup = !!st.needs_setup;
    setAuthUI();
    if (state.needsSetup) {
      // Clear any stale token from previous install
      state.token = "";
      state.username = "";
      localStorage.removeItem("dockerops_token");
      localStorage.removeItem("dockerops_user");
      const dlg = $("#setup-dialog");
      if (dlg && !dlg.open) dlg.showModal();
    }
    return st;
  } catch (e) {
    state.needsSetup = false;
    return null;
  }
}

async function loadAll() {
  setAuthUI();
  try {
    await checkSetup();
    const [doctor, containers, ops, health, summary, compose, unraid, platform, events] =
      await Promise.all([
        api("/api/doctor"),
        api("/api/containers"),
        api("/api/ops/records?limit=30"),
        api("/api/health"),
        api("/api/managers/summary"),
        api("/api/compose/projects"),
        api("/api/unraid/templates"),
        api("/api/platform").catch(() => ({ platform: "generic" })),
        api("/api/events?limit=30").catch(() => ({ items: [] })),
      ]);

    state.takeover = !!summary.takeover_enabled;
    state.platform = platform.platform || summary.platform || health.platform || "generic";
    setAuthUI();

    const score = doctor.health_score ?? "--";
    $("#health-score").textContent = score;
    const label = $("#health-label");
    label.textContent = doctor.label || "未知";
    label.className = `badge ${scoreClass(Number(score) || 0)}`;

    const advice = $("#advice");
    advice.innerHTML = "";
    const tips = [
      ...(doctor.advice || []),
      ...(summary.hints || []),
      ...(platform.mount_hints || []).slice(0, 3),
    ];
    tips.forEach((a) => {
      const li = document.createElement("li");
      li.textContent = a;
      advice.appendChild(li);
    });

    $("#engine-info").textContent = JSON.stringify(
      {
        service: {
          version: health.version,
          takeover: health.takeover_enabled,
          resource_apis: health.resource_apis,
        },
        platform: {
          name: state.platform,
          capabilities: platform.capabilities,
        },
        managers: summary.counts,
        unraid: summary.unraid,
        compose: summary.compose,
        engine: doctor.engine,
        counts: doctor.counts,
      },
      null,
      2
    );

    // events
    const ev = $("#events-list");
    const evItems = events.items || [];
    if (!evItems.length) {
      ev.innerHTML = `<div class="muted">暂无近期事件</div>`;
    } else {
      ev.innerHTML = evItems
        .slice(0, 25)
        .map(
          (e) =>
            `<div class="event-line"><span class="muted">${escapeHtml(fmtTime(e.time))}</span> ` +
            `<span class="pill">${escapeHtml(e.type || "")}/${escapeHtml(e.action || e.status || "")}</span> ` +
            `<strong>${escapeHtml(e.name || e.id || "")}</strong></div>`
        )
        .join("");
    }

    // containers
    $("#container-count").textContent = `共 ${containers.count || 0} 个`;
    const tbody = $("#container-rows");
    tbody.innerHTML = "";
    (containers.items || []).forEach((c) => {
      const tr = document.createElement("tr");
      const mgrExtra =
        c.manager === "compose" && c.compose_project
          ? `<div class="muted mono">${escapeHtml(c.compose_project)}/${escapeHtml(c.compose_service || "")}</div>`
          : c.manager === "unraid"
            ? `<div class="muted mono">template</div>`
            : "";
      tr.innerHTML = `
        <td><strong>${escapeHtml(c.name || c.id)}</strong><div class="muted mono">${escapeHtml(c.id || "")}</div></td>
        <td>${managerPill(c.manager, c.label)}${mgrExtra}</td>
        <td class="mono">${escapeHtml(c.image || "")}</td>
        <td><span class="${pillClass(c.status)}">${escapeHtml(c.status || "-")}</span></td>
        <td><span class="${pillClass(c.health || "none")}">${escapeHtml(c.health || "-")}</span></td>
        <td>${c.restart_count ?? 0}</td>
        <td class="actions"></td>
      `;
      const actions = tr.querySelector(".actions");
      const id = c.id || c.name;
      const running = (c.status || "").toLowerCase() === "running";
      const paused = (c.status || "").toLowerCase() === "paused";
      if (!running && !paused) {
        actions.appendChild(actionBtn("启动", () => doLife(id, "start")));
      }
      if (running) {
        actions.appendChild(actionBtn("停止", () => doLife(id, "stop")));
        actions.appendChild(actionBtn("重启", () => doLife(id, "restart")));
        actions.appendChild(actionBtn("暂停", () => doLife(id, "pause")));
      }
      if (paused) {
        actions.appendChild(actionBtn("恢复", () => doLife(id, "unpause")));
      }
      actions.appendChild(actionBtn("日志", () => showLogs(id, c.name)));
      actions.appendChild(actionBtn("备份", () => doBackup(id)));
      actions.appendChild(actionBtn("更新", () => doUpdate(id)));
      actions.appendChild(actionBtn("回滚", () => doRollback(id)));
      if (c.manager === "third_party") {
        actions.appendChild(actionBtn("Adopt", () => doAdopt(id), { disabled: !state.takeover }));
      }
      actions.appendChild(
        actionBtn("删除", () => doRemove(id), { danger: true, disabled: !state.takeover })
      );
      tbody.appendChild(tr);
    });
    if (!(containers.items || []).length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">暂无容器或无法连接 Docker</td></tr>`;
    }

    // compose
    $("#compose-count").textContent = `共 ${(compose.items || []).length} 个项目`;
    const cbody = $("#compose-rows");
    cbody.innerHTML = "";
    (compose.items || []).forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${escapeHtml(p.name)}</strong><div class="muted">${escapeHtml(p.source || "")}</div></td>
        <td>${escapeHtml((p.services || []).join(", ") || "-")}</td>
        <td>${p.running ?? 0}/${p.total ?? 0}</td>
        <td class="mono small">${escapeHtml(p.working_dir || "-")}<div class="muted">${escapeHtml((p.config_files || []).join(", "))}</div></td>
        <td class="actions"></td>
      `;
      const actions = tr.querySelector(".actions");
      actions.appendChild(actionBtn("备份", () => doComposeBackup(p.name)));
      actions.appendChild(actionBtn("更新", () => doComposeUpdate(p.name)));
      actions.appendChild(actionBtn("Up", () => doComposeUp(p.name), { disabled: !state.takeover }));
      actions.appendChild(
        actionBtn("Down", () => doComposeDown(p.name), { danger: true, disabled: !state.takeover })
      );
      cbody.appendChild(tr);
    });
    if (!(compose.items || []).length) {
      cbody.innerHTML = `<tr><td colspan="5" class="muted">未发现 Compose 项目（需 labels 或 DOCKEROPS_COMPOSE_PROJECT_DIRS）</td></tr>`;
    }

    // unraid
    const uAvail = unraid.available;
    $("#unraid-count").textContent = uAvail
      ? `共 ${(unraid.items || []).length} 个 · ${unraid.path || ""}`
      : `模板目录未挂载 · ${unraid.path || ""}`;
    const ubody = $("#unraid-rows");
    ubody.innerHTML = "";
    if (!uAvail) {
      ubody.innerHTML = `<tr><td colspan="5" class="muted">请挂载 /boot/config/plugins/dockerMan/templates-user → /unraid/templates-user（非 Unraid 可忽略）</td></tr>`;
    } else {
      (unraid.items || []).forEach((t) => {
        const tr = document.createElement("tr");
        const st = (t.container && t.container.status) || "-";
        tr.innerHTML = `
          <td><strong>${escapeHtml(t.name || t.file || "")}</strong><div class="muted mono">${escapeHtml(t.file || "")}</div></td>
          <td class="mono small">${escapeHtml(t.repository || "-")}</td>
          <td>${escapeHtml(t.network || "-")}${t.privileged ? ' <span class="pill">privileged</span>' : ""}</td>
          <td><span class="${pillClass(st)}">${escapeHtml(st)}</span></td>
          <td class="actions"></td>
        `;
        const actions = tr.querySelector(".actions");
        const n = t.name || "";
        actions.appendChild(actionBtn("备份", () => doUnraidBackup(n)));
        actions.appendChild(actionBtn("模板更新", () => doUnraidUpdate(n)));
        ubody.appendChild(tr);
      });
      if (!(unraid.items || []).length) {
        ubody.innerHTML = `<tr><td colspan="5" class="muted">模板目录为空</td></tr>`;
      }
    }

    // findings
    const findings = $("#findings");
    findings.innerHTML = "";
    const list = doctor.findings || [];
    if (!list.length) {
      findings.innerHTML = `<div class="muted">暂无异常发现</div>`;
    } else {
      list.slice(0, 40).forEach((f) => {
        const div = document.createElement("div");
        div.className = `finding ${f.level || "info"}`;
        div.innerHTML = `<span class="lvl">${escapeHtml(f.level || "info")}</span>
          <strong>${escapeHtml(f.target || "")}</strong>
          <div>${escapeHtml(f.message || "")}</div>`;
        findings.appendChild(div);
      });
    }

    // ops
    const opsBody = $("#ops-rows");
    opsBody.innerHTML = "";
    (ops.items || []).forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(fmtTime(r.created_at))}</td>
        <td>${escapeHtml(r.action || "")}</td>
        <td>${escapeHtml(r.target || "-")}</td>
        <td>${escapeHtml(r.status || "")}</td>
        <td>${escapeHtml(r.actor || "-")}</td>
      `;
      opsBody.appendChild(tr);
    });
    if (!(ops.items || []).length) {
      opsBody.innerHTML = `<tr><td colspan="5" class="muted">暂无运维记录</td></tr>`;
    }

    if (["images", "networks", "volumes", "system"].includes(state.tab)) {
      await loadResources(state.tab);
    }
  } catch (e) {
    $("#engine-info").textContent = `加载失败：${e.message}`;
  }
}

async function loadResources(kind) {
  try {
    if (kind === "images" || kind === "all") {
      const data = await api("/api/images");
      $("#image-count").textContent = `共 ${data.count || 0} 个`;
      const body = $("#image-rows");
      body.innerHTML = "";
      (data.items || []).forEach((img) => {
        const tr = document.createElement("tr");
        const label = (img.tags && img.tags[0]) || img.label || img.id;
        tr.innerHTML = `
          <td class="mono"><strong>${escapeHtml(label)}</strong>
            <div class="muted">${escapeHtml(img.id || "")}</div>
            ${(img.tags || []).slice(1).map((t) => `<div class="muted">${escapeHtml(t)}</div>`).join("")}
          </td>
          <td>${fmtBytes(img.size)}</td>
          <td class="muted small">${escapeHtml(String(img.created || "-").slice(0, 19))}</td>
          <td class="actions"></td>
        `;
        const ref = (img.tags && img.tags[0]) || img.full_id || img.id;
        tr.querySelector(".actions").appendChild(
          actionBtn("删除", () => doImageRemove(ref), { danger: true, disabled: !state.takeover })
        );
        body.appendChild(tr);
      });
      if (!(data.items || []).length) {
        body.innerHTML = `<tr><td colspan="4" class="muted">无镜像</td></tr>`;
      }
    }
    if (kind === "networks" || kind === "all") {
      const data = await api("/api/networks");
      $("#network-count").textContent = `共 ${data.count || 0} 个`;
      const body = $("#network-rows");
      body.innerHTML = "";
      const reserved = new Set(["bridge", "host", "none"]);
      (data.items || []).forEach((n) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><strong>${escapeHtml(n.name)}</strong><div class="muted mono">${escapeHtml(n.id || "")}</div></td>
          <td>${escapeHtml(n.driver || "-")}</td>
          <td class="mono small">${escapeHtml(n.subnet || "-")}</td>
          <td>${n.containers ?? 0}</td>
          <td class="actions"></td>
        `;
        if (!reserved.has(n.name)) {
          tr.querySelector(".actions").appendChild(
            actionBtn("删除", () => doNetRemove(n.id || n.name), {
              danger: true,
              disabled: !state.takeover,
            })
          );
        }
        body.appendChild(tr);
      });
      if (!(data.items || []).length) {
        body.innerHTML = `<tr><td colspan="5" class="muted">无网络</td></tr>`;
      }
    }
    if (kind === "volumes" || kind === "all") {
      const data = await api("/api/volumes");
      $("#volume-count").textContent = `共 ${data.count || 0} 个`;
      const body = $("#volume-rows");
      body.innerHTML = "";
      (data.items || []).forEach((v) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><strong>${escapeHtml(v.name)}</strong></td>
          <td>${escapeHtml(v.driver || "-")}</td>
          <td class="mono small">${escapeHtml(v.mountpoint || "-")}</td>
          <td class="actions"></td>
        `;
        tr.querySelector(".actions").appendChild(
          actionBtn("删除", () => doVolRemove(v.name), { danger: true, disabled: !state.takeover })
        );
        body.appendChild(tr);
      });
      if (!(data.items || []).length) {
        body.innerHTML = `<tr><td colspan="4" class="muted">无命名卷</td></tr>`;
      }
    }
    if (kind === "system" || kind === "all") {
      const [info, df] = await Promise.all([api("/api/system/info"), api("/api/system/df")]);
      $("#sys-info").textContent = JSON.stringify(info.info || info, null, 2);
      const slim = {
        layers_size: df.df?.layers_size,
        images: (df.df?.images || []).length,
        containers: (df.df?.containers || []).length,
        volumes: (df.df?.volumes || []).length,
        top_images: (df.df?.images || []).slice(0, 8).map((i) => ({
          tags: i.tags,
          size: fmtBytes(i.size),
        })),
      };
      $("#sys-df").textContent = JSON.stringify(slim, null, 2);
    }
  } catch (e) {
    if (kind === "system") {
      $("#sys-info").textContent = `加载失败：${e.message}`;
    }
  }
}

async function doLife(id, action) {
  if (!requireLogin()) return;
  try {
    const r = await api(`/api/containers/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    if (!r.ok) throw new Error(r.message || "失败");
    loadAll();
  } catch (e) {
    alert(`${action} 失败：${e.message}`);
  }
}

async function doRemove(id) {
  if (!requireLogin() || !requireTakeover("删除容器")) return;
  if (!confirm(`确定删除容器 ${id}？Unraid 应用请优先用模板管理。`)) return;
  try {
    const r = await api(`/api/containers/${encodeURIComponent(id)}?force=true`, { method: "DELETE" });
    alert(r.message || "已删除");
    loadAll();
  } catch (e) {
    alert(e.message);
  }
}

async function showLogs(id, name) {
  try {
    const r = await api(`/api/containers/${encodeURIComponent(id)}/logs?tail=300`);
    $("#logs-title").textContent = `日志 · ${name || id}`;
    $("#logs-body").textContent = r.logs || "(空)";
    $("#logs-dialog").showModal();
  } catch (e) {
    alert(`读取日志失败：${e.message}`);
  }
}

async function doBackup(id) {
  if (!requireLogin()) return;
  try {
    const r = await api(`/api/ops/backup/${encodeURIComponent(id)}`, { method: "POST" });
    alert(r.message || "备份完成");
    loadAll();
  } catch (e) {
    alert(`备份失败：${e.message}`);
  }
}

async function doUpdate(id) {
  if (!requireLogin()) return;
  if (!confirm(`对 ${id} 按管理源执行安全更新？\nCompose→项目 · Unraid→模板重建 · 三方→仅拉镜像`)) return;
  try {
    const r = await api(`/api/ops/update/${encodeURIComponent(id)}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    alert(r.message || "更新完成");
    loadAll();
  } catch (e) {
    alert(`更新失败：${e.message}`);
  }
}

async function doRollback(id) {
  if (!requireLogin()) return;
  try {
    const r = await api(`/api/ops/rollback/${encodeURIComponent(id)}`, { method: "POST" });
    alert(r.message || "已记录回滚指引");
    loadAll();
  } catch (e) {
    alert(`回滚失败：${e.message}`);
  }
}

async function doAdopt(id) {
  if (!requireLogin() || !requireTakeover("Adopt 为 Unraid 模板")) return;
  if (!confirm(`将 ${id} Adopt 为 Unraid my-*.xml 并按模板重建？`)) return;
  try {
    const r = await api(`/api/unraid/adopt/${encodeURIComponent(id)}`, { method: "POST" });
    alert(r.message || "Adopt 完成");
    loadAll();
  } catch (e) {
    alert(`Adopt 失败：${e.message}`);
  }
}

async function doComposeBackup(name) {
  if (!requireLogin()) return;
  try {
    const r = await api(`/api/compose/projects/${encodeURIComponent(name)}/backup`, { method: "POST" });
    alert(r.message || "备份完成");
    loadAll();
  } catch (e) {
    alert(e.message);
  }
}

async function doComposeUpdate(name) {
  if (!requireLogin()) return;
  if (!confirm(`安全更新 Compose 项目 ${name}？`)) return;
  try {
    const r = await api(`/api/compose/projects/${encodeURIComponent(name)}/update`, {
      method: "POST",
      body: JSON.stringify({ recreate: true }),
    });
    alert(r.message || "完成");
    loadAll();
  } catch (e) {
    alert(e.message);
  }
}

async function doComposeUp(name) {
  if (!requireLogin() || !requireTakeover("Compose Up")) return;
  try {
    const r = await api(`/api/compose/projects/${encodeURIComponent(name)}/up`, { method: "POST" });
    alert(r.message || "up 完成");
    loadAll();
  } catch (e) {
    alert(e.message);
  }
}

async function doComposeDown(name) {
  if (!requireLogin() || !requireTakeover("Compose Down")) return;
  if (!confirm(`确定 compose down 项目 ${name}？`)) return;
  try {
    const r = await api(`/api/compose/projects/${encodeURIComponent(name)}/down`, { method: "POST" });
    alert(r.message || "down 完成");
    loadAll();
  } catch (e) {
    alert(e.message);
  }
}

async function doUnraidBackup(name) {
  if (!requireLogin()) return;
  try {
    const r = await api(`/api/unraid/templates/${encodeURIComponent(name)}/backup`, { method: "POST" });
    alert(r.message || "备份完成");
    loadAll();
  } catch (e) {
    alert(e.message);
  }
}

async function doUnraidUpdate(name) {
  if (!requireLogin()) return;
  if (!confirm(`按 Unraid 模板安全更新 ${name}？\n保持 dockerman，避免三方`)) return;
  try {
    const r = await api(`/api/unraid/templates/${encodeURIComponent(name)}/update`, {
      method: "POST",
      body: JSON.stringify({ recreate: true }),
    });
    alert(r.message || "完成");
    loadAll();
  } catch (e) {
    alert(e.message);
  }
}

async function doImageRemove(ref) {
  if (!requireLogin() || !requireTakeover("删除镜像")) return;
  if (!confirm(`删除镜像 ${ref}？`)) return;
  try {
    const r = await api(`/api/images/${encodeURIComponent(ref)}?force=false`, { method: "DELETE" });
    alert(r.message || "已删除");
    loadResources("images");
  } catch (e) {
    alert(e.message);
  }
}

async function doNetRemove(id) {
  if (!requireLogin() || !requireTakeover("删除网络")) return;
  if (!confirm(`删除网络 ${id}？`)) return;
  try {
    const r = await api(`/api/networks/${encodeURIComponent(id)}`, { method: "DELETE" });
    alert(r.message || "已删除");
    loadResources("networks");
  } catch (e) {
    alert(e.message);
  }
}

async function doVolRemove(name) {
  if (!requireLogin() || !requireTakeover("删除卷")) return;
  if (!confirm(`删除卷 ${name}？数据可能丢失。`)) return;
  try {
    const r = await api(`/api/volumes/${encodeURIComponent(name)}`, { method: "DELETE" });
    alert(r.message || "已删除");
    loadResources("volumes");
  } catch (e) {
    alert(e.message);
  }
}

// tabs
document.querySelectorAll("#main-tabs .tab").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

$("#btn-refresh").addEventListener("click", loadAll);
$("#logs-close").addEventListener("click", () => $("#logs-dialog").close());

$("#btn-image-pull").addEventListener("click", async () => {
  if (!requireLogin()) return;
  const image = prompt("镜像名（如 nginx:alpine）");
  if (!image) return;
  try {
    const r = await api("/api/images/pull", { method: "POST", body: JSON.stringify({ image }) });
    alert(r.message || "完成");
    loadResources("images");
  } catch (e) {
    alert(e.message);
  }
});

$("#btn-image-prune").addEventListener("click", async () => {
  if (!requireLogin() || !requireTakeover("清理镜像")) return;
  if (!confirm("清理 dangling 镜像？")) return;
  try {
    const r = await api("/api/images/prune", { method: "POST" });
    alert(r.message || "完成");
    loadResources("images");
  } catch (e) {
    alert(e.message);
  }
});

$("#btn-net-create").addEventListener("click", async () => {
  if (!requireLogin() || !requireTakeover("创建网络")) return;
  const name = prompt("网络名称");
  if (!name) return;
  try {
    const r = await api("/api/networks", {
      method: "POST",
      body: JSON.stringify({ name, driver: "bridge" }),
    });
    alert(r.message || "完成");
    loadResources("networks");
  } catch (e) {
    alert(e.message);
  }
});

$("#btn-vol-create").addEventListener("click", async () => {
  if (!requireLogin() || !requireTakeover("创建卷")) return;
  const name = prompt("卷名称");
  if (!name) return;
  try {
    const r = await api("/api/volumes", { method: "POST", body: JSON.stringify({ name }) });
    alert(r.message || "完成");
    loadResources("volumes");
  } catch (e) {
    alert(e.message);
  }
});

$("#btn-vol-prune").addEventListener("click", async () => {
  if (!requireLogin() || !requireTakeover("清理卷")) return;
  if (!confirm("清理未使用的卷？")) return;
  try {
    const r = await api("/api/volumes/prune", { method: "POST" });
    alert(r.message || "完成");
    loadResources("volumes");
  } catch (e) {
    alert(e.message);
  }
});

$("#btn-sys-prune").addEventListener("click", async () => {
  if (!requireLogin() || !requireTakeover("系统清理")) return;
  if (!confirm("清理已停止容器 + dangling 镜像 + 未使用网络？（默认不删卷）")) return;
  try {
    const r = await api("/api/system/prune", {
      method: "POST",
      body: JSON.stringify({
        containers: true,
        images: true,
        volumes: false,
        networks: true,
        dangling_images_only: true,
      }),
    });
    alert(r.message || "完成");
    loadResources("system");
    loadAll();
  } catch (e) {
    alert(e.message);
  }
});

$("#btn-login").addEventListener("click", async () => {
  if (state.needsSetup) {
    $("#setup-error").hidden = true;
    $("#setup-dialog").showModal();
    return;
  }
  if (state.token) {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch (_) {}
    state.token = "";
    state.username = "";
    localStorage.removeItem("dockerops_token");
    localStorage.removeItem("dockerops_user");
    setAuthUI();
    return;
  }
  $("#login-error").hidden = true;
  $("#login-dialog").showModal();
});

$("#login-cancel").addEventListener("click", () => $("#login-dialog").close());

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const password = fd.get("password");
  const password_confirm = fd.get("password_confirm");
  if (password !== password_confirm) {
    const el = $("#setup-error");
    el.hidden = false;
    el.textContent = "两次输入的密码不一致";
    return;
  }
  try {
    const data = await api("/api/auth/setup", {
      method: "POST",
      body: JSON.stringify({
        username: fd.get("username"),
        password,
        password_confirm,
      }),
    });
    state.token = data.access_token;
    state.username = data.username;
    state.needsSetup = false;
    localStorage.setItem("dockerops_token", state.token);
    localStorage.setItem("dockerops_user", state.username);
    $("#setup-dialog").close();
    setAuthUI();
    loadAll();
  } catch (err) {
    const el = $("#setup-error");
    el.hidden = false;
    el.textContent = err.message || "设置失败";
  }
});

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: fd.get("username"),
        password: fd.get("password"),
      }),
    });
    state.token = data.access_token;
    state.username = data.username;
    state.needsSetup = false;
    localStorage.setItem("dockerops_token", state.token);
    localStorage.setItem("dockerops_user", state.username);
    $("#login-dialog").close();
    setAuthUI();
    loadAll();
  } catch (err) {
    const el = $("#login-error");
    el.hidden = false;
    el.textContent = err.message || "登录失败";
  }
});

switchTab("overview");
loadAll();
setInterval(loadAll, 60000);
