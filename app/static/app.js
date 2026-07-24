const state = {
  token: localStorage.getItem("dockerops_token") || "",
  username: localStorage.getItem("dockerops_user") || "",
  takeover: false,
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
  return new Date(Number(ts) * 1000).toLocaleString();
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

function setAuthUI() {
  const el = $("#auth-state");
  if (state.token) {
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
    alert(`${action} 需要开启完整接管：DOCKEROPS_TAKEOVER_ENABLED=true，并挂载 rw docker.sock / 模板目录。`);
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

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function loadAll() {
  setAuthUI();
  try {
    const [doctor, containers, ops, health, summary, compose, unraid] = await Promise.all([
      api("/api/doctor"),
      api("/api/containers"),
      api("/api/ops/records?limit=30"),
      api("/api/health"),
      api("/api/managers/summary"),
      api("/api/compose/projects"),
      api("/api/unraid/templates"),
    ]);

    state.takeover = !!summary.takeover_enabled;
    setAuthUI();

    const score = doctor.health_score ?? "--";
    $("#health-score").textContent = score;
    const label = $("#health-label");
    label.textContent = doctor.label || "未知";
    label.className = `badge ${scoreClass(Number(score) || 0)}`;

    const advice = $("#advice");
    advice.innerHTML = "";
    const tips = [...(doctor.advice || []), ...((summary.hints || []))];
    tips.forEach((a) => {
      const li = document.createElement("li");
      li.textContent = a;
      advice.appendChild(li);
    });

    $("#engine-info").textContent = JSON.stringify(
      {
        service: { version: health.version, takeover: health.takeover_enabled },
        managers: summary.counts,
        unraid: summary.unraid,
        compose: summary.compose,
        engine: doctor.engine,
        counts: doctor.counts,
      },
      null,
      2
    );

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
      actions.appendChild(actionBtn("备份", () => doBackup(id)));
      actions.appendChild(actionBtn("更新", () => doUpdate(id)));
      actions.appendChild(actionBtn("回滚", () => doRollback(id)));
      if (c.manager === "third_party") {
        actions.appendChild(
          actionBtn("Adopt", () => doAdopt(id), { disabled: !state.takeover })
        );
      }
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
      actions.appendChild(actionBtn("Down", () => doComposeDown(p.name), { danger: true, disabled: !state.takeover }));
      cbody.appendChild(tr);
    });
    if (!(compose.items || []).length) {
      cbody.innerHTML = `<tr><td colspan="5" class="muted">未发现 Compose 项目（需容器带 compose labels 或挂载 DOCKEROPS_COMPOSE_PROJECT_DIRS）</td></tr>`;
    }

    // unraid
    const uAvail = unraid.available;
    $("#unraid-count").textContent = uAvail
      ? `共 ${(unraid.items || []).length} 个 · ${unraid.path || ""}`
      : `模板目录未挂载 · ${unraid.path || ""}`;
    const ubody = $("#unraid-rows");
    ubody.innerHTML = "";
    if (!uAvail) {
      ubody.innerHTML = `<tr><td colspan="5" class="muted">请挂载 /boot/config/plugins/dockerMan/templates-user → /unraid/templates-user</td></tr>`;
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
  } catch (e) {
    $("#engine-info").textContent = `加载失败：${e.message}`;
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
  if (!confirm(`对 ${id} 按管理源执行安全更新？\nCompose→项目更新 · Unraid→模板重建 · 三方→仅拉镜像`)) return;
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
  if (!confirm(`将 ${id} Adopt 为 Unraid my-*.xml 并按模板重建？\n将写入 dockerman 标签，不再显示为三方。`)) return;
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
  if (!confirm(`安全更新 Compose 项目 ${name}？\n(备份 + pull；接管开启时 up --force-recreate)`)) return;
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
  if (!confirm(`按 Unraid 模板安全更新 ${name}？\n(备份 XML + pull；接管开启时模板重建，保持 dockerman)`)) return;
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

$("#btn-refresh").addEventListener("click", loadAll);

$("#btn-login").addEventListener("click", async () => {
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

loadAll();
setInterval(loadAll, 60000);
