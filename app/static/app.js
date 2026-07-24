const state = {
  token: localStorage.getItem("dockerops_token") || "",
  username: localStorage.getItem("dockerops_user") || "",
  takeover: false,
  platform: "generic",
  version: "0.4.1",
  tab: "overview",
  needsSetup: false,
  containers: [],
  compose: [],
  unraid: [],
  updateItems: [],
  images: [],
  networks: [],
  volumes: [],
  prefs: {
    particles: true,
    particles_count: 90,
    bg_theme: "cyber",
    card_density: "comfortable",
    reduce_motion: false,
  },
  selected: new Set(),
};

const PLATFORM_LABEL = {
  unraid: "Unraid系统",
  fnos: "飞牛系统",
  generic: "通用系统",
};

const TAB_TITLES = {
  overview: ["总览", "平台 · 引擎 · 健康 · 活动容器"],
  containers: ["容器", "生命周期 · 批量操作 · 日志 · 安全更新"],
  updates: ["更新检测", "一键检测镜像更新并安全升级"],
  compose: ["Compose", "项目发现 · 双方接管"],
  unraid: ["Unraid", "dockerMan 模板 · 非三方更新"],
  images: ["镜像", "拉取 · 清理 · 历史"],
  networks: ["网络", "列表 · 创建 · 删除"],
  volumes: ["卷", "列表 · 创建 · 清理"],
  system: ["系统", "Engine · 磁盘占用 · 清理"],
  docs: ["说明日志", "使用说明 · 版本更新日志"],
  settings: ["个性化", "背景 · 粒子 · 卡片密度"],
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
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function platformLabel(p) {
  return PLATFORM_LABEL[p] || PLATFORM_LABEL.generic;
}

function kv(k, v, mono = false) {
  return `<div class="kv"><span class="k">${escapeHtml(k)}</span><span class="v${mono ? " mono" : ""}">${escapeHtml(v ?? "—")}</span></div>`;
}

function applyPrefsLocal(prefs) {
  state.prefs = { ...state.prefs, ...prefs };
  document.body.dataset.bg = state.prefs.bg_theme || "cyber";
  document.body.dataset.density = state.prefs.card_density || "comfortable";
  document.body.classList.toggle("reduce-motion", !!state.prefs.reduce_motion);
  if (window.DockerOpsParticles) {
    window.DockerOpsParticles.applyPrefs(state.prefs);
  }
  const p = $("#pref-particles");
  const pc = $("#pref-particles-count");
  const pcv = $("#pref-particles-count-val");
  const bg = $("#pref-bg-theme");
  const dens = $("#pref-density");
  const rm = $("#pref-reduce-motion");
  if (p) p.checked = !!state.prefs.particles;
  if (pc) pc.value = state.prefs.particles_count || 90;
  if (pcv) pcv.textContent = String(state.prefs.particles_count || 90);
  if (bg) bg.value = state.prefs.bg_theme || "cyber";
  if (dens) dens.value = state.prefs.card_density || "comfortable";
  if (rm) rm.checked = !!state.prefs.reduce_motion;
}

function setVersionUI() {
  const plat = platformLabel(state.platform);
  const ver = state.version || "0.4.0";
  const side = $("#sidebar-version");
  if (side) side.textContent = `v${ver} · ${plat}`;
  const footV = $("#foot-version");
  if (footV) footV.textContent = `v${ver}`;
  const footP = $("#foot-platform");
  if (footP) footP.textContent = plat;
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
  pb.textContent = platformLabel(state.platform);
  pb.className = `badge platform-${state.platform || "generic"}`;
  setVersionUI();
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

function setSidebarOpen(open) {
  const sb = $("#sidebar");
  const bd = $("#sidebar-backdrop");
  if (!sb) return;
  sb.classList.toggle("open", !!open);
  if (bd) {
    bd.classList.toggle("show", !!open);
    // keep [hidden] in sync so CSS [hidden] rule always wins when closed
    if (open) {
      bd.hidden = false;
      bd.removeAttribute("hidden");
    } else {
      bd.hidden = true;
      bd.setAttribute("hidden", "");
    }
  }
  document.body.classList.toggle("sidebar-open", !!open);
}

/** Show Compose menu only when there are real compose projects. */
function updateComposeNavVisibility() {
  const hasCompose = Array.isArray(state.compose) && state.compose.length > 0;
  const nav = document.querySelector('#main-tabs .nav-item[data-tab="compose"]');
  if (nav) {
    nav.hidden = !hasCompose;
    nav.style.display = hasCompose ? "" : "none";
  }
  // if currently on compose but none available, bounce to overview
  if (!hasCompose && state.tab === "compose") {
    switchTab("overview");
  }
}

function switchTab(name) {
  // guard: cannot open compose when hidden
  if (name === "compose" && !(state.compose && state.compose.length)) {
    name = "overview";
  }
  state.tab = name;
  document.querySelectorAll("#main-tabs .nav-item").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    const panels = (p.dataset.panel || "").split(/\s+/);
    p.hidden = !panels.includes(name);
  });
  const titles = TAB_TITLES[name] || [name, ""];
  const pt = $("#page-title");
  const ps = $("#page-sub");
  if (pt) pt.textContent = titles[0];
  if (ps) ps.textContent = titles[1];
  if (["images", "networks", "volumes", "system"].includes(name)) {
    loadResources(name);
  }
  if (name === "docs") loadChangelog();
  if (name === "settings") applyPrefsLocal(state.prefs);
  setSidebarOpen(false);
}

