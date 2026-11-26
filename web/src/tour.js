// The walk-through: a short guided build-up of the bell on a decision card, so it reads in ten seconds.
// The bar to ship appears look by look (very high at the first look, lower each look), then Flagsmith's
// dashed 95% "Winning" line, then Flagsmith's own line draws look by look, naming each siren.
// Every number in the captions comes from the page's scene JSON (formatted by the server).
// Skippable (button or Escape); ?tour=0 skips it; ?tour=1 forces it; reduced motion never auto-plays.
import { SIREN_SVG } from "./labels.js";
import { xOf } from "./elevation.js";

const SEEN = "ulysses.tour-seen";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const AFTER = {
  hold: "It holds; nothing ships.",
  win: "Past the bar: the contract fires.",
  loss: "Past the loss line: the contract keeps control.",
  horizon: "The last look, no crossing: no decision.",
  guardrail_breach: "A guardrail broke: the contract stops.",
};

export function tourSteps(scene) {
  const planned = scene.planned || [];
  const first = planned.find((p) => p.z !== null && p.z !== undefined);
  const last = planned[planned.length - 1];
  if (!first || !last) return [];
  const steps = [
    { do: "rings", k: first.look, ms: 1100, hold: 1900,
      text: `The blue rings are the bar the contract must clear to ship. At look ${first.look} it needs <b>${esc(first.needs)}</b>.` },
    { do: "rings", k: last.look, ms: 1400, hold: 1700,
      text: `Each look lowers the bar. By the last look, ${last.look}, it needs <b>${esc(last.needs)}</b>.` },
    { do: "banner", ms: 500, hold: 2100,
      text: `The dashed purple line is Flagsmith's 95%. Flagsmith's docs call anything above it <b>Winning</b>.` },
  ];
  const looks = (scene.history || []).filter((h) => h.counted && h.z !== null && h.z !== undefined && h.ctw);
  const quick = looks.length > 8;
  for (const h of looks) {
    const needs = h.needs && h.needs !== "no stop possible" ? `the contract needs ${esc(h.needs)}` : "no stop is possible yet";
    const text = h.siren
      ? `${SIREN_SVG}<b>Look ${h.look_number}: a siren.</b> Flagsmith says ${esc(h.ctw)}, which its docs call Winning. The contract needs ${esc(h.needs)}, so it holds.`
      : `Look ${h.look_number}: Flagsmith says ${esc(h.ctw)}; ${needs}. ${AFTER[h.outcome] || esc(h.word || "")}`;
    steps.push({ do: "draw", seq: h.seq, ms: quick ? 350 : 600, hold: h.siren ? 2200 : quick ? 700 : 1100, text });
  }
  return steps;
}

export class Tour {
  constructor(plate, steps) {
    this.plate = plate;
    this.steps = steps;
    this.run = 0;
    this.driver = null;
    const bar = document.createElement("div");
    bar.className = "tour-bar";
    bar.innerHTML = `<p class="tour-text" role="status" aria-live="polite"></p><span class="tour-steps" aria-hidden="true"></span><button type="button" class="tour-btn"></button>`;
    plate.fig.querySelector(".plate-stage-wrap").after(bar);
    this.bar = bar;
    this.textEl = bar.querySelector(".tour-text");
    this.dots = bar.querySelector(".tour-steps");
    this.btn = bar.querySelector(".tour-btn");
    this.btn.addEventListener("click", () => (this.playing ? this.skip() : this.play({ manual: true })));
    plate.fig.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && this.playing) this.skip(); });
    this.rest();
  }

  setDriver(d) { this.driver = d; }

  rest() {
    this.playing = false;
    this.plate.fig.dataset.touring = "false";
    this.textEl.textContent = "How to read this bell, look by look: a short walk-through.";
    this.dots.innerHTML = "";
    this.btn.textContent = "Walk me through it";
  }

  static wanted(params, reduced) {
    const t = params.get("tour");
    if (t === "0" || params.has("arrived")) return false;
    if (t === "1") return true;
    if (reduced) return false;
    try { return localStorage.getItem(SEEN) !== "1"; } catch { return true; }
  }

  async play({ manual = false, reduced = false } = {}) {
    const d = this.driver;
    if (!d || !this.steps.length) return;
    const run = ++this.run;
    this.playing = true;
    this.plate.fig.dataset.touring = "true";
    this.btn.textContent = "Skip";
    this.dots.innerHTML = this.steps.map(() => "<i></i>").join("");
    const dots = [...this.dots.children];
    const slow = reduced ? 0 : 1;
    d.reset();
    try { localStorage.setItem(SEEN, "1"); } catch { /* storage unavailable */ }
    for (let i = 0; i < this.steps.length; i++) {
      const s = this.steps[i];
      if (run !== this.run) return;
      this.textEl.innerHTML = s.text;
      dots.forEach((x, j) => x.classList.toggle("on", j <= i));
      if (s.do === "rings") await d.rings(s.k, s.ms * slow);
      else if (s.do === "banner") await d.banner(s.ms * slow);
      else if (s.do === "draw") await d.drawTo(s.seq, s.ms * slow);
      if (run !== this.run) return;
      await wait(s.hold);
    }
    if (run !== this.run) return;
    d.finish();
    this.rest();
  }

  skip() {
    this.run++;
    this.driver?.finish();
    this.rest();
  }
}

