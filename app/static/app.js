const state = {
  token: localStorage.getItem("dockerops_token") || "",
  username: localStorage.getItem("dockerops_user") || "",
  takeover: false,
  consoleEnabled: false,
  platform: "generic",
  version: "0.7.1",
  tab: "overview",
  endpoints: [],
  endpointId: localStorage.getItem("dockerops_endpoint") || "",
  endpoint: null,
  remote: null,
  remotePairTimer: null,
  remotePollTimer: null,
  managedLocked: false,
  /** Live log stream controller */
  logs: {
    id: null,
    name: "",
    es: null,
    abort: null,
    paused: false,
    follow: true,
  },
  /** Web terminal session */
  term: {
    id: null,
    name: "",
    ws: null,
    term: null,
    fit: null,
    ro: null,
  },
  needsSetup: false,
  usernames: [],
  passwordReset: null,
  authGateOpen: false,
  /** When true, dialog close handlers must not re-open the gate. */
  authUnlocking: false,
  containers: [],
  compose: [],
  unraid: [],
  updateItems: [],
  updateStatus: null, // cached GET /api/ops/update-status
  updateById: {},
  images: [],
  networks: [],
  volumes: [],
  selectedImages: new Set(),
  prefs: {
    particles: true,
    particles_count: 90,
    bg_theme: "cyber",
    card_density: "comfortable",
    reduce_motion: false,
  },
  systemSettings: null,
  selected: new Set(),
  /** In-flight load token to drop stale overlapping loadAll results. */
  loadSeq: 0,
  loading: false,
};

const PLATFORM_LABEL = {
  unraid: "Unraid系统",
  fnos: "飞牛系统",
  generic: "通用系统",
};

const TAB_TITLES = {
  overview: ["总览", "平台 · 引擎 · 健康 · 活动容器"],
  containers: ["容器", "生命周期 · 更新检测 · 终端 · 日志 · 安全更新"],
  compose: ["Compose", "项目发现 · 双方接管"],
  unraid: ["Unraid", "dockerMan 模板 · 非三方更新"],
  images: ["镜像", "标签 · 占用 · 拉取 · 清理"],
  networks: ["网络", "列表 · 创建 · 删除"],
  volumes: ["卷", "列表 · 创建 · 清理"],
  system: ["系统", "Engine · 磁盘占用 · 清理"],
  docs: ["说明日志", "使用说明 · 版本更新日志"],
  settings: ["系统设置", "远程模式 · Docker 端点 · 代理 · 自动更新 · 个性化"],
};

const ENDPOINT_STORAGE_KEY = "dockerops_endpoint";

const TAB_KEYS = Object.keys(TAB_TITLES);
const TAB_STORAGE_KEY = "dockerops_tab";

/** Resolve last tab: URL hash > localStorage > overview */
function readSavedTab() {
  try {
    const hash = (location.hash || "").replace(/^#\/?/, "").trim();
    if (hash === "updates") return "containers";
    if (hash && TAB_KEYS.includes(hash)) return hash;
  } catch (_) {
    /* ignore */
  }
  try {
    const saved = localStorage.getItem(TAB_STORAGE_KEY) || "";
    if (saved === "updates") return "containers";
    if (saved && TAB_KEYS.includes(saved)) return saved;
  } catch (_) {
    /* ignore */
  }
  return "overview";
}

function persistTab(name) {
  if (!name || !TAB_KEYS.includes(name)) return;
  try {
    localStorage.setItem(TAB_STORAGE_KEY, name);
  } catch (_) {
    /* ignore */
  }
  try {
    const want = `#${name}`;
    if (location.hash !== want) {
      // replaceState keeps history clean; hash still works for share/bookmark
      history.replaceState(null, "", want);
    }
  } catch (_) {
    try {
      location.hash = name;
    } catch (__) {
      /* ignore */
    }
  }
}

const $ = (sel) => document.querySelector(sel);

function authHeaders() {
  const h = { "Content-Type": "application/json" };
  if (state.token) h.Authorization = `Bearer ${state.token}`;
  if (state.endpointId) h["X-DockerOps-Endpoint"] = state.endpointId;
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
    el.textContent = "需要首次初始化";
    $("#btn-login").textContent = "初始化";
  } else if (state.token) {
    el.textContent = `已登录：${state.username || "user"}`;
    $("#btn-login").textContent = "退出";
  } else {
    el.textContent = "未登录";
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
  renderEndpointSelect();
  const cancel = $("#login-cancel");
  if (cancel) {
    // Allow cancel only when already logged-in session is open for re-auth; gate is forced otherwise
    cancel.hidden = !state.token;
  }
}

function applyEndpoints(data) {
  const items = data?.items || [];
  state.endpoints = items;
  const activeId = data?.active_id || state.endpointId || "";
  if (activeId) {
    state.endpointId = activeId;
    try {
      localStorage.setItem(ENDPOINT_STORAGE_KEY, activeId);
    } catch (_) {
      /* ignore */
    }
  }
  state.endpoint = items.find((e) => e.id === state.endpointId) || items[0] || null;
  if (state.endpoint && state.endpoint.id !== state.endpointId) {
    state.endpointId = state.endpoint.id;
  }
  renderEndpointSelect();
  renderEndpointsTable();
}

function renderEndpointSelect() {
  const sel = $("#endpoint-select");
  if (!sel) return;
  const items = state.endpoints || [];
  const cur = state.endpointId || "";
  const opts = items.length
    ? items
        .map((e) => {
          const remoteKind = e.kind === "remote_agent";
          const tag = e.is_local
            ? ""
            : remoteKind
              ? e.online
                ? " · 远程节点·在线"
                : " · 远程节点·离线"
              : " · 远程";
          return `<option value="${escapeHtml(e.id)}" ${e.id === cur ? "selected" : ""}>${escapeHtml(
            e.name || e.docker_host || e.id
          )}${tag}</option>`;
        })
        .join("")
    : `<option value="">本机</option>`;
  sel.innerHTML = opts;
  sel.disabled = !items.length;
  sel.title = state.endpoint
    ? `${state.endpoint.name} · ${state.endpoint.docker_host || state.endpoint.kind || ""}`
    : "Docker 端点";
}

function renderEndpointsTable() {
  const tbody = $("#endpoint-rows");
  if (!tbody) return;
  const items = state.endpoints || [];
  const active = state.endpointId || "";
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">暂无端点（启动后会自动创建「本机」）</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  items.forEach((e) => {
    const tr = document.createElement("tr");
    const caps = e.capabilities || {};
    const badge = e.is_active || e.id === active
      ? `<span class="pill running">活动</span>`
      : e.is_default
        ? `<span class="pill">默认</span>`
        : "";
    const isRemoteAgent = e.kind === "remote_agent";
    const kind = e.is_local ? "本地" : isRemoteAgent ? "远程节点" : "远程";
    const onlineBadge = isRemoteAgent
      ? e.online
        ? `<span class="pill running">在线</span>`
        : `<span class="pill">离线</span>`
      : badge;
    tr.innerHTML = `
      <td class="cell-text"><strong class="cell-clip">${escapeHtml(e.name || "")}</strong>
        <div class="muted small cell-clip">${escapeHtml(e.docker_host || "")}</div></td>
      <td class="col-status">${escapeHtml(kind)} ${e.tls_enabled ? "· TLS" : ""}</td>
      <td class="col-status">${onlineBadge} ${badge}</td>
      <td class="cell-text muted small">${
        isRemoteAgent
          ? "拨出 RPC · 容器/镜像/更新"
          : `${caps.compose ? "Compose " : ""}${caps.unraid ? "Unraid " : ""}${caps.console ? "终端" : ""}`
      }</td>
      <td class="col-actions actions"></td>
    `;
    const actions = tr.querySelector(".actions");
    const primary = [];
    if (!(e.is_active || e.id === active)) {
      primary.push({
        label: "切换",
        primary: true,
        fn: () => activateEndpoint(e.id),
      });
    }
    primary.push({ label: "测试", fn: () => testEndpoint(e.id) });
    const more = isRemoteAgent
      ? [
          {
            label: "断开节点",
            danger: true,
            fn: () => disconnectRemoteSession(String(e.id).replace(/^remote:/, "")),
          },
        ]
      : [
          {
            label: "设为默认",
            fn: () => updateEndpoint(e.id, { is_default: true }),
            disabled: !!e.is_default,
          },
          {
            label: "删除",
            danger: true,
            fn: () => deleteEndpoint(e.id),
            disabled: items.length <= 1,
          },
        ];
    fillActionGroup(actions, primary, more);
    tbody.appendChild(tr);
  });
}

async function loadEndpoints() {
  try {
    const data = await api("/api/endpoints");
    applyEndpoints(data);
    return data;
  } catch (e) {
    return null;
  }
}

async function activateEndpoint(id) {
  if (!id || id === state.endpointId) return;
  if (!requireLogin()) return;
  try {
    // Set header target immediately so subsequent calls hit the chosen node
    state.endpointId = id;
    try {
      localStorage.setItem(ENDPOINT_STORAGE_KEY, id);
    } catch (_) {
      /* ignore */
    }
    const r = await api(`/api/endpoints/${encodeURIComponent(id)}/activate`, {
      method: "POST",
      body: "{}",
    });
    state.endpointId = r.active_id || id;
    try {
      localStorage.setItem(ENDPOINT_STORAGE_KEY, state.endpointId);
    } catch (_) {
      /* ignore */
    }
    applyEndpoints({
      items: state.endpoints.map((x) => ({ ...x, is_active: x.id === state.endpointId })),
      active_id: state.endpointId,
    });
    await loadEndpoints();
    await loadAll({ banner: true });
  } catch (e) {
    alert(e.message || String(e));
  }
}

async function testEndpoint(id) {
  if (!requireLogin()) return;
  try {
    const r = await api(`/api/endpoints/${encodeURIComponent(id)}/test`, {
      method: "POST",
      body: "{}",
    });
    alert(r.message || (r.ok ? "连通正常" : "连通失败"));
  } catch (e) {
    alert(e.message || String(e));
  }
}