async function checkSetup() {
  try {
    const st = await api("/api/auth/status");
    state.needsSetup = !!st.needs_setup;
    setAuthUI();
    if (state.needsSetup) {
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

function matchFilter(text, q) {
  if (!q) return true;
  return String(text || "").toLowerCase().includes(q.toLowerCase());
}

function updateBatchCount() {
  const el = $("#batch-sel-count");
  if (el) el.textContent = `已选 ${state.selected.size}`;
}

function renderContainers() {
  const q = ($("#container-filter")?.value || "").trim();
  const stf = ($("#container-status-filter")?.value || "").trim();
  const mgr = ($("#container-mgr-filter")?.value || "").trim();
  let items = state.containers || [];
  items = items.filter((c) => {
    if (stf && (c.status || "").toLowerCase() !== stf) return false;
    if (mgr && (c.manager || "") !== mgr) return false;
    if (!q) return true;
    const blob = [c.name, c.id, c.image, c.manager, c.compose_project, c.label].join(" ");
    return matchFilter(blob, q);
  });
  $("#container-count").textContent = `显示 ${items.length} / 共 ${state.containers.length} 个`;
  const tbody = $("#container-rows");
  tbody.innerHTML = "";
  items.forEach((c) => {
    const tr = document.createElement("tr");
    const id = c.id || c.name;
    const checked = state.selected.has(id) ? "checked" : "";
    const mgrExtra =
      c.manager === "compose" && c.compose_project
        ? `<div class="muted mono">${escapeHtml(c.compose_project)}/${escapeHtml(c.compose_service || "")}</div>`
        : c.manager === "unraid"
          ? `<div class="muted mono">template</div>`
          : "";
    tr.innerHTML = `
      <td><input type="checkbox" class="ctr-sel" data-id="${escapeHtml(id)}" ${checked} /></td>
      <td><strong>${escapeHtml(c.name || c.id)}</strong><div class="muted mono">${escapeHtml(c.id || "")}</div></td>
      <td>${managerPill(c.manager, c.label)}${mgrExtra}</td>
      <td class="mono">${escapeHtml(c.image || "")}</td>
      <td><span class="${pillClass(c.status)}">${escapeHtml(c.status || "-")}</span></td>
      <td><span class="${pillClass(c.health || "none")}">${escapeHtml(c.health || "-")}</span></td>
      <td>${c.restart_count ?? 0}</td>
      <td class="actions"></td>
    `;
    const actions = tr.querySelector(".actions");
    const running = (c.status || "").toLowerCase() === "running";
    const paused = (c.status || "").toLowerCase() === "paused";
    if (!running && !paused) actions.appendChild(actionBtn("启动", () => doLife(id, "start")));
    if (running) {
      actions.appendChild(actionBtn("停止", () => doLife(id, "stop")));
      actions.appendChild(actionBtn("重启", () => doLife(id, "restart")));
      actions.appendChild(actionBtn("暂停", () => doLife(id, "pause")));
    }
    if (paused) actions.appendChild(actionBtn("恢复", () => doLife(id, "unpause")));
    actions.appendChild(actionBtn("详情", () => showDetail(id, c.name)));
    actions.appendChild(actionBtn("日志", () => showLogs(id, c.name)));
    actions.appendChild(actionBtn("重命名", () => doRename(id, c.name)));
    actions.appendChild(actionBtn("备份", () => doBackup(id)));
    actions.appendChild(actionBtn("更新", () => doUpdate(id)));
    actions.appendChild(actionBtn("回滚", () => doRollback(id)));
    if (c.manager === "third_party") {
      actions.appendChild(actionBtn("Adopt", () => doAdopt(id), { disabled: !state.takeover }));
    }
    actions.appendChild(actionBtn("删除", () => doRemove(id), { danger: true, disabled: !state.takeover }));
    tbody.appendChild(tr);
  });
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">无匹配容器</td></tr>`;
  }
  tbody.querySelectorAll(".ctr-sel").forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = cb.dataset.id;
      if (cb.checked) state.selected.add(id);
      else state.selected.delete(id);
      updateBatchCount();
    });
  });
  updateBatchCount();
}