// The flat drawing's driver: the same build-up on the server-rendered SVG.
export function svgDriver(plate) {
  const svg = plate.stage.querySelector("svg.elevation");
  if (!svg) return null;
  const NS = "http://www.w3.org/2000/svg";
  const vb = svg.viewBox.baseVal;
  const scene = plate.scene;
  const id = "reveal-" + Math.random().toString(36).slice(2, 8);
  const cp = document.createElementNS(NS, "clipPath");
  cp.id = id;
  const rect = document.createElementNS(NS, "rect");
  rect.setAttribute("x", vb.x); rect.setAttribute("y", vb.y); rect.setAttribute("height", vb.height); rect.setAttribute("width", vb.width);
  cp.appendChild(rect);
  const g = document.createElementNS(NS, "g");
  g.setAttribute("clip-path", `url(#${id})`);
  svg.append(cp, g);
  for (const el of svg.querySelectorAll(".thread, .bead, .hollow, .halo, .needle, .siren")) g.appendChild(el);
  const rings = [...svg.querySelectorAll(".ring, .sup")];
  const shell = [...svg.querySelectorAll(".env, .edge")];
  const banner = [...svg.querySelectorAll(".banner, .banner-fill")];
  const xOfSeq = new Map();
  for (const h of scene.history || []) xOfSeq.set(h.seq, xOf(h.t));
  let width = vb.width, anim = 0;
  const setW = (w) => { width = w; rect.setAttribute("width", Math.max(0, w)); };
  const fade = (els, on, ms) => els.forEach((el) => { el.style.transition = ms ? `opacity ${ms}ms linear` : "none"; el.style.opacity = on ? "" : "0"; });
  return {
    reset() { cancelAnimationFrame(anim); fade(rings, false, 0); fade(shell, false, 0); fade(banner, false, 0); setW(0); },
    async rings(k, ms) {
      const show = rings.filter((r) => r.style.opacity === "0" && (r.classList.contains("sup") || Number(r.dataset.k) <= k));
      show.forEach((r, i) => setTimeout(() => fade([r], true, 200), (ms * i) / Math.max(show.length, 1)));
      await wait(ms + 200);
      if (!rings.some((r) => r.style.opacity === "0")) fade(shell, true, 500);
    },
    async banner(ms) { fade(banner, true, ms); await wait(ms); },
    drawTo(seq, ms) {
      const x = xOfSeq.get(seq);
      if (x === undefined) return Promise.resolve();
      const from = width, to = x - vb.x + 0.12, t0 = performance.now();
      cancelAnimationFrame(anim);
      return new Promise((res) => {
        const step = (now) => {
          const k = ms ? Math.min(1, (now - t0) / ms) : 1;
          setW(from + (to - from) * k);
          if (k < 1) anim = requestAnimationFrame(step); else res();
        };
        anim = requestAnimationFrame(step);
      });
    },
    finish() { cancelAnimationFrame(anim); fade(rings, true, 0); fade(shell, true, 0); fade(banner, true, 0); setW(vb.width); },
  };
}
