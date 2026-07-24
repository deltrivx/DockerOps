/**
 * Canvas particles — adapted from deltrivx/solidjs blog Particles.jsx
 * Purple cyber particles with mouse repulsion; Unraid-friendly dark UI.
 */
(function () {
  const canvas = document.getElementById("particles");
  if (!canvas) return;

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) {
    canvas.style.display = "none";
    return;
  }

  const ctx = canvas.getContext("2d");
  let particles = [];
  let mouse = { x: null, y: null };
  let animationId = 0;
  let running = true;

  function resizeCanvas() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
  }

  class Particle {
    constructor() {
      this.reset();
    }
    reset() {
      this.x = Math.random() * canvas.width;
      this.y = Math.random() * canvas.height;
      this.size = Math.random() * 2 + 0.5;
      this.speedX = (Math.random() - 0.5) * 0.5;
      this.speedY = (Math.random() - 0.5) * 0.5;
      this.opacity = Math.random() * 0.5 + 0.1;
    }
    update() {
      this.x += this.speedX;
      this.y += this.speedY;
      if (mouse.x !== null) {
        const dx = mouse.x - this.x;
        const dy = mouse.y - this.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 120 && dist > 0.01) {
          const force = (120 - dist) / 120;
          this.x -= (dx / dist) * force * 1.5;
          this.y -= (dy / dist) * force * 1.5;
        }
      }
      if (this.x < 0 || this.x > canvas.width || this.y < 0 || this.y > canvas.height) {
        this.reset();
      }
    }
    draw() {
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
      // blog accent purple + slight cyan mix by opacity
      ctx.fillStyle = `rgba(108, 92, 231, ${this.opacity})`;
      ctx.fill();
    }
  }

  function initParticles() {
    particles = [];
    const count = Math.min(120, Math.floor((canvas.width * canvas.height) / 12000));
    for (let i = 0; i < count; i++) particles.push(new Particle());
  }

  function animate() {
    if (!running) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const p of particles) {
      p.update();
      p.draw();
    }
    animationId = requestAnimationFrame(animate);
  }

  resizeCanvas();
  initParticles();
  animate();

  window.addEventListener("resize", () => {
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

  // pause when tab hidden
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      running = false;
      cancelAnimationFrame(animationId);
    } else {
      running = true;
      animate();
    }
  });

  // mouse spotlight CSS vars (blog App.jsx)
  const spot = document.getElementById("spotlight");
  if (spot) {
    document.addEventListener("mousemove", (e) => {
      document.documentElement.style.setProperty("--mouse-x", `${e.clientX}px`);
      document.documentElement.style.setProperty("--mouse-y", `${e.clientY}px`);
    });
  }
})();
