// Ulysses page module: wires every plate, table, slider and tab, then loads the
// three.js instrument after first paint. Text and buttons work before any 3D
// loads, and keep working if it never does (the server-rendered SVG stays).
import { elevation } from "./elevation.js";
import { Chips, flatProjector, labelScene, note, lowPowerRemembered } from "./labels.js";

const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const phone = () => matchMedia("(max-width: 639px)").matches;
function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch { return false; }
}

const idle = (fn) => ("requestIdleCallback" in window ? requestIdleCallback(fn, { timeout: 300 }) : setTimeout(fn, 300));

// ---- one plate -------------------------------------------------------------------------------
class Plate {
  constructor(fig) {
    this.fig = fig;
    this.stage = fig.querySelector(".plate-stage");
    this.scene = JSON.parse(fig.querySelector(".instrument-data").textContent);
    this.chips = new Chips(fig.querySelector(".chips"));
    this.gl = null;
    this.view = phone() || reduced() || this.scene.kind === "calibration" ? "read" : "inspect";
    this.wantsView = this.view;
    this.listeners = [];
    this.bindControls();
    this.flat();
  }

  // Labels for the flat drawing: only the ones that do not need depth.
  flat() {
    const e = elevation(this.scene);
    labelScene(this.scene, e, this.chips, { flat: true });
    const project = flatProjector(this.stage);
    const place = () => this.chips.place(project, this.stage.clientWidth, this.stage.clientHeight);
    this.placeFlat = place;
    requestAnimationFrame(place);
    new ResizeObserver(() => { if (!this.gl) place(); }).observe(this.stage);
  }

  bindControls() {
    const btns = this.fig.querySelectorAll("[data-view]");
    const turn = this.fig.querySelector("[data-turn]");
    const sync = () => {
      btns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === this.wantsView)));
      if (turn) turn.setAttribute("aria-pressed", String(this.wantsView === "inspect"));
    };
    this.syncView = sync;
    btns.forEach((b) => b.addEventListener("click", () => this.setView(b.dataset.view)));
    if (turn) turn.addEventListener("click", () => this.setView(this.wantsView === "inspect" ? "read" : "inspect"));
    this.fig.addEventListener("keydown", (ev) => {
      if ((ev.key === "r" || ev.key === "R") && !ev.ctrlKey && !ev.metaKey && !ev.altKey && ev.target.tagName !== "INPUT") {
        this.setView(this.wantsView === "inspect" ? "read" : "inspect");
      }
      if (ev.key === "Escape") this.emit("highlight", null);
    });
    sync();
  }

  setView(v) {
    this.wantsView = v;
    this.syncView();
    if (this.gl) this.gl.setView(v);
  }

  on(fn) { this.listeners.push(fn); }
  emit(type, detail) { for (const fn of this.listeners) fn(type, detail); }

  highlight(seq) {
    if (this.gl) this.gl.highlight(seq);
  }
}

// ---- tables, slider, live line ------------------------------------------------------------------
function wireLooks(plate) {
  const table = document.querySelector("table[data-looks]");
  const range = document.getElementById("look-range");
  const out = document.getElementById("look-out");
  const sayEl = document.getElementById("look-say");
  if (!table) return;
  const rows = [...table.querySelectorAll("tbody tr[data-seq]")].filter((r) => !r.classList.contains("later"));
  const select = (row, { announce = true, fromSlider = false } = {}) => {
    rows.forEach((r) => r.classList.toggle("hi", r === row));
    const seq = row ? Number(row.dataset.seq) : null;
    if (plate) plate.highlight(seq);
    if (!row) return;
    if (range && !fromSlider) range.value = String(rows.indexOf(row) + 1);
    if (out) out.textContent = row.dataset.out;
    if (announce && sayEl) sayEl.textContent = row.dataset.say;
  };
  rows.forEach((r, i) => {
    r.addEventListener("focus", () => select(r));
    r.addEventListener("mouseenter", () => select(r, { announce: false }));
    r.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowDown" && rows[i + 1]) { ev.preventDefault(); rows[i + 1].focus(); }
      if (ev.key === "ArrowUp" && rows[i - 1]) { ev.preventDefault(); rows[i - 1].focus(); }
      if (ev.key === "Escape") { select(null); r.blur(); }
    });
  });
  table.addEventListener("mouseleave", () => { if (!table.contains(document.activeElement)) select(null); });
  if (range) {
    range.addEventListener("input", () => select(rows[Number(range.value) - 1], { fromSlider: true }));
    document.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => {
      const v = Math.max(1, Math.min(rows.length, Number(range.value) + Number(b.dataset.step)));
      range.value = String(v);
      select(rows[v - 1], { fromSlider: true });
    }));
  }
  if (plate) plate.on((type, detail) => {
    if (type === "highlight") select(detail === null ? null : rows.find((r) => Number(r.dataset.seq) === detail) || null);
    if (type === "pick") { const r = rows.find((x) => Number(x.dataset.seq) === detail); if (r) r.focus(); }
  });
}

function wireBoundaryTable(plate) {
  const table = document.querySelector("table[data-boundary]");
  if (!table) return;
  const rows = [...table.querySelectorAll("tbody tr[data-look]")];
  const select = (row) => {
    rows.forEach((r) => r.classList.toggle("hi", r === row));
    if (plate?.gl) plate.gl.highlightRing(row ? Number(row.dataset.look) : null, row ? row.dataset.leader : null);
  };
  rows.forEach((r, i) => {
    r.addEventListener("focus", () => select(r));
    r.addEventListener("mouseenter", () => select(r));
    r.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowDown" && rows[i + 1]) { ev.preventDefault(); rows[i + 1].focus(); }
      if (ev.key === "ArrowUp" && rows[i - 1]) { ev.preventDefault(); rows[i - 1].focus(); }
      if (ev.key === "Escape") { select(null); r.blur(); }
    });
  });
  table.addEventListener("mouseleave", () => select(null));
  if (plate) plate.on((type, detail) => {
    if (type === "pick-ring") { const r = rows.find((x) => Number(x.dataset.look) === detail); if (r) { r.scrollIntoView({ block: "nearest" }); r.focus(); } }
    if (type === "highlight" && detail === null) select(null);
  });
}

