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

/** Wrap a chart with a « Tableau » toggle that shows the same data as a table.
 *  `help`: trusted HTML put after the title (finance.js passes its « ? » button). */
function fcFrame(el, { title, sub, help, legend, table }) {
  el.classList.add("fc");
  el.innerHTML = `
    ${title ? `<div class="fc-head"><div><h4>${fcEsc(title)}${help || ""}</h4>${sub ? `<p>${sub}</p>` : ""}</div>
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
    title: opts.title, sub: opts.sub, help: opts.help,
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
    title: opts.title, sub: opts.sub, help: opts.help, legend: fcLegend(series.map((s) => ({ label: s.label, color: s.color }))),
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
    title: opts.title, sub: opts.sub, help: opts.help,
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
  el.innerHTML = `${opts.title ? `<div class="fc-head"><div><h4>${fcEsc(opts.title)}${opts.help || ""}</h4>${opts.sub ? `<p>${opts.sub}</p>` : ""}</div></div>` : ""}
    <div class="fc-seg">${parts.filter((p) => p.value > 0).map((p, i) =>
      `<span style="flex:${p.value / tot};background:${p.color}" data-i="${i}" title="${fcEsc(p.label)} · ${fcChf(p.value)}"></span>`).join("")}</div>
    <div class="fc-seg-legend">${parts.filter((p) => p.value > 0).map((p) =>
      `<span><i style="background:${p.color}"></i>${fcEsc(p.label)} <b>${Math.round(p.value / tot * 100)} %</b></span>`).join("")}</div>`;
}

/* ------------------------------------------------------------------ sankey (income → uses) */

