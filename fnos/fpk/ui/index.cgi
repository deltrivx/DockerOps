#!/bin/sh
# DockerOps desktop launcher for FnOS AppCenter.
# Opens the real Web UI via the browser hostname (NOT 127.0.0.1),
# so remote clients no longer get a black iframe.
#
# Port file is written by cmd/main on start (ui/port).

APP_ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PORT_FILE="${APP_ROOT}/ui/port"
PORT="8080"
if [ -f "${PORT_FILE}" ]; then
  PORT="$(tr -cd '0-9' < "${PORT_FILE}" | head -c 5)"
  [ -n "${PORT}" ] || PORT="8080"
fi

# CGI headers
printf 'Content-Type: text/html; charset=utf-8\r\n'
printf 'Cache-Control: no-store\r\n'
printf '\r\n'

cat <<EOF
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="Cache-Control" content="no-store" />
  <title>DockerOps</title>
  <style>
    :root {
      --bg: #0a0a0f;
      --panel: rgba(18, 22, 32, 0.92);
      --text: #e8e8f0;
      --muted: #8888a0;
      --cyan: #00f3ff;
      --purple: #6c5ce7;
      --pink: #fb3f62;
      --border: rgba(0, 243, 255, 0.18);
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0; min-height: 100%;
      font-family: "SF Pro Text", "PingFang SC", "Segoe UI", system-ui, sans-serif;
      color: var(--text);
      background:
        radial-gradient(900px 500px at 20% 10%, rgba(108,92,231,.22), transparent 55%),
        radial-gradient(700px 400px at 80% 0%, rgba(0,243,255,.12), transparent 50%),
        var(--bg);
    }
    .wrap {
      max-width: 520px; margin: 12vh auto; padding: 1.75rem 1.5rem;
      border: 1px solid var(--border); border-radius: 16px;
      background: var(--panel); box-shadow: 0 18px 50px rgba(0,0,0,.45);
    }
    .brand { display: flex; align-items: center; gap: .75rem; margin-bottom: .75rem; }
    .dot {
      width: 14px; height: 14px; border-radius: 50%;
      background: linear-gradient(135deg, var(--cyan), var(--purple));
      box-shadow: 0 0 16px rgba(0,243,255,.55);
    }
    h1 { margin: 0; font-size: 1.35rem; letter-spacing: .04em; }
    p { color: var(--muted); line-height: 1.65; margin: .55rem 0; }
    code {
      color: var(--cyan); background: rgba(0,243,255,.08);
      padding: .1rem .35rem; border-radius: 6px; font-size: .92em;
    }
    .actions { display: flex; flex-wrap: wrap; gap: .6rem; margin-top: 1.1rem; }
    a.btn, button.btn {
      appearance: none; border: 0; cursor: pointer;
      display: inline-flex; align-items: center; justify-content: center;
      padding: .65rem 1rem; border-radius: 10px; font-weight: 600;
      text-decoration: none; color: #fff;
      background: linear-gradient(135deg, var(--purple), #4f46e5);
    }
    a.btn.secondary, button.btn.secondary {
      background: transparent; color: var(--text);
      border: 1px solid rgba(255,255,255,.14);
    }
    #status { font-size: .92rem; min-height: 1.4em; }
    .ok { color: #22c55e; }
    .warn { color: #f59e0b; }
    .bad { color: var(--pink); }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="brand"><span class="dot"></span><h1>DockerOps</h1></div>
    <p>正在打开控制台… 若长时间黑屏，请点击下方按钮（将使用当前飞牛主机名 + 端口，而不是 127.0.0.1）。</p>
    <p>目标：<code id="target">…</code></p>
    <p id="status" class="warn">检测中…</p>
    <div class="actions">
      <a class="btn" id="go" href="#" target="_top" rel="noopener">打开 DockerOps</a>
      <button type="button" class="btn secondary" id="retry">重新检测</button>
    </div>
    <p style="margin-top:1.2rem;font-size:.85rem">首次使用请在 Web 界面设置管理员，或安装时预置 <code>DOCKEROPS_ADMIN_PASSWORD</code>。</p>
  </div>
  <script>
    (function () {
      var PORT = "${PORT}";
      function buildUrl() {
        var host = window.location.hostname || "127.0.0.1";
        // Prefer current page protocol; DockerOps serves plain HTTP by default
        var proto = "http:";
        return proto + "//" + host + ":" + PORT + "/";
      }
      function setStatus(cls, text) {
        var el = document.getElementById("status");
        el.className = cls;
        el.textContent = text;
      }
      function apply() {
        var url = buildUrl();
        document.getElementById("target").textContent = url;
        document.getElementById("go").href = url;
        return url;
      }
      function tryOpen(auto) {
        var url = apply();
        // Health probe via no-cors image/fetch — success means service is up
        var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
        var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, 2500);
        var opts = { mode: "no-cors", cache: "no-store" };
        if (ctrl) opts.signal = ctrl.signal;
        fetch(url + "api/health", opts).then(function () {
          clearTimeout(timer);
          setStatus("ok", "服务可达，正在跳转…");
          if (auto) {
            try { window.top.location.href = url; }
            catch (e) { window.location.href = url; }
          }
        }).catch(function () {
          clearTimeout(timer);
          setStatus("warn", "暂未探测到服务（容器可能仍在启动）。可稍后重试，或直接点「打开 DockerOps」。");
          if (auto) {
            // Still attempt open after short delay — container may be healthy for browser even if fetch blocked
            setTimeout(function () {
              try { window.top.location.href = url; }
              catch (e) { /* stay on launcher */ }
            }, 1200);
          }
        });
      }
      document.getElementById("retry").addEventListener("click", function () { tryOpen(false); });
      apply();
      tryOpen(true);
    })();
  </script>
</body>
</html>
EOF