async function updateEndpoint(id, patch) {
  if (!requireLogin()) return;
  try {
    await api(`/api/endpoints/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    });
    await loadEndpoints();
  } catch (e) {
    alert(e.message || String(e));
  }
}

async function deleteEndpoint(id) {
  if (!requireLogin()) return;
  if (!confirm("确定删除该 Docker 端点？")) return;
  try {
    const r = await api(`/api/endpoints/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (r.active_id) {
      state.endpointId = r.active_id;
      try {
        localStorage.setItem(ENDPOINT_STORAGE_KEY, state.endpointId);
      } catch (_) {
        /* ignore */
      }
    }
    await loadEndpoints();
    await loadAll({ banner: true });
  } catch (e) {
    alert(e.message || String(e));
  }
}

async function createEndpointFromForm() {
  if (!requireLogin()) return;
  const name = ($("#ep-name")?.value || "").trim();
  const docker_host = ($("#ep-host")?.value || "").trim();
  const tls_enabled = !!$("#ep-tls")?.checked;
  const verify_tls = !!$("#ep-verify-tls")?.checked;
  const tls_ca = ($("#ep-tls-ca")?.value || "").trim();
  const tls_cert = ($("#ep-tls-cert")?.value || "").trim();
  const tls_key = ($("#ep-tls-key")?.value || "").trim();
  const notes = ($("#ep-notes")?.value || "").trim();
  const status = $("#ep-form-status");
  if (!name || !docker_host) {
    if (status) status.textContent = "请填写名称与 Docker Host";
    return;
  }
  try {
    if (status) status.textContent = "创建中…";
    await api("/api/endpoints", {
      method: "POST",
      body: JSON.stringify({
        name,
        docker_host,
        tls_enabled,
        verify_tls,
        tls_ca,
        tls_cert,
        tls_key,
        notes,
      }),
    });
    if (status) status.textContent = "已创建";
    ["#ep-name", "#ep-host", "#ep-tls-ca", "#ep-tls-cert", "#ep-tls-key", "#ep-notes"].forEach(
      (s) => {
        const el = $(s);
        if (el) el.value = "";
      }
    );
    if ($("#ep-tls")) $("#ep-tls").checked = false;
    await loadEndpoints();
  } catch (e) {
    if (status) status.textContent = e.message || String(e);
  }
}

function lockAuthGate(kind) {
  state.authGateOpen = true;
  document.body.classList.add("auth-locked");
  const setup = $("#setup-dialog");
  const login = $("#login-dialog");
  // Suppress close-handler re-open while switching dialogs
  state.authUnlocking = true;
  try {
    if (kind === "setup") {
      if (login?.open) login.close();
      if (setup && !setup.open) setup.showModal();
      const err = $("#setup-error");
      if (err) err.hidden = true;
    } else {
      if (setup?.open) setup.close();
      if (login && !login.open) login.showModal();
      const err = $("#login-error");
      if (err) err.hidden = true;
      fillForgotPasswordUI();
    }
  } finally {
    setTimeout(() => {
      state.authUnlocking = false;
    }, 30);
  }
}

function unlockAuthGate() {
  state.authUnlocking = true;
  state.authGateOpen = false;
  document.body.classList.remove("auth-locked");
  const setup = $("#setup-dialog");
  const login = $("#login-dialog");
  try {
    if (setup?.open) setup.close();
    if (login?.open) login.close();
  } finally {
    // allow next forced gate if needed
    setTimeout(() => {
      state.authUnlocking = false;
    }, 50);
  }
}

function fillForgotPasswordUI() {
  const pr = state.passwordReset || {};
  const names = state.usernames || pr.usernames_hint || [];
  const hint = $("#login-users-hint");
  if (hint) {
    if (names.length) {
      hint.hidden = false;
      hint.textContent = `已有账号：${names.join("、")}`;
      const input = document.querySelector('#login-form input[name="username"]');
      if (input && !input.value) input.value = names[0];
    } else {
      hint.hidden = true;
    }
  }
  const summary = $("#forgot-summary");
  if (summary) {
    summary.textContent = pr.summary || "Web 不提供自助重置。请在 NAS 主机终端执行：";
  }
  const cmds = pr.commands || [];
  const primary = cmds[0]?.cmd
    || 'docker exec -it DockerOps python -m tools.reset_password --username YourUser --password "新密码至少6位"';
  const extra = cmds
    .slice(1)
    .map((c) => `# ${c.label}\n${c.cmd}`)
    .join("\n\n");
  const cmdEl = $("#forgot-cmd");
  if (cmdEl) {
    cmdEl.textContent = extra ? `${primary}\n\n${extra}` : primary;
  }
  const notes = $("#forgot-notes");
  if (notes) {
    const lines = pr.notes || [];
    notes.textContent = lines.length ? lines.map((n) => `• ${n}`).join("\n") : "";
  }
}

function enforceAuthGate() {
  if (state.needsSetup) {
    lockAuthGate("setup");
    return "setup";
  }
  if (!state.token) {
    lockAuthGate("login");
    return "login";
  }
  unlockAuthGate();
  return "ok";
}

function requireLogin() {
  if (state.needsSetup) {
    lockAuthGate("setup");
    return false;
  }
  if (!state.token) {
    lockAuthGate("login");
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

function actionBtn(text, fn, { danger = false, disabled = false, primary = false } = {}) {
  const b = document.createElement("button");
  b.className = `btn small${danger ? " danger" : ""}${primary ? " primary" : ""}`;
  b.textContent = text;
  b.disabled = disabled;
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    fn(e);
  });
  return b;
}

function isNarrowScreen() {
  try {
    return window.matchMedia && window.matchMedia("(max-width: 960px)").matches;
  } catch (_) {
    return false;
  }
}

/** Approx width of 「更多」 summary button (px) when measuring overflow. */
const ACTION_MORE_BTN_W = 52;
const ACTION_GAP_PX = 4;

/**
 * Preferred max visible buttons only as a hard ceiling (safety); real limit is
 * available cell width — overflow auto-folds into 「更多」.
 */
function actionMaxPrimary(defaultN = 99) {
  return isNarrowScreen() ? Math.min(2, defaultN) : defaultN;
}

/**
 * Render row actions: show as many as fit in the cell; only overflow goes to 「更多」.
 * Order = primary then more. Dedupes by label.
 * @param {HTMLElement} host
 * @param {{label:string, fn:Function, danger?:boolean, disabled?:boolean, primary?:boolean}[]} primary
 * @param {{label:string, fn:Function, danger?:boolean, disabled?:boolean}[]} [more]
 * @param {{maxPrimary?:number}} [opts]
 */
function fillActionGroup(host, primary, more = [], opts = {}) {
  host.classList.add("actions", "col-actions");
  // disconnect previous observer if re-rendering
  if (host._actionRo) {
    try {
      host._actionRo.disconnect();
    } catch (_) {
      /* ignore */
    }
    host._actionRo = null;
  }
  host.innerHTML = "";

  const hardMax = opts.maxPrimary ?? actionMaxPrimary(99);
  const seen = new Set();
  const all = [];
  for (const a of [...(primary || []), ...(more || [])]) {
    if (!a || !a.label || seen.has(a.label)) continue;
    seen.add(a.label);
    all.push(a);
  }
  if (!all.length) return;

  const group = document.createElement("div");
  group.className = "action-group";
  host.appendChild(group);

  const measureWidths = () => {
    const probe = document.createElement("div");
    probe.className = "action-group action-group-measure";
    probe.setAttribute("aria-hidden", "true");
    probe.style.cssText =
      "position:absolute;left:-9999px;top:0;visibility:hidden;display:inline-flex;align-items:center;gap:0.22rem;white-space:nowrap;pointer-events:none;";
    document.body.appendChild(probe);
    const widths = all.map((a) => {
      const b = actionBtn(a.label, () => {}, {
        danger: !!a.danger,
        disabled: !!a.disabled,
        primary: !!a.primary,
      });
      probe.appendChild(b);
      return b.getBoundingClientRect().width || b.offsetWidth || 48;
    });
    // also measure 更多
    const moreProbe = document.createElement("button");
    moreProbe.className = "btn small";
    moreProbe.textContent = "更多";
    probe.appendChild(moreProbe);
    const moreW = moreProbe.getBoundingClientRect().width || ACTION_MORE_BTN_W;
    document.body.removeChild(probe);
    return { widths, moreW };
  };

  const fitCount = (avail, widths, moreW) => {
    if (!widths.length) return 0;
    const gap = ACTION_GAP_PX;
    const sumAll = widths.reduce((s, w, i) => s + w + (i ? gap : 0), 0);
    const ceiling = Math.min(hardMax, widths.length);
    if (sumAll <= avail && widths.length <= hardMax) return widths.length;
    // leave room for 「更多」 when not all fit
    let used = 0;
    let n = 0;
    for (let i = 0; i < ceiling; i++) {
      const w = widths[i] + (i ? gap : 0);
      const remaining = widths.length - (i + 1);
      const needMore = remaining > 0 || i + 1 < widths.length ? moreW + gap : 0;
      // if taking this button still leaves room for 更多 (if anything left) or is last
      const isLastPossible = i + 1 === widths.length;
      if (isLastPossible) {
        if (used + w <= avail) n = i + 1;
        break;
      }
      if (used + w + needMore <= avail + 0.5) {
        used += w;
        n = i + 1;
      } else {
        break;
      }
    }
    return Math.max(n, Math.min(1, ceiling)); // always show at least 1 if any
  };

  const render = () => {
    const avail = Math.max(0, host.clientWidth || host.getBoundingClientRect().width || 0);
    // if host not laid out yet, show all up to hardMax then reflow
    const { widths, moreW } = measureWidths();
    let n =
      avail > 8
        ? fitCount(avail, widths, moreW)
        : Math.min(hardMax, widths.length);
    n = Math.min(n, hardMax, all.length);
    if (n < 1 && all.length) n = 1;

    // if everything fits without 更多, use full list
    const sumAll = widths.reduce((s, w, i) => s + w + (i ? ACTION_GAP_PX : 0), 0);
    if (sumAll <= avail && all.length <= hardMax) n = all.length;

    const shown = all.slice(0, n);
    const extra = all.slice(n);
    group.innerHTML = "";
    shown.forEach((a) => {
      group.appendChild(
        actionBtn(a.label, a.fn, {
          danger: !!a.danger,
          disabled: !!a.disabled,
          primary: !!a.primary,
        })
      );
    });
    if (extra.length) {
      const det = document.createElement("details");
      det.className = "action-menu";
      const sum = document.createElement("summary");
      sum.className = "btn small";
      sum.textContent = "更多";
      sum.title = `更多操作（${extra.length}）`;
      det.appendChild(sum);
      const panel = document.createElement("div");
      panel.className = "action-menu-panel";
      extra.forEach((a) => {
        panel.appendChild(
          actionBtn(
            a.label,
            (e) => {
              det.open = false;
              a.fn(e);
            },
            { danger: !!a.danger, disabled: !!a.disabled }
          )
        );
      });
      det.appendChild(panel);
      det.addEventListener("toggle", () => {
        if (!det.open) return;
        document.querySelectorAll("details.action-menu[open]").forEach((d) => {
          if (d !== det) d.open = false;
        });
      });
      group.appendChild(det);
    }
  };

  render();
  // second pass after layout (table col may still be 0 on first paint)
  requestAnimationFrame(() => {
    render();
    requestAnimationFrame(render);
  });
  if (typeof ResizeObserver !== "undefined") {
    let t = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(t);
      t = requestAnimationFrame(render);
    });
    ro.observe(host);
    host._actionRo = ro;
  }
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

function switchTab(name, opts = {}) {
  // updates tab merged into containers
  if (name === "updates") name = "containers";
  // guard: cannot open compose when hidden
  if (name === "compose" && !(state.compose && state.compose.length)) {
    name = "overview";
  }
  if (!TAB_KEYS.includes(name)) name = "overview";
  state.tab = name;
  if (!opts.skipPersist) persistTab(name);
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
  if (name === "settings") {
    applyPrefsLocal(state.prefs);
    loadSystemSettings();
  }
  if (name === "containers") {
    if (!(state.updateItems && state.updateItems.length) && state.updateStatus) {
      applyUpdateStatus(state.updateStatus);
    }
  }
  setSidebarOpen(false);
}

function fmtWhen(ts) {
  if (ts == null || ts === "") return "—";
  let d;
  if (typeof ts === "number") d = new Date(ts * (ts < 1e12 ? 1000 : 1));
  else d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  return d.toLocaleString();
}