/** opts: { sources: [{label, value}], targets: [{label, value, color}], title, sub } — two columns. */
function fcSankey(el, opts) {
  const plot = fcFrame(el, {
    title: opts.title, sub: opts.sub, help: opts.help,
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
  const peaks = (opts.peaks || []).slice().sort((a, b) => a.progress - b.progress);
  const f1 = (v) => v.toFixed(1);
  const line = (pts) => pts.map((q, i) => `${i ? "L" : "M"}${f1(q[0])},${f1(q[1])}`).join("");

  // Geometry. The climb runs up the left flank, from the lake shore to the summit.
  // x follows a concave curve, so the slope is gentle at the bottom and steep near the top.
  const shoreX = Math.max(40, W * 0.09), shoreY = H - 20;
  const topX = W * 0.76, topY = 30;
  const X = (t) => shoreX + (topX - shoreX) * (1.5 * t - 0.5 * t * t);
  const Y = (t) => shoreY - t * (shoreY - topY);

  // Left ridge: each checkpoint is a small top, with a col before it.
  // The col never drops below the previous top, so the line never dips into the lake.
  const ridge = [[X(0), Y(0)]];
  for (let i = 1; i < peaks.length; i++) {
    const a = peaks[i - 1].progress, b = peaks[i].progress;
    const gap = Y(a) - Y(b);
    if (gap > 4) {
      const m = (a + b) / 2;
      ridge.push([X(m), Y(m) + Math.min(9, gap * 0.45)]);
    }
    ridge.push([X(b), Y(b)]);
  }
  if (!peaks.length || peaks[peaks.length - 1].progress < 1) ridge.push([X(1), Y(1)]);
  const summit = ridge[ridge.length - 1];

  // Right flank: a shoulder, then down to the right edge.
  const rw = W - topX, rh = shoreY - topY;
  const flank = [
    [topX + rw * 0.16, topY + rh * 0.13], [topX + rw * 0.3, topY + rh * 0.08], [topX + rw * 0.52, topY + rh * 0.3],
    [topX + rw * 0.72, topY + rh * 0.36], [W, topY + rh * 0.56],
  ];
  const outline = [[shoreX - 16, H], ...ridge, ...flank, [W, H]];

  // Snow cap, above about 3'200 m. It starts where the ridge stays above the snow line for good.
  const snowY = Y(0.66);
  let k = ridge.length - 1;
  while (k > 0 && ridge[k - 1][1] < snowY) k--;
  const cross = (a, b) => [a[0] + (b[0] - a[0]) * (snowY - a[1]) / (b[1] - a[1] || 1), snowY];
  const snowLeft = k > 0 ? cross(ridge[k - 1], ridge[k]) : ridge[0];
  const fall = [summit, ...flank];
  let j = 0;
  while (j < fall.length - 1 && fall[j + 1][1] < snowY) j++;
  const snowRight = j < fall.length - 1 ? cross(fall[j], fall[j + 1]) : fall[j];
  const teeth = [];
  const nTeeth = Math.max(4, Math.round((snowRight[0] - snowLeft[0]) / 16));
  for (let i = nTeeth - 1; i >= 1; i--) {
    teeth.push([snowLeft[0] + (snowRight[0] - snowLeft[0]) * i / nTeeth, snowY + (i % 2 ? 9 : 2)]);
  }
  const snow = [snowLeft, ...ridge.slice(k), ...flank.slice(0, j), snowRight, ...teeth];

  // Shadow side: from the summit down to the base, to the right.
  const shade = [summit, ...flank, [W, H], [topX - W * 0.05, H], [topX + rw * 0.05, topY + rh * 0.45]];

  // Far range and foothills, as fixed fractions of the box.
  const far = [[0, H * 0.62], [W * 0.1, H * 0.42], [W * 0.2, H * 0.55], [W * 0.33, H * 0.3], [W * 0.45, H * 0.48],
    [W * 0.56, H * 0.36], [W * 0.66, H * 0.5], [W * 0.88, H * 0.22], [W, H * 0.4], [W, H], [0, H]];

  // Position on the ridge for a progress value (by x).
  const at = (t) => {
    const x = X(t);
    for (let i = 1; i < ridge.length; i++) {
      if (ridge[i][0] >= x - 1e-6) {
        const a = ridge[i - 1], b = ridge[i];
        const f = (x - a[0]) / (b[0] - a[0] || 1);
        return [x, a[1] + f * (b[1] - a[1])];
      }
    }
    return summit;
  };
  const me = at(p);
  const trail = ridge.filter((q) => q[0] < me[0]).concat([me]);

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${fcEsc(opts.label || "")}">
    <defs>
      <linearGradient id="fc-sky" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#dfe7f4"/><stop offset="1" stop-color="#fbf8f0"/></linearGradient>
      <linearGradient id="fc-rock" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#aab4c8"/><stop offset="0.55" stop-color="#c8cfdb"/><stop offset="1" stop-color="#dfe4d3"/></linearGradient>
      <linearGradient id="fc-lake" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#a9cbe6"/><stop offset="1" stop-color="#cfe2f1"/></linearGradient>
    </defs>
    <rect width="${W}" height="${H}" fill="url(#fc-sky)" rx="8"/>
    <path d="${line(far)}Z" fill="#d6ddeb"/>
    <path d="M0,${H * 0.8} Q${W * 0.18},${H * 0.6} ${W * 0.4},${H * 0.78} T${W * 0.8},${H * 0.82} V${H} H0Z" fill="#d9e3cc"/>
    <rect x="0" y="${shoreY}" width="${W}" height="${H - shoreY}" fill="url(#fc-lake)"/>
    <path d="M8,${shoreY + 7} h14 M${shoreX * 0.35},${shoreY + 13} h12" stroke="#fff" stroke-width="1.2" stroke-linecap="round" opacity="0.7"/>
    <path d="${line(outline)}Z" fill="url(#fc-rock)"/>
    <path d="${line(snow)}Z" fill="#fbfcfe"/>
    <path d="${line(shade)}Z" fill="#25314b" opacity="0.08"/>
    <path d="${line([...ridge, ...flank])}" fill="none" stroke="#7d889f" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${line(trail)}" fill="none" stroke="#eb6834" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="round"/>`;

  // Checkpoints (the first one is the lake shore, the start).
  peaks.forEach((pk, i) => {
    if (i === 0 || pk.progress >= 1) return;
    const q = at(pk.progress);
    const reached = pk.progress <= p + 1e-9;
    svg += `<circle cx="${f1(q[0])}" cy="${f1(q[1])}" r="3.5" fill="${reached ? FC.ink : "#fffefa"}" stroke="${FC.ink}" stroke-width="1.5"><title>${fcEsc(pk.name)} · ${fcNum(pk.meters)} m</title></circle>`;
  });

  // Labels sit above and to the left of their point: the ridge there is lower, so they never cross it.
  // Order: summit, next goal, then from the top down. A label that would touch another one is skipped.
  const fs = W > 600 ? 11 : 10;
  // boxes: [x0, x1, y0, y1, owner] — a label may touch its own dot.
  const boxes = [[me[0] - 12, me[0] + 12, me[1] - 12, me[1] + 12, -1], [summit[0] - 2, summit[0] + 18, summit[1] - 24, summit[1], -1]];
  peaks.forEach((pk, i) => { if (i) { const q = at(pk.progress); boxes.push([q[0] - 5, q[0] + 5, q[1] - 5, q[1] + 5, i]); } });
  const free = (b, i) => !boxes.some((o) => o[4] !== i && b[0] < o[1] + 3 && b[1] > o[0] - 3 && b[2] < o[3] + 1 && b[3] > o[2] - 1);
  const nextIdx = peaks.findIndex((pk) => pk.progress > p + 1e-9);
  const order = peaks.map((_, i) => i).filter((i) => i > 0).sort((a, b) => {
    const rank = (i) => (i === peaks.length - 1 ? 0 : i === nextIdx ? 1 : 2);
    return rank(a) - rank(b) || b - a;
  });
  order.forEach((i) => {
    const pk = peaks[i];
    const q = at(pk.progress);
    const name = pk.name.replace("Cathédrale de ", "");
    const w = name.length * fs * 0.55;
    const right = Math.max(q[0] - 6, w + 4);
    const base = q[1] - 7;
    const b = [right - w, right, base - fs, base + 2, i];
    if (i !== peaks.length - 1 && !free(b, i)) return;
    boxes.push(b);
    const reached = pk.progress <= p + 1e-9;
    svg += `<text x="${f1(right)}" y="${f1(base)}" text-anchor="end" font-size="${fs}" font-weight="${i === nextIdx || i === peaks.length - 1 ? 700 : 400}" fill="${reached || i === nextIdx ? FC.ink : FC.soft}" font-family='${FC.font}' paint-order="stroke" stroke="#eef2f8" stroke-width="3" stroke-linejoin="round">${fcEsc(name)}</text>`;
  });
  if (W >= 400) {
    svg += `<text x="${f1(shoreX * 0.45)}" y="${H - 5}" text-anchor="middle" font-size="9" font-style="italic" fill="#4f7397" font-family='${FC.font}'>Léman</text>`;
  }

  // Flag on the summit, and the climber: an orange dot with a soft halo (no emoji, they render badly in SVG).
  svg += `<path d="M${f1(summit[0])},${f1(summit[1])} v-22" stroke="${FC.ink}" stroke-width="1.5"/>
    <path d="M${f1(summit[0])},${f1(summit[1] - 22)} l15,5 l-15,5 Z" fill="#e34948"/>
    <g class="fc-climber" transform="translate(${f1(me[0])},${f1(me[1])})"><title>Vous êtes ici</title>
      <circle class="fc-climber-halo" r="11" fill="#eb6834" opacity="0.22"/>
      <circle r="6.5" fill="#eb6834" stroke="#fffefa" stroke-width="2.5"/></g>`;
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
