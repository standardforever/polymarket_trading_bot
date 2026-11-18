const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const lerp = (a, b, t) => a + (b - a) * t;
document.documentElement.classList.remove("no-js");

/* Shared: the live-trading snapshot used by "Why" and "Results". */
const snapshot = fetch("data/snapshot.json").then((r) => (r.ok ? r.json() : null)).catch(() => null);

/* ───────── FAQ ───────── */
const FAQ = [
  ["What is Five?", "Five is an automated trader for Polymarket’s 5-minute Bitcoin Up or Down markets. Every five minutes it works out, with math, whether to place a trade or sit the round out."],
  ["Where does it run?", "On your own computer. You clone the code from GitHub and start it with Docker. Nothing runs on our servers."],
  ["How do I set it up?", "Clone the repository, copy the example settings file, paste in your Polymarket keys, and run docker compose up. Then open the dashboard and press Start."],
  ["Is my money safe?", "Your funds stay in your own Polymarket account. Your keys stay in a settings file on your computer and are never sent to the browser."],
  ["Can I lose money?", "Yes. Trading always involves risk and Five can’t promise profits. Only use money you can afford to lose."],
  ["Do I need to watch it?", "No. Start it and check the dashboard whenever you like. You can stop it at any time."],
];
$("faqList").innerHTML = FAQ.map(([q, a], i) => `<details${i === 0 ? " open" : ""}><summary>${q}</summary><p>${a}</p></details>`).join("");

/* ───────── Scroll: progress bar, nav, dashboard frame ───────── */
const nav = $("nav"), progress = $("progress"), frame = $("frame"), frameWrap = $("frameWrap");
let ticking = false;
function onScroll() {
  ticking = false;
  const y = scrollY, max = document.documentElement.scrollHeight - innerHeight;
  progress.style.width = `${max > 0 ? (y / max) * 100 : 0}%`;
  nav.classList.toggle("stuck", y > 8);
  if (!reduceMotion && frame) {
    const r = frameWrap.getBoundingClientRect();
    const p = clamp(1 - r.top / (innerHeight * 0.95), 0, 1);
    frame.style.transform = `rotateX(${(1 - p) * 16}deg) scale(${0.92 + p * 0.08})`;
  }
}
addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
onScroll();

const revealer = new IntersectionObserver((entries) => entries.forEach((e) => {
  if (e.isIntersecting) { e.target.classList.add("in"); revealer.unobserve(e.target); }
}), { threshold: 0.15 });
$$(".reveal").forEach((el) => revealer.observe(el));