function renderCompose() {
  const q = ($("#compose-filter")?.value || "").trim();
  let items = state.compose || [];
  if (q) {
    items = items.filter((p) =>
      matchFilter([p.name, p.working_dir, (p.services || []).join(" ")].join(" "), q)
    );
  }
  $("#compose-count").textContent = `显示 ${items.length} / 共 ${state.compose.length} 个项目`;
  const cbody = $("#compose-rows");
  cbody.innerHTML = "";
  items.forEach((p) => {
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
  if (!items.length) {
    cbody.innerHTML = `<tr><td colspan="5" class="muted">未发现 Compose 项目</td></tr>`;
  }
}

function renderUnraid() {
  const q = ($("#unraid-filter")?.value || "").trim();
  let items = state.unraid || [];
  if (q) {
    items = items.filter((t) =>
      matchFilter([t.name, t.repository, t.file, t.network].join(" "), q)
    );
  }
  const ubody = $("#unraid-rows");
  ubody.innerHTML = "";
  items.forEach((t) => {
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
  if (!items.length) {
    ubody.innerHTML = `<tr><td colspan="5" class="muted">无匹配模板</td></tr>`;
  }
}

function renderUpdates() {
  const body = $("#update-rows");
  const items = state.updateItems || [];
  body.innerHTML = "";
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="7" class="muted">点击「检测更新」开始扫描</td></tr>`;
    return;
  }
  items.forEach((u) => {
    const tr = document.createElement("tr");
    let pill = "update-err";
    let label = u.message || "-";
    if (u.update_available) {
      pill = "update-yes";
      label = "可更新";
    } else if (u.check_ok) {
      pill = "update-no";
      label = "最新";
    }
    const canSel = !!u.update_available;
    tr.innerHTML = `
      <td><input type="checkbox" class="upd-sel" data-id="${escapeHtml(u.id || "")}" ${canSel ? "" : "disabled"} ${canSel ? "checked" : ""} /></td>
      <td><strong>${escapeHtml(u.name || "")}</strong><div class="muted mono">${escapeHtml((u.id || "").slice(0, 12))}</div></td>
      <td>${managerPill(u.manager)}</td>
      <td class="mono small">${escapeHtml(u.image || "-")}</td>
      <td><span class="${pillClass(u.status)}">${escapeHtml(u.status || "-")}</span></td>
      <td><span class="pill ${pill}">${escapeHtml(label)}</span>
        <div class="muted small">${escapeHtml(u.message || "")}${u.remote_digest ? `<br/>remote: ${escapeHtml(String(u.remote_digest).slice(0, 24))}…` : ""}</div>
      </td>
      <td class="actions"></td>
    `;
    const actions = tr.querySelector(".actions");
    if (u.id) {
      actions.appendChild(actionBtn("安全更新", () => doUpdate(u.id)));
      actions.appendChild(actionBtn("日志", () => showLogs(u.id, u.name)));
    }
    body.appendChild(tr);
  });
}

function renderActivity(items) {
  const el = $("#activity-list");
  if (!el) return;
  if (!items || !items.length) {
    el.innerHTML = `<div class="muted">暂无运行中容器的资源数据</div>`;
    return;
  }
  el.innerHTML = items
    .map((it) => {
      const s = it.stats || {};
      const cpu = Math.min(100, Number(s.cpu_percent) || 0);
      const memP = Math.min(100, Number(s.mem_percent) || 0);
      const mem = s.mem_usage != null ? fmtBytes(s.mem_usage) : "—";
      const memL = s.mem_limit != null ? fmtBytes(s.mem_limit) : "";
      return `<div class="activity-card">
        <div class="name" title="${escapeHtml(it.name || "")}">${escapeHtml(it.name || it.id || "")}</div>
        <div class="meter"><span>CPU</span><div class="meter-bar"><div class="meter-fill" style="width:${cpu}%"></div></div><span class="meter-val">${cpu.toFixed(1)}%</span></div>
        <div class="meter"><span>MEM</span><div class="meter-bar"><div class="meter-fill mem" style="width:${memP}%"></div></div><span class="meter-val">${memP.toFixed(1)}%</span></div>
        <div class="muted small">${escapeHtml(mem)}${memL ? " / " + escapeHtml(memL) : ""}</div>
      </div>`;
    })
    .join("");
}

function renderOverviewCards({ doctor, health, platform, summary, sysInfo }) {
  const dcounts = doctor.counts || {};
  const ctrs = state.containers || [];
  const running = ctrs.filter((c) => (c.status || "").toLowerCase() === "running").length;
  const unhealthy = ctrs.filter((c) => {
    const h = (c.health || "").toLowerCase();
    const st = (c.status || "").toLowerCase();
    return h === "unhealthy" || st === "exited" || st === "dead";
  }).length;
  const setTxt = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.textContent = v ?? "-";
  };
  setTxt("stat-running", running);
  setTxt("stat-total", dcounts.containers ?? ctrs.length);
  setTxt("stat-unhealthy", unhealthy);
  setTxt("stat-images", sysInfo?.Images ?? sysInfo?.images ?? dcounts.warning ?? "-");

  const eng = doctor.engine || health.docker || {};
  const si = sysInfo || {};
  const osr = platform.os_release || {};
  const platName = platformLabel(state.platform);

  const pcb = $("#platform-card-badge");
  if (pcb) {
    pcb.textContent = platName;
    pcb.className = `badge platform-${state.platform || "generic"}`;
  }
  const pc = $("#platform-cards");
  if (pc) {
    pc.innerHTML = [
      kv("平台", platName),
      kv("系统", osr.PRETTY_NAME || osr.NAME || si.OperatingSystem || "—"),
      kv("接管", state.takeover ? "已开启" : "关闭"),
      kv("资源 API", platform.resource_apis !== false ? "开启" : "关闭"),
      kv("模板目录", platform.unraid?.available ? "已挂载" : "未挂载"),
      kv("控制台", platform.console_enabled ? "开启" : "关闭"),
    ].join("");
  }

  const dockerVer =
    eng.engine_version || eng.version || si.ServerVersion || health.docker?.engine_version || "Docker";
  const evb = $("#engine-version-badge");
  if (evb) evb.textContent = dockerVer;
  const ec = $("#engine-cards");
  if (ec) {
    ec.innerHTML = [
      kv("Docker", dockerVer),
      kv("API", eng.api_version || si.ApiVersion || "—"),
      kv("架构", eng.arch || si.Architecture || "—"),
      kv("内核", si.KernelVersion || "—"),
      kv("存储驱动", si.Driver || "—"),
      kv("操作系统", eng.os || si.OperatingSystem || "—"),
      kv("服务版本", `DockerOps ${health.version || state.version}`),
      kv("引擎连通", eng.ok === false || health.docker?.ok === false ? "异常" : "正常"),
    ].join("");
  }

  const hostBadge = $("#sys-host-badge");
  if (hostBadge) hostBadge.textContent = platName;
  const sys = $("#sys-info-cards");
  if (sys) {
    const caps = platform.capabilities || {};
    const capOn = Object.entries(caps)
      .filter(([, v]) => v)
      .map(([k]) => k)
      .slice(0, 6)
      .join(", ");
    sys.innerHTML = [
      kv("主机名", si.Name || "—"),
      kv("CPU", si.NCPU ?? "—"),
      kv("内存", si.MemTotal ? fmtBytes(si.MemTotal) : "—"),
      kv("容器总数", dcounts.containers ?? ctrs.length),
      kv("运行中", si.ContainersRunning ?? running),
      kv("能力", capOn || "—"),
    ].join("");
  }

  const mc = summary.counts || {};
  setTxt("mgr-unraid", mc.unraid ?? 0);
  setTxt("mgr-compose", mc.compose ?? 0);
  setTxt("mgr-third", mc.third_party ?? 0);
}

async function loadAll() {
  setAuthUI();
  try {
    await checkSetup();
    const [doctor, containers, ops, health, summary, compose, unraid, platform, events, prefs, activity, sysPack] =
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
        api("/api/prefs").catch(() => ({ prefs: state.prefs })),
        api("/api/activity?limit=12").catch(() => ({ items: [] })),
        api("/api/system/info").catch(() => ({ info: null })),
      ]);

    state.takeover = !!summary.takeover_enabled;
    state.platform = platform.platform || summary.platform || health.platform || "generic";
    state.version = health.version || state.version;
    state.containers = containers.items || [];
    state.compose = compose.items || [];
    state.unraid = unraid.items || [];
    updateComposeNavVisibility();
    if (prefs.prefs) applyPrefsLocal(prefs.prefs);
    // ensure particles re-apply after load (prefs may have toggled)
    if (window.DockerOpsParticles) {
      window.DockerOpsParticles.applyPrefs(state.prefs);
    }
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

    renderOverviewCards({
      doctor,
      health,
      platform,
      summary,
      sysInfo: sysPack.info || sysPack || null,
    });
    renderActivity(activity.items || []);

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

    renderContainers();
    renderCompose();

    const uAvail = unraid.available;
    $("#unraid-count").textContent = uAvail
      ? `共 ${(unraid.items || []).length} 个 · ${unraid.path || ""}`
      : `模板目录未挂载 · ${unraid.path || ""}`;
    if (!uAvail) {
      $("#unraid-rows").innerHTML = `<tr><td colspan="5" class="muted">请挂载 templates-user（非 Unraid 可忽略）</td></tr>`;
    } else {
      renderUnraid();
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

    if (["images", "networks", "volumes", "system"].includes(state.tab)) {
      await loadResources(state.tab);
    }
    if (state.tab === "docs") await loadChangelog();
  } catch (e) {
    const pc = $("#platform-cards");
    if (pc) pc.innerHTML = kv("错误", e.message);
  }
}

async function loadChangelog() {
  const el = $("#changelog-list");
  if (!el) return;
  try {
    const data = await api("/api/changelog");
    const items = data.items || [];
    if (!items.length) {
      el.innerHTML = `<div class="muted">暂无更新日志</div>`;
      return;
    }
    el.innerHTML = items
      .map(
        (c) => `<article class="cl-item">
          <h3><span>v${escapeHtml(c.version)}</span><span class="muted small">${escapeHtml(c.date || "")}</span></h3>
          <ul>${(c.items || []).map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>
        </article>`
      )
      .join("");
  } catch (e) {
    el.innerHTML = `<div class="muted">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

async function loadResources(kind) {
  try {
    if (kind === "images" || kind === "all") {
      const data = await api("/api/images");
      state.images = data.items || [];
      renderImages();
    }
    if (kind === "networks" || kind === "all") {
      const data = await api("/api/networks");
      state.networks = data.items || [];
      renderNetworks();
    }
    if (kind === "volumes" || kind === "all") {
      const data = await api("/api/volumes");
      state.volumes = data.items || [];
      renderVolumes();
    }
    if (kind === "system") {
      const [info, df] = await Promise.all([api("/api/system/info"), api("/api/system/df")]);
      const eng = info.info || info || {};
      const infoEl = $("#sys-info");
      if (infoEl) {
        infoEl.innerHTML = [
          kv("Version", eng.version || eng.Version || "—"),
          kv("API", eng.api_version || eng.ApiVersion || "—"),
          kv("OS", eng.operating_system || eng.OperatingSystem || "—"),
          kv("Arch", eng.architecture || eng.Architecture || "—"),
          kv("CPUs", eng.ncpu || eng.NCPU || "—"),
          kv("Memory", eng.mem_total || eng.MemTotal ? fmtBytes(eng.mem_total || eng.MemTotal) : "—"),
          kv("Driver", eng.driver || eng.Driver || "—"),
          kv("Kernel", eng.kernel_version || eng.KernelVersion || "—"),
        ].join("");
      }
      const d = df.df || df || {};
      const overview = $("#sys-overview-cards");
      if (overview) {
        overview.innerHTML = `
          <div class="sys-stat"><div class="n">${(d.images || []).length}</div><div class="l">镜像</div></div>
          <div class="sys-stat"><div class="n">${(d.containers || []).length}</div><div class="l">容器记录</div></div>
          <div class="sys-stat"><div class="n">${(d.volumes || []).length}</div><div class="l">卷</div></div>
          <div class="sys-stat"><div class="n">${fmtBytes(d.layers_size || 0)}</div><div class="l">层大小</div></div>
        `;
      }
      const dfEl = $("#sys-df");
      if (dfEl) {
        dfEl.innerHTML = [
          kv("镜像数", (d.images || []).length),
          kv("容器记录", (d.containers || []).length),
          kv("卷数", (d.volumes || []).length),
          kv("层大小", fmtBytes(d.layers_size || 0)),
        ].join("");
      }
      const top = $("#sys-top-images");
      if (top) {
        const rows = (d.images || [])
          .slice()
          .sort((a, b) => (b.size || 0) - (a.size || 0))
          .slice(0, 8);
        top.innerHTML = rows.length
          ? rows
              .map(
                (i) =>
                  `<tr><td class="mono small">${escapeHtml((i.tags && i.tags[0]) || i.id || "-")}</td><td>${fmtBytes(i.size)}</td></tr>`
              )
              .join("")
          : `<tr><td colspan="2" class="muted">无数据</td></tr>`;
      }
    }
  } catch (e) {
    if (kind === "system") {
      const infoEl = $("#sys-info");
      if (infoEl) infoEl.innerHTML = kv("错误", e.message);
    }
  }
}

function renderImages() {
  const q = ($("#image-filter")?.value || "").trim();
  let items = state.images || [];
  if (q) items = items.filter((i) => matchFilter([i.label, ...(i.tags || []), i.id].join(" "), q));
  $("#image-count").textContent = `显示 ${items.length} / 共 ${state.images.length} 个`;
  const body = $("#image-rows");
  body.innerHTML = "";
  items.forEach((img) => {
    const tr = document.createElement("tr");
    const label = (img.tags && img.tags[0]) || img.label || img.id;
    tr.innerHTML = `
      <td class="mono"><strong>${escapeHtml(label)}</strong>
        <div class="muted">${escapeHtml((img.tags || []).slice(1).join(", "))}</div>
        <div class="muted">${escapeHtml(img.id || "")}</div>
      </td>
      <td>${fmtBytes(img.size)}</td>
      <td class="muted small">${escapeHtml(img.created || "-")}</td>
      <td class="actions"></td>
    `;
    const ref = (img.tags && img.tags[0]) || img.id;
    const actions = tr.querySelector(".actions");
    actions.appendChild(actionBtn("历史", () => showImageHistory(ref)));
    actions.appendChild(actionBtn("删除", () => doImageRemove(ref), { danger: true, disabled: !state.takeover }));
    body.appendChild(tr);
  });
  if (!items.length) body.innerHTML = `<tr><td colspan="4" class="muted">无镜像</td></tr>`;
}

function renderNetworks() {
  const q = ($("#network-filter")?.value || "").trim();
  let items = state.networks || [];
  if (q) items = items.filter((n) => matchFilter([n.name, n.driver, n.subnet].join(" "), q));
  $("#network-count").textContent = `显示 ${items.length} / 共 ${state.networks.length} 个`;
  const body = $("#network-rows");
  body.innerHTML = "";
  items.forEach((n) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(n.name)}</strong><div class="muted mono">${escapeHtml(n.id || "")}</div></td>
      <td>${escapeHtml(n.driver || "-")}</td>
      <td class="mono small">${escapeHtml(n.subnet || "-")}</td>
      <td>${n.containers ?? 0}</td>
      <td class="actions"></td>
    `;
    const protectedNet = ["bridge", "host", "none"].includes(n.name);
    tr.querySelector(".actions").appendChild(
      actionBtn("删除", () => doNetRemove(n.id || n.name), {
        danger: true,
        disabled: !state.takeover || protectedNet,
      })
    );
    body.appendChild(tr);
  });
  if (!items.length) body.innerHTML = `<tr><td colspan="5" class="muted">无网络</td></tr>`;
}

function renderVolumes() {
  const q = ($("#volume-filter")?.value || "").trim();
  let items = state.volumes || [];
  if (q) items = items.filter((v) => matchFilter([v.name, v.driver, v.mountpoint].join(" "), q));
  $("#volume-count").textContent = `显示 ${items.length} / 共 ${state.volumes.length} 个`;
  const body = $("#volume-rows");
  body.innerHTML = "";
  items.forEach((v) => {
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
  if (!items.length) body.innerHTML = `<tr><td colspan="4" class="muted">无命名卷（Unraid 多为 bind mount）</td></tr>`;
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

async function doBatch(action) {
  if (!requireLogin()) return;
  const ids = Array.from(state.selected);
  if (!ids.length) {
    alert("请先勾选容器");
    return;
  }
  if (action === "remove") {
    if (!requireTakeover("批量删除")) return;
    if (!confirm(`确定批量删除 ${ids.length} 个容器？`)) return;
  } else if (!confirm(`对 ${ids.length} 个容器执行 ${action}？`)) {
    return;
  }
  try {
    const r = await api("/api/containers/batch", {
      method: "POST",
      body: JSON.stringify({ action, ids, force: action === "remove" }),
    });
    alert(r.message || "完成");
    state.selected.clear();
    loadAll();
  } catch (e) {
    alert(e.message);
  }
}

async function doRename(id, oldName) {
  if (!requireLogin()) return;
  const name = prompt("新容器名称", oldName || "");
  if (!name || name === oldName) return;
  try {
    const r = await api(`/api/containers/${encodeURIComponent(id)}/rename`, {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    alert(r.message || "已重命名");
    loadAll();
  } catch (e) {
    alert(e.message);
  }
}

async function showDetail(id, name) {
  try {
    const [detail, stats] = await Promise.all([
      api(`/api/containers/${encodeURIComponent(id)}`),
      api(`/api/containers/${encodeURIComponent(id)}/stats`).catch(() => null),
    ]);
    const c = detail.item || {};
    const s = (stats && stats.item && stats.item.stats) || null;
    $("#detail-title").textContent = `容器 · ${name || c.name || id}`;
    const rows = [
      kv("名称", c.name || "—"),
      kv("ID", c.id || id, true),
      kv("镜像", c.image || "—", true),
      kv("状态", c.status || "—"),
      kv("健康", c.health || "—"),
      kv("管理源", c.label || c.manager || "—"),
      kv("重启次数", c.restart_count ?? 0),
      kv("创建", c.created ? fmtTime(c.created) : "—"),
    ];
    if (s) {
      rows.push(
        kv("CPU", `${(Number(s.cpu_percent) || 0).toFixed(1)}%`),
        kv("内存", `${fmtBytes(s.mem_usage || 0)} / ${fmtBytes(s.mem_limit || 0)}`)
      );
    }
    if (c.ports) rows.push(kv("端口", typeof c.ports === "string" ? c.ports : JSON.stringify(c.ports)));
    if (c.compose_project) rows.push(kv("Compose", `${c.compose_project}/${c.compose_service || ""}`));
    $("#detail-body").innerHTML = `<div class="kv-grid">${rows.join("")}</div>`;
    $("#detail-dialog").showModal();
  } catch (e) {
    alert(`详情失败：${e.message}`);
  }
}

async function showImageHistory(ref) {
  try {
    const r = await api(`/api/images/${encodeURIComponent(ref)}/history`);
    const items = r.items || [];
    $("#detail-title").textContent = `镜像历史 · ${ref}`;
    if (!items.length) {
      $("#detail-body").innerHTML = `<div class="muted">无历史层</div>`;
    } else {
      $("#detail-body").innerHTML = `<div class="table-wrap"><table>
        <thead><tr><th>ID</th><th>大小</th><th>创建命令</th></tr></thead>
        <tbody>${items
          .slice(0, 40)
          .map(
            (h) =>
              `<tr><td class="mono small">${escapeHtml(h.id || "")}</td><td>${fmtBytes(h.size)}</td><td class="mono small">${escapeHtml(h.created_by || "")}</td></tr>`
          )
          .join("")}</tbody></table></div>`;
    }
    $("#detail-dialog").showModal();
  } catch (e) {
    alert(e.message);
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

async function runDetectUpdates() {
  if (!requireLogin()) return;
  const onlyRunning = !!$("#upd-only-running")?.checked;
  const prog = $("#update-progress");
  if (prog) {
    prog.hidden = false;
    prog.textContent = "正在检测镜像更新（registry digest）…";
  }
  try {
    const r = await api("/api/ops/detect-updates", {
      method: "POST",
      body: JSON.stringify({ only_running: onlyRunning }),
    });
    state.updateItems = r.items || [];
    $("#update-summary").textContent = r.message || `可更新 ${r.update_available_count || 0}`;
    if (prog) prog.textContent = `完成 · 耗时 ${r.elapsed_sec ?? "?"}s`;
    renderUpdates();
    return r;
  } catch (e) {
    if (prog) prog.textContent = `检测失败：${e.message}`;
    alert(e.message);
    return null;
  }
}

async function runOneClickUpdate(ids) {
  if (!requireLogin()) return;
  const onlyRunning = !!$("#upd-only-running")?.checked;
  const n = ids ? ids.length : "全部可更新";
  if (!confirm(`一键安全更新 ${n} 个容器？\n将按管理源执行备份+拉取+重建（需接管才真正 recreate）。`)) return;
  const prog = $("#update-progress");
  if (prog) {
    prog.hidden = false;
    prog.textContent = "正在一键更新…";
  }
  try {
    const body = {
      only_available: true,
      only_running: onlyRunning,
    };
    if (ids && ids.length) body.container_ids = ids;
    const r = await api("/api/ops/one-click-update", {
      method: "POST",
      body: JSON.stringify(body),
    });
    alert(r.message || "完成");
    if (prog) prog.textContent = r.message || "完成";
    await runDetectUpdates();
    loadAll();
  } catch (e) {
    if (prog) prog.textContent = `更新失败：${e.message}`;
    alert(e.message);
  }
}

function selectedUpdateIds() {
  return Array.from(document.querySelectorAll(".upd-sel:checked"))
    .map((el) => el.dataset.id)
    .filter(Boolean);
}

// ── Event bindings ──
document.querySelectorAll("#main-tabs .nav-item").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

$("#btn-refresh").addEventListener("click", loadAll);
$("#logs-close").addEventListener("click", () => $("#logs-dialog").close());
$("#detail-close")?.addEventListener("click", () => $("#detail-dialog").close());

$("#btn-menu")?.addEventListener("click", () => setSidebarOpen(true));
$("#btn-sidebar-close")?.addEventListener("click", () => setSidebarOpen(false));
$("#sidebar-backdrop")?.addEventListener("click", () => setSidebarOpen(false));

["container-filter", "container-status-filter", "container-mgr-filter"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("input", renderContainers);
  if (el) el.addEventListener("change", renderContainers);
});
const cf = $("#compose-filter");
if (cf) cf.addEventListener("input", renderCompose);
const uf = $("#unraid-filter");
if (uf) uf.addEventListener("input", renderUnraid);
const imf = $("#image-filter");
if (imf) imf.addEventListener("input", renderImages);
const nf = $("#network-filter");
if (nf) nf.addEventListener("input", renderNetworks);
const vf = $("#volume-filter");
if (vf) vf.addEventListener("input", renderVolumes);

$("#ctr-check-all")?.addEventListener("change", (e) => {
  document.querySelectorAll(".ctr-sel").forEach((c) => {
    c.checked = e.target.checked;
    const id = c.dataset.id;
    if (e.target.checked) state.selected.add(id);
    else state.selected.delete(id);
  });
  updateBatchCount();
});

document.querySelectorAll("[data-batch]").forEach((btn) => {
  btn.addEventListener("click", () => doBatch(btn.dataset.batch));
});

$("#btn-detect-updates")?.addEventListener("click", runDetectUpdates);
$("#btn-one-click-update")?.addEventListener("click", () => {
  const ids = selectedUpdateIds();
  runOneClickUpdate(ids.length ? ids : null);
});
$("#btn-quick-detect")?.addEventListener("click", async () => {
  switchTab("updates");
  await runDetectUpdates();
});
$("#btn-quick-update-all")?.addEventListener("click", async () => {
  switchTab("updates");
  if (!state.updateItems.length) await runDetectUpdates();
  await runOneClickUpdate(null);
});
$("#btn-goto-containers")?.addEventListener("click", () => switchTab("containers"));
$("#btn-goto-system")?.addEventListener("click", () => switchTab("system"));
$("#btn-goto-docs")?.addEventListener("click", () => switchTab("docs"));

$("#upd-check-all")?.addEventListener("change", (e) => {
  document.querySelectorAll(".upd-sel:not(:disabled)").forEach((c) => {
    c.checked = e.target.checked;
  });
});

// prefs live preview
$("#pref-particles")?.addEventListener("change", (e) => {
  applyPrefsLocal({ ...state.prefs, particles: e.target.checked });
});
$("#pref-particles-count")?.addEventListener("input", (e) => {
  const n = Number(e.target.value) || 90;
  $("#pref-particles-count-val").textContent = String(n);
  applyPrefsLocal({ ...state.prefs, particles_count: n });
});
$("#pref-bg-theme")?.addEventListener("change", (e) => {
  applyPrefsLocal({ ...state.prefs, bg_theme: e.target.value });
});
$("#pref-density")?.addEventListener("change", (e) => {
  applyPrefsLocal({ ...state.prefs, card_density: e.target.value });
});
$("#pref-reduce-motion")?.addEventListener("change", (e) => {
  applyPrefsLocal({ ...state.prefs, reduce_motion: e.target.checked });
});

$("#btn-save-prefs")?.addEventListener("click", async () => {
  if (!requireLogin()) return;
  const body = {
    particles: !!$("#pref-particles")?.checked,
    particles_count: Number($("#pref-particles-count")?.value || 90),
    bg_theme: $("#pref-bg-theme")?.value || "cyber",
    card_density: $("#pref-density")?.value || "comfortable",
    reduce_motion: !!$("#pref-reduce-motion")?.checked,
  };
  try {
    const r = await api("/api/prefs", { method: "PUT", body: JSON.stringify(body) });
    applyPrefsLocal(r.prefs || body);
    $("#prefs-status").textContent = r.message || "已保存";
  } catch (e) {
    $("#prefs-status").textContent = e.message;
  }
});

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

// initial
setSidebarOpen(false); // ensure mobile backdrop never blocks first paint
applyPrefsLocal(state.prefs);
if (window.DockerOpsParticles) {
  window.DockerOpsParticles.applyPrefs(state.prefs);
}
updateComposeNavVisibility();
switchTab("overview");
loadAll();
setInterval(loadAll, 60000);
