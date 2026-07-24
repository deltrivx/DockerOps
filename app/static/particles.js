/**
 * Canvas particles — visible cyber dots for Unraid-style dark UI.
 * Controlled via window.DockerOpsParticles.applyPrefs(prefs).
 * Never captures pointer events.
 */
(function () {
  const canvas = document.getElementById("particles");
  if (!canvas) return;

  canvas.style.pointerEvents = "none";
  canvas.setAttribute("aria-hidden", "true");

  const api = {
    enabled: true,
    count: 110,
    reduceMotion: false,
    running: false,
  };

  const ctx = canvas.getContext("2d", { alpha: true });
  if (!ctx) return;

  let particles = [];
  let mouse = { x: null, y: null };
  let animationId = 0;
  let dpr = 1;

  function cssSize() {
    return {
      w: Math.max(window.innerWidth || document.documentElement.clientWidth || 1, 1),
      h: Math.max(window.innerHeight || document.documentElement.clientHeight || 1, 1),
    };
  }

  function shouldRun() {
    // Only honor explicit user reduce_motion — OS prefers-reduced-motion alone
    // no longer auto-disables (users reported "no particles on PC").
    return api.enabled && !api.reduceMotion && !document.hidden;
  }

  function resizeCanvas() {
    const { w, h } = cssSize();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
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
      const { w, h } = cssSize();
      this.x = Math.random() * w;
      this.y = Math.random() * h;
      if (!initial) {
        // re-enter from a random edge
        const edge = Math.floor(Math.random() * 4);
        if (edge === 0) {
          this.x = 0;
          this.y = Math.random() * h;
        } else if (edge === 1) {
          this.x = w;
          this.y = Math.random() * h;
        } else if (edge === 2) {
          this.x = Math.random() * w;
          this.y = 0;
        } else {
          this.x = Math.random() * w;
          this.y = h;
        }
      }
      this.size = Math.random() * 3.2 + 1.4;
      this.speedX = (Math.random() - 0.5) * 0.85;
      this.speedY = (Math.random() - 0.5) * 0.85;
      this.opacity = Math.random() * 0.45 + 0.45;
      this.hue = Math.random() > 0.5 ? "purple" : "cyan";
    }
    update() {
      const { w, h } = cssSize();
      this.x += this.speedX;
      this.y += this.speedY;
      if (mouse.x !== null) {
        const dx = mouse.x - this.x;
        const dy = mouse.y - this.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 150 && dist > 0.01) {
          const force = (150 - dist) / 150;
          this.x -= (dx / dist) * force * 2.2;
          this.y -= (dy / dist) * force * 2.2;
        }
      }
      if (this.x < -12 || this.x > w + 12 || this.y < -12 || this.y > h + 12) {
        this.reset(false);
      }
    }
    draw() {
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
      if (this.hue === "cyan") {
        ctx.fillStyle = `rgba(0, 243, 255, ${this.opacity})`;
        ctx.shadowColor = "rgba(0, 243, 255, 0.75)";
      } else {
        ctx.fillStyle = `rgba(167, 139, 250, ${this.opacity})`;
        ctx.shadowColor = "rgba(108, 92, 231, 0.8)";
      }
      ctx.shadowBlur = 12;
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  }

  function targetCount() {
    const { w, h } = cssSize();
    const area = w * h;
    const auto = Math.floor(area / 7500);
    const n = api.count || 110;
    // prefer user count; floor so large screens still dense
    return Math.max(50, Math.min(Math.max(n, Math.min(auto, 160)), 240));
  }

  function initParticles() {
    particles = [];
    const count = targetCount();
    for (let i = 0; i < count; i++) particles.push(new Particle());
  }

  function drawLinks() {
    const maxDist = 120;
    const len = particles.length;
    for (let i = 0; i < len; i++) {
      for (let j = i + 1; j < len; j++) {
        const a = particles[i];
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d = dx * dx + dy * dy;
        if (d < maxDist * maxDist) {
          const dist = Math.sqrt(d);
          const alpha = (1 - dist / maxDist) * 0.28;
          ctx.beginPath();
          ctx.strokeStyle = `rgba(140, 120, 255, ${alpha})`;
          ctx.lineWidth = 1;
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
  }

  function animate() {
    if (!api.running) return;
    const { w, h } = cssSize();
    // clear in CSS pixel space (transform already applied)
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
    canvas.style.pointerEvents = "none";
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
      const { w, h } = cssSize();
      ctx.clearRect(0, 0, w, h);
    } catch (_) {}
  }

  function applyPrefs(prefs) {
    if (!prefs || typeof prefs !== "object") {
      start();
      return;
    }
    if (typeof prefs.particles === "boolean") api.enabled = prefs.particles;
    if (typeof prefs.particles_count === "number") api.count = prefs.particles_count;
    if (typeof prefs.reduce_motion === "boolean") api.reduceMotion = prefs.reduce_motion;
    document.body.classList.toggle("reduce-motion", !!api.reduceMotion);
    document.documentElement.style.setProperty(
      "--particle-opacity",
      api.enabled && !api.reduceMotion ? "1" : "0"
    );
    // restart loop so count/enable changes apply
    stop();
    start();
  }

  window.DockerOpsParticles = { applyPrefs, start, stop, api };

  // boot
  resizeCanvas();
  start();

  window.addEventListener("resize", () => {
    if (!shouldRun()) return;
    resizeCanvas();
    initParticles();
    if (!api.running) start();
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
    spot.style.pointerEvents = "none";
    document.addEventListener("mousemove", (e) => {
      document.documentElement.style.setProperty("--mouse-x", `${e.clientX}px`);
      document.documentElement.style.setProperty("--mouse-y", `${e.clientY}px`);
    });
  }
})();