function containerUpdateInfo(c) {
  const by = state.updateById || {};
  const id = c.id || "";
  const name = String(c.name || "").replace(/^\//, "");
  return (
    by[id] ||
    by[String(id).slice(0, 12)] ||
    by[`name:${name}`] ||
    null
  );
}

function applyUpdateStatus(data) {
  if (!data) return;
  state.updateStatus = data;
  state.updateById = data.by_id || {};
  if (Array.isArray(data.items) && data.items.length) {
    state.updateItems = data.items;
  }
  const n = data.update_available_count ?? data.summary?.update_available_count ?? 0;
  const el = $("#stat-updates");
  if (el) el.textContent = String(n);
  const chip = $("#stat-updates-chip");
  if (chip) chip.classList.toggle("has-updates", Number(n) > 0);

  const checked = data.checked_at ? fmtWhen(data.checked_at) : null;
  const auto = data.auto || {};
  const autoOn = auto.auto_check_enabled !== false;
  const sum = $("#update-summary");
  if (sum) {
    if (data.cached && checked) {
      sum.textContent =
        (data.message || data.summary?.message || `可更新 ${n}`) +
        ` · 上次检测 ${checked}` +
        (autoOn ? " · 自动检测已开启" : " · 自动检测已关闭");
    } else if (!data.cached) {
      sum.textContent = autoOn
        ? "尚未检测 · 后台将自动扫描（也可点「立即检测」）"
        : "尚未检测 · 请点击「立即检测」";
    }
  }
  const hint = $("#update-auto-hint");
  if (hint && auto) {
    const hours = auto.auto_check_interval_hours || 6;
    hint.textContent = autoOn
      ? `后台每 ${hours} 小时自动比对 registry digest；容器列表直接显示「有更新」。`
      : "后台自动检测已关闭；可在系统设置开启，或手动「立即检测」。";
  }
  if (state.tab === "containers" || state.tab === "overview") renderContainers();
}

async function checkSetup() {
  try {
    const st = await api("/api/auth/status");
    state.needsSetup = !!st.needs_setup;
    state.usernames = Array.isArray(st.usernames) ? st.usernames : [];
    state.passwordReset = st.password_reset || null;
    if (state.needsSetup) {
      state.token = "";
      state.username = "";
      localStorage.removeItem("dockerops_token");
      localStorage.removeItem("dockerops_user");
    }
    setAuthUI();
    enforceAuthGate();
    return st;
  } catch (e) {
    // If status fails, still force login rather than open console anonymously
    state.needsSetup = false;
    setAuthUI();
    if (!state.token) lockAuthGate("login");
    return null;
  }
}

async function validateSession() {
  if (!state.token || state.needsSetup) return false;
  try {
    const me = await api("/api/auth/me");
    if (me && me.username) state.username = me.username;
    return true;
  } catch (e) {
    const msg = String(e.message || "");
    if (/401|未授权|登录|Token|token|Unauthorized|需要登录/i.test(msg)) {
      state.token = "";
      state.username = "";
      localStorage.removeItem("dockerops_token");
      localStorage.removeItem("dockerops_user");
      setAuthUI();
      lockAuthGate("login");
      return false;
    }
    // Network blip: keep token
    return true;
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
  const updf = ($("#container-update-filter")?.value || "").trim();
  let items = state.containers || [];
  items = items.filter((c) => {
    if (stf && (c.status || "").toLowerCase() !== stf) return false;
    if (mgr && (c.manager || "") !== mgr) return false;
    if (updf) {
      const upd = containerUpdateInfo(c);
      const avail = !!(upd && upd.update_available);
      if (updf === "available" && !avail) return false;
      if (updf === "latest" && avail) return false;
    }
    if (!q) return true;
    const blob = [c.name, c.id, c.image, c.manager, c.compose_project, c.label].join(" ");
    return matchFilter(blob, q);
  });
  const countEl = $("#container-count");
  if (countEl) countEl.textContent = `显示 ${items.length} / 共 ${state.containers.length} 个`;
  const tbody = $("#container-rows");
  if (!tbody) return;
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
    const upd = containerUpdateInfo(c);
    let detectHtml = `<span class="muted">—</span>`;
    if (upd) {
      if (upd.update_available) {
        detectHtml = `<span class="pill update-yes" title="${escapeHtml(upd.message || "有更新")}">可更新</span>`;
      } else if (upd.check_ok) {
        detectHtml = `<span class="pill update-no" title="${escapeHtml(upd.message || "最新")}">最新</span>`;
      } else {
        detectHtml = `<span class="pill update-err" title="${escapeHtml(upd.message || "检测异常")}">异常</span>`;
      }
    }
    const shortId = String(c.id || "").replace(/^sha256:/, "").slice(0, 12);
    tr.innerHTML = `
      <td class="col-check"><input type="checkbox" class="ctr-sel" data-id="${escapeHtml(id)}" ${checked} /></td>
      <td class="cell-text"><strong class="cell-clip" title="${escapeHtml(c.name || c.id || "")}">${escapeHtml(c.name || c.id)}</strong><div class="muted mono cell-clip" title="${escapeHtml(c.id || "")}">${escapeHtml(shortId)}</div></td>
      <td class="col-mgr">${managerPill(c.manager, c.label)}${mgrExtra}</td>
      <td class="cell-text mono"><span class="cell-clip" title="${escapeHtml(c.image || "")}">${escapeHtml(c.image || "")}</span></td>
      <td class="col-status"><span class="${pillClass(c.status)}">${escapeHtml(c.status || "-")}</span></td>
      <td class="col-health"><span class="${pillClass(c.health || "none")}">${escapeHtml(c.health || "-")}</span></td>
      <td class="col-detect"><div class="detect-cell">${detectHtml}</div></td>
      <td class="col-num">${c.restart_count ?? 0}</td>
      <td class="col-actions actions"></td>
    `;
    const actions = tr.querySelector(".actions");
    const running = (c.status || "").toLowerCase() === "running";
    const paused = (c.status || "").toLowerCase() === "paused";
    // Preferred order — width-fit decides what stays out vs 「更多」
    const primary = [];
    if (!running && !paused) primary.push({ label: "启动", fn: () => doLife(id, "start") });
    else if (paused) primary.push({ label: "恢复", fn: () => doLife(id, "unpause") });
    else primary.push({ label: "停止", fn: () => doLife(id, "stop") });
    if (running) primary.push({ label: "重启", fn: () => doLife(id, "restart") });
    primary.push({ label: "日志", fn: () => showLogs(id, c.name) });
    if (upd && upd.update_available) {
      primary.push({ label: "更新", fn: () => doUpdate(id), primary: true });
    }
    if (running && state.consoleEnabled) {
      primary.push({ label: "终端", fn: () => openConsole(id, c.name) });
    }
    primary.push({ label: "详情", fn: () => showDetail(id, c.name) });
    const more = [
      running ? { label: "暂停", fn: () => doLife(id, "pause") } : null,
      running ? { label: "强制停止", fn: () => doLife(id, "kill"), danger: true } : null,
      { label: "重命名", fn: () => doRename(id, c.name) },
      { label: "备份", fn: () => doBackup(id) },
      !(upd && upd.update_available) ? { label: "安全更新", fn: () => doUpdate(id) } : null,
      { label: "回滚指引", fn: () => doRollback(id) },
      c.manager === "third_party"
        ? { label: "Adopt", fn: () => doAdopt(id), disabled: !state.takeover }
        : null,
      { label: "删除", fn: () => doRemove(id), danger: true, disabled: !state.takeover },
    ];
    fillActionGroup(actions, primary, more);
    tbody.appendChild(tr);
  });
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted">无匹配容器</td></tr>`;
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
      <td class="cell-text"><strong class="cell-clip" title="${escapeHtml(p.name || "")}">${escapeHtml(p.name)}</strong><div class="muted cell-clip">${escapeHtml(p.source || "")}</div></td>
      <td class="cell-text"><span class="cell-clip" title="${escapeHtml((p.services || []).join(", "))}">${escapeHtml((p.services || []).join(", ") || "-")}</span></td>
      <td class="col-num">${p.running ?? 0}/${p.total ?? 0}</td>
      <td class="cell-text mono small"><span class="cell-clip" title="${escapeHtml(p.working_dir || "")}">${escapeHtml(p.working_dir || "-")}</span></td>
      <td class="col-actions actions"></td>
    `;
    fillActionGroup(
      tr.querySelector(".actions"),
      [
        { label: "更新", fn: () => doComposeUpdate(p.name) },
        { label: "Up", fn: () => doComposeUp(p.name), disabled: !state.takeover },
        { label: "备份", fn: () => doComposeBackup(p.name) },
        { label: "Down", fn: () => doComposeDown(p.name), danger: true, disabled: !state.takeover },
      ],
      []
    );
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
      <td class="cell-text"><strong class="cell-clip" title="${escapeHtml(t.name || t.file || "")}">${escapeHtml(t.name || t.file || "")}</strong><div class="muted mono cell-clip">${escapeHtml(t.file || "")}</div></td>
      <td class="cell-text mono small"><span class="cell-clip" title="${escapeHtml(t.repository || "")}">${escapeHtml(t.repository || "-")}</span></td>
      <td class="col-status">${escapeHtml(t.network || "-")}${t.privileged ? ' <span class="pill">privileged</span>' : ""}</td>
      <td class="col-status"><span class="${pillClass(st)}">${escapeHtml(st)}</span></td>
      <td class="col-actions actions"></td>
    `;
    const n = t.name || "";
    fillActionGroup(
      tr.querySelector(".actions"),
      [
        { label: "模板更新", fn: () => doUnraidUpdate(n) },
        { label: "备份", fn: () => doUnraidBackup(n) },
      ],
      []
    );
    ubody.appendChild(tr);
  });
  if (!items.length) {
    ubody.innerHTML = `<tr><td colspan="5" class="muted">无匹配模板</td></tr>`;
  }
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
  const updN =
    state.updateStatus?.update_available_count ??
    state.updateStatus?.summary?.update_available_count ??
    0;
  setTxt("stat-updates", updN);
  const chip = $("#stat-updates-chip");
  if (chip) chip.classList.toggle("has-updates", Number(updN) > 0);

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
      kv("控制台", (platform.console_enabled || state.consoleEnabled) ? "开启" : "关闭"),
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

function setLoading(on, msg) {
  state.loading = !!on;
  const bar = $("#load-banner");
  if (!bar) return;
  if (on) {
    bar.hidden = false;
    bar.textContent = msg || "加载中…";
  } else {
    bar.hidden = true;
    bar.textContent = "";
  }
  const btn = $("#btn-refresh");
  if (btn) btn.disabled = !!on;
}

async function loadActivityDeferred(seq) {
  const meta = $("#activity-meta");
  const list = $("#activity-list");
  if (meta) meta.textContent = "采样中…";
  if (list && !list.children.length) {
    list.innerHTML = `<div class="muted small">活动容器资源采样中（不阻塞总览）…</div>`;
  }
  try {
    // fewer containers = faster; still useful overview
    const activity = await api("/api/activity?limit=6").catch(() => ({ items: [] }));
    if (seq !== state.loadSeq) return;
    renderActivity(activity.items || []);
    if (meta) meta.textContent = "CPU / 内存";
  } catch (e) {
    if (seq !== state.loadSeq) return;
    if (meta) meta.textContent = "采样失败";
    if (list) list.innerHTML = `<div class="muted small">活动采样失败：${escapeHtml(e.message || "")}</div>`;
  }
}

/**
 * Load console data.
 * Portainer-like: with a stored token, boot quietly in background — never flash
 * a full-page "加载控制台" gate. Only show banner for explicit user refresh
 * when opts.banner is true (default false when session already known).
 */