function wireHomeTable() {
  document.querySelectorAll("tr[data-href]").forEach((r) => {
    r.addEventListener("keydown", (ev) => { if (ev.key === "Enter") location.href = r.dataset.href; });
    r.addEventListener("click", (ev) => { if (ev.target.closest("a")) return; location.href = r.dataset.href; });
  });
  const rows = [...document.querySelectorAll("tr[data-href]")];
  rows.forEach((r, i) => r.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown" && rows[i + 1]) { ev.preventDefault(); rows[i + 1].focus(); }
    if (ev.key === "ArrowUp" && rows[i - 1]) { ev.preventDefault(); rows[i - 1].focus(); }
  }));
}

function wireCopy() {
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Copied"; }
    catch { b.textContent = "Select"; const s = b.previousElementSibling; if (s) getSelection().selectAllChildren(s); }
    setTimeout(() => { b.textContent = "Copy"; }, 1600);
  }));
}

function wireTabs() {
  const list = document.querySelector("[role=tablist]");
  if (!list) return;
  const tabs = [...list.querySelectorAll("[role=tab]")].filter((t) => t.getAttribute("aria-disabled") !== "true");
  tabs.forEach((t, i) => t.addEventListener("keydown", (ev) => {
    const n = ev.key === "ArrowRight" ? 1 : ev.key === "ArrowLeft" ? -1 : 0;
    if (!n) return;
    ev.preventDefault();
    const next = tabs[(i + n + tabs.length) % tabs.length];
    next.focus();
  }));
}

// Calibration: the look slider and the filter drive the fan; the counter counts the same first exits.
function wireCalibration(plate) {
  const range = document.getElementById("cal-look");
  if (!range || !plate) return;
  const out = document.getElementById("cal-out");
  const counter = document.getElementById("cal-counter");
  const paths = plate.scene.paths || [];
  const looks = plate.scene.looks;
  const update = () => {
    const n = Number(range.value);
    const b = paths.filter((r) => r.banner && r.banner.look <= n).length;
    const c = paths.filter((r) => r.contract && r.contract.look <= n).length;
    out.textContent = `${n} of ${looks}`;
    counter.innerHTML = `first-look rule calls so far <b>${b}</b> · contract calls <b>${c}</b>`;
    plate.gl?.setFan({ look: n });
  };
  range.addEventListener("input", update);
  document.querySelectorAll("input[name=cal-filter]").forEach((r) => r.addEventListener("change", () => plate.gl?.setFan({ filter: Number(r.value) })));
}

// Card: poll quietly for a newer look on the same experiment; Flagsmith refreshes at most every five minutes.
function pollCard() {
  const meta = document.querySelector("[data-poll]");
  if (!meta) return;
  const url = meta.dataset.poll;
  let timer = null;
  const tick = async () => {
    if (document.hidden) return;
    const ro = document.querySelector(".readout");
    try {
      ro?.setAttribute("aria-busy", "true");
      const r = await fetch(url, { headers: { accept: "text/html" } });
      if (!r.ok) return;
      const doc = new DOMParser().parseFromString(await r.text(), "text/html");
      const later = doc.querySelector("[data-later-id]");
      if (later) location.assign(`/card/${later.dataset.laterId}?arrived=1`);
    } catch { /* stay quiet; the next poll tries again */ } finally { ro?.setAttribute("aria-busy", "false"); }
  };
  const start = () => { if (!timer) timer = setInterval(tick, 30000); };
  const stop = () => { clearInterval(timer); timer = null; };
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  start();
}

// ---- boot ----------------------------------------------------------------------------------------
function boot() {
  wireCopy();
  wireTabs();
  wireHomeTable();
  pollCard();
  const figs = [...document.querySelectorAll("figure[data-instrument]")];
  const plates = figs.map((f) => new Plate(f));
  const main = plates[0] || null;
  wireLooks(main);
  wireBoundaryTable(main);
  wireCalibration(main);

  // sign and confirm: the scene acts only after the server has answered
  document.addEventListener("ulysses:signed", (ev) => {
    if (main?.gl && !(ev.detail?.needs || []).length) main.gl.cast().then(() => ev.detail?.done?.()); else ev.detail?.done?.();
  });
  document.addEventListener("ulysses:executed", (ev) => {
    if (main?.gl) main.gl.release().then(() => ev.detail?.done?.()); else ev.detail?.done?.();
  });

  if (!plates.length) return;
  const params = new URLSearchParams(location.search);
  const noGL = params.has("flat") || lowPowerRemembered() || !webglAvailable();
  if (noGL) {
    for (const p of plates) p.fig.dataset.flat = "true";
    if (lowPowerRemembered()) for (const p of plates) note(p, "Showing the flat drawing: this device was drawing slowly.");
    return;
  }
  idle(async () => {
    const { mountInstrument } = await import("./bell.js");
    for (const p of plates) {
      try {
        p.gl = mountInstrument(p, { reduced: reduced(), phone: phone(), debug: params.has("debug"), forceGL: params.has("gl"), arrived: params.has("arrived") });
      } catch (err) {
        console.warn("instrument: staying with the flat drawing", err);
        p.fig.dataset.flat = "true";
        note(p, "3D view unavailable: showing the flat drawing.");
      }
    }
  });
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
