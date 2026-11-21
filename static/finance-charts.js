/* Finances — small SVG chart kit (no library, no build step).
 *
 * Specs (see the dataviz rules): 2px lines, ≤24px bars with a 4px rounded data end,
 * area fills as a ~12% wash, hairline recessive grid, one y-axis, legend for ≥ 2 series,
 * text in ink tokens (never the series colour), hover tooltip on every chart, and a
 * « tableau » view for every chart (two of the series hues sit below 3:1 on paper).
 *
 * Categorical slots, fixed order (validated on the #fffefa paper):
 *   1 blue #2a78d6 · 2 orange #eb6834 · 3 aqua #1baf7a · 4 yellow #eda100
 */
"use strict";

const FC = {
  s: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  ink: "#25314b", soft: "#4d5872", muted: "#8b8f9b", grid: "#ece4d2", surface: "#fffefa",
  font: 'Karla, "Segoe UI", system-ui, sans-serif',
};

function fcNum(v, opts = {}) {
  if (v == null || !isFinite(v)) return "—";
  return new Intl.NumberFormat("de-CH", { maximumFractionDigits: opts.dec ?? 0, minimumFractionDigits: opts.dec ?? 0 }).format(v);
}
function fcChf(v) { return v == null || !isFinite(v) ? "—" : `${fcNum(v)} CHF`; }
function fcCompact(v) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e6) return `${fcNum(v / 1e6, { dec: a >= 1e7 ? 0 : 1 })} M`;
  if (a >= 1e4) return `${fcNum(v / 1e3)} k`;
  return fcNum(v);
}
function fcEsc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Clean axis ticks between lo and hi (≈ n ticks). */
function fcTicks(lo, hi, n = 5) {
  if (hi === lo) hi = lo + 1;
  const span = hi - lo;
  const step0 = span / Math.max(1, n);
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || 10 * mag;
  const start = Math.floor(lo / step) * step;
  const out = [];
  for (let v = start; v <= hi + step * 0.5; v += step) out.push(Math.round(v / step) * step);
  return out;
}

function fcWidth(el, fallback = 640) {
  return Math.max(280, Math.floor(el.clientWidth || el.parentElement?.clientWidth || fallback));
}

/* ------------------------------------------------------------------ tooltip */

function fcTip(el) {
  let tip = el.querySelector(".fc-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "fc-tip";
    tip.setAttribute("role", "status");
    el.appendChild(tip);
  }
  return {
    show(html, x, y) {
      tip.innerHTML = html;
      tip.style.display = "block";
      const w = tip.offsetWidth, h = tip.offsetHeight, W = el.clientWidth;
      let left = x + 14, top = y - h - 10;
      if (left + w > W - 4) left = x - w - 14;
      if (left < 4) left = 4;
      if (top < 4) top = y + 16;
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
    },
    hide() { tip.style.display = "none"; },
  };
}

function fcLegend(items) {
  if (items.length < 2) return "";
  return `<div class="fc-legend">${items.map((it) =>
    `<span><i style="background:${it.color}${it.dashed ? ";height:2px;border-radius:0" : ""}"></i>${fcEsc(it.label)}</span>`).join("")}</div>`;
}