async function loadAll(opts = {}) {
  const seq = ++state.loadSeq;
  const showBanner = opts.banner === true;
  setAuthUI();
  try {
    await checkSetup();
    if (seq !== state.loadSeq) return;
    if (state.needsSetup) {
      setLoading(false);
      return; // forced setup — do not load console data yet
    }
    if (!state.token) {
      setLoading(false);
      return; // forced login — wait for credentials
    }
    // Quiet path when we already have a token (normal page open / soft refresh)
    if (showBanner) setLoading(true, "刷新数据…");
    else setLoading(false);

    const okSession = await validateSession();
    if (seq !== state.loadSeq) return;
    if (!okSession) {
      setLoading(false);
      return;
    }

    // Fast path only — never wait on container stats (activity is deferred)
    const [doctor, containers, ops, health, summary, compose, unraid, platform, events, prefs, sysPack, updStatus, endpoints] =
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
        api("/api/system/info").catch(() => ({ info: null })),
        api("/api/ops/update-status").catch(() => null),
        api("/api/endpoints").catch(() => null),
      ]);

    if (seq !== state.loadSeq) return;

    if (endpoints) applyEndpoints(endpoints);
    else if (health.endpoint?.id) {
      state.endpointId = health.endpoint.id;
      state.endpoint = health.endpoint;
      renderEndpointSelect();
    }

    state.takeover = !!summary.takeover_enabled;
    state.consoleEnabled = !!(
      summary.console_enabled ||
      platform.console_enabled ||
      (platform.capabilities && platform.capabilities.console)
    );
    state.platform = platform.platform || summary.platform || health.platform || "generic";
    state.version = health.version || state.version;
    if (health.remote) {
      const prev = state.remote || {};
      applyRemoteSettingsUI({
        settings: { ...(prev.settings || {}), ...health.remote },
        status: { ...(prev.status || {}), ...health.remote },
        runtime: prev.runtime || {},
      });
    } else if (typeof health.managed_locked === "boolean") {
      state.managedLocked = !!health.managed_locked;
      applyManagedLockUI(state.managedLocked, health.remote || {}, state.remote?.settings || {}, state.remote?.runtime || {});
    }
    state.containers = containers.items || [];
    state.compose = compose.items || [];
    state.unraid = unraid.items || [];
    updateComposeNavVisibility();
    if (prefs.prefs) applyPrefsLocal(prefs.prefs);
    if (updStatus) applyUpdateStatus(updStatus);
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
    if (advice) {
      advice.innerHTML = "";
      const tips = [
        ...(doctor.advice || []),
        ...(summary.hints || []),
        ...(platform.mount_hints || []).slice(0, 3),
      ];
      if (!tips.length) {
        const li = document.createElement("li");
        li.className = "muted";
        li.textContent = "暂无诊断建议，系统状态良好时可为空。";
        advice.appendChild(li);
      } else {
        tips.forEach((a) => {
          const li = document.createElement("li");
          li.textContent = a;
          advice.appendChild(li);
        });
      }
    }

    renderOverviewCards({
      doctor,
      health,
      platform,
      summary,
      sysInfo: sysPack.info || sysPack || null,
    });

    // Activity stats are slow (docker stats ~1s/container) — never block first paint
    loadActivityDeferred(seq);

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
        <td class="col-date mono small">${escapeHtml(fmtTime(r.created_at))}</td>
        <td class="cell-text"><span class="cell-clip" title="${escapeHtml(r.action || "")}">${escapeHtml(r.action || "")}</span></td>
        <td class="cell-text"><span class="cell-clip" title="${escapeHtml(r.target || "-")}">${escapeHtml(r.target || "-")}</span></td>
        <td class="col-status">${escapeHtml(r.status || "")}</td>
        <td class="cell-text"><span class="cell-clip">${escapeHtml(r.actor || "-")}</span></td>
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
    if (seq === state.loadSeq) setLoading(false);
  } catch (e) {
    if (seq !== state.loadSeq) return;
    setLoading(false);
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

function imageRef(img) {
  return (img.tags && img.tags[0]) || img.full_id || img.id;
}

function updateImageSelCount() {
  const el = $("#image-sel-count");
  const n = state.selectedImages.size;
  if (el) el.textContent = `已选 ${n}`;
  const btn = $("#btn-image-batch-remove");
  if (btn) btn.disabled = n === 0 || !state.takeover;
}

function splitImageRef(tag) {
  if (!tag) return { repo: "", tag: "" };
  const s = String(tag);
  // split only the final :tag; keep host:port/name intact
  const i = s.lastIndexOf(":");
  if (i <= 0) return { repo: s, tag: "" };
  const maybeTag = s.slice(i + 1);
  if (!maybeTag || maybeTag.includes("/")) return { repo: s, tag: "" };
  // pure numeric after : with no slash is likely host:port
  if (/^\d+$/.test(maybeTag) && !s.slice(0, i).includes("/")) {
    return { repo: s, tag: "" };
  }
  return { repo: s.slice(0, i), tag: maybeTag };
}

function fmtImageCreated(created) {
  if (!created) return "—";
  const s = String(created).trim();
  // Docker may return nanosecond ISO: 2026-05-28T07:49:24.130421496Z — browsers reject >3 fractional digits
  const normalized = s
    .replace(/(\.\d{3})\d+/, "$1")
    .replace(/([+-]\d{2}:\d{2})$/, (m) => m)
    .replace(/\+08:00$/, "Z"); // keep parseable; display uses local via Date
  let d = new Date(normalized);
  if (Number.isNaN(d.getTime())) {
    // strip fractional seconds entirely
    d = new Date(s.replace(/\.\d+/, ""));
  }
  if (Number.isNaN(d.getTime())) {
    // last resort: YYYY-MM-DD HH:MM from string
    const m = s.match(/(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2})/);
    return m ? `${m[1]} ${m[2]}` : s.slice(0, 16);
  }
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderImages() {
  const q = ($("#image-filter")?.value || "").trim();
  const vf = ($("#image-view-filter")?.value || "all").trim();
  let items = [...(state.images || [])];
  if (vf === "dangling") items = items.filter((i) => i.dangling || !(i.tags && i.tags.length));
  if (vf === "unused") items = items.filter((i) => !i.used_by);
  if (q) items = items.filter((i) => matchFilter([i.label, ...(i.tags || []), i.id, i.full_id].join(" "), q));
  // Portainer-like sort: used first, then by label
  items.sort((a, b) => {
    const ua = Number(a.used_by) || 0;
    const ub = Number(b.used_by) || 0;
    if (ua !== ub) return ub - ua;
    const da = a.dangling ? 1 : 0;
    const db = b.dangling ? 1 : 0;
    if (da !== db) return da - db;
    return String(a.label || "").localeCompare(String(b.label || ""));
  });
  const countEl = $("#image-count");
  if (countEl) countEl.textContent = `显示 ${items.length} / 共 ${state.images.length} 个`;
  const body = $("#image-rows");
  if (!body) return;
  body.innerHTML = "";
  items.forEach((img) => {
    const tr = document.createElement("tr");
    const tags = img.tags || [];
    const dangling = img.dangling || !tags.length;
    const ref = imageRef(img);
    const selKey = img.full_id || img.id || ref;
    const checked = state.selectedImages.has(selKey) ? "checked" : "";
    const usedList = img.used_by_containers || [];
    const used = Number(img.used_by) || usedList.length || 0;
    const names = usedList.map((c) => c.name || c.id).filter(Boolean);
    const usedTitle = names.length ? names.join(", ") : used ? `${used} 个容器` : "未使用";
    const usedText = used > 0
      ? `${used}${names.length ? " · " + names.slice(0, 2).join(", ") + (names.length > 2 ? "…" : "") : ""}`
      : "—";
    const usedHtml = used > 0
      ? `<span class="used-by-cell" title="${escapeHtml(usedTitle)}">${escapeHtml(usedText)}</span>`
      : `<span class="badge-unused">—</span>`;
    const shortId = String(img.id || "").replace(/^sha256:/, "").slice(0, 12);
    // Prefer server-formatted created_fmt (handles Docker nanosecond ISO)
    const createdLabel = img.created_fmt || fmtImageCreated(img.created);
    // Single-line name: repo:tag (no multi-line stack that collapses under narrow cols)
    let nameOneLine;
    if (dangling) {
      nameOneLine = `<span class="badge-dangling" title="dangling">&lt;none&gt;:&lt;none&gt;</span>`;
    } else {
      const main = (tags[0] || img.label || "");
      const extra = tags.length > 1 ? ` (+${tags.length - 1})` : "";
      nameOneLine = `<span class="cell-clip" title="${escapeHtml(tags.join("\n"))}">${escapeHtml(main)}${escapeHtml(extra)}</span>`;
    }
    tr.innerHTML = `
      <td class="col-check"><input type="checkbox" class="img-sel" data-id="${escapeHtml(selKey)}" data-ref="${escapeHtml(ref)}" ${checked} /></td>
      <td class="cell-text">${nameOneLine}</td>
      <td class="col-id mono small" title="${escapeHtml(img.full_id || img.id || "")}">${escapeHtml(shortId)}</td>
      <td class="col-size">${fmtBytes(img.size)}</td>
      <td class="col-date muted small">${escapeHtml(createdLabel)}</td>
      <td class="col-used">${usedHtml}</td>
      <td class="col-actions actions"></td>
    `;
    const actions = tr.querySelector(".actions");
    const g = document.createElement("div");
    g.className = "action-group";
    g.appendChild(actionBtn("详情", () => showImageDetail(img)));
    g.appendChild(actionBtn("历史", () => showImageHistory(ref)));
    g.appendChild(
      actionBtn("删除", () => doImageRemove(ref), { danger: true, disabled: !state.takeover })
    );
    actions.appendChild(g);
    tr.querySelector(".used-by-cell")?.addEventListener("click", () => showImageDetail(img));
    tr.querySelector(".cell-text")?.addEventListener("click", (e) => {
      if (e.target.closest("input,button,a")) return;
      showImageDetail(img);
    });
    body.appendChild(tr);
  });
  if (!items.length) body.innerHTML = `<tr><td colspan="7" class="muted">无镜像</td></tr>`;
  body.querySelectorAll(".img-sel").forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = cb.dataset.id;
      if (cb.checked) state.selectedImages.add(id);
      else state.selectedImages.delete(id);
      updateImageSelCount();
    });
  });
  updateImageSelCount();
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
      <td class="cell-text"><strong class="cell-clip">${escapeHtml(n.name)}</strong><div class="muted mono cell-clip">${escapeHtml((n.id || "").slice(0, 12))}</div></td>
      <td class="col-status">${escapeHtml(n.driver || "-")}</td>
      <td class="cell-text mono small"><span class="cell-clip">${escapeHtml(n.subnet || "-")}</span></td>
      <td class="col-num">${n.containers ?? 0}</td>
      <td class="col-actions actions"></td>
    `;
    const protectedNet = ["bridge", "host", "none"].includes(n.name);
    fillActionGroup(
      tr.querySelector(".actions"),
      [
        {
          label: "删除",
          fn: () => doNetRemove(n.id || n.name),
          danger: true,
          disabled: !state.takeover || protectedNet,
        },
      ],
      []
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
      <td class="cell-text"><strong class="cell-clip" title="${escapeHtml(v.name || "")}">${escapeHtml(v.name)}</strong></td>
      <td class="col-status">${escapeHtml(v.driver || "-")}</td>
      <td class="cell-text mono small"><span class="cell-clip" title="${escapeHtml(v.mountpoint || "")}">${escapeHtml(v.mountpoint || "-")}</span></td>
      <td class="col-actions actions"></td>
    `;
    fillActionGroup(
      tr.querySelector(".actions"),
      [{ label: "删除", fn: () => doVolRemove(v.name), danger: true, disabled: !state.takeover }],
      []
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

function fmtPorts(ports) {
  if (!ports) return "—";
  if (typeof ports === "string") return ports;
  const lines = [];
  try {
    Object.entries(ports).forEach(([k, v]) => {
      if (!v || !v.length) {
        lines.push(`${k} → (未发布)`);
        return;
      }
      v.forEach((p) => {
        lines.push(`${p.HostIp || "0.0.0.0"}:${p.HostPort} → ${k}`);
      });
    });
  } catch (_) {
    return JSON.stringify(ports);
  }
  return lines.length ? lines.join("\n") : "—";
}

function sectionHtml(title, inner) {
  return `<div class="detail-section"><h4>${escapeHtml(title)}</h4>${inner}</div>`;
}

async function showDetail(id, name) {
  try {
    const [detail, stats] = await Promise.all([
      api(`/api/containers/${encodeURIComponent(id)}`),
      api(`/api/containers/${encodeURIComponent(id)}/stats`).catch(() => null),
    ]);
    const c = detail.item || {};
    const s = (stats && stats.item && stats.item.stats) || c.stats || null;
    const running = (c.status || "").toLowerCase() === "running";
    $("#detail-title").textContent = `容器 · ${name || c.name || id}`;

    const act = $("#detail-actions");
    if (act) {
      act.innerHTML = "";
      act.appendChild(actionBtn("日志", () => showLogs(id, c.name || name)));
      act.appendChild(
        actionBtn("终端", () => openConsole(id, c.name || name), {
          disabled: !state.consoleEnabled || !running,
        })
      );
      if (running) {
        act.appendChild(actionBtn("重启", () => doLife(id, "restart")));
        act.appendChild(actionBtn("停止", () => doLife(id, "stop")));
      } else {
        act.appendChild(actionBtn("启动", () => doLife(id, "start")));
      }
    }

    const basic = [
      kv("名称", c.name || "—"),
      kv("ID", c.full_id || c.id || id, true),
      kv("镜像", c.image || "—", true),
      kv("状态", c.status || "—"),
      kv("健康", c.health || "—"),
      kv("管理源", c.label || c.manager || "—"),
      kv("重启策略", (c.restart_policy_full && c.restart_policy_full.Name) || c.restart_policy || "—"),
      kv("重启次数", c.restart_count ?? 0),
      kv("创建", c.created ? fmtTime(c.created) : "—"),
      kv("启动时间", c.started_at ? fmtTime(c.started_at) : "—"),
    ];
    if (c.compose_project) basic.push(kv("Compose", `${c.compose_project}/${c.compose_service || ""}`));
    if (s) {
      basic.push(
        kv("CPU", `${(Number(s.cpu_percent) || 0).toFixed(1)}%`),
        kv(
          "内存",
          `${fmtBytes(s.memory_usage || s.mem_usage || 0)} / ${fmtBytes(s.memory_limit || s.mem_limit || 0)} (${(Number(s.memory_percent || s.mem_percent) || 0).toFixed(1)}%)`
        )
      );
    }
    if (c.memory_limit) basic.push(kv("内存限制", fmtBytes(c.memory_limit)));
    if (c.privileged) basic.push(kv("特权", "是"));

    const portsHtml = `<pre class="mono small" style="margin:0;white-space:pre-wrap">${escapeHtml(fmtPorts(c.ports || c.port_bindings))}</pre>`;

    const mounts = c.mounts || [];
    const mountsHtml = mounts.length
      ? `<ul class="detail-list">${mounts
          .map(
            (m) =>
              `<li><span class="mono small">${escapeHtml(m.source || "")} → ${escapeHtml(m.destination || "")}</span><span class="muted small">${escapeHtml(m.type || "")}${m.rw === false ? " ro" : ""}</span></li>`
          )
          .join("")}</ul>`
      : `<div class="muted small">无挂载</div>`;

    const envs = c.env || [];
    const envHtml = envs.length
      ? `<ul class="detail-list">${envs
          .slice(0, 80)
          .map((e) => `<li><span class="mono small">${escapeHtml(e)}</span></li>`)
          .join("")}</ul>${envs.length > 80 ? `<div class="muted small">…共 ${envs.length} 项</div>` : ""}`
      : `<div class="muted small">无环境变量</div>`;

    const netDetails = c.network_details || {};
    const netNames = c.networks || Object.keys(netDetails);
    const netsHtml = netNames.length
      ? `<ul class="detail-list" id="detail-net-list">${netNames
          .map((n) => {
            const d = netDetails[n] || {};
            return `<li data-net="${escapeHtml(n)}"><span><strong>${escapeHtml(n)}</strong><div class="muted mono small">${escapeHtml(d.ip || "—")} ${d.mac ? "· " + escapeHtml(d.mac) : ""}</div></span><button type="button" class="btn small danger detail-net-disc" data-net="${escapeHtml(n)}">断开</button></li>`;
          })
          .join("")}</ul>
         <div class="row-between wrap" style="margin-top:0.45rem">
           <input type="text" class="input" id="detail-net-name" placeholder="网络名，如 bridge" style="flex:1;min-width:140px" />
           <button type="button" class="btn small" id="detail-net-connect">连接到…</button>
         </div>`
      : `<div class="muted small">无网络</div>
         <div class="row-between wrap" style="margin-top:0.45rem">
           <input type="text" class="input" id="detail-net-name" placeholder="网络名" style="flex:1;min-width:140px" />
           <button type="button" class="btn small" id="detail-net-connect">连接到…</button>
         </div>`;

    $("#detail-body").innerHTML = [
      sectionHtml("基本信息", `<div class="kv-grid">${basic.join("")}</div>`),
      sectionHtml("端口", portsHtml),
      sectionHtml("挂载", mountsHtml),
      sectionHtml("环境变量", envHtml),
      sectionHtml("网络", netsHtml),
    ].join("");

    $("#detail-dialog").showModal();

    $("#detail-net-connect")?.addEventListener("click", async () => {
      const net = ($("#detail-net-name")?.value || "").trim();
      if (!net) return alert("请输入网络名");
      if (!requireLogin()) return;
      try {
        const r = await api(`/api/containers/${encodeURIComponent(id)}/networks/connect`, {
          method: "POST",
          body: JSON.stringify({ network: net }),
        });
        alert(r.message || "已连接");
        showDetail(id, name);
      } catch (e) {
        alert(e.message);
      }
    });
    document.querySelectorAll(".detail-net-disc").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const net = btn.dataset.net;
        if (!net || !requireLogin()) return;
        if (!confirm(`断开网络 ${net}？`)) return;
        try {
          const r = await api(`/api/containers/${encodeURIComponent(id)}/networks/disconnect`, {
            method: "POST",
            body: JSON.stringify({ network: net }),
          });
          alert(r.message || "已断开");
          showDetail(id, name);
        } catch (e) {
          alert(e.message);
        }
      });
    });
  } catch (e) {
    alert(`详情失败：${e.message}`);
  }
}

function showImageDetail(img) {
  if (!img) return;
  const ref = imageRef(img);
  const tags = img.tags || [];
  const usedList = img.used_by_containers || [];
  $("#image-detail-title").textContent = `镜像 · ${img.label || ref}`;
  const actions = $("#image-detail-actions");
  if (actions) {
    actions.innerHTML = "";
    const g = document.createElement("div");
    g.className = "action-group";
    g.appendChild(actionBtn("历史", () => showImageHistory(ref)));
    g.appendChild(
      actionBtn("删除", () => {
        $("#image-detail-dialog")?.close();
        doImageRemove(ref);
      }, { danger: true, disabled: !state.takeover })
    );
    actions.appendChild(g);
  }
  const contRows = usedList.length
    ? usedList
        .map(
          (c) =>
            `<tr><td><a href="#" class="img-cont-link" data-id="${escapeHtml(c.id || "")}" data-name="${escapeHtml(c.name || "")}">${escapeHtml(c.name || c.id || "")}</a></td><td><span class="${pillClass(c.state || "")}">${escapeHtml(c.state || "-")}</span></td><td class="mono small">${escapeHtml(c.id || "")}</td></tr>`
        )
        .join("")
    : `<tr><td colspan="3" class="muted">无关联容器（可清理）</td></tr>`;
  $("#image-detail-body").innerHTML = `
    <div class="detail-section">
      <h4>基本信息</h4>
      <div class="kv-grid">
        <div class="kv"><span class="k">标签</span><span class="v mono">${escapeHtml(tags.join(", ") || "<none>")}</span></div>
        <div class="kv"><span class="k">ID</span><span class="v mono">${escapeHtml(img.full_id || img.id || "")}</span></div>
        <div class="kv"><span class="k">大小</span><span class="v">${fmtBytes(img.size)}</span></div>
        <div class="kv"><span class="k">创建</span><span class="v">${escapeHtml(img.created_fmt || fmtImageCreated(img.created) || "—")}</span></div>
        <div class="kv"><span class="k">占用</span><span class="v">${Number(img.used_by) || usedList.length || 0} 个容器</span></div>
      </div>
    </div>
    <div class="detail-section">
      <h4>关联容器</h4>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>名称</th><th>状态</th><th>ID</th></tr></thead>
          <tbody>${contRows}</tbody>
        </table>
      </div>
    </div>
  `;
  $("#image-detail-body")
    .querySelectorAll(".img-cont-link")
    .forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        $("#image-detail-dialog")?.close();
        const cid = a.dataset.id;
        const name = a.dataset.name;
        if (cid) showDetail(cid, name);
      });
    });
  $("#image-detail-dialog")?.showModal();
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