/* ───────── How it works: a real local setup, pinned while you scroll ───────── */
(function howItWorks() {
  const section = $("how");
  if (!section) return;
  const steps = $$(".how-step", section), stage = $("howStage"), lines = $("termLines");
  const REPO = "https://github.com/standardforever/polymarket_trading_bot";
  // One continuous terminal session. Each step adds its own lines.
  const SCRIPT = {
    1: [["cmd", `git clone ${REPO}`], ["out", "Cloning into 'polymarket_trading_bot'..."], ["out", "Receiving objects: 100%, done."], ["cmd", "cd polymarket_trading_bot"]],
    2: [["gap"], ["cmd", "cp .env.example .env"], ["cmd", "nano .env"], ["file", "# Your Polymarket keys. They stay on this computer."], ["key", "POLYMARKET_API_KEY=", "••••••••••••••••"], ["key", "POLYMARKET_API_SECRET=", "••••••••••••••••"], ["key", "POLYMARKET_API_PASSPHRASE=", "••••••••••••"], ["key", "POLYMARKET_PRIVATE_KEY=", "••••••••••••••••••••"]],
    3: [["gap"], ["cmd", "docker compose up --build"], ["ok", "✔ Container polymarket_trading_bot-api-1  Started"], ["ok", "✔ Container polymarket_trading_bot-ui-1   Started"], ["out", "api-1  | Application startup complete."]],
  };
  const typed = new Set(["cmd", "key"]);
  const pinned = matchMedia("(min-width: 961px)");
  let current = 0, timer = 0, seen = false, autoTimer = 0;

  const text = (l) => (l[0] === "key" ? l[1] + l[2] : l[1] || "");
  function paint(el, l, count) {
    el.textContent = "";
    if (l[0] === "cmd") {
      const p = document.createElement("span"); p.className = "p"; p.textContent = "$";
      el.append(p, document.createTextNode(l[1].slice(0, count)));
    } else if (l[0] === "key") {
      const t = text(l).slice(0, count);
      const k = document.createElement("span"); k.textContent = t.slice(0, l[1].length);
      const v = document.createElement("span"); v.className = "v"; v.textContent = t.slice(l[1].length);
      el.append(k, v);
    } else if (l[0] !== "gap") el.textContent = l[1];
  }
  const body = lines.parentElement;
  const follow = () => { body.scrollTop = body.scrollHeight; body.classList.toggle("scrolled", body.scrollTop > 0); };
  const addLine = (l) => { const el = document.createElement("div"); el.className = `t-${l[0]}`; lines.append(el); follow(); return el; };
  const cursor = () => { const c = document.createElement("div"); c.className = "t-cursor"; lines.append(c); follow(); };

  // Draw steps before n instantly, then type step n live.
  function playTerminal(n) {
    clearTimeout(timer);
    lines.textContent = ""; follow();
    const upto = Math.min(n, 3);
    for (let s = 1; s < upto; s++) SCRIPT[s].forEach((l) => paint(addLine(l), l, Infinity));
    const todo = SCRIPT[upto];
    if (reduceMotion || n > 3) { todo.forEach((l) => paint(addLine(l), l, Infinity)); cursor(); return; }
    let i = 0, chars = 0, el = null;
    const tick = () => {
      if (i >= todo.length) { cursor(); return; }
      const l = todo[i];
      if (!el) el = addLine(l);
      if (!typed.has(l[0])) { paint(el, l, Infinity); i++; el = null; timer = setTimeout(tick, l[0] === "gap" ? 60 : 300); return; }
      chars++; paint(el, l, chars); el.classList.add("typing"); if (chars === 1 || chars % 24 === 0) follow();
      if (chars >= text(l).length) { el.classList.remove("typing"); i++; el = null; chars = 0; timer = setTimeout(tick, 380); }
      else timer = setTimeout(tick, (l[0] === "key" && chars > l[1].length ? 14 : 22) + Math.random() * 30);
    };
    timer = setTimeout(tick, 280);
  }

  // The dashboard: an illustrative round, about 14 seconds standing in for 5 minutes.
  const round = (function () {
    const canvas = $("roundCanvas"), ctx = canvas.getContext("2d");
    const clockEl = $("roundClock"), statusEl = $("roundStatus"), statusText = $("roundStatusText");
    const btn = $("dashBtn"), btnText = $("dashBtnText");
    let w = 0, h = 0, dpr = 1, running = false, visible = false, last = 0, startTimer = 0;
    const DURATION = 14, HOLD = 2.8, DECIDE = 0.6;
    let points, value, shown, acc, clock, decision, drift, state, count = 0;
    const resize = () => {
      const r = canvas.parentElement.getBoundingClientRect();
      dpr = Math.min(devicePixelRatio || 1, 2); w = Math.round(r.width); h = Math.round(r.height);
      canvas.width = w * dpr; canvas.height = h * dpr; draw();
    };
    function reset() {
      points = [{ p: 0, v: 0 }]; value = 0; shown = 0; acc = 0; clock = 0; decision = null; state = "";
      count++;
      // Most rounds are skipped; every third demo round drifts far enough to trade.
      drift = (count % 3 === 0 ? 1 : 0.22) * (Math.random() < 0.5 ? -1 : 1) * (0.9 + Math.random() * 0.4);
    }
    function setStatus(kind, side) {
      const key = kind + (side || "");
      if (state === key) return;
      state = key;
      statusEl.className = `round-status ${kind}`;
      statusText.textContent = kind === "watch" ? "Watching the round"
        : kind === "decide" ? "Checking the numbers"
        : kind === "skip" ? "Skipped. Too close to call."
        : `Trade placed: ${side}`;
    }
    function step(dt) {
      clock += dt; acc += dt;
      const p = clamp(clock / DURATION, 0, 1);
      while (acc >= 0.3 && p < 1) {
        acc -= 0.3;
        value += drift * 0.05 - value * 0.045 + (Math.random() - 0.5) * 0.22;
        value = clamp(value, -1.15, 1.15);
        points.push({ p: clamp((clock - acc) / DURATION, 0, 1), v: value });
      }
      shown = lerp(shown, value, Math.min(1, dt * 3.4));
      if (p >= DECIDE && !decision) decision = Math.abs(value) > 0.55 ? { trade: true, side: value > 0 ? "Up" : "Down" } : { trade: false };
      if (p < DECIDE - 0.08) setStatus("watch");
      else if (!decision) setStatus("decide");
      else decision.trade ? setStatus("trade", decision.side) : setStatus("skip");
      const left = Math.round(300 * (1 - p));
      clockEl.textContent = p >= 1 ? "Round over" : `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} left`;
      if (clock > DURATION + HOLD) reset();
    }
    function draw() {
      if (!w || !points) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
      const padX = 2, top = 22, bottom = 8, ph = h - top - bottom, pw = w - padX * 2;
      const X = (p) => padX + p * pw, Y = (v) => top + ph / 2 - (v / 1.2) * (ph / 2);
      ctx.font = "500 11px Geist, sans-serif";
      const dx = X(DECIDE);
      ctx.setLineDash([3, 5]); ctx.strokeStyle = "#d4d4d4"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(dx, top); ctx.lineTo(dx, h - bottom); ctx.stroke();
      ctx.fillStyle = "#8a8a8a"; ctx.textAlign = "center"; ctx.fillText("Decision", dx, top - 8);
      const baseY = Y(0);
      ctx.setLineDash([6, 6]); ctx.strokeStyle = "#bdbdbd";
      ctx.beginPath(); ctx.moveTo(padX, baseY); ctx.lineTo(w - padX, baseY); ctx.stroke(); ctx.setLineDash([]);
      ctx.textAlign = "left"; ctx.fillText("Price to beat", padX, baseY - 8);
      if (!running) return;
      const path = [...points, { p: clamp(clock / DURATION, 0, 1), v: shown }];
      const trace = () => {
        ctx.beginPath(); ctx.moveTo(X(path[0].p), Y(path[0].v));
        for (let i = 1; i < path.length - 1; i++) {
          const mx = (X(path[i].p) + X(path[i + 1].p)) / 2, my = (Y(path[i].v) + Y(path[i + 1].v)) / 2;
          ctx.quadraticCurveTo(X(path[i].p), Y(path[i].v), mx, my);
        }
        const end = path.at(-1); ctx.lineTo(X(end.p), Y(end.v));
      };
      const end = path.at(-1);
      trace(); ctx.lineTo(X(end.p), baseY); ctx.lineTo(X(path[0].p), baseY); ctx.closePath();
      ctx.fillStyle = "rgba(10,10,10,.05)"; ctx.fill();
      trace(); ctx.strokeStyle = "#0a0a0a"; ctx.lineWidth = 2.2; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.stroke();
      if (!reduceMotion) {
        const pulse = (performance.now() % 1600) / 1600;
        ctx.beginPath(); ctx.arc(X(end.p), Y(end.v), 4 + pulse * 10, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(10,10,10,${0.35 * (1 - pulse)})`; ctx.lineWidth = 1.5; ctx.stroke();
      }
      ctx.beginPath(); ctx.arc(X(end.p), Y(end.v), 4.5, 0, Math.PI * 2); ctx.fillStyle = "#0a0a0a"; ctx.fill();
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
    }
    function loop(now) {
      requestAnimationFrame(loop);
      if (!running || !visible || document.hidden) { last = 0; return; }
      const dt = last ? Math.min(0.05, (now - last) / 1000) : 0; last = now;
      if (!reduceMotion) step(dt);
      draw();
    }
    function stop() {
      clearTimeout(startTimer); running = false;
      btn.className = "dash-btn"; btnText.textContent = "Start";
      statusEl.className = "round-status"; statusText.textContent = "Waiting for Start";
      clockEl.textContent = "5:00 left"; reset(); draw();
    }
    function start() {
      stop();
      // "Press Start": the button is pressed, then the round begins.
      startTimer = setTimeout(() => {
        btn.classList.add("press");
        setTimeout(() => {
          btn.className = "dash-btn running"; btnText.textContent = "Running";
          running = true; reset();
          if (reduceMotion) for (let i = 0; i < 160; i++) step(1 / 15);
          draw();
        }, 160);
      }, reduceMotion ? 0 : 900);
    }
    reset();
    new ResizeObserver(resize).observe(canvas.parentElement);
    new IntersectionObserver((e) => { visible = e[0].isIntersecting; }).observe(canvas.parentElement);
    requestAnimationFrame(loop);
    return { start, stop };
  })();

  function show(n, force) {
    if (n === current && !force) return;
    const prev = current;
    current = n;
    steps.forEach((s) => {
      const k = Number(s.dataset.step);
      s.classList.toggle("on", k === n); s.classList.toggle("done", k < n);
      s.querySelector("button").setAttribute("aria-current", k === n ? "step" : "false");
    });
    stage.classList.toggle("show-dash", n === 4);
    if (n === 4) { if (prev !== 4) round.start(); }
    else { if (prev === 4) round.stop(); if (seen) playTerminal(n); }
  }

  // Desktop: the section is pinned and the scroll position picks the step.
  function onPinScroll() {
    if (!pinned.matches) return;
    const r = section.getBoundingClientRect(), total = r.height - innerHeight;
    const p = clamp(-r.top / Math.max(total, 1), 0, 0.9999);
    const n = Math.floor(p * 4) + 1, local = p * 4 - (n - 1);
    steps.forEach((s) => { const k = Number(s.dataset.step); s.style.setProperty("--fill", k < n ? 1 : k === n ? local.toFixed(3) : 0); });
    show(n);
  }
  let t2 = false;
  addEventListener("scroll", () => { if (!t2) { t2 = true; requestAnimationFrame(() => { t2 = false; onPinScroll(); }); } }, { passive: true });
  addEventListener("resize", onPinScroll);

  // Phones and tablets: the steps play on their own; tapping one jumps to it.
  function autoplay() {
    clearTimeout(autoTimer);
    if (pinned.matches || reduceMotion) return;
    autoTimer = setTimeout(() => { show(current % 4 + 1); autoplay(); }, current === 4 ? 16000 : current === 2 ? 9000 : 7000);
  }
  steps.forEach((s) => s.querySelector("button").addEventListener("click", () => {
    const n = Number(s.dataset.step);
    if (pinned.matches) {
      const total = section.offsetHeight - innerHeight;
      scrollTo({ top: section.offsetTop + ((n - 1) / 4) * total + 6, behavior: reduceMotion ? "auto" : "smooth" });
    } else { show(n); autoplay(); }
  }));

  new IntersectionObserver((entries) => entries.forEach((e) => {
    if (!e.isIntersecting || seen) return;
    seen = true;
    if (current < 4) playTerminal(current);
    autoplay();
  }), { threshold: 0.2 }).observe(section);

  show(1, true);
  onPinScroll();
  pinned.addEventListener("change", () => { onPinScroll(); autoplay(); });
})();

/* ───────── Why: one day of rounds ───────── */
(function day() {
  const box = $("dayCells"), TOTAL = 288;
  box.innerHTML = "<i></i>".repeat(TOTAL);
  const cells = $$("i", box), tradedEl = $("dayTraded"), skippedEl = $("daySkipped");
  let skipRate = 0.93, real = false;
  snapshot.then((s) => {
    if (s && Number.isFinite(s.skip_rate)) {
      skipRate = s.skip_rate; real = true;
      $("dayCaption").textContent = `A day has 288 five-minute rounds. In live trading, Five sat out ${Math.round(s.skip_rate * 100)}% of them. Black squares are trades.`;
    }
  });
  function play() {
    const traded = Math.max(1, Math.round(TOTAL * (1 - skipRate)));
    // Spread the traded rounds through the day without clumping.
    const pick = new Set();
    for (let i = 0; i < traded; i++) pick.add(Math.min(TOTAL - 1, Math.floor((i + 0.2 + Math.random() * 0.6) * (TOTAL / traded))));
    cells.forEach((c, i) => c.classList.toggle("t", pick.has(i)));
    if (reduceMotion) {
      cells.forEach((c) => c.classList.add("on"));
      tradedEl.textContent = pick.size; skippedEl.textContent = TOTAL - pick.size; return;
    }
    let i = 0, t = 0;
    const per = 2200 / TOTAL;
    const stepFn = () => {
      const target = Math.min(TOTAL, i + 6);
      for (; i < target; i++) { cells[i].classList.add("on"); if (pick.has(i)) t++; }
      tradedEl.textContent = t; skippedEl.textContent = i - t;
      if (i < TOTAL) setTimeout(stepFn, per * 6);
    };
    stepFn();
  }
  const io = new IntersectionObserver((e) => {
    if (!e[0].isIntersecting) return;
    io.disconnect();
    snapshot.then(play);
  }, { threshold: 0.35 });
  io.observe($("day"));
})();

/* ───────── Dashboard tabs ───────── */
(function tabs() {
  const text = {
    live: "The live round against the price to beat, with Five’s decision and one-click controls.",
    insights: "Profit over time, wins and losses, and every recent trade.",
  };
  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $$("#tabs button").forEach((x) => { const on = x === b; x.classList.toggle("on", on); x.setAttribute("aria-selected", String(on)); });
    $("shotLive").classList.toggle("on", b.dataset.tab === "live");
    $("shotInsights").classList.toggle("on", b.dataset.tab === "insights");
    $("caption").textContent = text[b.dataset.tab];
  });
})();

/* ───────── Results ───────── */
const fmtDate = (ms) => new Date(ms).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
function countUp(el, to, format) {
  if (reduceMotion) { el.textContent = format(to); return; }
  const start = performance.now(), dur = 1600;
  const stepFn = (now) => { const t = Math.min(1, (now - start) / dur); el.textContent = format(to * (1 - Math.pow(1 - t, 3))); if (t < 1) requestAnimationFrame(stepFn); };
  requestAnimationFrame(stepFn);
}

function profitChart(values) {
  const canvas = $("profitCanvas"), ctx = canvas.getContext("2d"), tip = $("tip"), box = $("chartBox");
  const series = [0, ...values];
  let w = 0, h = 0, dpr = 1, progress = reduceMotion ? 1 : 0, hover = null;
  const pad = { l: 4, r: 4, t: 14, b: 14 };
  const resize = () => { const r = box.getBoundingClientRect(); dpr = Math.min(devicePixelRatio || 1, 2); w = Math.round(r.width); h = Math.round(r.height); canvas.width = w * dpr; canvas.height = h * dpr; draw(); };
  new ResizeObserver(resize).observe(box);
  function draw() {
    if (!w) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    const min = Math.min(...series), max = Math.max(...series), span = Math.max(max - min, 0.01);
    const X = (i) => pad.l + (i / (series.length - 1)) * (w - pad.l - pad.r);
    const Y = (v) => pad.t + (1 - (v - min) / span) * (h - pad.t - pad.b);
    ctx.strokeStyle = "#e3e3e3"; ctx.lineWidth = 1;
    for (let i = 0; i < 5; i++) { const y = pad.t + ((h - pad.t - pad.b) * i) / 4; ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke(); }
    ctx.setLineDash([5, 5]); ctx.strokeStyle = "#bdbdbd"; ctx.beginPath(); ctx.moveTo(pad.l, Y(0)); ctx.lineTo(w - pad.r, Y(0)); ctx.stroke(); ctx.setLineDash([]);
    const upTo = Math.max(1, Math.floor(progress * (series.length - 1)));
    const path = () => { ctx.beginPath(); for (let i = 0; i <= upTo; i++) (i ? ctx.lineTo(X(i), Y(series[i])) : ctx.moveTo(X(i), Y(series[i]))); };
    const g = ctx.createLinearGradient(0, 0, 0, h); g.addColorStop(0, "rgba(10,10,10,.12)"); g.addColorStop(1, "rgba(10,10,10,0)");
    path(); ctx.lineTo(X(upTo), h); ctx.lineTo(X(0), h); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
    path(); ctx.strokeStyle = "#0a0a0a"; ctx.lineWidth = 2.2; ctx.lineJoin = "round"; ctx.stroke();
    if (hover != null) {
      const i = clamp(hover, 1, upTo), x = X(i), y = Y(series[i]);
      ctx.strokeStyle = "#bdbdbd"; ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke(); ctx.setLineDash([]);
      ctx.beginPath(); ctx.arc(x, y, 5.5, 0, Math.PI * 2); ctx.fillStyle = "#0a0a0a"; ctx.fill(); ctx.strokeStyle = "#fff"; ctx.lineWidth = 2.5; ctx.stroke();
      tip.innerHTML = `<b>${series[i] >= 0 ? "+" : "−"}$${Math.abs(series[i]).toFixed(2)}</b><span>after ${i} trade${i === 1 ? "" : "s"}</span>`;
      tip.classList.add("show");
      tip.style.transform = `translate(${clamp(x + 14, 4, w - tip.offsetWidth - 4)}px, ${clamp(y - tip.offsetHeight - 14, 4, h - tip.offsetHeight - 4)}px)`;
    } else tip.classList.remove("show");
  }
  const move = (e) => { const r = canvas.getBoundingClientRect(); hover = Math.round(((e.clientX - r.left - pad.l) / (w - pad.l - pad.r)) * (series.length - 1)); draw(); };
  canvas.addEventListener("pointermove", move); canvas.addEventListener("pointerdown", move);
  canvas.addEventListener("pointerleave", () => { hover = null; draw(); });
  return {
    play() {
      if (reduceMotion) { progress = 1; draw(); return; }
      const start = performance.now();
      const stepFn = (now) => { const t = Math.min(1, (now - start) / 2400); progress = 1 - Math.pow(1 - t, 3); draw(); if (t < 1) requestAnimationFrame(stepFn); };
      requestAnimationFrame(stepFn);
    },
  };
}

snapshot.then((s) => {
  if (!s) { $("results").remove(); $$('a[href="#results"]').forEach((a) => a.remove()); return; }
  $("resSub").textContent = `${s.trades} live trades with real money, ${fmtDate(s.period.from_ms)} to ${fmtDate(s.period.to_ms)}.`;
  $("stats").innerHTML = `
    <div class="stat"><b data-v="${s.net_profit}" data-f="money">+$0.00</b><span>total profit after fees</span></div>
    <div class="stat"><b data-v="${s.win_rate * 100}" data-f="pct">0%</b><span>of trades won (${s.wins} of ${s.trades})</span></div>
    <div class="stat"><b data-v="${s.skip_rate * 100}" data-f="pct">0%</b><span>of rounds skipped</span></div>`;
  const chart = profitChart(s.profit_line);
  const formats = { money: (v) => `+$${v.toFixed(2)}`, pct: (v) => `${Math.round(v)}%` };
  const seen = new IntersectionObserver((entries) => entries.forEach((e) => {
    if (!e.isIntersecting) return;
    seen.disconnect(); chart.play();
    $$("#stats b").forEach((b) => countUp(b, Number(b.dataset.v), formats[b.dataset.f]));
  }), { threshold: 0.3 });
  seen.observe($("results"));
});