/** Wrap a chart with a « Tableau » toggle that shows the same data as a table. */
function fcFrame(el, { title, sub, legend, table }) {
  el.classList.add("fc");
  el.innerHTML = `
    ${title ? `<div class="fc-head"><div><h4>${fcEsc(title)}</h4>${sub ? `<p>${sub}</p>` : ""}</div>
      ${table ? `<button type="button" class="fc-table-btn" aria-expanded="false">Tableau</button>` : ""}</div>` : ""}
    ${legend || ""}
    <div class="fc-plot"></div>
    ${table ? `<div class="fc-table hidden"></div>` : ""}`;
  const btn = el.querySelector(".fc-table-btn");
  if (btn) {
    btn.addEventListener("click", () => {
      const t = el.querySelector(".fc-table");
      const open = t.classList.toggle("hidden") === false;
      btn.setAttribute("aria-expanded", String(open));
      btn.textContent = open ? "Graphique" : "Tableau";
      el.querySelector(".fc-plot").classList.toggle("hidden", open);
      if (open && !t.innerHTML) {
        const [head, ...rows] = table();
        t.innerHTML = `<table><thead><tr>${head.map((h) => `<th>${fcEsc(h)}</th>`).join("")}</tr></thead>
          <tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td${i ? ' class="num"' : ""}>${fcEsc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      }
    });
  }
  return el.querySelector(".fc-plot");
}

/* ------------------------------------------------------------------ area / line (stacked or not) */

/**
 * opts: { x: [labels], series: [{label, color, values, dashed?, line?}], stacked, height,
 *         markers: [{x, label}], fmt, yZero, title, sub, band: {lo:[], hi:[], color, label} }
 */
function fcArea(el, opts) {
  // a series that is zero everywhere (no property, no pension yet…) is left out entirely
  const series = opts.series.filter((s) => s.values && s.values.some((v) => v != null && v !== 0));
  const plot = fcFrame(el, {
    title: opts.title, sub: opts.sub,
    legend: fcLegend([...(opts.band ? [{ label: opts.band.label, color: opts.band.color + "40" }] : []),
      ...series.map((s) => ({ label: s.label, color: s.color, dashed: s.dashed }))]),
    table: () => [["", ...series.map((s) => s.label)],
      ...opts.x.map((x, i) => [String(x), ...series.map((s) => fcChf(s.values[i]))])],
  });
  const W = fcWidth(el), H = opts.height || 260;
  const m = { l: 52, r: 16, t: 14, b: 28 };
  const n = opts.x.length;
  const stacks = series.map(() => new Array(n).fill(0));
  if (opts.stacked) {
    for (let i = 0; i < n; i++) {
      let pos = 0, neg = 0;
      series.forEach((s, k) => {
        const v = s.values[i] || 0;
        if (v >= 0) { pos += v; stacks[k][i] = pos; } else { neg += v; stacks[k][i] = neg; }
      });
    }
  }
  const top = (k, i) => opts.stacked ? stacks[k][i] : series[k].values[i];
  let lo = Infinity, hi = -Infinity;
  series.forEach((s, k) => s.values.forEach((v, i) => {
    if (v == null) return;
    const t = top(k, i);
    lo = Math.min(lo, t, opts.stacked ? (t - v) : t);
    hi = Math.max(hi, t);
  }));
  if (opts.band) opts.band.lo.forEach((v, i) => { lo = Math.min(lo, v); hi = Math.max(hi, opts.band.hi[i]); });
  if (opts.yZero !== false) lo = Math.min(lo, 0);
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  const ticks = fcTicks(lo, hi, 4);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
  const X = (i) => m.l + (n <= 1 ? 0 : (i / (n - 1)) * (W - m.l - m.r));
  const Y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * (H - m.t - m.b);
  const fmt = opts.fmt || fcCompact;
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${fcEsc(opts.title || "graphique")}">`;
  ticks.forEach((t) => {
    svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}" stroke="${t === 0 ? "#d4c8ad" : FC.grid}" stroke-width="1"/>`;
    svg += `<text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end" font-size="11" fill="${FC.muted}" font-family='${FC.font}'>${fmt(t)}</text>`;
  });
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor((W - m.l - m.r) / 64))));
  opts.x.forEach((x, i) => {
    // the last label only when it does not crowd the previous one
    if (i % every && !(i === n - 1 && (n - 1) % every >= every * 0.6)) return;
    svg += `<text x="${X(i)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="${FC.muted}" font-family='${FC.font}'>${fcEsc(x)}</text>`;
  });
  if (opts.band) {
    const b = opts.band;
    const d = b.hi.map((v, i) => `${i ? "L" : "M"}${X(i)},${Y(v)}`).join("") +
      b.lo.map((v, i) => `L${X(b.lo.length - 1 - i)},${Y(b.lo[b.lo.length - 1 - i])}`).join("") + "Z";
    svg += `<path d="${d}" fill="${b.color}" fill-opacity="0.16" stroke="none"/>`;
    if (b.lo2) {
      const d2 = b.hi2.map((v, i) => `${i ? "L" : "M"}${X(i)},${Y(v)}`).join("") +
        b.lo2.map((v, i) => `L${X(b.lo2.length - 1 - i)},${Y(b.lo2[b.lo2.length - 1 - i])}`).join("") + "Z";
      svg += `<path d="${d2}" fill="${b.color}" fill-opacity="0.18" stroke="none"/>`;
    }
  }
  // fills (stacked: from previous stack), then lines on top
  series.forEach((s, k) => {
    if (s.line) return;
    const base = () => Math.max(lo, 0);
    const pts = s.values.map((v, i) => [X(i), Y(top(k, i))]);
    const basePts = s.values.map((v, i) => [X(i), Y(opts.stacked ? top(k, i) - (v || 0) : base(i))]).reverse();
    const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join("") + basePts.map((p) => `L${p[0]},${p[1]}`).join("") + "Z";
    svg += `<path d="${d}" fill="${s.color}" fill-opacity="${opts.stacked ? 0.22 : 0.10}"/>`;
  });
  series.forEach((s, k) => {
    // stacked: no line where the series is zero (it would sit on top of the others)
    const off = (v) => v == null || (opts.stacked && v === 0);
    const d = s.values.map((v, i) => off(v) ? "" : `${i && !off(s.values[i - 1]) ? "L" : "M"}${X(i)},${Y(top(k, i))}`).join("");
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"${s.dashed ? ' stroke-dasharray="5 4"' : ""}/>`;
  });
  (opts.markers || []).forEach((mk) => {
    const i = opts.x.indexOf(mk.x);
    if (i < 0) return;
    svg += `<line x1="${X(i)}" x2="${X(i)}" y1="${m.t}" y2="${H - m.b}" stroke="${FC.soft}" stroke-width="1" stroke-dasharray="2 3"/>`;
    svg += `<text x="${X(i) + 4}" y="${m.t + 10}" font-size="11" fill="${FC.soft}" font-family='${FC.font}'>${fcEsc(mk.label)}</text>`;
  });
  svg += `<line class="fc-cross" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="${FC.ink}" stroke-width="1" opacity="0"/>`;
  svg += `<g class="fc-dots"></g><rect class="fc-hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/></svg>`;
  plot.innerHTML = svg;
  const tip = fcTip(el);
  const hit = plot.querySelector(".fc-hit"), cross = plot.querySelector(".fc-cross"), dots = plot.querySelector(".fc-dots");
  const move = (ev) => {
    const r = plot.querySelector("svg").getBoundingClientRect();
    const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
    const sx = px * (W / r.width);
    const i = Math.max(0, Math.min(n - 1, Math.round(((sx - m.l) / (W - m.l - m.r)) * (n - 1))));
    cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i)); cross.setAttribute("opacity", "0.35");
    dots.innerHTML = series.map((s, k) => s.values[i] == null ? "" :
      `<circle cx="${X(i)}" cy="${Y(top(k, i))}" r="4.5" fill="${s.color}" stroke="${FC.surface}" stroke-width="2"/>`).join("");
    const rows = series.slice().reverse().map((s) => s.values[i] == null ? "" :
      `<div><i style="background:${s.color}"></i>${fcEsc(s.label)}<b>${fcChf(s.values[i])}</b></div>`).join("");
    const total = opts.stacked && series.length > 1 ? `<div class="fc-tip-total">Total<b>${fcChf(series.reduce((a, s) => a + (s.values[i] || 0), 0))}</b></div>` : "";
    const band = opts.band ? `<div class="fc-tip-band">${fcEsc(opts.band.label)}<b>${fcCompact(opts.band.lo[i])} – ${fcCompact(opts.band.hi[i])}</b></div>` : "";
    const extra = opts.tipExtra ? opts.tipExtra(i) : "";
    tip.show(`<strong>${fcEsc(opts.x[i])}</strong>${rows}${total}${band}${extra}`, X(i) * (r.width / W), (ev.touches ? ev.touches[0].clientY : ev.clientY) - el.getBoundingClientRect().top);
  };
  hit.addEventListener("mousemove", move);
  hit.addEventListener("touchmove", move, { passive: true });
  hit.addEventListener("mouseleave", () => { tip.hide(); cross.setAttribute("opacity", "0"); dots.innerHTML = ""; });
}