function stopLogStream() {
  if (state.logs.es) {
    try {
      state.logs.es.close();
    } catch (_) {}
    state.logs.es = null;
  }
  if (state.logs.abort) {
    try {
      state.logs.abort.abort();
    } catch (_) {}
    state.logs.abort = null;
  }
  const st = $("#logs-status");
  if (st) st.textContent = "已停止";
}

function setLogsPaused(paused) {
  state.logs.paused = !!paused;
  const btn = $("#logs-pause");
  if (btn) btn.textContent = state.logs.paused ? "继续" : "暂停";
  const st = $("#logs-status");
  if (st && state.logs.es) st.textContent = state.logs.paused ? "已暂停" : "实时跟随中";
}

function appendLogLine(line) {
  if (state.logs.paused) return;
  const body = $("#logs-body");
  if (!body) return;
  if (body.textContent === "…" || body.textContent === "(空)") body.textContent = "";
  body.textContent += line.endsWith("\n") ? line : line + "\n";
  // auto-scroll
  body.scrollTop = body.scrollHeight;
  // cap size ~1.5MB
  if (body.textContent.length > 1_500_000) {
    body.textContent = body.textContent.slice(-1_000_000);
  }
}

async function showLogs(id, name) {
  if (!requireLogin()) return;
  stopLogStream();
  state.logs.id = id;
  state.logs.name = name || id;
  state.logs.paused = false;
  setLogsPaused(false);
  $("#logs-title").textContent = `日志 · ${name || id}`;
  $("#logs-body").textContent = "加载中…";
  const follow = $("#logs-follow")?.checked !== false;
  const timestamps = $("#logs-timestamps")?.checked !== false;
  const tail = Number($("#logs-tail")?.value || 300);
  $("#logs-dialog").showModal();
  const st = $("#logs-status");

  if (follow) {
    // EventSource cannot set Authorization header — use fetch stream with token
    // Fallback: query via fetch SSE manually
    try {
      if (st) st.textContent = "连接中…";
      const ctrl = new AbortController();
      state.logs.abort = ctrl;
      const url = `/api/containers/${encodeURIComponent(id)}/logs?follow=1&tail=${tail}&timestamps=${timestamps ? "true" : "false"}`;
      const res = await fetch(url, {
        headers: authHeaders(),
        signal: ctrl.signal,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || res.statusText);
      }
      $("#logs-body").textContent = "";
      if (st) st.textContent = "实时跟随中";
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      (async () => {
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const parts = buf.split("\n\n");
            buf = parts.pop() || "";
            for (const block of parts) {
              const lines = block.split("\n");
              let dataLine = "";
              for (const ln of lines) {
                if (ln.startsWith("data:")) dataLine += ln.slice(5).trim();
              }
              if (!dataLine) continue;
              try {
                const obj = JSON.parse(dataLine);
                if (obj.line != null) appendLogLine(String(obj.line));
                else if (obj.error) appendLogLine(`[error] ${obj.error}`);
              } catch (_) {
                appendLogLine(dataLine);
              }
            }
          }
          if (st) st.textContent = "流已结束";
        } catch (e) {
          if (e.name !== "AbortError" && st) st.textContent = `断开：${e.message || e}`;
        }
      })();
    } catch (e) {
      // fallback snapshot
      try {
        const r = await api(
          `/api/containers/${encodeURIComponent(id)}/logs?tail=${tail}&timestamps=${timestamps ? "true" : "false"}`
        );
        $("#logs-body").textContent = r.logs || "(空)";
        if (st) st.textContent = `快照（跟随失败：${e.message}）`;
      } catch (e2) {
        $("#logs-body").textContent = "";
        alert(`读取日志失败：${e2.message}`);
      }
    }
  } else {
    try {
      const r = await api(
        `/api/containers/${encodeURIComponent(id)}/logs?tail=${tail}&timestamps=${timestamps ? "true" : "false"}`
      );
      $("#logs-body").textContent = r.logs || "(空)";
      if (st) st.textContent = "快照";
    } catch (e) {
      alert(`读取日志失败：${e.message}`);
    }
  }
}

function getXtermConstructors() {
  const Terminal = window.Terminal || (window.xterm && window.xterm.Terminal);
  let FitAddonCtor =
    (window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon)) ||
    (window.FitAddonModule && window.FitAddonModule.FitAddon);
  return { Terminal, FitAddonCtor };
}

function closeConsole() {
  if (state.term.ws) {
    try {
      state.term.ws.close();
    } catch (_) {}
    state.term.ws = null;
  }
  if (state.term.ro) {
    try {
      state.term.ro.disconnect();
    } catch (_) {}
    state.term.ro = null;
  }
  if (state.term.term) {
    try {
      state.term.term.dispose();
    } catch (_) {}
    state.term.term = null;
    state.term.fit = null;
  }
  const host = $("#console-term");
  if (host) host.innerHTML = "";
  const st = $("#console-status");
  if (st) st.textContent = "未连接";
}

function openConsole(id, name) {
  if (!requireLogin()) return;
  if (!state.consoleEnabled) {
    alert("控制台未启用。请在容器环境变量设置 DOCKEROPS_CONSOLE_ENABLED=true 后重建。");
    return;
  }
  closeConsole();
  state.term.id = id;
  state.term.name = name || id;
  $("#console-title").textContent = `终端 · ${name || id}`;
  $("#console-dialog").showModal();
  connectConsole();
}

function connectConsole() {
  const id = state.term.id;
  if (!id) return;
  const { Terminal, FitAddonCtor } = getXtermConstructors();
  if (!Terminal) {
    $("#console-status").textContent = "xterm 未加载";
    alert("终端组件未加载，请强制刷新页面");
    return;
  }
  const host = $("#console-term");
  if (!host) return;
  host.innerHTML = "";

  if (state.term.ws) {
    try {
      state.term.ws.close();
    } catch (_) {}
    state.term.ws = null;
  }
  if (state.term.term) {
    try {
      state.term.term.dispose();
    } catch (_) {}
  }

  const term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    theme: {
      background: "#0b0e14",
      foreground: "#e5e7eb",
      cursor: "#00f3ff",
      selectionBackground: "rgba(108,92,231,0.35)",
    },
    convertEol: true,
  });
  let fit = null;
  if (FitAddonCtor) {
    try {
      fit = new FitAddonCtor();
      term.loadAddon(fit);
    } catch (_) {
      fit = null;
    }
  }
  term.open(host);
  state.term.term = term;
  state.term.fit = fit;
  try {
    fit && fit.fit();
  } catch (_) {}

  const shell = $("#console-shell")?.value || "sh";
  const cols = term.cols || 120;
  const rows = term.rows || 30;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const qs = new URLSearchParams({
    token: state.token || "",
    shell,
    cols: String(cols),
    rows: String(rows),
  });
  if (state.endpointId) qs.set("endpoint", state.endpointId);
  const url = `${proto}//${location.host}/api/containers/${encodeURIComponent(id)}/console?${qs}`;
  const st = $("#console-status");
  if (st) st.textContent = "连接中…";
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  state.term.ws = ws;

  ws.onopen = () => {
    if (st) st.textContent = "已连接";
    try {
      fit && fit.fit();
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    } catch (_) {}
  };
  ws.onmessage = (ev) => {
    if (!state.term.term) return;
    if (ev.data instanceof ArrayBuffer) {
      state.term.term.write(new Uint8Array(ev.data));
    } else if (typeof ev.data === "string") {
      state.term.term.write(ev.data);
    }
  };
  ws.onerror = () => {
    if (st) st.textContent = "连接错误";
  };
  ws.onclose = (ev) => {
    if (st) st.textContent = `已断开${ev.reason ? " · " + ev.reason : ""}`;
    state.term.ws = null;
  };

  term.onData((data) => {
    if (state.term.ws && state.term.ws.readyState === WebSocket.OPEN) {
      state.term.ws.send(data);
    }
  });
  term.onResize(({ cols: c, rows: r }) => {
    if (state.term.ws && state.term.ws.readyState === WebSocket.OPEN) {
      state.term.ws.send(JSON.stringify({ type: "resize", cols: c, rows: r }));
    }
  });

  if (window.ResizeObserver) {
    if (state.term.ro) {
      try {
        state.term.ro.disconnect();
      } catch (_) {}
    }
    state.term.ro = new ResizeObserver(() => {
      try {
        fit && fit.fit();
      } catch (_) {}
    });
    state.term.ro.observe(host);
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
  if (!confirm(`对 ${id} 按管理源执行安全更新？\nCompose→pull+force-recreate+remove-orphans+清旧镜像\nUnraid→模板重建+清旧镜像\n三方→仅拉镜像（不重建）\n需开启完整接管才会真正替换容器`)) return;
  await runUpdateStream(`/api/ops/update/${encodeURIComponent(id)}/stream`, {}, `更新 · ${id}`);
}

/** Progress dialog state for update SSE */
const progressState = {
  abort: null,
  layers: {},
};

function openProgressDialog(title) {
  progressState.layers = {};
  const dlg = $("#progress-dialog");
  if (!dlg) return null;
  $("#progress-title").textContent = title || "更新进度";
  $("#progress-stage").textContent = "准备中…";
  $("#progress-bars").innerHTML = "";
  $("#progress-log").textContent = "";
  $("#progress-status").textContent = "";
  const cancel = $("#progress-cancel");
  if (cancel) {
    cancel.disabled = false;
    cancel.textContent = "取消跟随";
  }
  dlg.showModal();
  return dlg;
}

function appendProgressLog(line) {
  const el = $("#progress-log");
  if (!el) return;
  el.textContent += (line.endsWith("\n") ? line : line + "\n");
  el.scrollTop = el.scrollHeight;
  if (el.textContent.length > 120_000) {
    el.textContent = el.textContent.slice(-80_000);
  }
}

function setProgressStage(text) {
  const el = $("#progress-stage");
  if (el) el.textContent = text || "";
}

function updatePullLayer(ev) {
  const id = ev.id || ev.status || "layer";
  if (!id) return;
  const bars = $("#progress-bars");
  if (!bars) return;
  let row = progressState.layers[id];
  if (!row) {
    row = document.createElement("div");
    row.className = "progress-layer";
    row.innerHTML = `<span class="mono muted">${escapeHtml(String(id).slice(0, 12))}</span><div class="bar"><i></i></div><span class="pct muted">0%</span>`;
    bars.appendChild(row);
    progressState.layers[id] = row;
  }
  const pct = typeof ev.percent === "number" ? ev.percent : null;
  const fill = row.querySelector("i");
  const label = row.querySelector(".pct");
  if (pct != null && fill) {
    fill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    if (label) label.textContent = `${Math.round(pct)}%`;
  } else if (ev.status && label) {
    label.textContent = String(ev.status).slice(0, 10);
  }
}

function stopProgressStream() {
  if (progressState.abort) {
    try {
      progressState.abort.abort();
    } catch (_) {
      /* ignore */
    }
    progressState.abort = null;
  }
}

