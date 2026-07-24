const state = {
  token: localStorage.getItem("dockerops_token") || "",
  username: localStorage.getItem("dockerops_user") || "",
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
  const d = new Date(Number(ts) * 1000);
  return d.toLocaleString();
}

function scoreClass(score) {
  if (score >= 75) return "ok";
  if (score >= 50) return "warn";
  return "bad";
}

function pillClass(text) {
  const t = (text || "").toLowerCase();
  return `pill ${t}`;
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
}

async function loadAll() {
  setAuthUI();
  try {
    const [doctor, containers, ops, health] = await Promise.all([
      api("/api/doctor"),
      api("/api/containers"),
      api("/api/ops/records?limit=30"),
      api("/api/health"),
    ]);

    const score = doctor.health_score ?? "--";
    $("#health-score").textContent = score;
    const label = $("#health-label");
    label.textContent = doctor.label || "未知";
    label.className = `badge ${scoreClass(Number(score) || 0)}`;

    const advice = $("#advice");
    advice.innerHTML = "";
    (doctor.advice || []).forEach((a) => {
      const li = document.createElement("li");
      li.textContent = a;
      advice.appendChild(li);
    });

    $("#engine-info").textContent = JSON.stringify(
      {
        service: health,
        engine: doctor.engine,
        counts: doctor.counts,
      },
      null,
      2
    );

    $("#container-count").textContent = `共 ${containers.count || 0} 个`;
    const tbody = $("#container-rows");
    tbody.innerHTML = "";
    (containers.items || []).forEach((c) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${escapeHtml(c.name || c.id)}</strong><div class="muted mono">${escapeHtml(c.id || "")}</div></td>
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
      tbody.appendChild(tr);
    });
    if (!(containers.items || []).length) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted">暂无容器或无法连接 Docker</td></tr>`;
    }

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

function actionBtn(text, fn) {
  const b = document.createElement("button");
  b.className = "btn small";
  b.textContent = text;
  b.addEventListener("click", fn);
  return b;
}

function requireLogin() {
  if (!state.token) {
    alert("写操作需要先登录");
    $("#login-dialog").showModal();
    return false;
  }
  return true;
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
  if (!confirm(`对 ${id} 执行安全更新（备份 + 拉镜像）？`)) return;
  try {
    const r = await api(`/api/ops/update/${encodeURIComponent(id)}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    alert(r.message || "更新流程完成");
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

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
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
