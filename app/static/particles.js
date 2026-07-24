/**
 * Canvas particles — denser/brighter for Unraid-style dark UI.
 * Controlled via window.DockerOpsParticles.applyPrefs(prefs).
 */
(function () {
  const canvas = document.getElementById("particles");
  if (!canvas) return;

  const api = {
    enabled: true,
    count: 90,
    reduceMotion: false,
    running: false,
  };

  const ctx = canvas.getContext("2d");
  let particles = [];
  let mouse = { x: null, y: null };
  let animationId = 0;

  function systemReduce() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function shouldRun() {
    return api.enabled && !api.reduceMotion && !systemReduce() && !document.hidden;
  }

  function resizeCanvas() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = window.innerWidth;
    const h = window.innerHeight;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  class Particle {
    constructor() {
      this.reset(true);
    }
    reset(initial) {
      const w = window.innerWidth;
      const h = window.innerHeight;
      this.x = Math.random() * w;
      this.y = initial ? Math.random() * h : Math.random() > 0.5 ? 0 : h;
      if (!initial && Math.random() > 0.5) {
        this.x = Math.random() > 0.5 ? 0 : w;
        this.y = Math.random() * h;
      }
      this.size = Math.random() * 2.8 + 1.0;
      this.speedX = (Math.random() - 0.5) * 0.7;
      this.speedY = (Math.random() - 0.5) * 0.7;
      this.opacity = Math.random() * 0.55 + 0.35;
      this.hue = Math.random() > 0.55 ? "purple" : "cyan";
    }
    update() {
      const w = window.innerWidth;
      const h = window.innerHeight;
      this.x += this.speedX;
      this.y += this.speedY;
      if (mouse.x !== null) {
        const dx = mouse.x - this.x;
        const dy = mouse.y - this.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 140 && dist > 0.01) {
          const force = (140 - dist) / 140;
          this.x -= (dx / dist) * force * 2;
          this.y -= (dy / dist) * force * 2;
        }
      }
      if (this.x < -10 || this.x > w + 10 || this.y < -10 || this.y > h + 10) {
        this.reset(false);
      }
    }
    draw() {
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
      if (this.hue === "cyan") {
        ctx.fillStyle = `rgba(0, 243, 255, ${this.opacity})`;
      } else {
        ctx.fillStyle = `rgba(167, 139, 250, ${this.opacity})`;
      }
      ctx.shadowBlur = 8;
      ctx.shadowColor = this.hue === "cyan" ? "rgba(0,243,255,0.55)" : "rgba(108,92,231,0.6)";
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  }

  function targetCount() {
    const area = window.innerWidth * window.innerHeight;
    const auto = Math.floor(area / 9000);
    const n = api.count || 90;
    return Math.max(30, Math.min(n, auto, 220));
  }

  function initParticles() {
    particles = [];
    const count = targetCount();
    for (let i = 0; i < count; i++) particles.push(new Particle());
  }

  function drawLinks() {
    const maxDist = 110;
    for (let i = 0; i < particles.length; i++) {
      for (let j = i + 1; j < particles.length; j++) {
        const a = particles[i];
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d = Math.sqrt(dx * dx + dy * dy);
        if (d < maxDist) {
          const alpha = (1 - d / maxDist) * 0.18;
          ctx.beginPath();
          ctx.strokeStyle = `rgba(108, 92, 231, ${alpha})`;
          ctx.lineWidth = 0.8;
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
  }

  function animate() {
    if (!api.running) return;
    const w = window.innerWidth;
    const h = window.innerHeight;
    ctx.clearRect(0, 0, w, h);
    drawLinks();
    for (const p of particles) {
      p.update();
      p.draw();
    }
    animationId = requestAnimationFrame(animate);
  }

  function start() {
    if (!shouldRun()) {
      stop();
      canvas.style.display = "none";
      return;
    }
    canvas.style.display = "block";
    resizeCanvas();
    initParticles();
    if (!api.running) {
      api.running = true;
      animate();
    }
  }

  function stop() {
    api.running = false;
    cancelAnimationFrame(animationId);
    try {
      ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    } catch (_) {}
  }

  function applyPrefs(prefs) {
    if (!prefs || typeof prefs !== "object") return;
    if (typeof prefs.particles === "boolean") api.enabled = prefs.particles;
    if (typeof prefs.particles_count === "number") api.count = prefs.particles_count;
    if (typeof prefs.reduce_motion === "boolean") api.reduceMotion = prefs.reduce_motion;
    document.body.classList.toggle("reduce-motion", !!api.reduceMotion || systemReduce());
    document.documentElement.style.setProperty(
      "--particle-opacity",
      api.enabled && !api.reduceMotion ? "1" : "0"
    );
    start();
  }

  window.DockerOpsParticles = { applyPrefs, start, stop, api };

  resizeCanvas();
  if (shouldRun()) {
    start();
  } else {
    canvas.style.display = "none";
  }

  window.addEventListener("resize", () => {
    if (!api.running) return;
    resizeCanvas();
    initParticles();
  });
  document.addEventListener("mousemove", (e) => {
    mouse.x = e.clientX;
    mouse.y = e.clientY;
  });
  document.addEventListener("mouseleave", () => {
    mouse.x = null;
    mouse.y = null;
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else if (shouldRun()) start();
  });

  const spot = document.getElementById("spotlight");
  if (spot) {
    document.addEventListener("mousemove", (e) => {
      document.documentElement.style.setProperty("--mouse-x", `${e.clientX}px`);
      document.documentElement.style.setProperty("--mouse-y", `${e.clientY}px`);
    });
  }
})();