async function runUpdateStream(url, body, title) {
  openProgressDialog(title);
  stopProgressStream();
  const ctrl = new AbortController();
  progressState.abort = ctrl;
  let finalOk = null;
  let finalMsg = "";
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        ...authHeaders(),
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || data.message || res.statusText);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() || "";
      for (const block of parts) {
        const lines = block.split("\n");
        let dataLine = "";
        for (const ln of lines) {
          if (ln.startsWith("data:")) dataLine += ln.slice(5).trim();
        }
        if (!dataLine) continue;
        let obj;
        try {
          obj = JSON.parse(dataLine);
        } catch (_) {
          appendProgressLog(dataLine);
          continue;
        }
        const ev = obj.event || obj.type || "";
        if (ev === "stage") {
          const st = obj.stage || "";
          const msg = obj.message || st;
          setProgressStage(msg);
          appendProgressLog(`[${st}] ${msg}`);
        } else if (ev === "pull") {
          updatePullLayer(obj);
          if (obj.status && obj.id) {
            const p = obj.percent != null ? ` ${obj.percent}%` : "";
            appendProgressLog(`pull ${obj.id}: ${obj.status}${p}`);
          } else if (obj.status) {
            appendProgressLog(`pull: ${obj.status}`);
          }
        } else if (ev === "item_start") {
          setProgressStage(obj.message || `更新 ${obj.name || ""}`);
          appendProgressLog(`→ ${obj.message || obj.name || ""}`);
        } else if (ev === "item_done") {
          appendProgressLog(`${obj.ok ? "✓" : "✗"} ${obj.name || ""} ${obj.message || ""}`);
        } else if (ev === "error") {
          appendProgressLog(`[error] ${obj.message || ""}`);
          setProgressStage(obj.message || "错误");
        } else if (ev === "done") {
          finalOk = !!obj.ok;
          finalMsg = obj.message || (finalOk ? "完成" : "失败");
          setProgressStage(finalMsg);
          appendProgressLog(`=== ${finalMsg}`);
          $("#progress-status").textContent = finalMsg;
        } else {
          appendProgressLog(JSON.stringify(obj));
        }
      }
    }
    if (finalOk == null) {
      finalOk = true;
      finalMsg = "流已结束";
      $("#progress-status").textContent = finalMsg;
    }
  } catch (e) {
    if (e.name === "AbortError") {
      appendProgressLog("已取消跟随（引擎侧拉取可能仍在进行）");
      $("#progress-status").textContent = "已取消跟随";
    } else {
      appendProgressLog(`[error] ${e.message || e}`);
      setProgressStage(`失败：${e.message || e}`);
      $("#progress-status").textContent = e.message || String(e);
      alert(`更新失败：${e.message}`);
    }
  } finally {
    progressState.abort = null;
    const cancel = $("#progress-cancel");
    if (cancel) {
      cancel.disabled = true;
      cancel.textContent = "已结束";
    }
    try {
      await runDetectUpdates();
    } catch (_) {
      /* ignore */
    }
    loadAll();
  }
  return { ok: finalOk, message: finalMsg };
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
  if (!confirm(`安全更新 Compose 项目 ${name}？\n将 force-recreate + remove-orphans，成功后清理旧镜像（需完整接管）。`)) return;
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
    // rebuild by_id for container badges
    const by = {};
    (r.items || []).forEach((it) => {
      if (!it.id) return;
      by[it.id] = it;
      by[String(it.id).slice(0, 12)] = it;
      if (it.name) by[`name:${String(it.name).replace(/^\//, "")}`] = it;
    });
    state.updateById = by;
    state.updateStatus = {
      cached: true,
      checked_at: Date.now() / 1000,
      items: r.items || [],
      by_id: by,
      update_available_count: r.update_available_count || 0,
      summary: {
        scanned: r.scanned,
        update_available_count: r.update_available_count,
        message: r.message,
        elapsed_sec: r.elapsed_sec,
      },
      message: r.message,
      auto: state.updateStatus?.auto,
    };
    applyUpdateStatus(state.updateStatus);
    if (prog) prog.textContent = `完成 · 耗时 ${r.elapsed_sec ?? "?"}s`;
    renderContainers();
    return r;
  } catch (e) {
    if (prog) prog.textContent = `检测失败：${e.message}`;
    alert(e.message);
    return null;
  }
}

async function loadSystemSettings() {
  try {
    const r = await api("/api/system/settings");
    state.systemSettings = r;
    const proxy = r.proxy || {};
    const auto = r.update_auto || {};
    const hp = $("#sys-http-proxy");
    const hsp = $("#sys-https-proxy");
    const np = $("#sys-no-proxy");
    if (hp) hp.value = proxy.http || "";
    if (hsp) hsp.value = proxy.https || "";
    if (np) np.value = proxy.no_proxy || "";
    const locked = !!proxy.env_locked;
    [hp, hsp, np].forEach((el) => {
      if (el) el.readOnly = locked;
    });
    ["row-http-proxy", "row-https-proxy", "row-no-proxy"].forEach((id) => {
      const row = document.getElementById(id);
      if (row) row.classList.toggle("locked", locked);
    });
    const badge = $("#proxy-env-badge");
    if (badge) badge.hidden = !locked;
    const src = $("#sys-settings-source");
    if (src) {
      src.textContent =
        proxy.source === "env"
          ? "代理来源：环境变量"
          : proxy.source === "stored"
            ? "代理来源：已保存配置"
            : "代理未配置";
    }
    const ac = $("#sys-auto-check");
    if (ac) ac.checked = auto.auto_check_enabled !== false;
    const iv = $("#sys-auto-interval");
    if (iv) iv.value = String(auto.auto_check_interval_hours || 6);
    await loadRemoteSettings();
  } catch (e) {
    const st = $("#sys-settings-status");
    if (st) st.textContent = e.message || "加载系统设置失败";
  }
}

/** Progressive disclosure for remote settings form (local UI only). */
function syncRemoteFormVisibility(opts = {}) {
  const st = state.remote?.settings || state.remote?.status || {};
  const status = state.remote?.status || st;
  const runtime = state.remote?.runtime || {};
  const phase = opts.phase || st.ui_phase || status.ui_phase || "setup";
  const enEl = $("#remote-enabled");
  const enabled = enEl ? !!enEl.checked : !!st.enabled;
  const roleEl = $("#remote-role");
  const role = roleEl ? roleEl.value || "" : st.role || "";
  const isCtrl = role === "controller";
  const isAgent = role === "agent";
  const modeEl = $("#remote-agent-mode");
  const mode = modeEl ? modeEl.value || "" : st.agent_mode || "";
  const hasMode = mode === "collab" || mode === "managed";
  const pubEl = $("#remote-public-url");
  const hasUrl = !!(pubEl && (pubEl.value || "").trim());
  const pairCode = (($("#remote-pair-code")?.textContent || "").trim());
  const hasPair = !!(pairCode && pairCode !== "—");
  const waiting = phase === "waiting" || st.status === "waiting_pair";
  const connectedPhase =
    phase === "collab_banner" ||
    phase === "managed_full" ||
    st.status === "connected" ||
    st.status === "managed_lock";
  const modePick = phase === "mode_pick";
  const sessions = status.sessions || state.remote?.status?.sessions || [];
  const hasSessions = Array.isArray(sessions) && sessions.length > 0;

  const introOff = $("#remote-intro-off");
  const introOn = $("#remote-intro-on");
  if (introOff) introOff.hidden = enabled;
  if (introOn) introOn.hidden = !enabled;

  const stepRole = $("#remote-step-role");
  if (stepRole) stepRole.hidden = !enabled;

  const rowName = $("#row-remote-display-name");
  const saveBar = $("#remote-save-bar");
  if (rowName) rowName.hidden = !(enabled && (isCtrl || isAgent));
  if (saveBar) saveBar.hidden = !(enabled && (isCtrl || isAgent));

  const ctrlPanel = $("#remote-controller-panel");
  const agentPanel = $("#remote-agent-panel");
  if (ctrlPanel) ctrlPanel.hidden = !(enabled && isCtrl);
  if (agentPanel) agentPanel.hidden = !(enabled && isAgent);

  // controller: session table only when there are nodes (or after first connect attempt)
  const sessWrap = $("#remote-session-table-wrap");
  if (sessWrap) {
    sessWrap.hidden = !(enabled && isCtrl && (hasSessions || runtime.online_count > 0 || opts.forceSessions));
  }

  if (enabled && isAgent) {
    const setup = $("#remote-agent-setup");
    const conn = $("#remote-agent-connected");
    const showConnected = connectedPhase && !modePick && !waiting;
    if (setup) setup.hidden = !!showConnected;
    if (conn) conn.hidden = !showConnected;

    const rowUrl = $("#row-remote-public-url");
    const urlHint = $("#remote-agent-url-hint");
    const actions = $("#remote-agent-actions");
    const pairBox = $("#remote-pair-box");
    const applyBtn = $("#btn-remote-apply-mode");
    const discBtn = $("#btn-remote-disconnect");
    const pairBtn = $("#btn-remote-pair");
    const regenBtn = $("#btn-remote-pair-regen");
    const modeRow = $("#row-remote-agent-mode");

    // mode_pick: only mode + apply; hide url/pair until applied path regenerates
    if (modePick) {
      if (modeRow) modeRow.hidden = false;
      if (rowUrl) rowUrl.hidden = true;
      if (urlHint) urlHint.hidden = true;
      if (actions) actions.hidden = false;
      if (pairBtn) pairBtn.hidden = true;
      if (applyBtn) applyBtn.hidden = !hasMode;
      if (discBtn) discBtn.hidden = false;
      if (pairBox) pairBox.hidden = true;
      if (regenBtn) regenBtn.hidden = true;
    } else if (waiting && hasPair) {
      // waiting for controller: focus on pair box
      if (modeRow) modeRow.hidden = true;
      if (rowUrl) rowUrl.hidden = true;
      if (urlHint) urlHint.hidden = true;
      if (actions) actions.hidden = false;
      if (pairBtn) pairBtn.hidden = true;
      if (applyBtn) applyBtn.hidden = true;
      if (discBtn) discBtn.hidden = false;
      if (pairBox) pairBox.hidden = false;
      if (regenBtn) regenBtn.hidden = false;
    } else if (showConnected) {
      // connected panel handles UI
    } else {
      // setup wizard: mode → url → generate
      if (modeRow) modeRow.hidden = false;
      if (rowUrl) rowUrl.hidden = !hasMode;
      if (urlHint) urlHint.hidden = !hasMode;
      if (actions) actions.hidden = !(hasMode && hasUrl);
      if (pairBtn) pairBtn.hidden = !(hasMode && hasUrl);
      if (applyBtn) applyBtn.hidden = true;
      if (discBtn) discBtn.hidden = !!(st.active_session_id || hasPair);
      if (pairBox) pairBox.hidden = true;
      if (regenBtn) regenBtn.hidden = true;
    }

    if (showConnected) {
      const peer = st.active_peer_name || status.active_peer_name || "—";
      const locked =
        phase === "managed_full" ||
        st.status === "managed_lock" ||
        !!status.managed_locked;
      const ct = $("#remote-agent-connected-text");
      if (ct) {
        ct.textContent = locked
          ? `当前由远程设备「${peer}」完全管理`
          : `远程设备「${peer}」正在协同管理`;
      }
    }
  }
}

function applyRemoteSettingsUI(data) {
  const st = data?.settings || data?.status || data?.remote || {};
  const status = data?.status || data?.remote || st;
  const runtime = data?.runtime || {};
  state.remote = { ...(data || {}), settings: st, status, runtime };
  const en = $("#remote-enabled");
  if (en) en.checked = !!st.enabled;
  const role = $("#remote-role");
  if (role) role.value = st.role || "";
  const dn = $("#remote-display-name");
  if (dn) dn.value = st.display_name || "";
  const mode = $("#remote-agent-mode");
  if (mode) {
    // keep empty option until user/server has a real mode in setup
    const m = st.agent_mode || "";
    const phase = st.ui_phase || status.ui_phase || "setup";
    if (m === "collab" || m === "managed") mode.value = m;
    else if (phase === "mode_pick") mode.value = "";
    else if (!mode.value) mode.value = "";
  }
  const pub = $("#remote-public-url");
  if (pub && st.public_base_url) pub.value = st.public_base_url;

  const show = !!st.enabled;
  const isCtrl = st.role === "controller";
  const isAgent = st.role === "agent";
  const phase = st.ui_phase || status.ui_phase || "setup";
  const peer = st.active_peer_name || status.active_peer_name || "—";
  const locked = !!(
    status.managed_locked ||
    (isAgent && (phase === "managed_full" || st.status === "managed_lock"))
  );
  const collabOn = !!(
    isAgent &&
    !locked &&
    (phase === "collab_banner" ||
      (st.status === "connected" && (st.agent_mode || "collab") !== "managed"))
  );
  state.managedLocked = locked;
  applyManagedLockUI(locked, collabOn, status, st, runtime, peer);
  syncRemoteFormVisibility({ phase });

  const hint = $("#remote-banner-hint");
  if (hint) {
    hint.textContent =
      status.hint ||
      (!st.enabled
        ? ""
        : isCtrl
          ? `主控端 · 已连接远程 ${status.online_count ?? 0} 台`
          : isAgent
            ? status.hint || ""
            : "请选择本机角色：主控端或被控端");
  }
  const agSt = $("#remote-agent-status");
  if (agSt && isAgent) {
    if (locked) agSt.textContent = `托管中 · ${peer}`;
    else if (collabOn) agSt.textContent = `协同中 · ${peer}`;
    else if (phase === "waiting" || st.status === "waiting_pair") agSt.textContent = "等待主控连接…";
    else if (phase === "mode_pick") agSt.textContent = "请重新选择模式";
    else agSt.textContent = "";
  }
  const cSt = $("#remote-controller-status");
  if (cSt && isCtrl) {
    if (runtime.connecting) cSt.textContent = "连接中…";
    else if (runtime.last_error) cSt.textContent = runtime.last_error;
    else cSt.textContent = runtime.online_count ? `在线 ${runtime.online_count}` : "";
  }
  renderRemoteSessions(status.sessions || data?.status?.sessions || []);
  // show session table if any
  const sessWrap = $("#remote-session-table-wrap");
  const sessions = status.sessions || data?.status?.sessions || [];
  if (sessWrap && isCtrl && Array.isArray(sessions) && sessions.length) {
    sessWrap.hidden = false;
  }
  ensureRemotePolling(show && (isCtrl || isAgent));
}

function applyManagedLockUI(locked, collabOn, status, st, runtime, peer) {
  const banner = $("#remote-lock-banner");
  const collab = $("#remote-collab-banner");
  const badge = $("#remote-badge");
  const text = $("#remote-lock-text");
  const title = $("#remote-lock-title");
  if (banner) banner.hidden = !locked;
  if (collab) collab.hidden = !collabOn || locked;
  document.body.classList.toggle("remote-managed-lock", !!locked);
  document.body.classList.toggle("remote-collab-on", !!collabOn && !locked);
  if (badge) {
    const role = st?.role || status?.role || "";
    const enabled = !!(st?.enabled || status?.enabled);
    if (!enabled) {
      badge.hidden = true;
    } else {
      badge.hidden = false;
      if (locked) {
        badge.textContent = "托管中";
        badge.className = "badge bad";
      } else if (collabOn) {
        badge.textContent = "协同中";
        badge.className = "badge ok";
      } else if (role === "controller") {
        const on = status?.online_count ?? runtime?.online_count ?? 0;
        badge.textContent = on > 0 ? `主控 · 在线${on}` : "主控";
        badge.className = "badge ok";
      } else if (role === "agent") {
        const waiting = (st?.ui_phase || status?.ui_phase) === "waiting";
        badge.textContent = waiting ? "被控 · 等待" : "被控";
        badge.className = waiting ? "badge warn" : "badge";
      } else {
        badge.textContent = "远程";
        badge.className = "badge";
      }
    }
  }
  if (text && locked) {
    text.textContent = `当前由远程设备「${peer || "—"}」完全管理。可点「切换模式」回到设置重选协同/托管，或断开远程。`;
  }
  if (title && locked) title.textContent = "远程托管";
  const ctext = $("#remote-collab-text");
  if (ctext && collabOn) {
    ctext.textContent = `远程设备「${peer || "—"}」正在协同管理；本地功能仍可使用。`;
  }
}