/* ------------------------------------------------------------------ columns (grouped) */

/** opts: { x: [labels], series: [{label, color, values}], height, title, sub, fmt, refLine: {value, label} } */
function fcColumns(el, opts) {
  const series = opts.series;
  const plot = fcFrame(el, {
    title: opts.title, sub: opts.sub, legend: fcLegend(series.map((s) => ({ label: s.label, color: s.color }))),
    table: () => [["", ...series.map((s) => s.label)], ...opts.x.map((x, i) => [String(x), ...series.map((s) => opts.fmtTable ? opts.fmtTable(s.values[i]) : fcChf(s.values[i]))])],
  });
  const W = fcWidth(el), H = opts.height || 220, m = { l: 48, r: 10, t: 12, b: 28 };
  const n = opts.x.length, k = series.length;
  let lo = 0, hi = 0;
  series.forEach((s) => s.values.forEach((v) => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
  if (opts.refLine) hi = Math.max(hi, opts.refLine.value);
  const ticks = fcTicks(lo, hi || 1, 4);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
  const Y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * (H - m.t - m.b);
  const band = (W - m.l - m.r) / Math.max(1, n);
  const bw = Math.min(24, Math.max(4, (band * 0.72 - (k - 1) * 2) / k));
  const fmt = opts.fmt || fcCompact;
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${fcEsc(opts.title || "")}">`;
  ticks.forEach((t) => {
    svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}" stroke="${t === 0 ? "#d4c8ad" : FC.grid}"/>`;
    svg += `<text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end" font-size="11" fill="${FC.muted}" font-family='${FC.font}'>${fmt(t)}</text>`;
  });
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor((W - m.l - m.r) / 46))));
  for (let i = 0; i < n; i++) {
    const cx = m.l + band * (i + 0.5);
    const groupW = k * bw + (k - 1) * 2;
    series.forEach((s, j) => {
      const v = s.values[i];
      if (v == null) return;
      const x = cx - groupW / 2 + j * (bw + 2);
      const y0 = Y(0), y1 = Y(v), h = Math.abs(y0 - y1);
      const r = Math.min(4, h, bw / 2);
      const up = v >= 0;
      const d = up
        ? `M${x},${y0}V${y1 + r}Q${x},${y1} ${x + r},${y1}H${x + bw - r}Q${x + bw},${y1} ${x + bw},${y1 + r}V${y0}Z`
        : `M${x},${y0}V${y1 - r}Q${x},${y1} ${x + r},${y1}H${x + bw - r}Q${x + bw},${y1} ${x + bw},${y1 - r}V${y0}Z`;
      svg += `<path d="${d}" fill="${(opts.colorOf && opts.colorOf(v, i, j)) || s.color}" data-i="${i}" data-j="${j}" class="fc-bar"/>`;
    });
    if (i % every === 0 || (i === n - 1 && (n - 1) % every >= every * 0.6)) {
      svg += `<text x="${cx}" y="${H - 8}" text-anchor="middle" font-size="11" fill="${FC.muted}" font-family='${FC.font}'>${fcEsc(opts.x[i])}</text>`;
    }
    svg += `<rect class="fc-colhit" data-i="${i}" x="${cx - band / 2}" y="${m.t}" width="${band}" height="${H - m.t - m.b}" fill="transparent"/>`;
  }
  if (opts.refLine) {
    const y = Y(opts.refLine.value);
    svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="${FC.ink}" stroke-width="1" stroke-dasharray="4 3" opacity="0.6"/>`;
    svg += `<text x="${W - m.r}" y="${y - 5}" text-anchor="end" font-size="11" fill="${FC.soft}" font-family='${FC.font}'>${fcEsc(opts.refLine.label)}</text>`;
  }
  svg += "</svg>";
  plot.innerHTML = svg;
  const tip = fcTip(el);
  plot.querySelectorAll(".fc-colhit").forEach((h) => {
    const show = (ev) => {
      const i = +h.dataset.i;
      const rows = series.map((s) => s.values[i] == null ? "" : `<div><i style="background:${s.color}"></i>${fcEsc(s.label)}<b>${opts.fmtTip ? opts.fmtTip(s.values[i]) : fcChf(s.values[i])}</b></div>`).join("");
      const r = el.getBoundingClientRect();
      tip.show(`<strong>${fcEsc(opts.x[i])}</strong>${rows}${opts.tipExtra ? opts.tipExtra(i) : ""}`, ev.clientX - r.left, ev.clientY - r.top);
    };
    h.addEventListener("mousemove", show);
    h.addEventListener("click", show);
    h.addEventListener("mouseleave", () => tip.hide());
  });
}

/* ------------------------------------------------------------------ horizontal bars */

/** opts: { items: [{label, value, ref?, emoji?}], color, title, sub, refLabel } — one series, no legend. */
function fcHBars(el, opts) {
  const items = opts.items.filter((it) => it.value > 0 || it.ref > 0);
  const plot = fcFrame(el, {
    title: opts.title, sub: opts.sub,
    legend: opts.refLabel ? `<div class="fc-legend"><span><i style="background:${opts.color}"></i>${fcEsc(opts.valueLabel || "Réel")}</span><span><i class="fc-tick-key"></i>${fcEsc(opts.refLabel)}</span></div>` : "",
    table: () => [["", opts.valueLabel || "Réel", ...(opts.refLabel ? [opts.refLabel] : [])],
      ...items.map((it) => [it.label, fcChf(it.value), ...(opts.refLabel ? [fcChf(it.ref)] : [])])],
  });
  const max = Math.max(1, ...items.map((it) => Math.max(it.value || 0, it.ref || 0)));
  plot.innerHTML = `<div class="fc-hbars">${items.map((it, i) => `
    <div class="fc-hbar" data-i="${i}">
      <span class="fc-hbar-lbl">${it.emoji ? it.emoji + " " : ""}${fcEsc(it.label)}</span>
      <span class="fc-hbar-track">
        <span class="fc-hbar-fill" style="width:${(it.value || 0) / max * 100}%;background:${opts.color}"></span>
        ${it.ref ? `<span class="fc-hbar-ref" style="left:${it.ref / max * 100}%"></span>` : ""}
      </span>
      <span class="fc-hbar-val">${fcCompact(it.value)}</span>
    </div>`).join("")}</div>`;
  const tip = fcTip(el);
  plot.querySelectorAll(".fc-hbar").forEach((row) => {
    row.addEventListener("mousemove", (ev) => {
      const it = items[+row.dataset.i];
      const r = el.getBoundingClientRect();
      tip.show(`<strong>${fcEsc(it.label)}</strong><div>${fcEsc(opts.valueLabel || "Réel")}<b>${fcChf(it.value)}</b></div>${it.ref ? `<div>${fcEsc(opts.refLabel)}<b>${fcChf(it.ref)}</b></div>` : ""}`, ev.clientX - r.left, ev.clientY - r.top);
    });
    row.addEventListener("mouseleave", () => tip.hide());
  });
}

/* ------------------------------------------------------------------ segmented bar (one whole, parts) */

function fcSegmented(el, parts, opts = {}) {
  const tot = parts.reduce((a, p) => a + Math.max(0, p.value), 0) || 1;
  el.classList.add("fc");
  el.innerHTML = `${opts.title ? `<div class="fc-head"><div><h4>${fcEsc(opts.title)}</h4>${opts.sub ? `<p>${opts.sub}</p>` : ""}</div></div>` : ""}
    <div class="fc-seg">${parts.filter((p) => p.value > 0).map((p, i) =>
      `<span style="flex:${p.value / tot};background:${p.color}" data-i="${i}" title="${fcEsc(p.label)} · ${fcChf(p.value)}"></span>`).join("")}</div>
    <div class="fc-seg-legend">${parts.filter((p) => p.value > 0).map((p) =>
      `<span><i style="background:${p.color}"></i>${fcEsc(p.label)} <b>${Math.round(p.value / tot * 100)} %</b></span>`).join("")}</div>`;
}

/* ------------------------------------------------------------------ sankey (income → uses) */

/** opts: { sources: [{label, value}], targets: [{label, value, color}], title, sub } — two columns. */
function fcSankey(el, opts) {
  const plot = fcFrame(el, {
    title: opts.title, sub: opts.sub,
    table: () => [["Flux", "Montant"], ...opts.sources.map((s) => ["↦ " + s.label, fcChf(s.value)]), ...opts.targets.map((t) => [t.label + " ↤", fcChf(t.value)])],
  });
  const W = fcWidth(el), gap = 6;
  const srcTot = opts.sources.reduce((a, s) => a + s.value, 0) || 1;
  const tgtTot = opts.targets.reduce((a, s) => a + s.value, 0) || 1;
  const tot = Math.max(srcTot, tgtTot);
  const SLOT = 17; // minimum vertical room per node, so labels never collide
  const H = Math.max(220, 26 * Math.max(opts.sources.length, opts.targets.length) + 40);
  const usable = H - 20 - (gap + SLOT) * Math.max(opts.sources.length, opts.targets.length);
  const k = Math.max(0.0001, usable) / tot;
  const nodeW = 12, lx = Math.min(150, W * 0.28), rx = W - Math.min(170, W * 0.32);
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${fcEsc(opts.title || "")}">`;
  let y = 10;
  const src = opts.sources.map((s) => { const n = { ...s, y, h: Math.max(2, s.value * k) }; y += Math.max(n.h, SLOT) + gap; return n; });
  y = 10;
  const tgt = opts.targets.map((t) => { const n = { ...t, y, h: Math.max(2, t.value * k) }; y += Math.max(n.h, SLOT) + gap; return n; });
  // ribbons: each source split proportionally over targets (pooled money)
  const srcOff = src.map(() => 0), tgtOff = tgt.map(() => 0);
  src.forEach((s, i) => {
    tgt.forEach((t, j) => {
      const v = s.value * (t.value / tgtTot);
      const hS = v * k, hT = v * k;
      const y0 = s.y + srcOff[i], y1 = t.y + tgtOff[j];
      srcOff[i] += hS; tgtOff[j] += hT;
      const x0 = lx + nodeW, x1 = rx, cx = (x0 + x1) / 2;
      svg += `<path class="fc-rib" data-s="${i}" data-t="${j}" data-v="${v}" d="M${x0},${y0}C${cx},${y0} ${cx},${y1} ${x1},${y1}L${x1},${y1 + hT}C${cx},${y1 + hT} ${cx},${y0 + hS} ${x0},${y0 + hS}Z" fill="${t.color}" fill-opacity="0.22"/>`;
    });
  });
  src.forEach((s) => {
    svg += `<rect x="${lx}" y="${s.y}" width="${nodeW}" height="${s.h}" rx="2" fill="${FC.ink}" opacity="0.75"/>`;
    svg += `<text x="${lx - 8}" y="${s.y + s.h / 2 + 4}" text-anchor="end" font-size="12" fill="${FC.ink}" font-family='${FC.font}'>${fcEsc(s.label)} <tspan fill="${FC.muted}">${fcCompact(s.value)}</tspan></text>`;
  });
  tgt.forEach((t) => {
    svg += `<rect x="${rx}" y="${t.y}" width="${nodeW}" height="${t.h}" rx="2" fill="${t.color}"/>`;
    svg += `<text x="${rx + nodeW + 8}" y="${t.y + t.h / 2 + 4}" font-size="12" fill="${FC.ink}" font-family='${FC.font}'>${fcEsc(t.label)} <tspan fill="${FC.muted}">${fcCompact(t.value)}</tspan></text>`;
  });
  svg += "</svg>";
  plot.innerHTML = svg;
  const tip = fcTip(el);
  plot.querySelectorAll(".fc-rib").forEach((p) => {
    p.addEventListener("mousemove", (ev) => {
      const s = opts.sources[+p.dataset.s], t = opts.targets[+p.dataset.t];
      const r = el.getBoundingClientRect();
      p.setAttribute("fill-opacity", "0.45");
      tip.show(`<strong>${fcEsc(s.label)} → ${fcEsc(t.label)}</strong><div>Montant<b>${fcChf(+p.dataset.v)}</b></div><div>Part de ${fcEsc(t.label)}<b>${Math.round(t.value / tgtTot * 100)} %</b></div>`, ev.clientX - r.left, ev.clientY - r.top);
    });
    p.addEventListener("mouseleave", () => { p.setAttribute("fill-opacity", "0.22"); tip.hide(); });
  });
}