function ensureRemotePolling(on) {
  if (state.remotePollTimer) {
    clearInterval(state.remotePollTimer);
    state.remotePollTimer = null;
  }
  if (!on || !state.token) return;
  state.remotePollTimer = setInterval(async () => {
    if (!state.token || document.hidden) return;
    try {
      const r = await api("/api/remote/status");
      if (r?.remote) {
        applyRemoteSettingsUI({
          settings: { ...(state.remote?.settings || {}), ...r.remote },
          status: r.remote,
          runtime: r.runtime || state.remote?.runtime || {},
        });
        if (r.remote.role === "controller") {
          // soft-refresh endpoint list so online dots update
          loadEndpoints().catch(() => null);
        }
      }
    } catch (_) {
      /* ignore */
    }
  }, 8000);
}

function renderRemoteSessions(sessions) {
  const tbody = $("#remote-session-rows");
  if (!tbody) return;
  const items = sessions || [];
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">暂无已配对节点</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  items.forEach((s) => {
    const tr = document.createElement("tr");
    const sid = s.session_id || s.id;
    const mode = s.mode || "collab";
    const modeLabel = mode === "managed" ? "托管" : "协同";
    tr.innerHTML = `
      <td class="cell-text"><strong>${escapeHtml(s.peer_name || s.name || sid)}</strong>
        <div class="muted small mono">${escapeHtml(String(sid).slice(0, 12))}…</div></td>
      <td class="col-status">${escapeHtml(modeLabel)}</td>
      <td class="col-status">${s.online ? '<span class="pill running">在线</span>' : '<span class="pill">离线</span>'}</td>
      <td class="col-actions actions"></td>`;
    const actions = tr.querySelector(".actions");
    const primary = [
      {
        label: "切换",
        primary: true,
        fn: () => activateEndpoint(`remote:${sid}`),
      },
    ];
    if (mode === "managed") {
      primary.push({ label: "改协同", fn: () => setRemoteSessionMode(sid, "collab") });
    } else {
      primary.push({ label: "改托管", fn: () => setRemoteSessionMode(sid, "managed") });
    }
    fillActionGroup(actions, primary, [
      { label: "断开", danger: true, fn: () => disconnectRemoteSession(sid) },
    ]);
    tbody.appendChild(tr);
  });
}

async function setRemoteSessionMode(sessionId, mode) {
  if (!requireLogin()) return;
  const label = mode === "managed" ? "托管锁定" : "协同";
  if (
    !confirm(
      mode === "managed"
        ? "切换为托管锁定？被控本地将无法启停/更新/删除，仅主控可写。"
        : "切换为协同？被控本地可再次操作。"
    )
  )
    return;
  try {
    const r = await api(`/api/remote/sessions/${encodeURIComponent(sessionId)}/mode`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    alert(r.message || `已切换为${label}`);
    await loadRemoteSettings();
    await loadEndpoints();
  } catch (e) {
    alert(e.message || String(e));
  }
}

async function loadRemoteSettings() {
  try {
    const r = await api("/api/remote/settings");
    applyRemoteSettingsUI(r);
    return r;
  } catch (e) {
    // older builds without remote API
    const st = $("#remote-settings-status");
    if (st && String(e.message || "").includes("404")) {
      st.textContent = "";
      return null;
    }
    if (st) st.textContent = e.message || "加载远程设置失败";
    return null;
  }
}

async function saveRemoteSettings() {
  if (!requireLogin()) return;
  const body = {
    enabled: !!$("#remote-enabled")?.checked,
    role: $("#remote-role")?.value || "",
    display_name: ($("#remote-display-name")?.value || "").trim(),
    public_base_url: ($("#remote-public-url")?.value || "").trim(),
  };
  const m = ($("#remote-agent-mode")?.value || "").trim();
  if (m === "collab" || m === "managed") body.agent_mode = m;
  const stEl = $("#remote-settings-status");
  try {
    if (stEl) stEl.textContent = "保存中…";
    const r = await api("/api/remote/settings", { method: "PUT", body: JSON.stringify(body) });
    if (stEl) stEl.textContent = r.message || "已保存";
    applyRemoteSettingsUI(r);
    await loadEndpoints();
    syncRemoteFormVisibility();
  } catch (e) {
    if (stEl) stEl.textContent = e.message || "保存失败";
  }
}

async function createRemotePair() {
  if (!requireLogin()) return;
  const mode = ($("#remote-agent-mode")?.value || "").trim();
  if (mode !== "collab" && mode !== "managed") {
    alert("请先选择协同或托管模式");
    return;
  }
  const public_base_url = ($("#remote-public-url")?.value || "").trim();
  if (!public_base_url) {
    alert("请填写本机公网域名或 IP（主控需能访问）");
    return;
  }
  const stEl = $("#remote-agent-status");
  try {
    if (stEl) stEl.textContent = "生成中…";
    // persist mode + url first
    await api("/api/remote/settings", {
      method: "PUT",
      body: JSON.stringify({
        enabled: true,
        role: "agent",
        agent_mode: mode,
        display_name: ($("#remote-display-name")?.value || "").trim() || "被控",
        public_base_url,
      }),
    });
    const r = await api("/api/remote/pair", {
      method: "POST",
      body: JSON.stringify({
        public_base_url,
        mode,
        agent_name: ($("#remote-display-name")?.value || "").trim() || "被控",
      }),
    });
    const codeEl = $("#remote-pair-code");
    if (codeEl) codeEl.textContent = r.pair_code || "";
    const msg = $("#remote-pair-msg");
    if (msg) msg.textContent = r.message || "等待主控在时限内粘贴连接…";
    if (state.remote) {
      state.remote.settings = {
        ...(state.remote.settings || {}),
        enabled: true,
        role: "agent",
        agent_mode: mode,
        public_base_url,
        ui_phase: "waiting",
        status: "waiting_pair",
      };
      state.remote.status = {
        ...(state.remote.status || {}),
        ui_phase: "waiting",
        status: "waiting_pair",
      };
    }
    syncRemoteFormVisibility({ phase: "waiting" });
    let left = Number(r.expires_in || 60);
    const ttl = $("#remote-pair-ttl");
    if (state.remotePairTimer) clearInterval(state.remotePairTimer);
    const tick = () => {
      if (ttl) ttl.textContent = left > 0 ? `${left}s` : "已过期，请重新生成";
      if (left <= 0) {
        clearInterval(state.remotePairTimer);
        state.remotePairTimer = null;
        const regen = $("#btn-remote-pair-regen");
        if (regen) regen.hidden = false;
        if (msg) msg.textContent = "凭证已过期，可点「重新生成」";
        return;
      }
      left -= 1;
    };
    tick();
    state.remotePairTimer = setInterval(tick, 1000);
    if (stEl) stEl.textContent = "等待主控连接…";
    const codeId = r.code_id;
    const poll = setInterval(async () => {
      try {
        const ps = await api(`/api/remote/pair?code_id=${encodeURIComponent(codeId || "")}`);
        if (ps.status === "used") {
          clearInterval(poll);
          await loadRemoteSettings();
        }
        if (ps.status === "expired" || left <= 0) {
          clearInterval(poll);
          if (msg) msg.textContent = "凭证已过期，可点「重新生成」";
          syncRemoteFormVisibility({ phase: "waiting" });
        }
      } catch (_) {
        clearInterval(poll);
      }
    }, 2000);
  } catch (e) {
    if (stEl) stEl.textContent = e.message || "生成失败";
    alert(e.message || String(e));
  }
}

/** 主控：粘贴被控凭证并连接 */
async function connectRemoteController() {
  if (!requireLogin()) return;
  const pair_code = ($("#remote-pair-input")?.value || "").trim();
  if (!pair_code) {
    alert("请粘贴被控生成的连接凭证");
    return;
  }
  const stEl = $("#remote-controller-status");
  try {
    if (stEl) stEl.textContent = "连接中…";
    // ensure role saved as controller
    await api("/api/remote/settings", {
      method: "PUT",
      body: JSON.stringify({
        enabled: true,
        role: "controller",
        display_name: ($("#remote-display-name")?.value || "").trim() || "主控",
      }),
    }).catch(() => null);
    const r = await api("/api/remote/controller/connect", {
      method: "POST",
      body: JSON.stringify({
        pair_code,
        controller_name: ($("#remote-display-name")?.value || "").trim() || "主控",
      }),
    });
    if (stEl) stEl.textContent = r.message || "已连接";
    const input = $("#remote-pair-input");
    if (input) input.value = "";
    const sessWrap = $("#remote-session-table-wrap");
    if (sessWrap) sessWrap.hidden = false;
    alert(r.message || "已连接被控");
    await loadRemoteSettings();
    await loadEndpoints();
    syncRemoteFormVisibility({ forceSessions: true });
  } catch (e) {
    if (stEl) stEl.textContent = e.message || "连接失败";
    alert(e.message || String(e));
  }
}

async function disconnectRemoteAgent() {
  if (!requireLogin()) return;
  try {
    await api("/api/remote/agent/disconnect", { method: "POST", body: "{}" });
    if (state.remotePairTimer) {
      clearInterval(state.remotePairTimer);
      state.remotePairTimer = null;
    }
    const codeEl = $("#remote-pair-code");
    if (codeEl) codeEl.textContent = "—";
    const ttl = $("#remote-pair-ttl");
    if (ttl) ttl.textContent = "—";
    const box = $("#remote-pair-box");
    if (box) box.hidden = true;
    await loadRemoteSettings();
    syncRemoteFormVisibility();
  } catch (e) {
    alert(e.message || String(e));
  }
}

async function switchRemoteAgentMode(mode) {
  if (!requireLogin()) return;
  try {
    const r = await api("/api/remote/agent/switch-mode", {
      method: "POST",
      body: JSON.stringify({ mode: mode || "mode_pick" }),
    });
    if (mode === "mode_pick") {
      const modeEl = $("#remote-agent-mode");
      if (modeEl) modeEl.value = "";
    }
    if (r.message && mode && mode !== "mode_pick") alert(r.message);
    applyRemoteSettingsUI(r);
    await loadRemoteSettings();
    syncRemoteFormVisibility({
      phase: mode === "mode_pick" ? "mode_pick" : undefined,
    });
  } catch (e) {
    alert(e.message || String(e));
  }
}

async function disconnectRemoteSession(sessionId) {
  if (!requireLogin()) return;
  if (!sessionId) return;
  if (!confirm("断开该远程节点？之后需被控重新生成凭证并在主控粘贴。")) return;
  try {
    await api(`/api/remote/sessions/${encodeURIComponent(sessionId)}/disconnect`, {
      method: "POST",
      body: "{}",
    });
    if (String(state.endpointId || "").includes(sessionId)) {
      state.endpointId = "";
      try {
        localStorage.removeItem(ENDPOINT_STORAGE_KEY);
      } catch (_) {
        /* ignore */
      }
    }
    await loadRemoteSettings();
    await loadEndpoints();
    await loadAll({ banner: true });
  } catch (e) {
    alert(e.message || String(e));
  }
}

async function runOneClickUpdate(ids) {
  if (!requireLogin()) return;
  const onlyRunning = !!$("#upd-only-running")?.checked;
  const n = ids ? ids.length : "全部可更新";
  if (!confirm(`一键安全更新 ${n} 个容器？\n备份+拉取+自动重建（Compose 含 remove-orphans；成功后清理旧镜像/dangling）。\n未开启完整接管时仅拉镜像，不会停/重建容器。`)) return;
  const body = {
    only_available: true,
    only_running: onlyRunning,
  };
  if (ids && ids.length) body.container_ids = ids;
  const prog = $("#update-progress");
  if (prog) {
    prog.hidden = false;
    prog.textContent = "进度见弹窗…";
  }
  const r = await runUpdateStream(
    "/api/ops/one-click-update/stream",
    body,
    `一键更新 · ${n}`
  );
  if (prog) prog.textContent = r?.message || "完成";
}

function selectedUpdateIds() {
  /** Prefer checked containers that currently have updates; else all available. */
  const checked = Array.from(document.querySelectorAll(".ctr-sel:checked"))
    .map((el) => el.dataset.id)
    .filter(Boolean);
  const available = new Set(
    (state.updateItems || [])
      .filter((u) => u.update_available && u.id)
      .map((u) => u.id)
  );
  const fromChecked = checked.filter((id) => {
    if (available.has(id)) return true;
    const short = String(id).slice(0, 12);
    return [...available].some((x) => x === id || String(x).startsWith(short) || String(x).slice(0, 12) === short);
  });
  if (fromChecked.length) return fromChecked;
  return (state.updateItems || [])
    .filter((u) => u.update_available && u.id)
    .map((u) => u.id);
}

// ── Event bindings ──
document.querySelectorAll("#main-tabs .nav-item").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// browser back/forward / shared #tab links
window.addEventListener("hashchange", () => {
  const t = readSavedTab();
  if (t && t !== state.tab) switchTab(t, { skipPersist: true });
});

$("#btn-refresh").addEventListener("click", () => loadAll({ banner: true }));
$("#logs-close").addEventListener("click", () => {
  stopLogStream();
  $("#logs-dialog").close();
});
$("#logs-dialog")?.addEventListener("close", () => stopLogStream());
$("#logs-pause")?.addEventListener("click", () => setLogsPaused(!state.logs.paused));
$("#logs-clear")?.addEventListener("click", () => {
  const b = $("#logs-body");
  if (b) b.textContent = "";
});
$("#logs-reload")?.addEventListener("click", () => {
  if (state.logs.id) showLogs(state.logs.id, state.logs.name);
});
$("#logs-follow")?.addEventListener("change", () => {
  if (state.logs.id) showLogs(state.logs.id, state.logs.name);
});
$("#logs-timestamps")?.addEventListener("change", () => {
  if (state.logs.id) showLogs(state.logs.id, state.logs.name);
});
$("#logs-tail")?.addEventListener("change", () => {
  if (state.logs.id) showLogs(state.logs.id, state.logs.name);
});
$("#detail-close")?.addEventListener("click", () => $("#detail-dialog").close());
$("#image-detail-close")?.addEventListener("click", () => $("#image-detail-dialog")?.close());
$("#progress-close")?.addEventListener("click", () => {
  stopProgressStream();
  $("#progress-dialog")?.close();
});
$("#progress-cancel")?.addEventListener("click", () => {
  stopProgressStream();
  $("#progress-status").textContent = "已取消跟随";
  appendProgressLog("用户取消跟随");
});
$("#progress-dialog")?.addEventListener("close", () => stopProgressStream());
$("#console-close")?.addEventListener("click", () => {
  closeConsole();
  $("#console-dialog").close();
});
$("#console-dialog")?.addEventListener("close", () => closeConsole());
$("#console-reconnect")?.addEventListener("click", () => connectConsole());
$("#console-clear")?.addEventListener("click", () => {
  try {
    state.term.term && state.term.term.clear();
  } catch (_) {}
});
$("#console-shell")?.addEventListener("change", () => {
  if (state.term.id) connectConsole();
});
// re-render action density on orientation / resize across 960px
let _narrowWas = isNarrowScreen();
window.addEventListener("resize", () => {
  const n = isNarrowScreen();
  if (n !== _narrowWas) {
    _narrowWas = n;
    try {
      renderContainers();
      renderImages();
    } catch (_) {}
  }
});

$("#btn-menu")?.addEventListener("click", () => setSidebarOpen(true));
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
const ivf = $("#image-view-filter");
if (ivf) ivf.addEventListener("change", renderImages);
$("#img-check-all")?.addEventListener("change", (e) => {
  document.querySelectorAll(".img-sel").forEach((c) => {
    c.checked = e.target.checked;
    const id = c.dataset.id;
    if (!id) return;
    if (e.target.checked) state.selectedImages.add(id);
    else state.selectedImages.delete(id);
  });
  updateImageSelCount();
});
$("#stat-updates-chip")?.addEventListener("click", () => {
  const uf = $("#container-update-filter");
  if (uf) uf.value = "available";
  switchTab("containers");
  renderContainers();
});
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
  switchTab("containers");
  await runDetectUpdates();
});
$("#btn-quick-update-all")?.addEventListener("click", async () => {
  switchTab("containers");
  if (!state.updateItems.length) await runDetectUpdates();
  await runOneClickUpdate(null);
});
$("#btn-goto-containers")?.addEventListener("click", () => switchTab("containers"));
$("#btn-goto-system")?.addEventListener("click", () => switchTab("system"));
$("#btn-goto-docs")?.addEventListener("click", () => switchTab("docs"));