/* ------------------------------------------------------------------ the mountain (illustration, not data) */

/**
 * The climb from the lake (372 m) to the Pointe Dufour (4'634 m).
 * opts: { progress 0..1, peaks: [{name, meters, progress}], label }
 */
function fcMountain(el, opts) {
  const W = fcWidth(el, 760), H = Math.round(Math.min(300, Math.max(200, W * 0.36)));
  const p = Math.max(0, Math.min(1, opts.progress || 0));
  // ridge: a path from bottom-left to the summit on the right, through the peaks
  const peaks = opts.peaks || [];
  const px = (t) => 24 + t * (W - 90);
  const py = (t) => H - 26 - t * (H - 70);
  const pts = [[px(0), py(0)]];
  peaks.forEach((pk, i) => {
    if (i === 0) return;
    const t = pk.progress;
    const prev = peaks[i - 1].progress;
    const dip = prev + (t - prev) * 0.55;
    pts.push([px(dip), py(dip) + 10 + (i % 2) * 8]);
    pts.push([px(t), py(t)]);
  });
  const ridge = pts.map((q, i) => `${i ? "L" : "M"}${q[0].toFixed(1)},${q[1].toFixed(1)}`).join("");
  const at = (t) => {
    // position on the ridge polyline for progress t (by x)
    const x = px(t);
    for (let i = 1; i < pts.length; i++) {
      if (pts[i][0] >= x) {
        const a = pts[i - 1], b = pts[i];
        const f = (x - a[0]) / (b[0] - a[0] || 1);
        return [x, a[1] + f * (b[1] - a[1])];
      }
    }
    return pts[pts.length - 1];
  };
  const climber = at(p);
  const doneIdx = pts.findIndex((q) => q[0] > climber[0]);
  const done = pts.slice(0, doneIdx < 0 ? pts.length : doneIdx).concat([climber]);
  const trail = done.map((q, i) => `${i ? "L" : "M"}${q[0].toFixed(1)},${q[1].toFixed(1)}`).join("");
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${fcEsc(opts.label || "")}">
    <defs>
      <linearGradient id="fc-sky" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#e4e9f5"/><stop offset="1" stop-color="#fffefa"/></linearGradient>
      <linearGradient id="fc-rock" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#c9cfdc"/><stop offset="1" stop-color="#eef0e6"/></linearGradient>
    </defs>
    <rect width="${W}" height="${H}" fill="url(#fc-sky)" rx="8"/>
    <path d="M0,${H - 36} Q${W * 0.25},${H - 120} ${W * 0.45},${H - 70} T${W},${H - 110} V${H} H0Z" fill="#dfe6d5" opacity="0.7"/>
    <path d="${ridge} L${W - 66},${H} L${px(0)},${H} Z" fill="url(#fc-rock)" stroke="#9aa3b5" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="M${px(1) - 34},${py(1) + 26} L${px(1)},${py(1)} L${px(1) + 30},${py(1) + 30} L${px(1) + 12},${py(1) + 22} L${px(1) - 4},${py(1) + 32} L${px(1) - 18},${py(1) + 24} Z" fill="#fff" opacity="0.9"/>
    <rect x="0" y="${H - 22}" width="${W}" height="22" fill="#b9d3e8" opacity="0.65"/>
    <path d="${trail}" fill="none" stroke="#eb6834" stroke-width="3" stroke-linecap="round" stroke-dasharray="1 7"/>`;
  // labels: the summit first, then right to left, skipping any that would collide
  const placed = [[climber[0] - 16, climber[0] + 16, climber[1]]];
  const fits = (x0, x1, y) => !placed.some(([a, b, yy]) => Math.abs(yy - y) < 13 && x0 < b + 4 && x1 > a - 4);
  const fs = W > 600 ? 11 : 10;
  const labels = [];
  for (let i = peaks.length - 1; i >= 1; i--) {
    const pk = peaks[i];
    const q = at(pk.progress);
    const name = pk.name.replace("Cathédrale de ", "");
    const w = name.length * fs * 0.56;
    const ly = i === peaks.length - 1 ? q[1] - 30 : q[1] - 10;
    const x0 = Math.min(Math.max(q[0] - w / 2, 4), W - w - 4);
    if (i === peaks.length - 1 || fits(x0, x0 + w, ly)) {
      placed.push([x0, x0 + w, ly]);
      labels.push({ x: x0 + w / 2, y: ly, name, reached: pk.progress <= p + 1e-9 });
    }
  }
  peaks.forEach((pk, i) => {
    if (i === 0) return;
    const q = at(pk.progress);
    const reached = pk.progress <= p + 1e-9;
    svg += `<circle cx="${q[0]}" cy="${q[1]}" r="${i === peaks.length - 1 ? 6 : 4}" fill="${reached ? "#25314b" : "#fffefa"}" stroke="#25314b" stroke-width="1.5"><title>${fcEsc(pk.name)} · ${fcNum(pk.meters)} m</title></circle>`;
  });
  labels.forEach((l) => {
    svg += `<text x="${l.x}" y="${l.y}" text-anchor="middle" font-size="${fs}" fill="${l.reached ? FC.ink : FC.muted}" font-family='${FC.font}' paint-order="stroke" stroke="#f2f4f8" stroke-width="3">${fcEsc(l.name)}</text>`;
  });
  const flag = at(1);
  svg += `<path d="M${flag[0]},${flag[1] - 6} v-18 l14,5 l-14,5" fill="#e34948" stroke="#25314b" stroke-width="1.2"/>`;
  svg += `<g class="fc-climber" transform="translate(${climber[0]},${climber[1]})">
      <circle r="13" fill="#fffefa" stroke="#25314b" stroke-width="2"/>
      <text y="5" text-anchor="middle" font-size="15">🧗</text></g>`;
  svg += `</svg>`;
  el.innerHTML = svg;
}

/* ------------------------------------------------------------------ meter */

function fcMeter(value, max, color = FC.s[0]) {
  const p = Math.max(0, Math.min(1, max ? value / max : 0));
  return `<span class="fc-meter"><span style="width:${p * 100}%;background:${color}"></span></span>`;
}

/* ------------------------------------------------------------------ confetti */

function fcConfetti() {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const box = document.createElement("div");
  box.className = "fc-confetti";
  const colors = FC.s;
  for (let i = 0; i < 90; i++) {
    const s = document.createElement("i");
    s.style.left = `${Math.random() * 100}%`;
    s.style.background = colors[i % colors.length];
    s.style.animationDelay = `${Math.random() * 0.6}s`;
    s.style.animationDuration = `${1.8 + Math.random() * 1.4}s`;
    s.style.transform = `rotate(${Math.random() * 360}deg)`;
    box.appendChild(s);
  }
  document.body.appendChild(box);
  setTimeout(() => box.remove(), 3600);
}