$("#container-update-filter")?.addEventListener("change", renderContainers);

/* legacy no-op if old markup still cached */
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

$("#endpoint-select")?.addEventListener("change", (e) => {
  const id = e.target.value;
  if (id) activateEndpoint(id);
});
$("#btn-ep-create")?.addEventListener("click", () => createEndpointFromForm());
$("#ep-tls")?.addEventListener("change", (e) => {
  const on = !!e.target.checked;
  ["#ep-tls-ca", "#ep-tls-cert", "#ep-tls-key"].forEach((s) => {
    const el = $(s);
    if (el) el.disabled = !on;
  });
});

$("#btn-save-sys-settings")?.addEventListener("click", async () => {
  if (!requireLogin()) return;
  const body = {
    auto_check_enabled: !!$("#sys-auto-check")?.checked,
    auto_check_interval_hours: Number($("#sys-auto-interval")?.value || 6),
  };
  const locked = !!state.systemSettings?.proxy?.env_locked;
  if (!locked) {
    body.http_proxy = $("#sys-http-proxy")?.value || "";
    body.https_proxy = $("#sys-https-proxy")?.value || "";
    body.no_proxy = $("#sys-no-proxy")?.value || "";
  }
  try {
    const r = await api("/api/system/settings", { method: "PUT", body: JSON.stringify(body) });
    $("#sys-settings-status").textContent = r.message || "已保存";
    state.systemSettings = r;
    if (r.update_auto && state.updateStatus) {
      state.updateStatus.auto = r.update_auto;
      applyUpdateStatus(state.updateStatus);
    }
    await loadSystemSettings();
  } catch (e) {
    $("#sys-settings-status").textContent = e.message || "保存失败";
  }
});

$("#btn-remote-save")?.addEventListener("click", () => saveRemoteSettings());
$("#btn-remote-pair")?.addEventListener("click", () => createRemotePair());
$("#btn-remote-pair-regen")?.addEventListener("click", () => createRemotePair());
$("#btn-remote-copy-pair")?.addEventListener("click", async () => {
  const code = ($("#remote-pair-code")?.textContent || "").trim();
  if (!code || code === "—") return;
  try {
    await navigator.clipboard.writeText(code);
    const msg = $("#remote-pair-msg");
    if (msg) msg.textContent = "连接凭证已复制，请在主控端粘贴";
  } catch (_) {
    prompt("复制以下连接凭证：", code);
  }
});
$("#btn-remote-refresh-sessions")?.addEventListener("click", async () => {
  await loadRemoteSettings();
  await loadEndpoints();
  syncRemoteFormVisibility({ forceSessions: true });
});
$("#btn-remote-connect")?.addEventListener("click", () => connectRemoteController());
$("#btn-remote-disconnect")?.addEventListener("click", () => disconnectRemoteAgent());
$("#btn-remote-agent-disconnect2")?.addEventListener("click", () => disconnectRemoteAgent());
$("#btn-remote-lock-disconnect")?.addEventListener("click", () => disconnectRemoteAgent());
$("#btn-remote-collab-disconnect")?.addEventListener("click", () => disconnectRemoteAgent());
$("#btn-remote-switch-mode")?.addEventListener("click", () => switchRemoteAgentMode("mode_pick"));
$("#btn-remote-collab-switch")?.addEventListener("click", () => switchRemoteAgentMode("mode_pick"));
$("#btn-remote-agent-switch-mode")?.addEventListener("click", () => switchRemoteAgentMode("mode_pick"));
$("#btn-remote-apply-mode")?.addEventListener("click", () => {
  const m = $("#remote-agent-mode")?.value || "";
  if (m !== "collab" && m !== "managed") {
    alert("请选择协同或托管");
    return;
  }
  switchRemoteAgentMode(m);
});
$("#btn-remote-goto-settings")?.addEventListener("click", () => {
  const btn = document.querySelector('.nav-item[data-tab="settings"]');
  if (btn) btn.click();
  else {
    state.tab = "settings";
    document.querySelectorAll(".tab-panel").forEach((p) => {
      p.hidden = p.getAttribute("data-panel") !== "settings";
    });
  }
  // scroll to remote section
  setTimeout(() => {
    $("#remote-mode-section")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, 50);
});
$("#remote-enabled")?.addEventListener("change", () => {
  syncRemoteFormVisibility();
  // auto-save enable toggle for clearer UX when turning off
  if (!$("#remote-enabled")?.checked) {
    saveRemoteSettings().catch?.(() => null);
  }
});
$("#remote-role")?.addEventListener("change", () => {
  const role = $("#remote-role")?.value || "";
  const dn = $("#remote-display-name");
  if (dn && !(dn.value || "").trim()) {
    dn.placeholder = role === "controller" ? "例如：Tower 主控" : role === "agent" ? "例如：飞牛被控" : "显示名称";
  }
  // reset agent mode UI when switching role
  if (role === "agent") {
    const mode = $("#remote-agent-mode");
    // keep server value if any; otherwise empty for wizard
    if (mode && !mode.value) mode.value = "";
  }
  syncRemoteFormVisibility();
});
$("#remote-agent-mode")?.addEventListener("change", () => {
  syncRemoteFormVisibility();
});
$("#remote-public-url")?.addEventListener("input", () => {
  syncRemoteFormVisibility();
});
$("#remote-public-url")?.addEventListener("change", () => {
  syncRemoteFormVisibility();
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

$("#btn-image-prune-unused")?.addEventListener("click", async () => {
  if (!requireLogin() || !requireTakeover("清理未使用镜像")) return;
  if (
    !confirm(
      "清理所有未使用的镜像（含 dangling 与带 tag、未被任何容器引用的镜像）？\n此操作不可恢复，请确认无误。"
    )
  )
    return;
  try {
    // dangling=false → docker image prune -a (all unused, including tagged)
    const r = await api("/api/images/prune?dangling=false", { method: "POST" });
    if (!r.ok) throw new Error(r.message || "清理失败");
    const deleted = r.result?.images_deleted || r.images_deleted || [];
    const n = Array.isArray(deleted) ? deleted.length : 0;
    const space = r.result?.space_reclaimed ?? r.space_reclaimed ?? 0;
    alert(r.message || `完成 · 删除 ${n} 项 · 回收 ${fmtBytes(space)}`);
    loadResources("images");
  } catch (e) {
    alert(e.message);
  }
});

$("#btn-image-batch-remove")?.addEventListener("click", async () => {
  if (!requireLogin() || !requireTakeover("批量删除镜像")) return;
  const ids = Array.from(state.selectedImages);
  if (!ids.length) return;
  if (!confirm(`删除选中的 ${ids.length} 个镜像？`)) return;
  let ok = 0;
  let fail = 0;
  for (const id of ids) {
    try {
      await api(`/api/images/${encodeURIComponent(id)}?force=false`, { method: "DELETE" });
      ok += 1;
      state.selectedImages.delete(id);
    } catch (_) {
      fail += 1;
    }
  }
  alert(`批量删除完成：成功 ${ok}，失败 ${fail}`);
  loadResources("images");
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
    lockAuthGate("setup");
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
    lockAuthGate("login");
    return;
  }
  lockAuthGate("login");
});

$("#login-cancel")?.addEventListener("click", () => {
  // Only meaningful if already authenticated (hidden otherwise)
  if (state.token) {
    $("#login-dialog")?.close();
    document.body.classList.remove("auth-locked");
    state.authGateOpen = false;
  }
});

$("#forgot-copy")?.addEventListener("click", async () => {
  const text = $("#forgot-cmd")?.textContent || "";
  try {
    await navigator.clipboard.writeText(text);
    const btn = $("#forgot-copy");
    if (btn) {
      const old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => {
        btn.textContent = old;
      }, 1200);
    }
  } catch (_) {
    alert("复制失败，请手动选中命令");
  }
});

// Prevent Esc / cancel from bypassing forced auth gate
["setup-dialog", "login-dialog"].forEach((id) => {
  const dlg = document.getElementById(id);
  if (!dlg) return;
  dlg.addEventListener("cancel", (e) => {
    if (state.authUnlocking) return;
    if (state.needsSetup || !state.token) {
      e.preventDefault();
    }
  });
  dlg.addEventListener("close", () => {
    if (state.authUnlocking) return;
    // If closed while still required, re-open on next tick
    if (state.needsSetup) {
      setTimeout(() => {
        if (!state.authUnlocking && state.needsSetup) lockAuthGate("setup");
      }, 0);
    } else if (!state.token) {
      setTimeout(() => {
        if (!state.authUnlocking && !state.token) lockAuthGate("login");
      }, 0);
    } else {
      document.body.classList.remove("auth-locked");
      state.authGateOpen = false;
    }
  });
});

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
    state.usernames = [data.username];
    localStorage.setItem("dockerops_token", state.token);
    localStorage.setItem("dockerops_user", state.username);
    unlockAuthGate();
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
    unlockAuthGate();
    setAuthUI();
    loadAll();
  } catch (err) {
    const el = $("#login-error");
    el.hidden = false;
    el.textContent = err.message || "登录失败";
    // Keep forgot-password visible on failure
    fillForgotPasswordUI();
  }
});

// initial — restore last tab, then load quietly if session exists
setSidebarOpen(false); // ensure mobile backdrop never blocks first paint
applyPrefsLocal(state.prefs);
if (window.DockerOpsParticles) {
  window.DockerOpsParticles.applyPrefs(state.prefs);
}
updateComposeNavVisibility();
// Stay on current page after refresh (hash / localStorage), not force overview
switchTab(readSavedTab());
// Open gate immediately if we already know session is empty (before network)
if (!state.token) {
  // status will refine setup vs login
  lockAuthGate("login");
}
loadAll();
// 取消自动刷新：仅顶部「刷新」按钮或操作后手动 loadAll

// close action menus when clicking outside
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.action-menu[open]").forEach((d) => {
    if (!d.contains(e.target)) d.open = false;
  });
});
