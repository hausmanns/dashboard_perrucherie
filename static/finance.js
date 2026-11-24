/* Finances / FIRE — the « Finances » tab (vanilla JS, no build step).
 *
 * Everything goes through /api/finance/* (see AGENTS.md). Charts: finance-charts.js.
 * Sub-views: Sommet (the climb) · Patrimoine · Budget · Plan · Objectifs · Documents · Réglages.
 * First visit: the onboarding wizard (#fin-wizard). Monthly ritual: « Point du mois ».
 */
"use strict";

const fin = {
  booted: false,
  tab: "summit",
  scope: "household",
  status: null,
  members: [],
  overview: null,
  projection: null,
  mc: null,
  scenarioId: null,
  tx: { q: "", month: "", category: "", uncategorized: false, offset: 0 },
  pollTimer: null,
  wz: null,
};

const FIN_TABS = [
  ["summit", "🏔️ Sommet"], ["wealth", "💰 Patrimoine"], ["budget", "🧾 Budget"], ["plan", "🧭 Plan"],
  ["goals", "🎯 Objectifs"], ["docs", "📎 Documents"], ["settings", "⚙️ Réglages"],
];

const FIN_KIND_ORDER = ["current", "savings", "invest", "crypto", "cash", "pillar3a", "lpp", "vested", "property", "mortgage", "loan", "other"];
const FIN_GROUPS = { liquid: "Disponible", pension: "Prévoyance (2e & 3e piliers)", property: "Immobilier", debt: "Dettes", other: "Autres biens" };

/* ------------------------------------------------------------------ helpers */

const chf = (v) => fcChf(v);
const kchf = (v) => v == null ? "—" : `${fcCompact(v)} CHF`;
const pct = (v, d = 0) => v == null || !isFinite(v) ? "—" : `${fcNum(v * 100, { dec: d })} %`;
const fe = (s) => fcEsc(s);

function finParse(v) {
  if (v == null) return null;
  const s = String(v).replace(/['’\s ]/g, "").replace(/CHF/i, "").replace(",", ".").trim();
  if (s === "" || s === "-") return null;
  const n = Number(s);
  return isFinite(n) ? n : null;
}

function finMoneyInput(id, value, attrs = "") {
  const v = value == null || value === "" ? "" : fcNum(value, { dec: Number.isInteger(value) ? 0 : 2 });
  return `<input id="${id}" type="text" inputmode="decimal" autocomplete="off" value="${fe(v)}" ${attrs} />`;
}

function finMember(id) { return fin.members.find((m) => m.id === id); }
function finAdults() { return fin.members.filter((m) => m.kind === "adult"); }
function finKids() { return fin.members.filter((m) => m.kind === "child"); }
function finWho(id) { const m = finMember(id); return m ? `${m.emoji} ${fe(m.name)}` : "Ménage"; }
function finAge(birth) {
  if (!birth) return null;
  const b = new Date(birth), n = new Date();
  return n.getFullYear() - b.getFullYear() - (n < new Date(n.getFullYear(), b.getMonth(), b.getDate()) ? 1 : 0);
}

function finModal(html, { wide = false } = {}) {
  finTipHide();
  const dlg = $("#fin-modal");
  dlg.classList.toggle("fin-modal-wide", wide);
  $("#fin-modal-body").innerHTML = html;
  dlg.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dlg.close()));
  if (!dlg.open) dlg.showModal();
  return dlg;
}

function finConfirm(text, { ok = "Confirmer", danger = false } = {}) {
  return new Promise((resolve) => {
    const dlg = finModal(`<h3>${text}</h3><div class="modal-actions">
      <button type="button" class="btn btn-ghost" id="fin-cf-no">Annuler</button>
      <button type="button" class="btn ${danger ? "btn-danger" : "btn-primary"}" id="fin-cf-ok">${ok}</button></div>`);
    $("#fin-cf-no").onclick = () => { dlg.close(); resolve(false); };
    $("#fin-cf-ok").onclick = () => { dlg.close(); resolve(true); };
  });
}

function finShares(owners) {
  const ids = Object.keys(owners || {});
  if (!ids.length) return `<span class="fin-owner fin-owner-joint">Commun</span>`;
  return ids.map((id) => {
    const m = finMember(+id);
    const share = owners[id];
    return `<span class="fin-owner" title="${m ? fe(m.name) : ""}">${m ? m.emoji : "?"}${ids.length > 1 ? ` ${Math.round(share * 100)} %` : ""}</span>`;
  }).join("");
}

/* ------------------------------------------------------------------ « ? » explanations
 * finQ(key) puts a small « ? » after a term. Hover, keyboard focus or a tap opens a short
 * explanation; a click (or tap) pins it, Escape or a click elsewhere closes it.
 * The texts below are static HTML written here — never user data. Any element with
 * data-tip-t / data-tip-b / data-tip-f (title, body, footer: plain text, escaped on display)
 * gets the same popup: the badges use it. */

const FIN_GLOSSARY = {
  // Sommet
  altitude: ["L'altitude", [
    "Votre chemin vers la liberté, dessiné comme une montée : du Léman (372 m) à la Pointe Dufour (4'634 m).",
    "Le calcul : l'argent <b>disponible</b> que vous avez ÷ celui qu'il faudrait pour arrêter de travailler aujourd'hui. À 50 %, vous êtes à mi-pente.",
  ]],
  level: ["Niveau et XP", [
    "Des points pour les bonnes habitudes : le point du mois, un document ajouté, des opérations classées, une quête faite.",
    "Ils ne changent aucun calcul : c'est pour la motivation.",
  ]],
  streak: ["La série 🔥", [
    "Le nombre de mois d'affilée où le <b>point du mois</b> a été fait. Un mois sauté, et la série repart de zéro.",
  ]],
  fi_year: ["Libres en", [
    "La première année où vous pourriez arrêter de travailler sans jamais manquer d'argent, jusqu'à la fin du plan (95 ans par défaut).",
    "Le plan suit chaque année : salaires, impôts vaudois et fédéraux, AVS, LPP, 3e pilier, enfants, rendements.",
    "Les trois années en dessous : 🎒 en dépensant moins, 🏔️ comme aujourd'hui, 🥂 en dépensant plus.",
  ]],
  net_worth: ["Patrimoine net", [
    "Tout ce que vous avez, moins ce que vous devez : comptes, placements, 2e et 3e piliers, logement, moins l'hypothèque et les crédits.",
  ]],
  freedom_days: ["Jours de liberté", [
    "Combien de jours vous pourriez vivre <b>sans salaire</b> avec l'argent disponible, au rythme de vos dépenses actuelles.",
    "Les 2e et 3e piliers ne comptent pas : ils sont bloqués jusqu'à la retraite.",
  ]],
  savings_rate: ["Taux d'épargne", [
    "La part de ce qui rentre qui n'est pas dépensée, sur les 12 derniers mois de relevés. 30 % = sur 100 fr. reçus, 30 restent.",
    "Il faut des relevés bancaires importés pour le mesurer. C'est le chiffre qui fait le plus bouger l'année de liberté.",
  ]],
  coast: ["Coast FI 🚡", [
    "« Coast », c'est avancer en roue libre, sans pédaler.",
    "Le montant : l'argent disponible qu'il faudrait avoir <b>aujourd'hui</b> pour pouvoir arrêter d'épargner. Vous continuez à travailler, les salaires paient juste la vie courante, et cet argent grossit seul jusqu'à 65 ans. Avec l'AVS et la LPP, la retraite à 65 ans est alors assurée.",
    "« Atteint » : vous y êtes déjà.",
  ]],
  need_now: ["Il faudrait aujourd'hui", [
    "L'argent disponible qu'il faudrait avoir pour arrêter de travailler <b>dès cette année</b>. Il doit tenir jusqu'à l'arrivée des rentes AVS et LPP et du 3e pilier.",
    "C'est le sommet de la montagne. La « règle des 3,5 % » (dépenses d'une année ÷ 3,5 %) est le calcul rapide des blogs FIRE, donné pour comparer : le plan, lui, calcule année par année.",
  ]],
  quests: ["Quêtes", [
    "Les petites choses utiles à faire maintenant : un solde à mettre à jour, un document à ajouter, le 3e pilier avant le 31 décembre…",
    "Chacune rapporte des XP. Le chiffre sur l'onglet Finances = le nombre de quêtes ouvertes.",
  ]],
  badges: ["Badges", [
    "Des étapes franchies, reconnues à partir de vos chiffres. Passez sur un badge (ou touchez-le) pour voir comment l'obtenir.",
  ]],
  milestones: ["Les étapes", [
    "Ce qui arrive, année après année : naissances, événements de vie, retraits du 3e pilier (dès 60 ans), rentes AVS, liberté financière.",
  ]],
  scope: ["Ensemble ou chacun", [
    "<b>👪 Ensemble</b> : le foyer entier.",
    "<b>Une personne</b> : comme si elle gérait ses finances seule. Ses revenus, ses comptes (et sa part des comptes communs), ses lignes de budget perso et sa part du budget commun (selon les revenus ou à parts égales, Réglages › Le foyer).",
  ]],
  // Patrimoine
  liquid: ["Disponible", [
    "L'argent utilisable à tout moment : comptes courants et épargne, titres et ETF, crypto, liquide.",
    "C'est lui qui paie les années entre l'arrêt du travail et les rentes.",
  ]],
  pension: ["Prévoyance", [
    "2e pilier (LPP, la caisse de pension), 3e pilier (3a) et libre passage.",
    "Bloqués jusqu'à la retraite (le 3a dès 60 ans). Le plan les compte quand ils deviennent utilisables.",
  ]],
  property_net: ["Immobilier net", [
    "La valeur de vos biens (le logement…) moins les dettes : hypothèque et crédits.",
  ]],
  equity_pct: ["Part en actions", [
    "La part de ce compte placée en actions. Elle fixe le rendement attendu et les hauts et les bas dans le plan.",
    "Vide = automatique : 0 % pour un compte bancaire, 100 % pour titres, ETF et crypto. Avec des positions, elle est calculée toute seule.",
  ]],
  include_in_fi: ["Compter dans le plan", [
    "Décochez pour un compte qui ne doit pas financer votre liberté, par exemple l'épargne d'un enfant.",
  ]],
  holdings: ["Positions", [
    "Une position = un titre ou un ETF, avec la quantité que vous avez.",
    "Avec un symbole Yahoo (<code>VT</code>, <code>CHSPI.SW</code>…), la valeur suit les cours toute seule, chaque jour à 18:30 : plus besoin de saisir le solde.",
  ]],
  // Budget
  income_month: ["Entrées / mois", [
    "Ce qui arrive sur vos comptes (salaires nets…), en moyenne sur les mois avec des relevés.",
    "Les virements entre vos propres comptes ne comptent pas.",
  ]],
  spending_month: ["Dépenses / mois", [
    "Ce qui sort de vos comptes, en moyenne sur les mois avec des relevés. Sans relevés : le total du budget prévu.",
  ]],
  savings_month: ["Épargne / mois", [
    "Entrées moins dépenses : ce qui reste chaque mois.",
  ]],
  budget: ["Budget prévu", [
    "Vos dépenses prévues par mois. <b>Commun</b> = ce que vous partagez (loyer, courses…). La colonne d'une personne = ce qu'elle dépense seule (sport, téléphone…).",
    "Dans la vue d'une personne : ses lignes perso + sa part du commun (selon les revenus ou à parts égales, Réglages › Le foyer). Son plan utilise ce total ; la vue Ensemble utilise tout.",
    "« réel » = la moyenne de vos relevés. Une catégorie budgétée seulement en commun compte aussi en commun dans les relevés, peu importe qui a payé.",
  ]],
  operations: ["Opérations", [
    "Les lignes de vos relevés importés. Chacune reçoit une catégorie. Corrigez-en une, et une règle est retenue pour les suivantes.",
    "Les virements entre vos propres comptes ne comptent ni comme entrée, ni comme dépense.",
    "Un gros montant payé une fois pour plusieurs mois (assurance maladie à l'année, impôts…) ? Choisissez <b>÷ 12 mois</b> à côté de sa catégorie. Il compte alors pour 1/12 dans son mois et dans chacun des 11 suivants. Les graphiques et le « réel » du budget restent lisses.",
  ]],
  // Plan
  fi_styles: ["Frugale, confortable, généreuse", [
    "Le même plan avec trois niveaux de dépenses une fois libres : 🎒 moins qu'aujourd'hui, 🏔️ comme aujourd'hui, 🥂 plus.",
    "L'année sous chaque bouton = quand ce style devient possible. Un clic choisit le style suivi partout.",
  ]],
  safe_year: ["Version prudente", [
    "Les marchés ne montent pas tous les ans. On rejoue le plan avec 1'000 marchés tirés au hasard.",
    "La version prudente = la première année d'arrêt qui marche dans au moins 85 % des cas.",
  ]],
  scenario: ["Scénario", [
    "Une variante du plan : se marier, acheter, passer à temps partiel… Le plan actuel ne change pas. Choisissez un scénario ici pour voir tout le plan avec, ou comparez-les côte à côte.",
  ]],
  proj_chart: ["Patrimoine projeté", [
    "Comment votre patrimoine évolue chaque année, si les marchés font une année « normale » à chaque fois (le rendement médian).",
    "En francs d'aujourd'hui : l'inflation est déjà retirée, 100'000 fr. en 2045 achètent autant que 100'000 fr. maintenant.",
  ]],
  mc_chart: ["Les 1'000 marchés", [
    "Le même plan, rejoué avec 1'000 suites de bonnes et de mauvaises années tirées au hasard.",
    "La ligne = le cas du milieu. La bande = 80 % des cas. Le % de réussite = la part des cas où l'argent ne manque jamais.",
  ]],
  curve_chart: ["Réussite selon l'année d'arrêt", [
    "Pour chaque année où vous pourriez arrêter : la chance de ne jamais manquer d'argent. Plus on attend, plus c'est sûr.",
  ]],
  sources_chart: ["D'où vient l'argent", [
    "Ce qui paie les dépenses, année par année : les salaires, puis les rentes AVS et LPP, les capitaux des 2e et 3e piliers, et des retraits sur votre épargne.",
  ]],
  taxes_chart: ["Impôts et cotisations", [
    "Impôt sur le revenu et la fortune (Vaud et Confédération), impôt sur les retraits en capital, et cotisations AVS.",
    "Ces cotisations AVS sont dues même sans travailler, jusqu'à 65 ans, d'après votre fortune.",
  ]],
  events: ["Événements de vie", [
    "Ce qui change votre trajectoire à une date : naissance, temps partiel, achat, mariage, départ, rentrée d'argent… Le plan en tient compte tout seul.",
  ]],
  scenarios: ["Scénarios", [
    "Des « et si… ? » à côté de votre plan, qui reste intact. « Comparer côte à côte » montre l'année de liberté, les impôts et le patrimoine de chacun.",
  ]],
  assumptions: ["Hypothèses", [
    "Les réglages du calcul : rendements, inflation, âge AVS… Les valeurs par défaut sont prudentes. Ne les changez que si vous savez pourquoi.",
    "« Réel » = après inflation.",
  ]],
  // Réglages
  communal: ["Coefficient communal", [
    "Le multiplicateur de votre commune sur l'impôt cantonal. Il figure sur votre décision de taxation.",
  ]],
  child_tax_split: ["Les enfants, côté impôts", [
    "Pour des parents non mariés : qui déduit les enfants aux impôts. 50 % = moitié chacun, le cas habituel.",
  ]],
  spending_split: ["Partager les dépenses communes", [
    "Comment partager les lignes <b>communes</b> du budget dans la vue d'une personne. Selon les revenus = qui gagne plus paie plus. À parts égales = moitié chacun.",
  ]],
};

function finQ(key) {
  const g = FIN_GLOSSARY[key];
  if (!g) return "";
  return `<button type="button" class="fin-q" data-q="${key}" aria-expanded="false" aria-label="Explication : ${fe(g[0])}">?</button>`;
}

const finPop = { el: null, trigger: null, pinned: false, timer: null };

function finTipTrigger(node) { return node?.closest?.("[data-q], [data-tip-t]") || null; }
function finTipInside(node) { return !!node?.closest?.("#fin-pop"); }

function finTipShow(t, pinned = false) {
  clearTimeout(finPop.timer);
  let html;
  if (t.dataset.q) {
    const g = FIN_GLOSSARY[t.dataset.q];
    if (!g) return;
    html = `<h5>${g[0]}</h5>${g[1].map((p) => `<p>${p}</p>`).join("")}`;
  } else {
    html = `<h5>${fe(t.dataset.tipT)}</h5>${t.dataset.tipB ? `<p>${fe(t.dataset.tipB)}</p>` : ""}${t.dataset.tipF ? `<p class="fin-pop-foot">${fe(t.dataset.tipF)}</p>` : ""}`;
  }
  if (finPop.trigger && finPop.trigger !== t) finTipHide();
  if (!finPop.el) {
    finPop.el = document.createElement("div");
    finPop.el.id = "fin-pop";
    finPop.el.className = "fin-pop";
    finPop.el.setAttribute("role", "tooltip");
  }
  // inside an open <dialog>, the popup must live in it to be drawn above (top layer)
  const host = t.closest("dialog[open]") || document.body;
  if (finPop.el.parentNode !== host) host.appendChild(finPop.el);
  finPop.el.innerHTML = html;
  finPop.el.hidden = false;
  finPop.trigger = t;
  finPop.pinned = pinned;
  t.setAttribute("aria-describedby", "fin-pop");
  if (t.classList.contains("fin-q")) t.setAttribute("aria-expanded", "true");
  finTipPlace();
}

function finTipHide() {
  clearTimeout(finPop.timer);
  const t = finPop.trigger;
  if (t) {
    t.removeAttribute("aria-describedby");
    if (t.classList.contains("fin-q")) t.setAttribute("aria-expanded", "false");
  }
  if (finPop.el) finPop.el.hidden = true;
  finPop.trigger = null;
  finPop.pinned = false;
}

function finTipPlace() {
  const { el, trigger } = finPop;
  if (!el || !trigger || el.hidden) return;
  if (!trigger.isConnected) { finTipHide(); return; }
  const r = trigger.getBoundingClientRect();
  const vw = document.documentElement.clientWidth, vh = window.innerHeight;
  const w = el.offsetWidth, h = el.offsetHeight;
  const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), vw - w - 8);
  let top = r.bottom + 8;
  if (top + h > vh - 8 && r.top - h - 8 >= 8) top = r.top - h - 8;
  el.style.left = `${Math.round(left)}px`;
  el.style.top = `${Math.round(top)}px`;
}

function finTipInit() {
  document.addEventListener("pointerover", (e) => {
    if (e.pointerType !== "mouse") return;
    const t = finTipTrigger(e.target);
    if (t) {
      clearTimeout(finPop.timer);
      if (t !== finPop.trigger) finPop.timer = setTimeout(() => finTipShow(t), 90);
    } else if (finTipInside(e.target)) {
      clearTimeout(finPop.timer);
    }
  });
  document.addEventListener("pointerout", (e) => {
    if (e.pointerType !== "mouse" || finPop.pinned) return;
    if (!finTipTrigger(e.target) && !finTipInside(e.target)) return;
    const to = e.relatedTarget;
    if (to && finPop.trigger && (finTipTrigger(to) === finPop.trigger || finTipInside(to))) return;
    clearTimeout(finPop.timer);
    finPop.timer = setTimeout(finTipHide, 140);
  });
  document.addEventListener("click", (e) => {
    const t = finTipTrigger(e.target);
    if (t) {
      e.preventDefault();  // a « ? » inside a <label> or <summary> must not toggle it
      if (t === finPop.trigger && finPop.pinned) finTipHide();
      else finTipShow(t, true);
    } else if (finPop.trigger && !finTipInside(e.target)) {
      finTipHide();
    }
  });
  document.addEventListener("focusin", (e) => {
    const t = finTipTrigger(e.target);
    if (t && t !== finPop.trigger) finTipShow(t);
  });
  document.addEventListener("focusout", (e) => {
    if (e.target === finPop.trigger && !finPop.pinned && !finTipInside(e.relatedTarget)) finTipHide();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !finPop.trigger) return;
    e.preventDefault();  // first Escape closes the popup, not the modal under it
    const t = finPop.trigger;
    finTipHide();
    if (t.isConnected && document.activeElement !== t) t.focus();
  });
  window.addEventListener("scroll", finTipPlace, { passive: true, capture: true });
  window.addEventListener("resize", finTipHide);
}

/* ------------------------------------------------------------------ boot & navigation */

function finOnShow() {
  if (!fin.booted) finBoot();
}

async function finBoot() {
  fin.booted = true;
  finTipInit();
  try { fin.tab = localStorage.getItem("fin.tab") || "summit"; } catch { /* private mode */ }
  try { fin.scope = localStorage.getItem("fin.scope") || "household"; } catch { /* ignore */ }
  $("#fin-checkin-btn").addEventListener("click", finOpenCheckin);
  $("#fin-drop-btn").addEventListener("click", () => $("#fin-file-input").click());
  $("#fin-file-input").addEventListener("change", (e) => { finUpload([...e.target.files]); e.target.value = ""; });
  const view = $("#view-finance"), zone = $("#fin-dropzone");
  let depth = 0;
  view.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types?.includes("Files")) { depth++; zone.classList.remove("hidden"); } });
  view.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) zone.classList.add("hidden"); });
  view.addEventListener("dragover", (e) => e.preventDefault());
  view.addEventListener("drop", (e) => {
    e.preventDefault(); depth = 0; zone.classList.add("hidden");
    if (e.dataTransfer.files.length) finUpload([...e.dataTransfer.files]);
  });
  // Charts are drawn for a width: redraw when the width changes. Not on a height change, since
  // a phone keyboard opening is one and the redraw would wipe what is being typed.
  let rt = null, lastW = window.innerWidth;
  window.addEventListener("resize", () => {
    if (window.innerWidth === lastW) return;
    lastW = window.innerWidth;
    clearTimeout(rt);
    rt = setTimeout(() => { if (fin.status?.onboarding_done && $("#view-finance").classList.contains("active")) finRenderTab(); }, 250);
  });
  await finRefresh();
}

async function finRefresh() {
  try {
    fin.status = await api.get("/api/finance/status");
    fin.members = await api.get("/api/finance/members");
  } catch (e) { $("#fin-body").innerHTML = `<p class="muted">${fe(e.message)}</p>`; return; }
  if (!fin.status.onboarding_done) {
    $("#fin-tabs").innerHTML = "";
    $("#fin-scope").innerHTML = "";
    $("#fin-checkin-btn").classList.add("hidden");
    finRenderWelcome();
    return;
  }
  $("#fin-checkin-btn").classList.remove("hidden");
  finRenderScope();
  finRenderTabs();
  finRenderTab();
}

function finRenderScope() {
  const adults = finAdults();
  if (fin.scope !== "household" && !adults.some((a) => `member:${a.id}` === fin.scope)) fin.scope = "household";
  const opts = [["household", "👪 Ensemble"], ...adults.map((a) => [`member:${a.id}`, `${a.emoji} ${a.name}`])];
  $("#fin-scope").innerHTML = opts.map(([k, l]) =>
    `<button class="seg-btn${fin.scope === k ? " active" : ""}" data-scope="${k}" role="tab" aria-selected="${fin.scope === k}">${fe(l)}</button>`).join("") + finQ("scope");
  $("#fin-scope").querySelectorAll("[data-scope]").forEach((b) => b.addEventListener("click", () => {
    fin.scope = b.dataset.scope;
    try { localStorage.setItem("fin.scope", fin.scope); } catch { /* ignore */ }
    finRenderScope();
    finRenderTab();
  }));
}

function finRenderTabs() {
  $("#fin-tabs").innerHTML = FIN_TABS.map(([k, l]) =>
    `<button class="chip${fin.tab === k ? " active" : ""}" data-tab="${k}">${l}</button>`).join("");
  $("#fin-tabs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => finGo(b.dataset.tab)));
}

function finGo(tab) {
  fin.tab = tab;
  try { localStorage.setItem("fin.tab", tab); } catch { /* ignore */ }
  finRenderTabs();
  finRenderTab();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function finRenderTab() {
  // A fresh container per render: a slow, older view that resolves late writes into a
  // detached box (and stops at its isConnected check) instead of replacing the new one.
  finTipHide();
  const host = $("#fin-body");
  const body = document.createElement("div");
  body.className = "fin-view";
  body.innerHTML = `<p class="muted fin-loading">Chargement…</p>`;
  host.replaceChildren(body);
  const views = { summit: finSummit, wealth: finWealth, budget: finBudget, plan: finPlan, goals: finGoals, docs: finDocs, settings: finSettings };
  (views[fin.tab] || finSummit)(body).catch((e) => {
    if (body.isConnected) body.innerHTML = `<div class="card"><p>⚠️ ${fe(e.message)}</p></div>`;
  });
}

/* ------------------------------------------------------------------ welcome (before onboarding) */

function finRenderWelcome() {
  $("#fin-body").innerHTML = `
    <div class="card fin-welcome">
      <div class="fin-mountain" id="fin-welcome-mountain"></div>
      <div class="fin-welcome-text">
        <h3>Et si on calculait le jour où travailler devient optionnel&nbsp;?</h3>
        <p>En dix minutes, on décrit le foyer : qui vous êtes, ce que vous gagnez, ce que vous avez, ce que vous dépensez
        et ce dont vous rêvez. Le tableau calcule ensuite, <b>avec les vrais barèmes d'impôts vaudois et fédéraux</b>,
        l'AVS, la LPP et le 3e pilier, l'année où vous serez libres — ensemble ou chacun de votre côté.</p>
        <ul class="fin-promise">
          <li>🔒 Les chiffres restent sur cette machine (fichier à part, jamais envoyé sur GitHub).</li>
          <li>🤖 Les documents sont lus par une IA <b>locale</b> d'abord : fiches de salaire, relevés, certificats LPP…</li>
          <li>📒 Ensuite, deux minutes par mois suffisent pour garder le cap.</li>
        </ul>
        <button class="btn btn-primary" id="fin-start">🧗 Commencer l'ascension</button>
      </div>
    </div>`;
  fcMountain($("#fin-welcome-mountain"), { progress: 0.08, peaks: finPeaks(), label: "La montagne de la liberté" });
  $("#fin-start").addEventListener("click", finWizardOpen);
}

function finPeaks() {
  const P = [[372, "Lac Léman"], [528, "Cathédrale de Lausanne"], [1080, "Mont Pèlerin"], [1607, "Chasseron"], [2042, "Rochers-de-Naye"],
    [2502, "Säntis"], [3238, "Titlis"], [3454, "Jungfraujoch"], [3883, "Petit Cervin"], [4158, "Jungfrau"], [4478, "Cervin"], [4634, "Pointe Dufour"]];
  return P.map(([m, n]) => ({ meters: m, name: n, progress: (m - 372) / (4634 - 372) }));
}

/* ------------------------------------------------------------------ onboarding wizard */

const WZ_BUDGET = [
  ["logement", "🏠 Loyer + charges", 2400, 0, 6000, 50],
  ["alimentation", "🛒 Courses", 900, 0, 3000, 25],
  ["sante", "🩺 Assurance maladie (LAMal) & santé", 950, 0, 3000, 25],
  ["transport", "🚆 Transports publics", 200, 0, 1000, 10],
  ["voiture", "🚗 Voiture", 0, 0, 1500, 25],
  ["telecom", "📶 Téléphone & internet", 120, 0, 400, 10],
  ["assurances", "🛡️ Autres assurances", 80, 0, 500, 10],
  ["restaurants", "🍽️ Restaurants & sorties", 300, 0, 2000, 25],
  ["loisirs", "🎾 Loisirs & sport", 250, 0, 2000, 25],
  ["voyages", "🧳 Voyages", 400, 0, 3000, 50],
  ["shopping", "🛍️ Shopping & maison", 250, 0, 2000, 25],
  ["abonnements", "📺 Abonnements", 60, 0, 300, 5],
  ["divers", "❓ Divers", 150, 0, 1500, 25],
];

const WZ_ACCOUNT_PRESETS = [
  ["current", "Compte courant"], ["savings", "Compte épargne"], ["invest", "Titres / ETF"], ["pillar3a", "3e pilier"],
  ["lpp", "Caisse de pension (LPP)"], ["vested", "Libre passage"], ["crypto", "Crypto"], ["loan", "Dette / crédit"],
];

async function finWizardOpen() {
  let names = [];
  try { names = (await api.get("/api/wishlist/people")).map((p) => ({ name: p.name, emoji: p.emoji })); } catch { /* optional */ }
  const adult = (i) => ({ key: `a${i}`, name: names[i]?.name || "", emoji: ["🦊", "🦉"][i], kind: "adult", birth_date: "",
    gross: "", months: 13, work_pct: 100, lpp_employee: "", side: "" });
  fin.wz = {
    step: 0,
    adults: [adult(0), adult(1)],
    civil: "maybe",
    commune: "Lausanne", coef: 78.5,
    baby: true, due: "", birth_parent: "a1", kids: [],
    childcare: 1500,
    accounts: [
      { kind: "current", name: "Compte courant commun", owner: "joint", balance: "" },
      { kind: "savings", name: "Épargne", owner: "a0", balance: "" },
      { kind: "pillar3a", name: "3e pilier", owner: "a0", balance: "" },
      { kind: "pillar3a", name: "3e pilier", owner: "a1", balance: "" },
      { kind: "lpp", name: "Caisse de pension", owner: "a0", balance: "" },
      { kind: "lpp", name: "Caisse de pension", owner: "a1", balance: "" },
    ],
    budget: Object.fromEntries(WZ_BUDGET.map(([k, , v]) => [k, v])),
    style: "normal", fi_change: 0, equity: 80, target_age: "",
    plans: { part_time: true, pt_who: "a1", pt_pct: 60, pt_years: 3, home: false, home_year: new Date().getFullYear() + 3,
      home_price: 1200000, abroad: false, abroad_year: new Date().getFullYear() + 20, abroad_factor: 70, side: false, side_who: "a0", side_amount: 6000 },
  };
  const dlg = $("#fin-wizard");
  if (!dlg.open) dlg.showModal();
  finWizardRender();
}

const WZ_STEPS = ["Bienvenue", "Le foyer", "Enfants", "Revenus", "Patrimoine", "Dépenses", "Le rêve", "Résultat"];

function finWizardRender() {
  const w = fin.wz;
  const nav = `<ol class="wz-steps">${WZ_STEPS.map((s, i) => `<li class="${i === w.step ? "on" : i < w.step ? "done" : ""}">${s}</li>`).join("")}</ol>`;
  const body = [wzWelcome, wzHousehold, wzKids, wzIncomes, wzWealth, wzBudget, wzDream, wzResult][w.step]();
  const last = w.step === WZ_STEPS.length - 1;
  $("#fin-wizard-body").innerHTML = `
    <div class="wz-top"><h3>🧗 ${WZ_STEPS[w.step]}</h3><button type="button" class="btn btn-ghost btn-sm" id="wz-close">✕</button></div>
    ${nav}<div class="wz-body">${body}</div>
    ${last ? "" : `<div class="modal-actions">
      ${w.step ? `<button type="button" class="btn btn-ghost" id="wz-back">← Retour</button>` : ""}
      <button type="button" class="btn btn-primary" id="wz-next">${w.step === WZ_STEPS.length - 2 ? "Calculer ma liberté 🏁" : "Continuer →"}</button></div>`}`;
  $("#wz-close").onclick = () => $("#fin-wizard").close();
  if ($("#wz-back")) $("#wz-back").onclick = () => { wzCollect(); w.step--; finWizardRender(); };
  if ($("#wz-next")) $("#wz-next").onclick = async () => {
    wzCollect();
    const err = wzValidate();
    if (err) { toast(err, true); return; }
    if (w.step === WZ_STEPS.length - 2) { await wzSubmit(); return; }
    w.step++;
    finWizardRender();
  };
  wzBind();
}

function wzWelcome() {
  return `<p class="wz-lead">On va décrire votre situation en 6 petites étapes. Tout se corrige plus tard, et les documents
    (fiches de salaire, certificats LPP, relevés) pourront remplir le reste automatiquement.</p>
    <div class="wz-cards">
      <div class="wz-card"><b>🇨🇭 Vraiment suisse</b><span>Barèmes 2026 IFD + Vaud, AVS avec 13e rente, LPP, 3e pilier, allocations familiales.</span></div>
      <div class="wz-card"><b>👫 À deux, ou chacun</b><span>Vue d'ensemble ou par personne. Pas marié·es ? On compare aussi « et si on se mariait ».</span></div>
      <div class="wz-card"><b>🔒 Privé</b><span>Base de données à part, hors de git. Sauvegarde chiffrée pour changer d'ordinateur.</span></div>
    </div>`;
}

function wzHousehold() {
  const w = fin.wz;
  return `<p class="wz-lead">Qui grimpe ? L'âge compte pour l'AVS, la LPP et l'horizon du plan.</p>
    <div class="wz-grid2">${w.adults.map((a, i) => `
      <fieldset class="wz-person"><legend>Adulte ${i + 1}</legend>
        <div class="field-row"><label>Prénom<input id="wz-a${i}-name" value="${fe(a.name)}" placeholder="Prénom" /></label>
        <label class="wz-emoji">Emoji<input id="wz-a${i}-emoji" value="${fe(a.emoji)}" maxlength="4" /></label></div>
        <label>Date de naissance<input id="wz-a${i}-birth" type="date" value="${fe(a.birth_date)}" /></label>
      </fieldset>`).join("")}</div>
    <label>Vous êtes…
      <select id="wz-civil">
        <option value="unmarried"${w.civil === "unmarried" ? " selected" : ""}>Pas marié·es (imposé·es séparément)</option>
        <option value="maybe"${w.civil === "maybe" ? " selected" : ""}>Pas encore — peut-être un jour (on comparera)</option>
        <option value="married"${w.civil === "married" ? " selected" : ""}>Marié·es (imposition commune)</option>
      </select></label>
    <div class="field-row"><label>Commune<input id="wz-commune" value="${fe(w.commune)}" /></label>
      <label>Coefficient communal (%)<input id="wz-coef" type="text" inputmode="decimal" value="${w.coef}" /><small class="muted">Lausanne 78.5 %. Il figure sur votre bordereau.</small></label></div>`;
}

function wzKids() {
  const w = fin.wz;
  const opts = w.adults.map((a) => `<option value="${a.key}"${w.birth_parent === a.key ? " selected" : ""}>${fe(a.name || a.key)}</option>`).join("");
  return `<label class="wz-check"><input type="checkbox" id="wz-baby"${w.baby ? " checked" : ""} /> 👶 Un bébé est en route</label>
    <div id="wz-baby-fields" class="${w.baby ? "" : "hidden"}">
      <div class="field-row"><label>Date prévue<input id="wz-due" type="date" value="${fe(w.due)}" /></label>
      <label>Qui porte le bébé ?<select id="wz-bp">${opts}</select><small class="muted">Pour le congé maternité (APG).</small></label></div>
    </div>
    <h4 class="wz-h4">Autres enfants</h4>
    <div id="wz-kids">${w.kids.map((k, i) => `<div class="field-row wz-kid"><label>Prénom<input data-kid="${i}" data-f="name" value="${fe(k.name)}" /></label>
      <label>Naissance<input type="date" data-kid="${i}" data-f="birth" value="${fe(k.birth)}" /></label>
      <button type="button" class="icon-btn" data-kid-del="${i}" title="Retirer">🗑️</button></div>`).join("") || `<p class="muted">Aucun pour l'instant.</p>`}</div>
    <button type="button" class="btn btn-ghost btn-sm" id="wz-kid-add">+ Ajouter un enfant</button>
    <label class="wz-gap">Crèche, par mois et par enfant (0-3 ans, si vous travaillez tous les deux)
      <input id="wz-childcare" type="text" inputmode="decimal" value="${fcNum(w.childcare)}" /><small class="muted">À Lausanne, le tarif dépend du revenu : 600 à 2'500 CHF pour un temps plein.</small></label>`;
}

function wzIncomes() {
  const w = fin.wz;
  return `<p class="wz-lead">Le salaire <b>brut annuel</b> (13e compris). Un doute ? Mettez une estimation : la fiche de salaire glissée
    plus tard corrigera tout.</p>
    <div class="wz-grid2">${w.adults.map((a, i) => `
      <fieldset class="wz-person"><legend>${fe(a.emoji)} ${fe(a.name || "Adulte " + (i + 1))}</legend>
        <label>Salaire brut annuel (CHF)${finMoneyInput(`wz-a${i}-gross`, finParse(a.gross), 'placeholder="ex. 95\'000"')}</label>
        <div class="field-row"><label>Taux d'activité (%)<input id="wz-a${i}-pct" type="text" inputmode="numeric" value="${a.work_pct}" /></label>
        <label>Cotisation LPP / an<small class="muted"> (facultatif)</small>${finMoneyInput(`wz-a${i}-lpp`, finParse(a.lpp_employee), 'placeholder="sur la fiche"')}</label></div>
        <label>Revenu annexe net / an<small class="muted"> (indépendant, location…)</small>${finMoneyInput(`wz-a${i}-side`, finParse(a.side))}</label>
      </fieldset>`).join("")}</div>`;
}

function wzWealth() {
  const w = fin.wz;
  const owners = [["joint", "Commun"], ...w.adults.map((a) => [a.key, a.name || a.key])];
  return `<p class="wz-lead">Ce que vous avez aujourd'hui, compte par compte — le solde approximatif suffit.
    Le 2e pilier figure sur le certificat de prévoyance (« avoir de vieillesse »).</p>
    <div class="wz-accounts">${w.accounts.map((a, i) => `
      <div class="wz-acc">
        <select data-acc="${i}" data-f="kind">${FIN_KIND_ORDER.map((k) => `<option value="${k}"${a.kind === k ? " selected" : ""}>${fin.status.account_kinds[k].emoji} ${fe(fin.status.account_kinds[k].label)}</option>`).join("")}</select>
        <input data-acc="${i}" data-f="name" value="${fe(a.name)}" placeholder="Nom (banque…)" />
        <select data-acc="${i}" data-f="owner">${owners.map(([k, l]) => `<option value="${k}"${a.owner === k ? " selected" : ""}>${fe(l)}</option>`).join("")}</select>
        <input data-acc="${i}" data-f="balance" type="text" inputmode="decimal" value="${a.balance === "" ? "" : fcNum(finParse(a.balance))}" placeholder="Solde CHF" />
        <button type="button" class="icon-btn" data-acc-del="${i}" title="Retirer">🗑️</button>
      </div>`).join("")}</div>
    <div class="chip-row wz-presets">${WZ_ACCOUNT_PRESETS.map(([k, l]) => `<button type="button" class="chip" data-preset="${k}">+ ${fe(l)}</button>`).join("")}</div>`;
}

function wzBudget() {
  const w = fin.wz;
  const total = Object.values(w.budget).reduce((a, b) => a + (+b || 0), 0);
  return `<p class="wz-lead">Les dépenses <b>mensuelles</b> du foyer, sans les impôts (le moteur les calcule) ni les enfants (modèle à part).
    Valeurs de départ : un couple à Lausanne. Vous pourrez importer vos relevés pour remplacer tout ça par du réel.</p>
    <div class="wz-budget">${WZ_BUDGET.map(([k, l, , min, max, step]) => `
      <label class="wz-slider"><span>${l}</span>
        <input type="range" min="${min}" max="${max}" step="${step}" value="${w.budget[k]}" data-budget="${k}" />
        <output id="wz-b-${k}">${fcNum(w.budget[k])}</output></label>`).join("")}</div>
    <p class="wz-total">Total : <b id="wz-btotal">${fcNum(total)}</b> CHF / mois · <span id="wz-byear">${fcNum(total * 12)}</span> CHF / an</p>`;
}

function wzDream() {
  const w = fin.wz, pl = w.plans;
  const who = (sel) => w.adults.map((a) => `<option value="${a.key}"${sel === a.key ? " selected" : ""}>${fe(a.name || a.key)}</option>`).join("");
  return `<p class="wz-lead">À quoi ressemble la liberté ?</p>
    <div class="wz-styles">${[["lean", "🎒 Frugale", "Lean FI : l'essentiel, sans folies."], ["normal", "🏔️ Confortable", "Votre train de vie actuel."], ["fat", "🥂 Généreuse", "Fat FI : plus de voyages, plus de marge."]]
      .map(([k, t, d]) => `<label class="wz-style${w.style === k ? " on" : ""}"><input type="radio" name="wz-style" value="${k}"${w.style === k ? " checked" : ""} /><b>${t}</b><span>${d}</span></label>`).join("")}</div>
    <div class="field-row">
      <label>Dépenses une fois libres (vs aujourd'hui, %)<input id="wz-fichange" type="text" inputmode="numeric" value="${w.fi_change}" /><small class="muted">-10 = 10 % de moins (plus de trajets, plus de temps).</small></label>
      <label>Âge rêvé (facultatif)<input id="wz-target" type="text" inputmode="numeric" value="${fe(w.target_age)}" placeholder="le plus tôt possible" /></label>
    </div>
    <label>Comment placer l'épargne ? <b id="wz-eq-out">${w.equity} % en actions</b>
      <input type="range" id="wz-equity" min="0" max="100" step="10" value="${w.equity}" /><small class="muted">Plus d'actions = plus de rendement attendu, mais plus de secousses (le Monte Carlo les montre).</small></label>
    <h4 class="wz-h4">Vos projets</h4>
    <div class="wz-plans">
      <div class="wz-plan"><label class="wz-check"><input type="checkbox" id="wz-pt"${pl.part_time ? " checked" : ""} /> ⏳ Temps partiel après le bébé</label>
        <div class="field-row"><label>Qui<select id="wz-pt-who">${who(pl.pt_who)}</select></label>
        <label>Taux (%)<input id="wz-pt-pct" type="text" inputmode="numeric" value="${pl.pt_pct}" /></label>
        <label>Pendant (ans)<input id="wz-pt-years" type="text" inputmode="numeric" value="${pl.pt_years}" /></label></div></div>
      <div class="wz-plan"><label class="wz-check"><input type="checkbox" id="wz-home"${pl.home ? " checked" : ""} /> 🏡 Acheter un logement</label>
        <div class="field-row"><label>En<input id="wz-home-year" type="text" inputmode="numeric" value="${pl.home_year}" /></label>
        <label>Prix (CHF)${finMoneyInput("wz-home-price", pl.home_price)}</label></div></div>
      <div class="wz-plan"><label class="wz-check"><input type="checkbox" id="wz-abroad"${pl.abroad ? " checked" : ""} /> ✈️ Vivre la retraite à l'étranger</label>
        <div class="field-row"><label>Dès<input id="wz-abroad-year" type="text" inputmode="numeric" value="${pl.abroad_year}" /></label>
        <label>Coût de la vie (% de la Suisse)<input id="wz-abroad-factor" type="text" inputmode="numeric" value="${pl.abroad_factor}" /></label></div></div>
      <div class="wz-plan"><label class="wz-check"><input type="checkbox" id="wz-side"${pl.side ? " checked" : ""} /> 🧑‍💻 Un revenu annexe à venir</label>
        <div class="field-row"><label>Qui<select id="wz-side-who">${who(pl.side_who)}</select></label>
        <label>Net / an${finMoneyInput("wz-side-amount", pl.side_amount)}</label></div></div>
    </div>`;
}

function wzResult() {
  const r = fin.wz.result;
  if (!r) return `<p class="muted">Calcul en cours…</p>`;
  const p = r.projection;
  const ages = p && p.fi_ages ? Object.entries(p.fi_ages).map(([n, a]) => `${fe(n)} ${a} ans`).join(" · ") : "";
  return `<div class="wz-finish">
      <div class="fin-mountain" id="wz-mountain"></div>
      ${p && p.fi_year ? `<p class="wz-big">Libres en <b>${p.fi_year}</b></p><p class="wz-ages">${ages}</p>`
        : `<p class="wz-big">Le sommet est encore loin</p><p class="wz-ages">Avec ces chiffres, il faut travailler jusqu'à la retraite. Le plan vous montrera quoi changer.</p>`}
      <p>Vous êtes à <b>${pct(p?.progress || 0)}</b> du chemin. Niveau ${r.level.level} · ${fe(r.level.title)} · ${fcNum(r.level.xp)} XP.</p>
      <button class="btn btn-primary" id="wz-go">Voir le sommet 🏔️</button>
    </div>`;
}

function wzBind() {
  const w = fin.wz;
  const el = $("#fin-wizard-body");
  if (w.step === 2) {
    $("#wz-baby").onchange = (e) => $("#wz-baby-fields").classList.toggle("hidden", !e.target.checked);
    $("#wz-kid-add").onclick = () => { wzCollect(); w.kids.push({ name: "", birth: "" }); finWizardRender(); };
    el.querySelectorAll("[data-kid-del]").forEach((b) => b.onclick = () => { wzCollect(); w.kids.splice(+b.dataset.kidDel, 1); finWizardRender(); });
  }
  if (w.step === 4) {
    el.querySelectorAll("[data-acc-del]").forEach((b) => b.onclick = () => { wzCollect(); w.accounts.splice(+b.dataset.accDel, 1); finWizardRender(); });
    el.querySelectorAll("[data-preset]").forEach((b) => b.onclick = () => {
      wzCollect();
      const k = b.dataset.preset;
      w.accounts.push({ kind: k, name: WZ_ACCOUNT_PRESETS.find((x) => x[0] === k)[1], owner: ["current", "savings", "loan"].includes(k) ? "joint" : "a0", balance: "" });
      finWizardRender();
    });
  }
  if (w.step === 5) {
    el.querySelectorAll("[data-budget]").forEach((r) => r.oninput = () => {
      w.budget[r.dataset.budget] = +r.value;
      $(`#wz-b-${r.dataset.budget}`).textContent = fcNum(+r.value);
      const t = Object.values(w.budget).reduce((a, b) => a + (+b || 0), 0);
      $("#wz-btotal").textContent = fcNum(t);
      $("#wz-byear").textContent = fcNum(t * 12);
    });
  }
  if (w.step === 6) {
    el.querySelectorAll('input[name="wz-style"]').forEach((r) => r.onchange = () => {
      el.querySelectorAll(".wz-style").forEach((l) => l.classList.toggle("on", l.querySelector("input").checked));
    });
    $("#wz-equity").oninput = (e) => { $("#wz-eq-out").textContent = `${e.target.value} % en actions`; };
  }
  if (w.step === 7 && w.result) {
    fcMountain($("#wz-mountain"), { progress: w.result.projection?.progress || 0, peaks: finPeaks(), label: "Votre position" });
    fcConfetti();
    $("#wz-go").onclick = () => { $("#fin-wizard").close(); fin.tab = "summit"; finRefresh(); };
  }
}

function wzCollect() {
  const w = fin.wz;
  const v = (id) => { const e = document.getElementById(id); return e ? e.value : undefined; };
  if (w.step === 1) {
    w.adults.forEach((a, i) => { a.name = v(`wz-a${i}-name`).trim(); a.emoji = v(`wz-a${i}-emoji`) || a.emoji; a.birth_date = v(`wz-a${i}-birth`); });
    w.civil = v("wz-civil"); w.commune = v("wz-commune"); w.coef = finParse(v("wz-coef")) ?? 78.5;
  } else if (w.step === 2) {
    w.baby = $("#wz-baby").checked; w.due = v("wz-due"); w.birth_parent = v("wz-bp");
    document.querySelectorAll("[data-kid]").forEach((inp) => { w.kids[+inp.dataset.kid][inp.dataset.f] = inp.value; });
    w.childcare = finParse(v("wz-childcare")) ?? 1500;
  } else if (w.step === 3) {
    w.adults.forEach((a, i) => { a.gross = v(`wz-a${i}-gross`); a.work_pct = finParse(v(`wz-a${i}-pct`)) ?? 100; a.lpp_employee = v(`wz-a${i}-lpp`); a.side = v(`wz-a${i}-side`); });
  } else if (w.step === 4) {
    document.querySelectorAll("[data-acc]").forEach((inp) => { w.accounts[+inp.dataset.acc][inp.dataset.f] = inp.value; });
  } else if (w.step === 6) {
    w.style = document.querySelector('input[name="wz-style"]:checked')?.value || "normal";
    w.fi_change = finParse(v("wz-fichange")) ?? 0;
    w.target_age = v("wz-target");
    w.equity = +v("wz-equity");
    const pl = w.plans;
    pl.part_time = $("#wz-pt").checked; pl.pt_who = v("wz-pt-who"); pl.pt_pct = finParse(v("wz-pt-pct")) ?? 60; pl.pt_years = finParse(v("wz-pt-years")) ?? 3;
    pl.home = $("#wz-home").checked; pl.home_year = finParse(v("wz-home-year")); pl.home_price = finParse(v("wz-home-price"));
    pl.abroad = $("#wz-abroad").checked; pl.abroad_year = finParse(v("wz-abroad-year")); pl.abroad_factor = finParse(v("wz-abroad-factor")) ?? 70;
    pl.side = $("#wz-side").checked; pl.side_who = v("wz-side-who"); pl.side_amount = finParse(v("wz-side-amount"));
  }
}

function wzValidate() {
  const w = fin.wz;
  if (w.step === 1) {
    if (!w.adults[0].name) return "Il faut au moins un prénom.";
    if (w.adults.some((a) => a.name && !a.birth_date)) return "La date de naissance compte pour l'AVS et la LPP.";
  }
  if (w.step === 2 && w.baby && !w.due) return "Quelle est la date prévue ?";
  return null;
}

async function wzSubmit() {
  const w = fin.wz;
  const adults = w.adults.filter((a) => a.name);
  const members = adults.map((a) => ({
    key: a.key, name: a.name, emoji: a.emoji, kind: "adult", birth_date: a.birth_date,
    salary: finParse(a.gross) ? { gross_annual: finParse(a.gross), work_pct: a.work_pct, lpp_employee_annual: finParse(a.lpp_employee) } : null,
    side_income: finParse(a.side),
  }));
  if (w.baby && w.due) members.push({ key: "baby", name: "Bébé", emoji: "👶", kind: "child", birth_date: w.due, expected: true, birth_parent: w.birth_parent });
  w.kids.filter((k) => k.birth).forEach((k, i) => members.push({ key: `k${i}`, name: k.name || `Enfant ${i + 1}`, emoji: "🧒", kind: "child", birth_date: k.birth }));
  const accounts = w.accounts.filter((a) => a.name).map((a) => ({
    kind: a.kind, name: a.name, balance: finParse(a.balance),
    owners: a.owner === "joint" ? Object.fromEntries(adults.map((x) => [x.key, 1 / adults.length])) : { [a.owner]: 1 },
  }));
  const events = [];
  const pl = w.plans;
  const y0 = new Date().getFullYear();
  if (pl.part_time && w.baby && w.due) {
    const start = new Date(w.due); start.setMonth(start.getMonth() + 4);
    const end = new Date(start); end.setFullYear(end.getFullYear() + Math.max(1, pl.pt_years || 3));
    events.push({ kind: "part_time", member: pl.pt_who, start_date: start.toISOString().slice(0, 10), end_date: end.toISOString().slice(0, 10), params: { work_pct: pl.pt_pct } });
  }
  if (pl.home && pl.home_price) events.push({ kind: "home_purchase", start_date: `${pl.home_year || y0 + 3}-06-30`, params: { price: pl.home_price, down_payment: pl.home_price * 0.2, mortgage_rate_pct: 1.8 } });
  if (pl.abroad) events.push({ kind: "move_abroad", start_date: `${pl.abroad_year || y0 + 20}-01-01`, params: { cost_factor: (pl.abroad_factor || 70) / 100, tax_rate_pct: 10, capital_tax_pct: 5 } });
  if (pl.side && pl.side_amount) events.push({ kind: "side_income", member: pl.side_who, start_date: `${y0 + 1}-01-01`, params: { annual: pl.side_amount } });
  const goals = [{ name: "Coussin de sécurité", kind: "emergency", emoji: "🛟", target: Math.round(Object.values(w.budget).reduce((a, b) => a + (+b || 0), 0) * 6 / 1000) * 1000 }];
  if (w.baby && w.due) goals.push({ name: "Fonds bébé", kind: "baby", emoji: "👶", target: 10000, target_date: w.due });
  if (pl.home && pl.home_price) goals.push({ name: "Apport pour le logement", kind: "home", emoji: "🏡", target: pl.home_price * 0.2, target_date: `${pl.home_year}-06-30` });
  const payload = {
    household: { civil_status: w.civil === "married" ? "married" : "unmarried", commune: w.commune, communal_coefficient: w.coef },
    members, accounts, budget: w.budget, goals, events,
    fire: { style: w.style, target_age: finParse(w.target_age) || null },
    assumptions: { target_equity_pct: w.equity, fi_spending_change_pct: w.fi_change },
    child_costs: { childcare_monthly: w.childcare },
  };
  $("#wz-next").disabled = true;
  $("#wz-next").textContent = "Calcul…";
  try {
    const res = await api.post("/api/finance/onboarding", payload);
    if (w.civil === "maybe") {
      await api.post("/api/finance/scenarios", { name: "💍 Et si on se mariait ?", description: "Mariage l'an prochain : impôts communs, AVS de couple.", overrides: { marriage_year: y0 + 1 } }).catch(() => {});
    }
    w.result = res;
    w.step = WZ_STEPS.length - 1;
    finWizardRender();
  } catch (e) {
    toast(e.message, true);
    $("#wz-next").disabled = false;
  }
}

/* ------------------------------------------------------------------ SOMMET */

async function finSummit(body) {
  const ov = await api.get(`/api/finance/overview?scope=${fin.scope}`);
  if (!body.isConnected) return;
  fin.overview = ov;
  const p = ov.projection || {};
  const g = ov.game;
  const alt = g.altitude;
  const need = p.required_liquid_now;
  const next = alt.next_peak;
  const toNext = next && need ? Math.max(0, (next.progress_needed - alt.progress) * need) : null;
  const delta = ov.last_checkins?.length ? ov.net_worth.total - ov.last_checkins[0].net_worth_cents / 100 : null;
  const fd = ov.freedom_days;
  const sr = ov.cashflow.savings_rate;
  const ages = p.fi_ages ? Object.entries(p.fi_ages).map(([n, a]) => `${fe(n)} ${a} ans`).join(" · ") : "";
  const lvl = g.level;
  const peakName = alt.last_peak.name;
  body.innerHTML = `
    <div class="fin-hero card">
      <div class="fin-mountain" id="fin-mountain"></div>
      <div class="fin-hero-side">
        <p class="fin-alt"><b>${fcNum(alt.meters)}</b> m${finQ("altitude")}</p>
        <p class="fin-alt-sub">${alt.progress >= 1 ? "🏁 Sommet atteint !" : `${fe(alt.last_peak.above || peakName)} · ${pct(alt.progress)} du chemin`}</p>
        ${next ? `<p class="fin-next">Prochain sommet : <b>${fe(next.name)}</b> (${fcNum(next.meters)} m)${toNext != null ? ` — encore <b>${kchf(toNext)}</b>` : ""}</p>` : ""}
        <div class="fin-level">
          <span class="fin-level-badge">${lvl.level}</span>
          <div><b>${fe(lvl.title)}${finQ("level")}</b><small>${fcNum(lvl.xp)} XP · prochain niveau à ${fcNum(lvl.next_level)}</small>${fcMeter(lvl.progress, 1, FC.s[1])}</div>
          <span class="fin-streak" data-q="streak" tabindex="0">🔥 ${g.streak.months}</span>
        </div>
      </div>
    </div>

    <div class="fin-kpis">
      <div class="fin-kpi fin-kpi-main"><span class="lbl">Libres en${finQ("fi_year")}</span>
        <span class="val">${p.fi_year || "—"}</span>
        <span class="sub">${p.fi_year ? ages : "pas avant la retraite avec ce plan"}</span>
        ${p.fi_years ? `<span class="sub fin-styles">🎒 ${p.fi_years.lean || "—"} · 🏔️ ${p.fi_years.normal || "—"} · 🥂 ${p.fi_years.fat || "—"}</span>` : ""}</div>
      <div class="fin-kpi"><span class="lbl">Patrimoine net${finQ("net_worth")}</span><span class="val">${kchf(ov.net_worth.total)}</span>
        <span class="sub">${delta != null ? `${delta >= 0 ? "▲" : "▼"} ${kchf(Math.abs(delta))} depuis le dernier point` : `dont ${kchf(ov.net_worth.liquid)} disponibles`}</span></div>
      <div class="fin-kpi"><span class="lbl">Jours de liberté${finQ("freedom_days")}</span><span class="val">${fd != null ? fcNum(fd) : "—"}</span>
        <span class="sub">${fd != null ? `≈ ${finDuration(fd)} de dépenses déjà à l'abri` : ""}</span></div>
      <div class="fin-kpi"><span class="lbl">Taux d'épargne${finQ("savings_rate")}</span><span class="val">${sr != null ? pct(sr) : "—"}</span>
        <span class="sub">${sr != null ? "sur 12 mois de relevés" : "importez vos relevés pour le mesurer"}</span></div>
      <div class="fin-kpi"><span class="lbl">Coast FI 🚡${finQ("coast")}</span><span class="val">${p.coast?.reached ? "Atteint" : p.coast?.required_liquid_now ? kchf(p.coast.required_liquid_now) : "—"}</span>
        <span class="sub">${p.coast?.reached ? "sans épargner un franc de plus, la retraite à 65 tient" : "disponibles nécessaires pour pouvoir arrêter d'épargner"}</span></div>
      <div class="fin-kpi"><span class="lbl">Il faudrait aujourd'hui${finQ("need_now")}</span><span class="val">${need != null ? kchf(need) : "—"}</span>
        <span class="sub">pour arrêter maintenant (règle des ${fcNum(p.rule_of_thumb?.swr_pct || 3.5, { dec: 1 })} % : ${kchf(p.rule_of_thumb?.number)})</span></div>
    </div>

    <div class="fin-cols">
      <div class="card fin-quests"><h3 class="fin-h3">🗺️ Quêtes${finQ("quests")}</h3>
        ${g.quests.length ? g.quests.map((q) => `
          <div class="fin-quest">
            <span class="fin-quest-emo">${q.emoji}</span>
            <div class="info"><strong>${fe(q.title)}</strong><small>${fe(q.text)}</small></div>
            <span class="fin-xp">+${q.xp} XP</span>
            ${finQuestButton(q)}
          </div>`).join("") : `<p class="muted">Rien à faire pour l'instant — profitez de la vue 🌄</p>`}
      </div>
      <div class="card fin-badges-card"><h3 class="fin-h3">🏅 Badges${finQ("badges")} <small class="muted">${g.earned_count} / ${g.badges.length}</small></h3>
        <div class="fin-badges">${g.badges.map((b) => `
          <div class="fin-badge${b.earned_at ? " on" : ""}" tabindex="0" data-tip-t="${fe(`${b.emoji} ${b.label}`)}" data-tip-b="${fe(b.desc)}"
            data-tip-f="${fe(b.earned_at ? `✅ Obtenu le ${new Date(b.earned_at).toLocaleDateString("fr-CH")} · +${b.xp} XP` : `🔒 Pas encore obtenu · +${b.xp} XP à la clé`)}">
            <span>${b.emoji}</span><small>${fe(b.label)}</small></div>`).join("")}</div>
      </div>
    </div>

    ${p.milestones ? `<div class="card"><h3 class="fin-h3">🧭 Les étapes${finQ("milestones")}</h3>
      <div class="fin-timeline">${p.milestones.filter((m) => m.year >= new Date().getFullYear() - 1).map((m) => `
        <div class="fin-step fin-step-${m.kind}"><span class="yr">${m.year}</span><span class="emo">${m.emoji}</span><span class="lbl">${fe(m.label)}</span></div>`).join("")}</div></div>` : ""}

    <div class="card fin-recent"><h3 class="fin-h3">✨ Derniers XP</h3>
      ${g.recent_xp.length ? `<ul>${g.recent_xp.map((x) => `<li><span>${fe(x.note || x.kind)}</span><b>+${x.xp}</b></li>`).join("")}</ul>` : `<p class="muted">Faites votre premier point du mois !</p>`}</div>`;
  fcMountain($("#fin-mountain"), { progress: alt.progress, peaks: alt.peaks, label: `${fcNum(alt.meters)} m` });
  body.querySelectorAll("[data-quest]").forEach((b) => b.addEventListener("click", () => finQuestAction(b.dataset.quest, b.dataset.action)));
  finBadge(g.quests.length);
}

function finDuration(days) {
  const y = Math.floor(days / 365), m = Math.floor((days % 365) / 30.4);
  if (y <= 0) return `${m} mois`;
  return `${y} an${y > 1 ? "s" : ""}${m ? ` et ${m} mois` : ""}`;
}

function finQuestButton(q) {
  const labels = { checkin: "Faire le point", accounts: "Voir", transactions: "Classer", documents: "Déposer", budget: "Estimer", goals: "Objectif", done: "C'est fait ✓", info: "Compris ✓" };
  return `<button class="btn btn-ghost btn-sm" data-quest="${fe(q.id)}" data-action="${q.action}">${labels[q.action] || "Ok"}</button>`;
}

async function finQuestAction(id, action) {
  if (action === "checkin") return finOpenCheckin();
  if (action === "accounts") return finGo("wealth");
  if (action === "transactions") { fin.tx.uncategorized = true; return finGo("budget"); }
  if (action === "documents") return finGo("docs");
  if (action === "budget") return finGo("budget");
  if (action === "goals") return finGo("goals");
  const r = await api.post(`/api/finance/quests/${encodeURIComponent(id)}/done`);
  if (r.xp) toast(`+${r.xp} XP ✨`);
  if (r.badges?.length) fcConfetti();
  finRenderTab();
}

function finBadge(n) {
  const b = $("#finance-badge");
  if (!b) return;
  b.textContent = n;
  b.title = n ? `${n} quête${n > 1 ? "s" : ""} à faire — onglet Sommet, carte « Quêtes »` : "";
  b.classList.toggle("hidden", !n);
}

/* ------------------------------------------------------------------ home card */

const FIN_HELP = {
  title: "🏔️ Depuis Telegram",
  needsAI: false,
  paragraphs: [
    "<code>/fire</code> — l'altitude, l'année de liberté et les quêtes du moment. <b>Jamais un montant</b> : le salon est partagé.",
    "Les premiers jours du mois, le bot rappelle une seule fois le <b>point du mois</b> s'il n'est pas fait.",
    "Le reste (soldes, documents, plan) se fait dans l'onglet Finances.",
  ],
  chat: [
    ["vous", "/fire"],
    ["bot", "🏔️ Liberté financière\n\nAltitude : 1'412 m (24 %) — Mont Pèlerin\nAnnée de liberté : 2041 (Seb 48 ans, Lea 47 ans)\nNiveau 4 · Randonneur·se du dimanche · série de 3 mois"],
  ],
};
if (typeof HOME_HELP === "object") HOME_HELP.finance = FIN_HELP;

function finRenderHomeCard(s) {
  const box = $("#home-finance");
  if (!box) return;
  if (!s || !s.onboarding_done) {
    box.innerHTML = `<p class="muted">Calculez le jour où travailler devient optionnel.</p>
      <button class="btn btn-primary btn-sm" onclick="document.querySelector('.tab[data-view=finance]').click()">🧗 Commencer</button>`;
    return;
  }
  const ages = s.fi_ages ? Object.entries(s.fi_ages).map(([n, a]) => `${fe(n)} ${a}`).join(", ") : "";
  box.innerHTML = `
    <div class="home-item"><span class="home-thumb">🧗</span><div class="info"><strong>${fcNum(s.altitude)} m · ${fe(s.peak)}</strong>
      <small>${s.fi_year ? `Libres en ${s.fi_year} (${ages})` : "Le plan cherche encore la date"}</small></div>
      <button class="btn btn-primary btn-sm" onclick="document.querySelector('.tab[data-view=finance]').click()">Voir</button></div>
    ${(s.quests || []).map((q) => `<div class="home-item"><span class="home-thumb">${q.emoji}</span><div class="info"><strong>${fe(q.title)}</strong></div></div>`).join("")}
    <p class="muted fridge-hint">Niveau ${s.level.level} · ${fe(s.level.title)} · 🔥 ${s.streak.months} mois</p>`;
  finBadge(s.quest_count ?? (s.quests || []).length);
}

/* ------------------------------------------------------------------ check-in (point du mois) */

async function finOpenCheckin() {
  const data = await api.get("/api/finance/checkin");
  const month = new Date(data.month + "-01").toLocaleDateString("fr-CH", { month: "long", year: "numeric" });
  const rows = data.accounts.map((a) => `
    <div class="ci-row">
      <span class="ci-emo">${a.emoji}</span>
      <div class="info"><strong>${fe(a.name)}</strong><small>${fe(a.institution || a.kind_label)}${a.last_date ? ` · ${new Date(a.last_date).toLocaleDateString("fr-CH")}` : " · jamais saisi"} ${finShares(a.owners)}</small></div>
      <span class="ci-cur">${fe(a.currency)}</span>
      ${finMoneyInput(`ci-${a.id}`, a.value, `data-acc="${a.id}" data-old="${a.value ?? ""}"`)}
    </div>`).join("");
  const auto = data.auto_accounts.map((a) => `<div class="ci-row ci-auto"><span class="ci-emo">${a.emoji}</span><div class="info"><strong>${fe(a.name)}</strong><small>valorisé automatiquement</small></div><b>${chf(a.value_chf)}</b></div>`).join("");
  const dlg = finModal(`
    <h3>📒 Le point de ${month}</h3>
    <p class="muted">Corrigez ce qui a bougé (le reste ne change pas). Un relevé glissé dans « Documents » le fait pour vous.
      ${data.streak.months ? `Série en cours : 🔥 ${data.streak.months} mois.` : ""}</p>
    <div class="ci-list">${rows || `<p class="muted">Aucun compte à saisir.</p>`}${auto}</div>
    <div class="modal-actions">
      <button type="button" class="btn btn-ghost" data-close>Plus tard</button>
      <button type="button" class="btn btn-primary" id="ci-save">Valider le point du mois ✓</button>
    </div>`, { wide: true });
  $("#ci-save").onclick = async () => {
    const balances = [];
    dlg.querySelectorAll("[data-acc]").forEach((inp) => {
      const v = finParse(inp.value);
      if (v != null) balances.push({ account_id: +inp.dataset.acc, balance: v });
    });
    $("#ci-save").disabled = true;
    try {
      const r = await api.post("/api/finance/checkin", { balances });
      finCheckinResult(r);
    } catch (e) { toast(e.message, true); $("#ci-save").disabled = false; }
  };
}

function finCheckinResult(r) {
  const up = r.change >= 0;
  const fy = r.fi_year_before && r.fi_year && r.fi_year !== r.fi_year_before
    ? `<p class="ci-fi">${r.fi_year < r.fi_year_before ? "🎉" : "⛅"} Année de liberté : ${r.fi_year_before} → <b>${r.fi_year}</b></p>` : "";
  const prog = r.progress != null && r.progress_before != null
    ? `<p>Altitude : ${fcNum(372 + r.progress_before * 4262)} m → <b>${fcNum(372 + r.progress * 4262)} m</b></p>` : "";
  finModal(`
    <div class="ci-result">
      <p class="ci-big ${up ? "up" : "down"}">${up ? "▲" : "▼"} ${chf(Math.abs(r.change))}</p>
      <p>Patrimoine net : <b>${chf(r.net_worth)}</b></p>
      <p>${r.freedom_days_gained >= 0 ? "+" : ""}${fcNum(r.freedom_days_gained)} jours de liberté ce mois-ci</p>
      ${prog}${fy}
      <p>🔥 Série : ${r.streak.months} mois · Niveau ${r.level.level} (${fe(r.level.title)})</p>
      ${r.new_badges.length ? `<div class="ci-badges">${r.new_badges.map((b) => `<div class="fin-badge on"><span>${b.emoji}</span><small>${fe(b.label)}</small></div>`).join("")}</div>` : ""}
      <div class="modal-actions"><button class="btn btn-primary" data-close>Super !</button></div>
    </div>`);
  fcConfetti();
  if (fin.status?.onboarding_done) finRenderTab();
  if (typeof loadHome === "function") loadHome();
}

/* ------------------------------------------------------------------ PATRIMOINE */

async function finWealth(body) {
  const [accounts, hist] = await Promise.all([api.get("/api/finance/accounts"), api.get(`/api/finance/history?scope=${fin.scope}&months=36`)]);
  if (!body.isConnected) return;
  const member = fin.scope.startsWith("member:") ? +fin.scope.split(":")[1] : null;
  const share = (a) => member == null ? 1 : (a.shares[member] || 0);
  const mine = accounts.filter((a) => share(a) > 0);
  const groups = {};
  mine.forEach((a) => { (groups[a.group] ||= []).push(a); });
  const sum = (g) => (groups[g] || []).reduce((t, a) => t + a.signed_chf * share(a), 0);
  const cash = mine.filter((a) => ["current", "savings", "cash"].includes(a.kind)).reduce((t, a) => t + a.value_chf * share(a), 0);
  const invested = sum("liquid") - cash;
  body.innerHTML = `
    <div class="fin-kpis fin-kpis-4">
      <div class="fin-kpi"><span class="lbl">Patrimoine net${finQ("net_worth")}</span><span class="val">${kchf(Object.keys(groups).reduce((t, g) => t + sum(g), 0))}</span></div>
      <div class="fin-kpi"><span class="lbl">Disponible${finQ("liquid")}</span><span class="val">${kchf(sum("liquid"))}</span><span class="sub">dont ${kchf(invested)} investis</span></div>
      <div class="fin-kpi"><span class="lbl">Prévoyance${finQ("pension")}</span><span class="val">${kchf(sum("pension"))}</span><span class="sub">2e et 3e piliers, bloqués jusqu'à 60 ans</span></div>
      <div class="fin-kpi"><span class="lbl">Immobilier net${finQ("property_net")}</span><span class="val">${kchf(sum("property") + sum("debt") + sum("other"))}</span></div>
    </div>
    <div class="card"><div id="fin-hist"></div></div>
    <div class="card"><div id="fin-alloc"></div></div>
    <div class="section-head fin-subhead"><h3>Comptes</h3>
      <div class="plan-actions"><button class="btn btn-ghost btn-sm" id="fin-refresh-prices" title="Cours, devises, inflation">🔄 Cours</button>
      <button class="btn btn-primary btn-sm" id="fin-add-account">+ Compte</button></div></div>
    ${Object.keys(FIN_GROUPS).filter((g) => groups[g]).map((g) => `
      <div class="card fin-accgroup"><h4>${FIN_GROUPS[g]} <span class="muted">${chf(sum(g))}</span></h4>
        ${groups[g].map((a) => `
          <div class="fin-acc" data-id="${a.id}">
            <span class="fin-acc-emo">${a.emoji}</span>
            <div class="info"><strong>${fe(a.name)}</strong>
              <small>${fe(a.institution || a.kind_label)} ${finShares(a.owners)} ${a.valuation === "holdings" ? "· 📈 cours en direct" : a.stale_days == null ? "· jamais saisi" : a.stale_days > 45 ? `· <span class="fin-stale">pas à jour (${a.stale_days} j)</span>` : `· ${new Date(a.last_date).toLocaleDateString("fr-CH")}`}${a.approx ? " · ≈" : ""}</small></div>
            <b class="fin-acc-val">${a.currency !== "CHF" && a.value != null ? `<small>${fcNum(a.value)} ${a.currency}</small> ` : ""}${chf(a.signed_chf * share(a))}</b>
            <button class="icon-btn" data-edit="${a.id}" title="Modifier">✏️</button>
          </div>`).join("")}
      </div>`).join("") || `<p class="muted">Aucun compte.</p>`}`;
  const months = hist.map((h) => h.month);
  if (hist.length >= 2) {
    fcArea($("#fin-hist"), {
      title: "Patrimoine net", sub: "fin de mois, d'après les points du mois et les relevés", x: months.map(finMonthLabel), stacked: true,
      series: [
        { label: "Disponible", color: FC.s[0], values: hist.map((h) => h.liquid) },
        { label: "Prévoyance", color: FC.s[1], values: hist.map((h) => h.pension) },
        { label: "Immobilier net", color: FC.s[2], values: hist.map((h) => h.property + h.debt + h.other) },
      ],
    });
  } else {
    $("#fin-hist").innerHTML = `<h4 class="fin-h4">Patrimoine net</h4><p class="muted">La courbe apparaît dès le deuxième point du mois.</p>`;
  }
  fcSegmented($("#fin-alloc"), [
    { label: "Liquidités", value: cash, color: FC.s[3] },
    { label: "Placements", value: invested, color: FC.s[0] },
    { label: "Prévoyance", value: sum("pension"), color: FC.s[1] },
    { label: "Immobilier net", value: Math.max(0, sum("property") + sum("debt")), color: FC.s[2] },
  ], { title: "Répartition", sub: "ce qui travaille pour vous" });
  $("#fin-add-account").onclick = () => finAccountModal();
  $("#fin-refresh-prices").onclick = async (e) => {
    e.target.disabled = true;
    try { await api.post("/api/finance/market/refresh"); toast("Cours mis à jour"); finRenderTab(); }
    catch (err) { toast(err.message, true); e.target.disabled = false; }
  };
  body.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => finAccountModal(+b.dataset.edit));
}

function finMonthLabel(m) {
  const d = new Date(m + "-01");
  return d.toLocaleDateString("fr-CH", { month: "short", year: "2-digit" });
}

async function finAccountModal(id = null) {
  const a = id ? await api.get(`/api/finance/accounts/${id}`) : { kind: "current", currency: "CHF", owners: {}, meta: {}, valuation: "manual", include_in_fi: 1 };
  const kinds = fin.status.account_kinds;
  const adults = finAdults();
  const owners = a.owners || {};
  const isLpp = a.kind === "lpp";
  const meta = a.meta || {};
  const dlg = finModal(`
    <h3>${id ? "Modifier le compte" : "Nouveau compte"}</h3>
    <div class="field-row"><label>Type<select id="fa-kind">${FIN_KIND_ORDER.map((k) => `<option value="${k}"${a.kind === k ? " selected" : ""}>${kinds[k].emoji} ${fe(kinds[k].label)}</option>`).join("")}</select></label>
      <label>Devise<select id="fa-cur">${["CHF", "EUR", "USD", "GBP"].map((c) => `<option${a.currency === c ? " selected" : ""}>${c}</option>`).join("")}</select></label></div>
    <div class="field-row"><label>Nom<input id="fa-name" value="${fe(a.name || "")}" required /></label>
      <label>Banque / fournisseur<input id="fa-inst" value="${fe(a.institution || "")}" /></label></div>
    <label>IBAN <small class="muted">(pour reconnaître ses relevés)</small><input id="fa-iban" value="${fe(a.iban || "")}" placeholder="CH.." /></label>
    <label>À qui ? <small class="muted">parts en %, vide = commun à parts égales</small></label>
    <div class="field-row">${adults.map((m) => `<label>${m.emoji} ${fe(m.name)}<input data-owner="${m.id}" type="text" inputmode="numeric" value="${owners[m.id] != null ? Math.round(owners[m.id] * 100) : ""}" placeholder="%" /></label>`).join("")}</div>
    <div class="field-row"><label>Solde aujourd'hui${finMoneyInput("fa-balance", a.valuation === "holdings" ? null : a.value)}</label>
      <label><span>Part en actions (%)${finQ("equity_pct")}</span><input id="fa-eq" type="text" inputmode="numeric" value="${a.equity_pct ?? ""}" placeholder="auto" /></label></div>
    <div id="fa-lpp" class="${isLpp ? "" : "hidden"}">
      <h4 class="wz-h4">Certificat de prévoyance</h4>
      <div class="field-row"><label>Bonifications d'épargne / an${finMoneyInput("fa-credits", meta.savings_credits_annual)}</label>
        <label>Cotisation salarié / an${finMoneyInput("fa-emp", meta.employee_annual)}</label></div>
      <div class="field-row"><label>Taux de conversion (%)<input id="fa-conv" type="text" inputmode="decimal" value="${meta.conversion_pct ?? ""}" placeholder="6.0" /></label>
        <label>Rachat possible${finMoneyInput("fa-buyin", meta.buyin_potential)}</label></div>
    </div>
    <label class="wz-check"><input type="checkbox" id="fa-fi"${a.include_in_fi ? " checked" : ""} /> Compte dans le plan de liberté${finQ("include_in_fi")}</label>
    ${a.holdings ? `<h4 class="wz-h4">Positions</h4><div class="fin-holdings">${a.holdings.map((h) => `<div class="fin-hold"><span title="${h.symbol ? "ticker" : h.isin ? "ISIN" : h.valor ? "n° de valeur" : ""}">${fe(h.symbol || h.isin || h.valor || "—")}</span><span>${fe(h.name)}</span><span>${fcNum(h.quantity, { dec: 2 })}</span><b>${chf(h.value_chf)}</b><span class="fin-hold-btns"><button type="button" class="icon-btn" data-hedit="${h.id}" title="Modifier">✏️</button><button type="button" class="icon-btn" data-hdel="${h.id}" title="Supprimer">🗑️</button></span></div>`).join("")}</div>` : ""}
    ${id ? `<p><button type="button" class="btn btn-ghost btn-sm" id="fa-hold">+ Position (titre / ETF)</button>${finQ("holdings")}</p>` : ""}
    <div class="modal-actions">
      ${id ? `<button type="button" class="btn btn-ghost fin-danger-btn" id="fa-del">Supprimer</button>` : ""}
      <button type="button" class="btn btn-ghost" data-close>Annuler</button>
      <button type="button" class="btn btn-primary" id="fa-save">Enregistrer</button>
    </div>`, { wide: true });
  $("#fa-kind").onchange = (e) => $("#fa-lpp").classList.toggle("hidden", e.target.value !== "lpp");
  $("#fa-save").onclick = async () => {
    const own = {};
    dlg.querySelectorAll("[data-owner]").forEach((i) => { const v = finParse(i.value); if (v) own[i.dataset.owner] = v / 100; });
    const kind = $("#fa-kind").value;
    const body = {
      name: $("#fa-name").value.trim() || "Compte", institution: $("#fa-inst").value, kind, currency: $("#fa-cur").value,
      owners: own, iban: $("#fa-iban").value, valuation: a.valuation || "manual", equity_pct: finParse($("#fa-eq").value),
      include_in_fi: $("#fa-fi").checked, archived: false, balance: finParse($("#fa-balance").value),
      meta: kind === "lpp" ? { savings_credits_annual: finParse($("#fa-credits").value), employee_annual: finParse($("#fa-emp").value),
        conversion_pct: finParse($("#fa-conv").value), buyin_potential: finParse($("#fa-buyin").value) } : {},
    };
    try {
      if (id) await api.put(`/api/finance/accounts/${id}`, body); else await api.post("/api/finance/accounts", body);
      dlg.close(); finRenderTab();
    } catch (e) { toast(e.message, true); }
  };
  if ($("#fa-del")) $("#fa-del").onclick = async () => {
    if (await finConfirm(`Supprimer « ${fe(a.name)} » et tout son historique ?`, { ok: "Supprimer", danger: true })) {
      await api.del(`/api/finance/accounts/${id}`); finRenderTab();
    }
  };
  if ($("#fa-hold")) $("#fa-hold").onclick = () => finHoldingModal(id);
  dlg.querySelectorAll("[data-hedit]").forEach((b) => b.onclick = () => finHoldingModal(id, a.holdings.find((h) => h.id === +b.dataset.hedit)));
  dlg.querySelectorAll("[data-hdel]").forEach((b) => b.onclick = async () => { await api.del(`/api/finance/holdings/${b.dataset.hdel}`); finAccountModal(id); });
}

/** Add a position, or edit `h` (a position of the account). */
function finHoldingModal(accountId, h = null) {
  const v = h || { currency: "CHF", asset_class: "equity" };
  const curs = [...new Set(["CHF", "USD", "EUR", "GBP", v.currency])];
  const classes = { equity: "Actions", bonds: "Obligations", real_estate: "Immobilier coté", crypto: "Crypto", cash: "Liquidités", commodity: "Matières premières" };
  const num = (x) => (x == null ? "" : String(x));
  const dlg = finModal(`
    <h3>${h ? "Modifier la position" : "Nouvelle position"}</h3>
    <p class="muted">Le symbole Yahoo Finance fait vivre la valeur tout seul : <code>VT</code>, <code>CHSPI.SW</code>, <code>NESN.SW</code>, <code>NVDA</code>, <code>BTC-USD</code>…</p>
    <div class="field-row"><label>Symbole<input id="fh-sym" placeholder="VT" value="${fe(v.symbol || "")}" /></label>
      <label>ISIN<input id="fh-isin" value="${fe(v.isin || "")}" /></label>
      <label>N° de valeur<input id="fh-valor" inputmode="numeric" value="${fe(v.valor || "")}" placeholder="Valor" /></label></div>
    <label>Nom<input id="fh-name" placeholder="Vanguard Total World" value="${fe(v.name || "")}" /></label>
    <div class="field-row"><label>Quantité<input id="fh-qty" type="text" inputmode="decimal" value="${num(v.quantity)}" /></label>
      <label>Devise<select id="fh-cur">${curs.map((c) => `<option${c === v.currency ? " selected" : ""}>${fe(c)}</option>`).join("")}</select></label></div>
    <div class="field-row"><label>Classe<select id="fh-class">${Object.entries(classes).map(([k, l]) => `<option value="${k}"${k === v.asset_class ? " selected" : ""}>${l}</option>`).join("")}</select></label>
      <label>Prix manuel <small class="muted">(si pas de cours)</small><input id="fh-price" type="text" inputmode="decimal" value="${num(v.manual_price)}" /></label></div>
    <div class="modal-actions"><button type="button" class="btn btn-ghost" data-close>Annuler</button><button type="button" class="btn btn-primary" id="fh-save">${h ? "Enregistrer" : "Ajouter"}</button></div>`);
  $("#fh-save").onclick = async () => {
    const body = {
      symbol: $("#fh-sym").value, isin: $("#fh-isin").value, valor: $("#fh-valor").value,
      name: $("#fh-name").value || $("#fh-sym").value || "Position",
      quantity: finParse($("#fh-qty").value) || 0, currency: $("#fh-cur").value, asset_class: $("#fh-class").value,
      manual_price: finParse($("#fh-price").value), cost_basis: h ? h.cost_basis : null,
    };
    try {
      if (h) await api.put(`/api/finance/holdings/${h.id}`, body);
      else await api.post(`/api/finance/accounts/${accountId}/holdings`, body);
      dlg.close();
      finRenderTab();
      if (h) finAccountModal(accountId);  // back to the account, with the new value
    } catch (e) { toast(e.message, true); }
  };
}

/* ------------------------------------------------------------------ BUDGET */

function finBudgetData() {
  return Promise.all([api.get(`/api/finance/cashflow?scope=${fin.scope}&months=12`),
    api.get(`/api/finance/budget?scope=${fin.scope}`)]);
}

async function finBudget(body) {
  const [cf, bud] = await finBudgetData();
  if (!body.isConnected) return;
  const cats = fin.status.categories;
  body.innerHTML = `
    <div id="fin-bud-top">${finBudgetTop(cf, bud)}</div>
    ${finBudgetCard(bud)}
    <div class="card"><div class="fin-cardhead"><h3 class="fin-h3">🧾 Opérations${finQ("operations")}</h3>
      <div class="plan-actions"><button class="btn btn-ghost btn-sm" id="fin-ai-cat">🤖 Classer avec l'IA</button></div></div>
      <div class="fin-txfilters">
        <input type="search" id="fin-tx-q" placeholder="Rechercher…" value="${fe(fin.tx.q)}" />
        <select id="fin-tx-cat"><option value="">Toutes catégories</option>${Object.entries(cats).map(([k, c]) => `<option value="${k}"${fin.tx.category === k ? " selected" : ""}>${c.emoji} ${fe(c.label)}</option>`).join("")}</select>
        <label class="wz-check"><input type="checkbox" id="fin-tx-unc"${fin.tx.uncategorized ? " checked" : ""} /> à classer (<span id="fin-unc-n">${cf.uncategorized}</span>)</label>
      </div>
      <div id="fin-txlist"><p class="muted">Chargement…</p></div></div>`;
  finBudgetCharts(cf, bud);
  finBudgetWire(body);
  $("#fin-ai-cat").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "🤖 Réflexion…";
    try {
      const r = await api.post("/api/finance/transactions/categorize-ai");
      toast(r.error ? `IA indisponible : ${r.error}` : `${r.done} opération(s) classée(s)`, !!r.error);
      finRenderTab();
    } catch (err) { toast(err.message, true); e.target.disabled = false; }
  };
  let qt = null;
  $("#fin-tx-q").oninput = (e) => { clearTimeout(qt); qt = setTimeout(() => { fin.tx.q = e.target.value; fin.tx.offset = 0; finTxList(); }, 250); };
  $("#fin-tx-cat").onchange = (e) => { fin.tx.category = e.target.value; fin.tx.offset = 0; finTxList(); };
  $("#fin-tx-unc").onchange = (e) => { fin.tx.uncategorized = e.target.checked; fin.tx.offset = 0; finTxList(); };
  finTxList();
}

/** KPIs + charts of the Budget tab (or the "no statements yet" card). */
function finBudgetTop(cf, bud) {
  const has = cf.months_with_data > 0;
  const n = Math.max(1, cf.months_with_data);
  return `
    <div class="fin-kpis fin-kpis-4">
      <div class="fin-kpi"><span class="lbl">Entrées / mois${finQ("income_month")}</span><span class="val">${has ? kchf(cf.income / n) : "—"}</span></div>
      <div class="fin-kpi"><span class="lbl">Dépenses / mois${finQ("spending_month")}</span><span class="val">${has ? kchf(cf.spending / n) : kchf(bud.planned_total)}</span><span class="sub">${has ? `budget : ${kchf(bud.planned_total)}` : "budget estimé"}</span></div>
      <div class="fin-kpi"><span class="lbl">Épargne / mois${finQ("savings_month")}</span><span class="val">${has ? kchf(cf.savings / n) : "—"}</span></div>
      <div class="fin-kpi"><span class="lbl">Taux d'épargne${finQ("savings_rate")}</span><span class="val">${cf.savings_rate != null ? pct(cf.savings_rate) : "—"}</span></div>
    </div>
    ${has ? `<div class="fin-grid2"><div class="card"><div id="fin-cf"></div></div><div class="card"><div id="fin-sr"></div></div></div>
      <div class="fin-grid2"><div class="card"><div id="fin-cats"></div></div><div class="card"><div id="fin-sankey"></div></div></div>` :
      `<div class="card fin-empty"><h3>📥 Pas encore de relevés</h3>
        <p>Pour mesurer le vrai budget sans rien saisir : dans votre e-banking, exportez le relevé des 3 derniers mois au format
        <b>CAMT.053 (XML)</b> — toutes les banques suisses le proposent — ou en CSV, puis glissez-le ici. Revolut, Neon, Wise, Yuh : l'export CSV marche aussi.</p>
        <button class="btn btn-primary" onclick="document.getElementById('fin-file-input').click()">📎 Déposer un relevé</button></div>`}`;
}

function finBudgetCharts(cf, bud) {
  if (!(cf.months_with_data > 0) || !$("#fin-cf")) return;
  const cats = fin.status.categories;
  const n = Math.max(1, cf.months_with_data);
  const months = cf.months.filter((m) => m.income || m.spending);
  fcColumns($("#fin-cf"), {
    title: "Entrées et dépenses", sub: "par mois, hors virements entre vos comptes", x: months.map((m) => finMonthLabel(m.month)),
    series: [{ label: "Entrées", color: FC.s[0], values: months.map((m) => m.income) }, { label: "Dépenses", color: FC.s[1], values: months.map((m) => m.spending) }],
  });
  fcColumns($("#fin-sr"), {
    title: "Taux d'épargne", sub: "part des entrées qui reste", x: months.map((m) => finMonthLabel(m.month)),
    series: [{ label: "Taux d'épargne", color: FC.s[2], values: months.map((m) => m.savings_rate != null ? m.savings_rate * 100 : null) }],
    fmt: (v) => `${fcNum(v)} %`, fmtTip: (v) => `${fcNum(v)} %`, fmtTable: (v) => v == null ? "—" : `${fcNum(v)} %`,
    refLine: { value: 30, label: "30 %" },
  });
  const byCat = Object.entries(cf.by_category).map(([k, v]) => ({ key: k, label: cats[k]?.label || k, emoji: cats[k]?.emoji, value: v / n, ref: (bud.rows.find((r) => r.category === k) || {}).planned || 0 }));
  fcHBars($("#fin-cats"), { title: "Où part l'argent", sub: "moyenne par mois", items: byCat, color: FC.s[1], valueLabel: "Réel", refLabel: "Budget" });
  const top = byCat.slice(0, 7);
  const rest = byCat.slice(7).reduce((a, c) => a + c.value, 0);
  const targets = top.map((c) => ({ label: `${c.emoji || ""} ${c.label}`, value: c.value, color: c.key === "impots" ? FC.s[3] : FC.s[1] }));
  if (rest > 0) targets.push({ label: "Autres", value: rest, color: FC.s[1] });
  const save = cf.income / n - cf.spending / n;
  if (save > 0) targets.push({ label: "🏔️ Épargne", value: save, color: FC.s[2] });
  fcSankey($("#fin-sankey"), { title: "Le chemin d'un franc", sub: "entrées mensuelles moyennes → usages", sources: [{ label: "Entrées", value: cf.income / n }], targets });
}

/** An operation changed (category, spread): redraw the figures. The list and any budget being typed stay as they are. */
async function finBudgetRefresh() {
  const top = $("#fin-bud-top");
  if (!top) return;
  const [cf, bud] = await finBudgetData();
  if (!top.isConnected) return;
  finTipHide();
  top.innerHTML = finBudgetTop(cf, bud);
  finBudgetCharts(cf, bud);
  bud.rows.forEach((r) => {
    const c = document.querySelector(`.fin-brow[data-row="${r.category}"] .fin-bcat`);
    if (c) c.innerHTML = finBudCat(r);
  });
  if ($("#fin-unc-n")) $("#fin-unc-n").textContent = cf.uncategorized;
}

function finBudCat(r) {
  return `${r.emoji} ${fe(r.label)}${r.actual != null ? `<small>réel ${fcNum(r.actual)}</small>` : ""}`;
}

/** The planned budget. Ensemble: one column for the shared part + one per adult, all editable.
 *  One adult: their own lines (editable) + their share of the shared part (read-only). */
function finBudgetCard(bud) {
  const solo = bud.member_id != null;
  const adults = finAdults();
  const rows = bud.rows.filter((r) => r.category !== "impots");
  const money = (v) => (v ? fcNum(v) : "");
  const input = (who, cat, v) => `<input type="text" inputmode="decimal" data-who="${who}" data-cat="${cat}" value="${money(v)}" placeholder="0" aria-label="${fe(fin.status.categories[cat]?.label || cat)}" />`;
  const cols = solo
    ? [`<span>Perso</span>`, `<span>Part du commun <small>${pct(bud.share)}</small></span>`]
    : [`<span>👪 Commun</span>`, ...adults.map((m) => `<span>${m.emoji} ${fe(m.name)}</span>`)];
  const cells = (r) => solo
    ? [input(bud.member_id, r.category, r.personal[bud.member_id]), `<span class="fin-bshare">${r.common_share ? fcNum(r.common_share) : "—"}</span>`]
    : [input("common", r.category, r.common), ...adults.map((m) => input(m.id, r.category, r.personal[m.id]))];
  const who = solo ? finWho(bud.member_id) : "";
  return `<div class="card"><div class="fin-cardhead"><h3 class="fin-h3">🧮 Budget prévu (par mois)${finQ("budget")}</h3>
      <button class="btn btn-primary btn-sm" id="fin-bud-save">Enregistrer</button></div>
    <p class="fin-bud-note">${solo
      ? `Les dépenses de ${who} : ses lignes <b>perso</b>, plus sa part des lignes <b>communes</b> (${pct(bud.share)}, ${bud.split === "equal" ? "à parts égales" : "selon les revenus"} · Réglages › Le foyer). Le commun se modifie dans la vue 👪 Ensemble.`
      : `<b>Commun</b> = ce que vous partagez (loyer, courses…). La colonne d'une personne = ce qu'elle dépense seule (sport, téléphone…).`}</p>
    <div class="fin-btable" style="--cols:${cols.length}">
      <div class="fin-brow fin-bhead"><span></span>${cols.join("")}<span>Total</span></div>
      ${rows.map((r) => `<div class="fin-brow" data-row="${r.category}">
        <span class="fin-bcat">${finBudCat(r)}</span>
        ${cells(r).join("")}<b class="fin-btot" data-tot>${r.planned ? fcNum(r.planned) : "—"}</b></div>`).join("")}
      <div class="fin-brow fin-bfoot"><span>Total</span>${cols.map((_, i) => `<b data-coltot="${i}"></b>`).join("")}<b data-grand></b></div>
    </div>
    <p class="muted">Les impôts et les enfants ne sont pas dans le budget : le moteur les calcule lui-même (barèmes 2026, allocations, crèche).</p></div>`;
}

function finBudgetWire(body) {
  const table = body.querySelector(".fin-btable");
  if (!table) return;
  // live totals: per row, per column, and the grand total
  const recalc = () => {
    const colTot = [];
    let grand = 0;
    table.querySelectorAll("[data-row]").forEach((row) => {
      let t = 0;
      [...row.children].slice(1, -1).forEach((cell, i) => {
        const v = cell.tagName === "INPUT" ? finParse(cell.value) || 0 : finParse(cell.textContent) || 0;
        colTot[i] = (colTot[i] || 0) + v;
        t += v;
      });
      row.querySelector("[data-tot]").textContent = t ? fcNum(t) : "—";
      grand += t;
    });
    table.querySelectorAll("[data-coltot]").forEach((b) => { b.textContent = fcNum(colTot[+b.dataset.coltot] || 0); });
    table.querySelector("[data-grand]").textContent = fcNum(grand);
  };
  table.addEventListener("input", recalc);
  recalc();
  $("#fin-bud-save").onclick = async (e) => {
    const byWho = {};
    table.querySelectorAll("input[data-who]").forEach((i) => {
      (byWho[i.dataset.who] ||= {})[i.dataset.cat] = finParse(i.value) || 0;
    });
    e.target.disabled = true;
    try {
      for (const [who, vals] of Object.entries(byWho)) {
        await api.put(`/api/finance/budget${who === "common" ? "" : `?member_id=${who}`}`, vals);
      }
      toast("Budget enregistré");
      finRenderTab();
    } catch (err) { toast(err.message, true); e.target.disabled = false; }
  };
}

const FIN_SPREAD = [1, 2, 3, 6, 12, 24];

/** "417 / mois" under an amount that is spread over n months. */
function finTxPart(amount, n) {
  return n > 1 ? `<small>${fcNum(Math.abs(amount) / n)} / mois</small>` : "";
}

async function finTxList() {
  const t = fin.tx;
  const qs = new URLSearchParams({ limit: 100, offset: t.offset });
  if (t.q) qs.set("q", t.q);
  if (t.category) qs.set("category", t.category);
  if (t.uncategorized) qs.set("uncategorized", "true");
  const res = await api.get(`/api/finance/transactions?${qs}`);
  const cats = fin.status.categories;
  const box = $("#fin-txlist");
  if (!box) return;
  if (!res.items.length) { box.innerHTML = `<p class="muted">Aucune opération.</p>`; return; }
  const opts = (sel) => `<option value="">— à classer —</option>` + Object.entries(cats).map(([k, c]) => `<option value="${k}"${sel === k ? " selected" : ""}>${c.emoji} ${fe(c.label)}</option>`).join("");
  const spreadOpts = (n) => [...new Set([...FIN_SPREAD, n])].sort((a, b) => a - b)
    .map((k) => `<option value="${k}"${k === n ? " selected" : ""}>${k === 1 ? "Ce mois" : `÷ ${k} mois`}</option>`).join("");
  box.innerHTML = `<div class="fin-txs">${res.items.map((x) => {
    const n = x.spread_months || 1;
    return `
    <div class="fin-tx${x.category ? "" : " todo"}">
      <span class="d">${new Date(x.date).toLocaleDateString("fr-CH", { day: "2-digit", month: "2-digit", year: "2-digit" })}</span>
      <span class="t"><b>${fe(x.counterparty || x.description)}</b><small>${fe(x.counterparty ? x.description : "")} · ${fe(x.account_name)}${x.category_source === "llm" ? " · 🤖" : ""}</small></span>
      <span class="a ${x.amount < 0 ? "neg" : "pos"}">${x.amount > 0 ? "+" : ""}${fcNum(x.amount, { dec: 2 })}${finTxPart(x.amount, n)}</span>
      <span class="fin-tx-ctl">
        <select data-tx="${x.id}" aria-label="Catégorie">${opts(x.category)}</select>
        <select data-spread="${x.id}" data-amt="${x.amount}" class="${n > 1 ? "on" : ""}" aria-label="Compter sur combien de mois"
          title="Compter sur plusieurs mois — p. ex. une assurance payée pour toute l'année">${spreadOpts(n)}</select>
      </span>
    </div>`;
  }).join("")}</div>
    <div class="fin-pager"><span class="muted">${t.offset + 1}–${t.offset + res.items.length} sur ${res.total}</span>
      ${t.offset ? `<button class="btn btn-ghost btn-sm" id="fin-tx-prev">←</button>` : ""}
      ${t.offset + res.items.length < res.total ? `<button class="btn btn-ghost btn-sm" id="fin-tx-next">→</button>` : ""}</div>`;
  box.querySelectorAll("[data-tx]").forEach((s) => s.onchange = async () => {
    try {
      const r = await api.put(`/api/finance/transactions/${s.dataset.tx}`, { category: s.value, learn: true });
      toast(r.rule ? `Classé · règle « ${r.rule} » retenue${r.reapplied ? ` (${r.reapplied} autres)` : ""}` : "Classé");
      s.closest(".fin-tx").classList.toggle("todo", !s.value);
      finBudgetRefresh().catch(() => {});
    } catch (e) { toast(e.message, true); }
  });
  box.querySelectorAll("[data-spread]").forEach((s) => s.onchange = async () => {
    const n = +s.value, amt = +s.dataset.amt;
    try {
      await api.put(`/api/finance/transactions/${s.dataset.spread}`, { spread_months: n });
      s.classList.toggle("on", n > 1);
      const a = s.closest(".fin-tx").querySelector(".a");
      a.querySelector("small")?.remove();
      a.insertAdjacentHTML("beforeend", finTxPart(amt, n));
      toast(n > 1 ? `Compté ${fcNum(Math.abs(amt) / n)} par mois, pendant ${n} mois` : "Compté sur son mois seulement");
      finBudgetRefresh().catch(() => {});
    } catch (e) { toast(e.message, true); }
  });
  if ($("#fin-tx-prev")) $("#fin-tx-prev").onclick = () => { t.offset = Math.max(0, t.offset - 100); finTxList(); };
  if ($("#fin-tx-next")) $("#fin-tx-next").onclick = () => { t.offset += 100; finTxList(); };
}

/* ------------------------------------------------------------------ PLAN */

async function finPlan(body) {
  const sid = fin.scenarioId;
  const [proj, scenarios, events, settings] = await Promise.all([
    api.get(`/api/finance/projection?scope=${fin.scope}${sid ? `&scenario_id=${sid}` : ""}`),
    api.get("/api/finance/scenarios"), api.get("/api/finance/events"), api.get("/api/finance/settings"),
  ]);
  if (!body.isConnected) return;
  fin.projection = proj;
  if (!proj.ready) { body.innerHTML = `<div class="card"><p class="muted">Ajoutez au moins un adulte dans les réglages.</p></div>`; return; }
  const yrs = proj.years;
  const a = settings.assumptions;
  const style = settings.fire.style;
  const ages = proj.fi_ages ? Object.entries(proj.fi_ages).map(([n, x]) => `${fe(n)} ${x} ans`).join(" · ") : "";
  const kinds = fin.status.event_kinds;
  body.innerHTML = `
    <div class="card fin-plan-head">
      <div><p class="lbl">Avec ce plan, vous êtes libres en${finQ("fi_year")}</p>
        <p class="fin-big">${proj.fi_year || "—"}</p><p>${proj.fi_year ? ages : "Le plan ne trouve pas d'année avant 70 ans : réduisez les dépenses ou augmentez l'épargne."}</p>
        <p class="muted" id="fin-safe">Monte Carlo en cours…</p></div>
      <div class="fin-plan-ctl">
        <div class="seg" id="fin-style">${[["lean", "🎒 Frugale"], ["normal", "🏔️ Confortable"], ["fat", "🥂 Généreuse"]].map(([k, l]) =>
          `<button class="seg-btn${style === k ? " active" : ""}" data-style="${k}">${l} <small>${proj.fi_years[k] || "—"}</small></button>`).join("")}${finQ("fi_styles")}</div>
        <label><span>Scénario${finQ("scenario")}</span><select id="fin-scn"><option value="">Plan actuel</option>${scenarios.map((s) => `<option value="${s.id}"${sid === s.id ? " selected" : ""}>${fe(s.name)}</option>`).join("")}</select></label>
      </div>
    </div>
    <div class="card"><div id="fin-proj"></div></div>
    <div class="fin-grid2"><div class="card"><div id="fin-mc"></div></div><div class="card"><div id="fin-curve"></div></div></div>
    <div class="fin-grid2"><div class="card"><div id="fin-sources"></div></div><div class="card"><div id="fin-taxes"></div></div></div>
    <div class="fin-grid2">
      <div class="card"><div class="fin-cardhead"><h3 class="fin-h3">📅 Événements de vie${finQ("events")}</h3><button class="btn btn-primary btn-sm" id="fin-ev-add">+ Événement</button></div>
        ${events.filter((e) => !e.scenario_id || e.scenario_id === sid).map((e) => `
          <div class="fin-ev"><span>${e.start_date ? e.start_date.slice(0, 4) : ""}${e.end_date ? "–" + e.end_date.slice(0, 4) : ""}</span>
            <span class="info"><b>${fe((kinds[e.kind] || e.kind).split(" (")[0])}</b><small>${e.member_id ? finWho(e.member_id) : "Ménage"} · ${fe(finEventParams(e))}${e.scenario_id ? " · scénario" : ""}</small></span>
            <button class="icon-btn" data-ev="${e.id}" title="Modifier">✏️</button></div>`).join("") || `<p class="muted">Aucun événement.</p>`}
      </div>
      <div class="card"><div class="fin-cardhead"><h3 class="fin-h3">🔀 Scénarios${finQ("scenarios")}</h3>
        <div class="plan-actions"><button class="btn btn-ghost btn-sm" id="fin-scn-marry">💍 Et si on se mariait ?</button><button class="btn btn-primary btn-sm" id="fin-scn-add">+ Scénario</button></div></div>
        ${scenarios.map((s) => `<div class="fin-ev"><span>🔀</span><span class="info"><b>${fe(s.name)}</b><small>${fe(s.description || JSON.stringify(s.overrides))}</small></span><button class="icon-btn" data-scndel="${s.id}" title="Supprimer">🗑️</button></div>`).join("") || `<p class="muted">Comparez des choix : se marier, acheter, réduire son temps de travail…</p>`}
        ${scenarios.length ? `<button class="btn btn-ghost btn-sm" id="fin-scn-cmp">Comparer côte à côte</button><div id="fin-cmp"></div>` : ""}
      </div>
    </div>
    <details class="card fin-assump"><summary><h3 class="fin-h3">⚙️ Hypothèses${finQ("assumptions")}</h3><span class="muted">rendements, inflation, retraits, âge AVS…</span></summary>
      <div class="fin-assump-grid">
        ${finAssumpInput("equity_real_return_pct", "Actions, rendement réel attendu (%)", a)}
        ${finAssumpInput("equity_vol_pct", "Actions, volatilité (%)", a)}
        ${finAssumpInput("bond_real_return_pct", "Obligations, rendement réel (%)", a)}
        ${finAssumpInput("cash_real_return_pct", "Compte épargne, rendement réel (%)", a)}
        ${finAssumpInput("inflation_pct", "Inflation (%)", a)}
        ${finAssumpInput("target_equity_pct", "Part en actions de l'épargne (%)", a, "selon les comptes")}
        ${finAssumpInput("salary_growth_real_pct", "Hausse réelle des salaires (%/an)", a)}
        ${finAssumpInput("salary_growth_until_age", "… jusqu'à l'âge de", a)}
        ${finAssumpInput("lpp_interest_nominal_pct", "Intérêt LPP crédité (%)", a)}
        ${finAssumpInput("pillar3a_accounts", "Nombre de comptes 3a (retraits échelonnés)", a)}
        ${finAssumpInput("pillar3a_withdraw_from_age", "Retraits 3a dès", a)}
        ${finAssumpInput("avs_start_age", "Rente AVS dès (63-70)", a)}
        ${finAssumpInput("fi_spending_change_pct", "Dépenses une fois libres (% vs aujourd'hui)", a)}
        ${finAssumpInput("liquid_floor", "Garder toujours au moins (CHF)", a)}
        ${finAssumpInput("swr_pct", "Taux de retrait « règle du pouce » (%)", a)}
        ${finAssumpInput("horizon_age", "Horizon du plan (âge)", a)}
        <label>2e pilier à la retraite<select data-assump="lpp_withdrawal">${[["capital", "Capital"], ["rente", "Rente"], ["half", "Moitié-moitié"]].map(([k, l]) => `<option value="${k}"${a.lpp_withdrawal === k ? " selected" : ""}>${l}</option>`).join("")}</select></label>
        <label class="wz-check"><input type="checkbox" data-assump="employer_maternity_topup"${a.employer_maternity_topup ? " checked" : ""} /> L'employeur paie 100 % pendant le congé maternité</label>
      </div>
      <div class="modal-actions"><button class="btn btn-primary" id="fin-assump-save">Recalculer</button></div>
      <p class="muted fin-src">Toutes les projections sont en francs d'aujourd'hui. Le moteur applique les barèmes 2026 (IFD, Vaud, coefficient de ${fe(settings.commune)} ${settings.communal_coefficient} %), la baisse vaudoise de 12 % dès 2027, l'AVS à 13 rentes, les cotisations AVS des personnes sans activité lucrative, l'impôt sur les retraits en capital.</p>
    </details>`;
  // projection chart
  const x = yrs.map((y) => String(y.year));
  const fiMarker = proj.fi_year ? [{ x: String(proj.fi_year), label: "🏁 liberté" }] : [];
  fcArea($("#fin-proj"), {
    title: "Patrimoine projeté", help: finQ("proj_chart"), sub: "en francs d'aujourd'hui, chemin médian", x, stacked: true, markers: fiMarker,
    series: [
      { label: "Disponible", color: FC.s[0], values: yrs.map((y) => Math.max(0, y.liquid)) },
      { label: "Prévoyance", color: FC.s[1], values: yrs.map((y) => y.pension_assets) },
      { label: "Immobilier net", color: FC.s[2], values: yrs.map((y) => y.property_equity) },
    ],
    tipExtra: (i) => `<div class="fc-tip-band">Âges<b>${yrs[i].ages.join(" / ")}</b></div>`,
  });
  // income sources
  fcArea($("#fin-sources"), {
    title: "D'où vient l'argent", help: finQ("sources_chart"), sub: "chaque année : salaires, rentes, capitaux, retraits", x, stacked: true,
    series: [
      { label: "Salaires & revenus", color: FC.s[0], values: yrs.map((y) => y.salary + y.side + y.allowances) },
      { label: "Rentes AVS / LPP", color: FC.s[1], values: yrs.map((y) => y.pension) },
      { label: "Capitaux 2e / 3e pilier", color: FC.s[2], values: yrs.map((y) => Math.max(0, y.capital - y.capital_tax)) },
      { label: "Retraits du portefeuille", color: FC.s[3], values: yrs.map((y) => Math.max(0, -y.net_flow)) },
    ],
    tipExtra: (i) => `<div class="fc-tip-band">Dépenses<b>${chf(yrs[i].spending)}</b></div><div class="fc-tip-band">Impôts<b>${chf(yrs[i].taxes)}</b></div>`,
  });
  fcColumns($("#fin-taxes"), {
    title: "Impôts & cotisations AVS", help: finQ("taxes_chart"), sub: "revenu + fortune + retraits en capital + AVS non-actif", x,
    series: [{ label: "Impôts", color: FC.s[1], values: yrs.map((y) => y.taxes + y.capital_tax + y.avs_nonactive) }],
  });
  // controls
  body.querySelectorAll("[data-style]").forEach((b) => b.onclick = async () => { await api.put("/api/finance/settings", { fire: { style: b.dataset.style } }); finRenderTab(); });
  $("#fin-scn").onchange = (e) => { fin.scenarioId = e.target.value ? +e.target.value : null; finRenderTab(); };
  $("#fin-assump-save").onclick = async () => {
    const patch = {};
    body.querySelectorAll("[data-assump]").forEach((i) => {
      const k = i.dataset.assump;
      if (i.type === "checkbox") patch[k] = i.checked;
      else if (i.tagName === "SELECT") patch[k] = i.value;
      else patch[k] = i.value === "" ? (k === "target_equity_pct" ? null : a[k]) : finParse(i.value);
    });
    await api.put("/api/finance/settings", { assumptions: patch });
    toast("Hypothèses enregistrées — recalcul…");
    finRenderTab();
  };
  $("#fin-ev-add").onclick = () => finEventModal(null, sid);
  body.querySelectorAll("[data-ev]").forEach((b) => b.onclick = () => finEventModal(events.find((e) => e.id === +b.dataset.ev), sid));
  $("#fin-scn-add").onclick = () => finScenarioModal();
  $("#fin-scn-marry").onclick = async () => {
    const s = await api.post("/api/finance/scenarios", { name: "💍 Mariés l'an prochain", description: "Imposition commune, AVS de couple, splitting.", overrides: { marriage_year: new Date().getFullYear() + 1 } });
    toast("Scénario créé");
    fin.scenarioId = null;
    await finRenderTab();
    setTimeout(() => finCompare([s.id]), 300);
  };
  body.querySelectorAll("[data-scndel]").forEach((b) => b.onclick = async () => {
    if (await finConfirm("Supprimer ce scénario ?", { ok: "Supprimer", danger: true })) {
      await api.del(`/api/finance/scenarios/${b.dataset.scndel}`);
      if (fin.scenarioId === +b.dataset.scndel) fin.scenarioId = null;
      finRenderTab();
    }
  });
  if ($("#fin-scn-cmp")) $("#fin-scn-cmp").onclick = () => finCompare(scenarios.map((s) => s.id));
  // Monte Carlo (async, it takes a second)
  api.get(`/api/finance/montecarlo?scope=${fin.scope}&runs=1000${sid ? `&scenario_id=${sid}` : ""}`).then((mc) => {
    fin.mc = mc;
    if (!mc.ready || !body.isConnected || !$("#fin-mc")) return;
    const m = mc.plan;
    const xs = m.years.map(String);
    fcArea($("#fin-mc"), {
      title: `Argent disponible — ${pct(m.success_rate)} de réussite`, help: finQ("mc_chart"), sub: `${fcNum(m.runs)} marchés tirés au hasard : la bande couvre 80 % des cas`, x: xs,
      band: { lo: m.liquid.p10, hi: m.liquid.p90, lo2: m.liquid.p25, hi2: m.liquid.p75, color: FC.s[0], label: "10e – 90e centile" },
      series: [{ label: "Médiane", color: FC.s[0], values: m.liquid.p50 }, { label: "Plan déterministe", color: FC.soft, values: m.deterministic, dashed: true }],
      markers: proj.fi_year ? [{ x: String(proj.fi_year), label: "🏁" }] : [],
    });
    fcColumns($("#fin-curve"), {
      title: "Réussite selon l'année d'arrêt", help: finQ("curve_chart"), sub: `probabilité de ne jamais manquer d'argent · seuil ${pct(mc.target)}`,
      x: mc.curve.map((c) => String(c.year)),
      series: [{ label: "Réussite", color: FC.s[0], values: mc.curve.map((c) => c.success_rate * 100) }],
      fmt: (v) => `${fcNum(v)} %`, fmtTip: (v) => `${fcNum(v)} %`, fmtTable: (v) => `${fcNum(v)} %`,
      refLine: { value: mc.target * 100, label: `${fcNum(mc.target * 100)} %` },
      tipExtra: (i) => `<div class="fc-tip-band">Âges<b>${Object.entries(mc.curve[i].ages).map(([n, x]) => `${fe(n)} ${x}`).join(", ")}</b></div>`,
    });
    const safe = $("#fin-safe");
    if (safe) safe.innerHTML = mc.safe_year
      ? `🛡️ Version prudente (${pct(mc.target)} de réussite) : <b>${mc.safe_year}</b>${finQ("safe_year")}`
      : `🛡️ Aucune année testée n'atteint ${pct(mc.target)} de réussite.${finQ("safe_year")}`;
  }).catch(() => { const s = $("#fin-safe"); if (s) s.textContent = ""; });
}

function finAssumpInput(k, label, a, ph = "") {
  return `<label>${fe(label)}<input type="text" inputmode="decimal" data-assump="${k}" value="${a[k] ?? ""}" placeholder="${fe(ph)}" /></label>`;
}

function finEventParams(e) {
  const p = e.params || {};
  const bits = [];
  if (p.work_pct != null) bits.push(`${p.work_pct} %`);
  if (p.price) bits.push(`prix ${kchf(p.price)}`);
  if (p.down_payment) bits.push(`apport ${kchf(p.down_payment)}`);
  if (p.annual) bits.push(`${kchf(p.annual)} / an`);
  if (p.amount) bits.push(kchf(p.amount));
  if (p.annual_delta) bits.push(`${p.annual_delta > 0 ? "+" : ""}${kchf(p.annual_delta)} / an`);
  if (p.cost_factor) bits.push(`coût de la vie ${Math.round(p.cost_factor * 100)} %`);
  if (p.gross_annual) bits.push(`${kchf(p.gross_annual)} brut`);
  if (p.pct_change) bits.push(`${p.pct_change > 0 ? "+" : ""}${p.pct_change} %`);
  return bits.join(" · ") || e.label || "";
}

const FIN_EVENT_FIELDS = {
  part_time: [["work_pct", "Taux d'activité (%)"]],
  career_break: [],
  salary_change: [["gross_annual", "Nouveau brut annuel (CHF)"], ["pct_change", "… ou variation (%)"]],
  side_income: [["annual", "Net par an (CHF)"]],
  home_purchase: [["price", "Prix (CHF)"], ["down_payment", "Apport total (CHF)"], ["use_3a", "Dont retiré du 3a (CHF)"], ["use_lpp", "Dont retiré de la LPP (CHF)"], ["mortgage_rate_pct", "Taux hypothécaire (%)"]],
  marriage: [],
  move_abroad: [["cost_factor", "Coût de la vie (1 = Suisse, 0.7 = 30 % moins cher)"], ["tax_rate_pct", "Impôt sur le revenu là-bas (%)"], ["capital_tax_pct", "Impôt sur les retraits en capital (%)"]],
  lump_sum: [["amount", "Montant (négatif = dépense)"]],
  expense_change: [["annual_delta", "Variation des dépenses par an (CHF)"]],
};

function finEventModal(ev, scenarioId) {
  const e = ev || { kind: "part_time", params: {}, member_id: finAdults()[0]?.id, start_date: "", end_date: "", scenario_id: scenarioId };
  const kinds = fin.status.event_kinds;
  const fields = () => (FIN_EVENT_FIELDS[$("#fe-kind").value] || []).map(([k, l]) => `<label>${fe(l)}<input type="text" inputmode="decimal" data-ep="${k}" value="${e.params?.[k] ?? ""}" /></label>`).join("");
  const dlg = finModal(`
    <h3>${ev ? "Modifier l'événement" : "Nouvel événement"}</h3>
    <label>Quoi<select id="fe-kind">${Object.entries(kinds).map(([k, l]) => `<option value="${k}"${e.kind === k ? " selected" : ""}>${fe(l.split(" (")[0])}</option>`).join("")}</select></label>
    <label>Qui<select id="fe-who"><option value="">Le ménage</option>${finAdults().map((m) => `<option value="${m.id}"${e.member_id === m.id ? " selected" : ""}>${m.emoji} ${fe(m.name)}</option>`).join("")}</select></label>
    <div class="field-row"><label>Dès le<input type="date" id="fe-start" value="${fe(e.start_date || "")}" /></label>
      <label>Jusqu'au <small class="muted">(facultatif)</small><input type="date" id="fe-end" value="${fe(e.end_date || "")}" /></label></div>
    <div id="fe-fields">${fields()}</div>
    <label>Dans<select id="fe-scn"><option value="">le plan actuel</option></select></label>
    <div class="modal-actions">
      ${ev ? `<button type="button" class="btn btn-ghost fin-danger-btn" id="fe-del">Supprimer</button>` : ""}
      <button type="button" class="btn btn-ghost" data-close>Annuler</button><button type="button" class="btn btn-primary" id="fe-save">Enregistrer</button></div>`);
  api.get("/api/finance/scenarios").then((sc) => {
    $("#fe-scn").innerHTML = `<option value="">le plan actuel</option>` + sc.map((s) => `<option value="${s.id}"${e.scenario_id === s.id ? " selected" : ""}>scénario « ${fe(s.name)} »</option>`).join("");
  });
  $("#fe-kind").onchange = () => { $("#fe-fields").innerHTML = fields(); };
  $("#fe-save").onclick = async () => {
    const params = {};
    dlg.querySelectorAll("[data-ep]").forEach((i) => { const v = finParse(i.value); if (v != null) params[i.dataset.ep] = v; });
    const body = { kind: $("#fe-kind").value, member_id: $("#fe-who").value ? +$("#fe-who").value : null, start_date: $("#fe-start").value || null,
      end_date: $("#fe-end").value || null, params, scenario_id: $("#fe-scn").value ? +$("#fe-scn").value : null, enabled: true, label: "" };
    if (!body.start_date) { toast("Quand ?", true); return; }
    try {
      if (ev) await api.put(`/api/finance/events/${ev.id}`, body); else await api.post("/api/finance/events", body);
      dlg.close(); finRenderTab();
    } catch (err) { toast(err.message, true); }
  };
  if ($("#fe-del")) $("#fe-del").onclick = async () => { await api.del(`/api/finance/events/${ev.id}`); dlg.close(); finRenderTab(); };
}

function finScenarioModal() {
  const dlg = finModal(`
    <h3>Nouveau scénario</h3>
    <p class="muted">Un scénario reprend le plan actuel et change quelques réglages. Ajoutez-lui ensuite des événements (achat, temps partiel…).</p>
    <label>Nom<input id="fs-name" placeholder="Achat à Pully en 2029" /></label>
    <label>Description<input id="fs-desc" /></label>
    <div class="field-row"><label>Mariage en <small class="muted">(année)</small><input id="fs-marry" type="text" inputmode="numeric" /></label>
      <label>Dépenses une fois libres (%)<input id="fs-fi" type="text" inputmode="numeric" placeholder="inchangé" /></label></div>
    <div class="field-row"><label>Actions (%)<input id="fs-eq" type="text" inputmode="numeric" placeholder="inchangé" /></label>
      <label>Rente AVS dès<input id="fs-avs" type="text" inputmode="numeric" placeholder="65" /></label></div>
    <div class="modal-actions"><button type="button" class="btn btn-ghost" data-close>Annuler</button><button type="button" class="btn btn-primary" id="fs-save">Créer</button></div>`);
  $("#fs-save").onclick = async () => {
    const ov = { assumptions: {} };
    if (finParse($("#fs-marry").value)) ov.marriage_year = finParse($("#fs-marry").value);
    if (finParse($("#fs-fi").value) != null) ov.assumptions.fi_spending_change_pct = finParse($("#fs-fi").value);
    if (finParse($("#fs-eq").value) != null) ov.assumptions.target_equity_pct = finParse($("#fs-eq").value);
    if (finParse($("#fs-avs").value)) ov.assumptions.avs_start_age = finParse($("#fs-avs").value);
    await api.post("/api/finance/scenarios", { name: $("#fs-name").value || "Scénario", description: $("#fs-desc").value, overrides: ov });
    dlg.close(); finRenderTab();
  };
}

async function finCompare(ids) {
  const box = $("#fin-cmp");
  if (!box) return;
  box.innerHTML = `<p class="muted">Calcul…</p>`;
  const rows = await api.get(`/api/finance/scenarios/compare?ids=${ids.join(",")}&scope=${fin.scope}`);
  box.innerHTML = `<table class="fin-cmp"><thead><tr><th></th>${rows.map((r) => `<th>${fe(r.name)}</th>`).join("")}</tr></thead><tbody>
    <tr><td>Libres en</td>${rows.map((r) => `<td>${r.fi_year || "—"}</td>`).join("")}</tr>
    <tr><td>Impôts sur 10 ans</td>${rows.map((r) => `<td>${kchf(r.taxes_10y)}</td>`).join("")}</tr>
    <tr><td>Impôts sur toute la vie</td>${rows.map((r) => `<td>${kchf(r.taxes_lifetime)}</td>`).join("")}</tr>
    <tr><td>Patrimoine à 65 ans</td>${rows.map((r) => `<td>${kchf(r.net_worth_65)}</td>`).join("")}</tr>
    <tr><td>Il faudrait aujourd'hui</td>${rows.map((r) => `<td>${kchf(r.required_liquid_now)}</td>`).join("")}</tr>
  </tbody></table>
  ${rows.length > 1 ? `<p class="fin-cmp-diff">${rows.slice(1).map((r) => `${fe(r.name)} : ${r.taxes_10y < rows[0].taxes_10y ? "💚" : "🧾"} ${kchf(Math.abs(r.taxes_10y - rows[0].taxes_10y))} d'impôts ${r.taxes_10y < rows[0].taxes_10y ? "en moins" : "en plus"} sur 10 ans`).join("<br>")}</p>` : ""}
  <div id="fin-cmp-chart"></div>`;
  const xs = rows[0].liquid_series.map((p) => String(p[0]));
  fcArea($("#fin-cmp-chart"), {
    title: "Argent disponible par scénario", x: xs,
    series: rows.slice(0, 4).map((r, i) => ({ label: r.name, color: FC.s[i], values: r.liquid_series.map((p) => p[1]) })),
  });
}

/* ------------------------------------------------------------------ OBJECTIFS */

async function finGoals(body) {
  const goals = await api.get("/api/finance/goals");
  if (!body.isConnected) return;
  body.innerHTML = `
    <div class="section-head fin-subhead"><h3>🎯 Objectifs</h3><button class="btn btn-primary btn-sm" id="fin-goal-add">+ Objectif</button></div>
    <div class="fin-goals">${goals.map((g) => `
      <div class="card fin-goal${g.achieved_at ? " done" : ""}">
        <div class="fin-goal-top"><span class="emo">${g.emoji}</span><div><b>${fe(g.name)}</b><small>${g.target_date ? `avant le ${new Date(g.target_date).toLocaleDateString("fr-CH")}` : "sans échéance"}</small></div>
          <button class="icon-btn" data-goal="${g.id}" title="Modifier">✏️</button></div>
        <p class="fin-goal-val"><b>${kchf(g.current)}</b> / ${kchf(g.target)}</p>
        ${fcMeter(g.progress, 1, g.achieved_at ? FC.s[2] : FC.s[0])}
        <small class="muted">${g.achieved_at ? "✅ Atteint !" : g.monthly_needed ? `≈ ${chf(g.monthly_needed)} par mois pour y arriver` : pct(g.progress)}</small>
      </div>`).join("") || `<p class="muted">Aucun objectif.</p>`}</div>`;
  $("#fin-goal-add").onclick = () => finGoalModal();
  body.querySelectorAll("[data-goal]").forEach((b) => b.onclick = () => finGoalModal(goals.find((g) => g.id === +b.dataset.goal)));
  if (goals.some((g) => g.just_achieved)) fcConfetti();
}

async function finGoalModal(g = null) {
  const accounts = await api.get("/api/finance/accounts");
  const prof = fin.overview?.spending_profile?.annual || 60000;
  const tpl = [["🛟", "Coussin de sécurité (6 mois)", "emergency", Math.round(prof / 2 / 1000) * 1000], ["👶", "Fonds bébé", "baby", 10000], ["🏡", "Apport logement", "home", 200000], ["🧳", "Grand voyage", "travel", 15000]];
  const sel = new Set(g?.account_ids || []);
  const dlg = finModal(`
    <h3>${g ? "Modifier l'objectif" : "Nouvel objectif"}</h3>
    ${g ? "" : `<div class="chip-row">${tpl.map(([e, n, k, v], i) => `<button type="button" class="chip" data-tpl="${i}">${e} ${fe(n)}</button>`).join("")}</div>`}
    <div class="field-row"><label class="wz-emoji">Emoji<input id="fg-emoji" value="${fe(g?.emoji || "🎯")}" maxlength="4" /></label><label>Nom<input id="fg-name" value="${fe(g?.name || "")}" /></label></div>
    <div class="field-row"><label>Montant visé${finMoneyInput("fg-target", g?.target)}</label><label>Pour le<input type="date" id="fg-date" value="${fe(g?.target_date || "")}" /></label></div>
    <label>Avec quels comptes ? <small class="muted">(aucun coché = tout le disponible)</small></label>
    <div class="fin-goal-accs">${accounts.filter((a) => a.group === "liquid" || a.kind === "pillar3a").map((a) => `<label class="wz-check"><input type="checkbox" data-gacc="${a.id}"${sel.has(a.id) ? " checked" : ""} /> ${a.emoji} ${fe(a.name)}</label>`).join("")}</div>
    <input type="hidden" id="fg-kind" value="${fe(g?.kind || "custom")}" />
    <div class="modal-actions">${g ? `<button type="button" class="btn btn-ghost fin-danger-btn" id="fg-del">Supprimer</button>` : ""}
      <button type="button" class="btn btn-ghost" data-close>Annuler</button><button type="button" class="btn btn-primary" id="fg-save">Enregistrer</button></div>`);
  dlg.querySelectorAll("[data-tpl]").forEach((b) => b.onclick = () => {
    const [e, n, k, v] = tpl[+b.dataset.tpl];
    $("#fg-emoji").value = e; $("#fg-name").value = n.replace(/ \(.*\)/, ""); $("#fg-kind").value = k; $("#fg-target").value = fcNum(v);
  });
  $("#fg-save").onclick = async () => {
    const ids = [...dlg.querySelectorAll("[data-gacc]:checked")].map((i) => +i.dataset.gacc);
    const body = { name: $("#fg-name").value || "Objectif", emoji: $("#fg-emoji").value || "🎯", kind: $("#fg-kind").value, target: finParse($("#fg-target").value) || 0,
      target_date: $("#fg-date").value || null, account_ids: ids, scope: fin.scope };
    try {
      if (g) await api.put(`/api/finance/goals/${g.id}`, body); else await api.post("/api/finance/goals", body);
      dlg.close(); finRenderTab();
    } catch (e) { toast(e.message, true); }
  };
  if ($("#fg-del")) $("#fg-del").onclick = async () => { await api.del(`/api/finance/goals/${g.id}`); dlg.close(); finRenderTab(); };
}

/* ------------------------------------------------------------------ DOCUMENTS */

async function finUpload(files) {
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  const type = $("#fin-up-type")?.value;  // chosen type guides the AI; empty = it guesses
  if (type) fd.append("doc_type", type);
  toast(`Envoi de ${files.length} document(s)…`);
  try {
    const res = await fetch("/api/finance/documents", { method: "POST", body: fd });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Erreur ${res.status}`);
    const docs = await res.json();
    toast(docs.some((d) => d.duplicate) ? "Déjà reçu : on le retrouve dans Documents" : "Reçu — lecture en cours 🤖");
    finGo("docs");
  } catch (e) { toast(e.message, true); }
}

async function finDocs(body) {
  const [docs, ai] = await Promise.all([api.get("/api/finance/documents"), api.get("/api/finance/llm/status").catch(() => null)]);
  if (!body.isConnected) return;
  const statusLbl = { pending: "⏳ en attente", parsing: "🤖 lecture…", review: "👀 à vérifier", applied: "✅ appliqué", error: "⚠️ erreur", ignored: "🙈 ignoré" };
  const aiLine = ai ? (ai.local.reachable
    ? `🟢 IA locale prête : <b>${fe(ai.local.model)}</b>${ai.local.model_installed === false ? ` <span class="fin-stale">— modèle pas installé (<code>ollama pull ${fe(ai.local.model)}</code>)</span>` : ""}`
    : `🟠 IA locale injoignable (${fe(ai.local.base_url)}). ${ai.mode !== "local_only" && ai.cloud.configured ? "Les PDF et photos passeront par le cloud (OpenRouter)." : "Les PDF et photos attendront."} Les exports CAMT/CSV n'ont pas besoin d'IA.`) : "";
  body.innerHTML = `
    <div class="card fin-drop" id="fin-drop-card">
      <div class="fin-drop-ico">📎</div>
      <div><h3>Glissez vos documents ici</h3>
        <p>Relevés <b>CAMT.053</b> (XML) ou <b>CSV</b> · positions de courtier · fiches et certificats de salaire · certificat <b>LPP</b> ·
        attestations 3a · décision de taxation · police LAMal. PDF ou photo.</p>
        <p class="muted fin-ai-line">${aiLine}</p></div>
      <div class="fin-drop-actions">
        <select id="fin-up-type" title="Indiquer le type aide l'IA : elle ne cherche que les bons champs">
          <option value="">🤖 Type : l'IA devine</option>
          ${Object.entries(fin.status.doc_types).map(([k, l]) => `<option value="${k}">📌 ${fe(l)}</option>`).join("")}
        </select>
        <button class="btn btn-primary" id="fin-pick">Choisir des fichiers</button>
      </div>
    </div>
    <div class="fin-docs">${docs.map((d) => `
      <div class="card fin-doc fin-doc-${d.status}">
        <div class="info"><strong>${fe(d.filename)}</strong>
          <small>${statusLbl[d.status] || d.status}${d.doc_type_label ? ` · ${d.hint_type ? `<span title="type choisi par vous">📌 </span>` : ""}${fe(d.doc_type_label)}` : ""}${d.engine ? ` · <span title="lu par">${fe(d.engine)}</span>` : ""} · ${new Date(d.uploaded_at + "Z").toLocaleDateString("fr-CH")}</small>
          ${d.extracted?.summary ? `<small>${fe(d.extracted.summary)}</small>` : ""}${d.error ? `<small class="fin-stale">${fe(d.error)}</small>` : ""}</div>
        <div class="plan-actions">
          ${d.status === "review" ? `<button class="btn btn-primary btn-sm" data-review="${d.id}">Vérifier</button>` : ""}
          ${["error", "review", "ignored"].includes(d.status) ? `<button class="btn btn-ghost btn-sm" data-reparse="${d.id}">Relire</button>` : ""}
          ${d.has_debug ? `<button class="icon-btn" data-debug="${d.id}" title="Sous le capot : ce que l'IA a reçu, pensé et répondu">🔍</button>` : ""}
          <a class="icon-btn" href="/api/finance/documents/${d.id}/file" target="_blank" rel="noopener" title="Ouvrir">👁️</a>
          <button class="icon-btn" data-docdel="${d.id}" title="Supprimer">🗑️</button>
        </div>
      </div>`).join("") || `<p class="muted">Aucun document pour l'instant.</p>`}</div>`;
  $("#fin-pick").onclick = () => $("#fin-file-input").click();
  body.querySelectorAll("[data-review]").forEach((b) => b.onclick = () => finReview(+b.dataset.review));
  body.querySelectorAll("[data-reparse]").forEach((b) => b.onclick = async () => { await api.post(`/api/finance/documents/${b.dataset.reparse}/reparse`); finRenderTab(); });
  body.querySelectorAll("[data-debug]").forEach((b) => b.onclick = () => finDebug(+b.dataset.debug));
  body.querySelectorAll("[data-docdel]").forEach((b) => b.onclick = async () => {
    if (await finConfirm("Supprimer ce document ? (Les opérations déjà importées restent.)", { ok: "Supprimer", danger: true })) {
      await api.del(`/api/finance/documents/${b.dataset.docdel}`); finRenderTab();
    }
  });
  clearTimeout(fin.pollTimer);
  if (docs.some((d) => d.status === "pending" || d.status === "parsing")) {
    fin.pollTimer = setTimeout(() => { if (fin.tab === "docs" && $("#view-finance").classList.contains("active")) finRenderTab(); }, 2500);
  }
}

const FIN_FIELD_LABELS = {
  person_name: "Personne", employer: "Employeur", period: "Période", gross_monthly: "Brut mensuel", net_monthly: "Net mensuel",
  work_pct: "Taux d'activité (%)", salaries_per_year: "Salaires par an", avs_ai_apg: "AVS/AI/APG", ac: "AC", aanp: "AANP", ijm: "IJM",
  lpp_employee: "LPP (salarié)", family_allowance: "Allocations familiales", year: "Année", gross_total: "Salaire brut (ch. 8)",
  net_total: "Salaire net (ch. 11)", avs_ai_apg_ac_aanp: "Cotisations sociales (ch. 9)", lpp_ordinary: "LPP ordinaire (ch. 10.1)",
  lpp_buyin: "Rachat LPP (ch. 10.2)", free_transport: "Transport gratuit (F)", canteen: "Cantine (G)", fund: "Caisse", date: "Date",
  savings_balance: "Avoir de vieillesse", savings_credits_annual: "Bonifications / an", employee_annual: "Cotisation salarié / an",
  employer_annual: "Cotisation employeur / an", insured_salary: "Salaire assuré", projected_capital_65: "Capital projeté à 65 ans",
  projected_pension_65: "Rente projetée à 65 ans", conversion_rate_pct: "Taux de conversion (%)", buyin_potential: "Rachat possible",
  provider: "Fournisseur", balance: "Solde", contributions_year: "Versements de l'année", taxable_income: "Revenu imposable (ICC)",
  rate_income: "Revenu déterminant pour le taux", taxable_wealth: "Fortune imposable", icc_amount: "Impôt cantonal + communal",
  ifd_amount: "Impôt fédéral", ifd_taxable_income: "Revenu imposable IFD", authority: "Autorité", insurer: "Assureur",
  monthly_premium: "Prime mensuelle", franchise: "Franchise", model: "Modèle", currency: "Devise", description: "Description",
};

async function finReview(id) {
  const [d, accounts] = await Promise.all([api.get(`/api/finance/documents/${id}`), api.get("/api/finance/accounts")]);
  const ex = d.extracted || {};
  const t = ex.doc_type;
  // Read by the AI (not a CAMT/CSV reader): its type can be wrong, so it can be changed and re-read.
  const aiRead = !!d.debug || (t && t !== "bank_statement" && t !== "broker_statement");
  const adults = finAdults();
  const memberSel = (sel) => `<label>Pour qui ?<select id="fr-member"><option value="">—</option>${adults.map((m) => `<option value="${m.id}"${sel === m.id ? " selected" : ""}>${m.emoji} ${fe(m.name)}</option>`).join("")}</select></label>`;
  const accSel = (kinds, sel) => `<label>Compte<select id="fr-account"><option value="">— choisir —</option>${accounts.filter((a) => !kinds || kinds.includes(a.kind)).map((a) => `<option value="${a.id}"${sel === a.id ? " selected" : ""}>${a.emoji} ${fe(a.name)}</option>`).join("")}<option value="new"${!sel && ex.account_hint?.iban ? " selected" : ""}>+ Nouveau compte…</option></select></label>
    <div id="fr-new" class="${!sel && ex.account_hint?.iban ? "" : "hidden "}field-row"><label>Nom<input id="fr-new-name" value="${fe(ex.account_hint?.institution || ex.fields?.broker || ex.fields?.bank || "Compte")}" /></label>
      <label>Type<select id="fr-new-kind">${FIN_KIND_ORDER.map((k) => `<option value="${k}"${kinds?.[0] === k ? " selected" : ""}>${fin.status.account_kinds[k].emoji} ${fe(fin.status.account_kinds[k].label)}</option>`).join("")}</select></label>
      <label>À qui ?<select id="fr-new-owner"><option value="">👥 Commun (parts égales)</option>${adults.map((m) => `<option value="${m.id}"${ex.member_id === m.id ? " selected" : ""}>${m.emoji} ${fe(m.name)}</option>`).join("")}</select></label></div>`;
  let main = "";
  if (t === "bank_statement") {
    const txs = ex.transactions || [];
    const sum = txs.reduce((a, x) => a + x.amount, 0);
    const dates = txs.map((x) => x.date).sort();
    const close = (ex.balances || []).find((b) => b.type === "closing");
    main = `${accSel(null, ex.account_id)}
      <p><b>${txs.length}</b> opérations${dates.length ? ` du ${new Date(dates[0]).toLocaleDateString("fr-CH")} au ${new Date(dates[dates.length - 1]).toLocaleDateString("fr-CH")}` : ""} · solde des mouvements ${chf(sum)}
        ${close ? ` · solde final <b>${chf(close.amount)}</b> au ${new Date(close.date).toLocaleDateString("fr-CH")}` : ""}${ex.account_hint?.iban ? ` · IBAN ${fe(ex.account_hint.iban)}` : ""}</p>
      <div class="fin-txs fin-txs-mini">${txs.slice(0, 8).map((x) => `<div class="fin-tx"><span class="d">${new Date(x.date).toLocaleDateString("fr-CH")}</span><span class="t"><b>${fe(x.counterparty || x.description)}</b></span><span class="a ${x.amount < 0 ? "neg" : "pos"}">${fcNum(x.amount, { dec: 2 })}</span></div>`).join("")}</div>
      ${txs.length > 8 ? `<p class="muted">… et ${txs.length - 8} autres. Les doublons d'un import précédent sont ignorés.</p>` : ""}`;
  } else if (t === "broker_statement") {
    const hs = ex.fields?.holdings || [];
    const noTicker = hs.filter((h) => !h.symbol).length;
    main = `${accSel(["invest", "crypto", "pillar3a", "vested"], ex.account_id)}
      <div class="fin-holdings">${hs.map((h) => `<div class="fin-hold"><span title="${h.symbol ? "ticker" : h.isin ? "ISIN" : h.valor ? "n° de valeur" : ""}">${fe(h.symbol || h.isin || h.valor || "—")}</span><span>${fe(h.name)}</span><span>${fcNum(h.quantity, { dec: 2 })}${h.price != null ? `<br><small class="muted">× ${fcNum(h.price, { dec: 2 })}</small>` : ""}</span><b>${h.value != null ? fcNum(h.value) + " " + fe(h.currency || "") : ""}</b></div>`).join("")}</div>
      <p class="muted">Les positions remplaceront celles du compte ; la valeur suivra ensuite les cours en ligne.${noTicker ? ` ${noTicker} position(s) sans ticker : leur valeur reste celle du document. Ajoutez le ticker dans le compte (ex. NESN.SW) pour suivre les cours.` : ""}</p>`;
  } else {
    const fields = ex.fields || {};
    main = `${t === "tax_assessment" ? memberSel(ex.member_id).replace('<option value="">—</option>', '<option value="">Ménage (marié·es)</option>') : t === "other" ? "" : memberSel(ex.member_id)}
      ${["lpp_certificate", "pillar3a_statement", "vested_statement"].includes(t) ? accSel([{ lpp_certificate: "lpp", pillar3a_statement: "pillar3a", vested_statement: "vested" }[t]], ex.account_id).replace("— choisir —", "— créer automatiquement —") : ""}
      <div class="fin-fields">${Object.entries(fields).filter(([k, v]) => typeof v !== "object" || v === null).map(([k, v]) => `
        <label>${fe(FIN_FIELD_LABELS[k] || k)}<input data-field="${k}" value="${v == null ? "" : fe(typeof v === "number" ? fcNum(v, { dec: Number.isInteger(v) ? 0 : 2 }) : v)}" /></label>`).join("")}</div>`;
  }
  const dlg = finModal(`
    <h3>${fe(d.doc_type_label || "Document")}</h3>
    <p class="muted">${fe(d.filename)} · lu par ${fe(d.engine)}${ex.confidence != null && ex.confidence < 1 ? ` · confiance ${pct(ex.confidence)}` : ""}</p>
    ${ex.summary ? `<p>${fe(ex.summary)}</p>` : ""}
    ${t && aiRead ? `<div class="fin-type-row"><label>Type de document<select id="fr-type">${Object.entries(fin.status.doc_types).map(([k, l]) => `<option value="${k}"${t === k ? " selected" : ""}>${fe(l)}</option>`).join("")}</select></label>
      <button type="button" class="btn btn-ghost btn-sm hidden" id="fr-reread">🔁 Relire comme ce type</button></div>` : ""}
    ${main}
    <p class="muted fin-check">Vérifiez les chiffres : l'IA peut se tromper, rien n'est écrit avant « Appliquer ».</p>
    ${d.debug ? `<details class="fin-debug-fold"><summary>🔍 Sous le capot</summary>${finDebugHtml(d.debug)}</details>` : ""}
    <div class="modal-actions">
      <button type="button" class="btn btn-ghost" id="fr-ignore">Ignorer</button>
      <button type="button" class="btn btn-ghost" data-close>Plus tard</button>
      ${t === "other" ? "" : `<button type="button" class="btn btn-primary" id="fr-apply">Appliquer ✓</button>`}
    </div>`, { wide: true });
  if ($("#fr-account")) $("#fr-account").onchange = (e) => $("#fr-new").classList.toggle("hidden", e.target.value !== "new");
  if ($("#fr-type")) {
    // Statements carry a list (operations, positions), not simple fields: changing to or from one needs a re-read.
    const listTypes = ["bank_statement", "broker_statement"];
    $("#fr-type").onchange = (e) => {
      const changed = e.target.value !== t;
      $("#fr-reread").classList.toggle("hidden", !changed);
      if ($("#fr-apply")) $("#fr-apply").disabled = changed && (listTypes.includes(t) || listTypes.includes(e.target.value));
    };
    $("#fr-reread").onclick = async () => {
      $("#fr-reread").disabled = true;
      try {
        await api.post(`/api/finance/documents/${id}/reparse`, { doc_type: $("#fr-type").value });
        toast("Relecture en cours 🤖");
        dlg.close();
        finRenderTab();
      } catch (e) { toast(e.message, true); $("#fr-reread").disabled = false; }
    };
  }
  $("#fr-ignore").onclick = async () => { await api.post(`/api/finance/documents/${id}/ignore`); dlg.close(); finRenderTab(); };
  if ($("#fr-apply")) $("#fr-apply").onclick = async () => {
    const payload = {};
    const acc = $("#fr-account")?.value;
    if (acc === "new") {
      const owner = $("#fr-new-owner").value;
      payload.create_account = { name: $("#fr-new-name").value, kind: $("#fr-new-kind").value, owners: owner ? { [owner]: 1 } : {} };
    }
    else if (acc) payload.account_id = +acc;
    if ($("#fr-member")?.value) payload.member_id = +$("#fr-member").value;
    if ($("#fr-type")) payload.doc_type = $("#fr-type").value;
    const fields = {};
    dlg.querySelectorAll("[data-field]").forEach((i) => {
      const raw = i.value.trim();
      const n = finParse(raw);
      fields[i.dataset.field] = raw === "" ? null : (n != null && /^[-\d'’.,\s]+$/.test(raw) ? n : raw === "true" ? true : raw === "false" ? false : raw);
    });
    if (Object.keys(fields).length) payload.fields = fields;
    $("#fr-apply").disabled = true;
    try {
      const r = await api.post(`/api/finance/documents/${id}/apply`, payload);
      const imp = r.result?.import;
      toast(imp ? `${imp.new} opération(s) ajoutée(s)${imp.duplicate ? `, ${imp.duplicate} déjà connues` : ""} ✨` : "Appliqué ✨");
      dlg.close();
      fin.members = await api.get("/api/finance/members");
      finRenderTab();
    } catch (e) { toast(e.message, true); $("#fr-apply").disabled = false; }
  };
}

/* What the AI was sent, thought and answered — for debugging. `dbg` = document.debug (app/llm.py chat_json trace). */
function finDebugHtml(dbg) {
  const pre = (s) => `<pre class="fin-debug-pre">${fe(s)}</pre>`;
  const tokens = (u) => u && (u.prompt_tokens || u.completion_tokens)
    ? ` · ${fcNum(u.prompt_tokens || 0)} tokens lus → ${fcNum(u.completion_tokens || 0)} écrits` : "";
  const tries = (dbg.attempts || []).map((a) => `
    <div class="fin-debug-try">
      <p><b>${a.error ? "❌" : "✅"} ${fe(a.engine)}</b> <span class="muted">· ${fcNum(a.seconds, { dec: 1 })} s${tokens(a.usage)}</span></p>
      ${a.error ? `<p class="fin-stale">${fe(a.error)}</p>` : ""}
      ${a.reasoning ? `<details open><summary>Réflexion</summary>${pre(a.reasoning)}</details>`
        : a.error ? "" : `<p class="muted">Ce modèle ne donne pas sa réflexion.</p>`}
      ${a.raw ? `<details><summary>Réponse brute</summary>${pre(a.raw)}</details>` : ""}
    </div>`).join("");
  return `<div class="fin-debug">
    <p class="muted">Envoyé : ${dbg.images ? `${dbg.images} image(s) + ` : ""}${fcNum((dbg.prompt || "").length)} caractères de texte</p>
    ${tries || `<p class="muted">Aucun modèle n'a été essayé.</p>`}
    <details><summary>Message envoyé (sans les images)</summary>${pre(dbg.prompt || "")}</details>
  </div>`;
}

async function finDebug(id) {
  const d = await api.get(`/api/finance/documents/${id}`);
  finModal(`
    <h3>🔍 Sous le capot</h3>
    <p class="muted">${fe(d.filename)}${d.engine ? ` · ${fe(d.engine)}` : ""}</p>
    ${d.debug ? finDebugHtml(d.debug) : `<p class="muted">Pas de trace pour ce document.</p>`}
    <div class="modal-actions"><button type="button" class="btn btn-ghost" data-close>Fermer</button></div>`, { wide: true });
}

/* ------------------------------------------------------------------ RÉGLAGES */

async function finSettings(body) {
  const [s, incomes, cal, ai, cpi] = await Promise.all([
    api.get("/api/finance/settings"), api.get("/api/finance/incomes"), api.get("/api/finance/calibration"),
    api.get("/api/finance/llm/status").catch(() => null), api.get("/api/finance/market/cpi").catch(() => ({})),
  ]);
  if (!body.isConnected) return;
  const adults = finAdults();
  const cc = s.child_costs;
  body.innerHTML = `
    <div class="fin-grid2">
      <div class="card"><h3 class="fin-h3">👪 Le foyer</h3>
        <label>État civil<select id="fs-civil"><option value="unmarried"${s.civil_status === "unmarried" ? " selected" : ""}>Pas marié·es</option><option value="married"${s.civil_status === "married" ? " selected" : ""}>Marié·es</option></select></label>
        <div class="field-row"><label>Commune<input id="fs-commune" value="${fe(s.commune)}" /></label><label><span>Coefficient communal (%)${finQ("communal")}</span><input id="fs-coef" type="text" inputmode="decimal" value="${s.communal_coefficient}" /></label></div>
        ${adults.length === 2 ? `<label><span>Les enfants, côté impôts : part de ${fe(adults[0].name)} (%)${finQ("child_tax_split")}</span><input id="fs-split" type="text" inputmode="numeric" value="${Math.round(s.child_tax_split * 100)}" /><small class="muted">Parents non mariés : moitié-moitié par défaut (part de 0.5 partagée, déduction IFD partagée).</small></label>` : ""}
        <label><span>Vue individuelle : les dépenses communes sont partagées${finQ("spending_split")}</span><select id="fs-spend"><option value="income"${s.spending_split === "income" ? " selected" : ""}>selon les revenus</option><option value="equal"${s.spending_split === "equal" ? " selected" : ""}>à parts égales</option></select></label>
        <div class="modal-actions"><button class="btn btn-primary btn-sm" id="fs-save-hh">Enregistrer</button></div>
      </div>
      <div class="card"><h3 class="fin-h3">🧑‍🤝‍🧑 Personnes</h3>
        ${fin.members.map((m) => `<div class="fin-ev"><span>${m.emoji}</span><span class="info"><b>${fe(m.name)}</b><small>${m.kind === "adult" ? `${finAge(m.birth_date) ?? "?"} ans` : m.expected ? `naissance prévue le ${new Date(m.birth_date).toLocaleDateString("fr-CH")}` : `né·e le ${new Date(m.birth_date).toLocaleDateString("fr-CH")}`}</small></span><button class="icon-btn" data-mem="${m.id}" title="Modifier">✏️</button></div>`).join("")}
        <div class="plan-actions"><button class="btn btn-ghost btn-sm" id="fs-add-child">+ Enfant</button><button class="btn btn-ghost btn-sm" id="fs-add-adult">+ Adulte</button></div>
      </div>
    </div>
    <div class="card"><div class="fin-cardhead"><h3 class="fin-h3">💼 Revenus</h3><button class="btn btn-primary btn-sm" id="fs-add-income">+ Revenu</button></div>
      ${incomes.map((i) => `<div class="fin-ev"><span>${i.kind === "salary" ? "💼" : "🧑‍💻"}</span><span class="info"><b>${finWho(i.member_id)} · ${chf(i.gross_annual)} brut / an</b><small>${fe(i.employer || i.label || "")} · ${i.work_pct} %${i.lpp_employee_annual != null ? ` · LPP ${chf(i.lpp_employee_annual)}` : ""}${i.start_date ? ` · dès ${i.start_date}` : ""}${i.end_date ? ` · jusqu'au ${i.end_date}` : ""}</small></span><button class="icon-btn" data-inc="${i.id}" title="Modifier">✏️</button></div>`).join("") || `<p class="muted">Aucun revenu — glissez une fiche de salaire dans Documents.</p>`}
    </div>
    <div class="fin-grid2">
      <div class="card"><h3 class="fin-h3">🍼 Coût des enfants (par mois, par enfant)</h3>
        <div class="fin-fields">${[["0-3", "0-3 ans"], ["4-12", "4-12 ans"], ["13-17", "13-17 ans"], ["18-24", "18-24 ans (études)"], ["childcare_monthly", "Crèche (0-3, si vous travaillez tous)"], ["afterschool_monthly", "Parascolaire (4-12)"], ["independent_at", "Indépendant·e à (âge)"]]
          .map(([k, l]) => `<label>${l}<input data-cc="${k}" type="text" inputmode="numeric" value="${cc[k]}" /></label>`).join("")}</div>
        <small class="muted">Coûts directs (nourriture, habits, LAMal, loisirs). Les allocations (322 CHF/mois, 1'617 CHF à la naissance) sont ajoutées automatiquement.</small>
        <div class="modal-actions"><button class="btn btn-primary btn-sm" id="fs-save-cc">Enregistrer</button></div>
      </div>
      <div class="card"><h3 class="fin-h3">🤖 Lecture des documents</h3>
        <label>Mode<select id="fs-llm-mode">${[["local_first", "Local d'abord, cloud en secours"], ["local_only", "Local seulement (rien ne sort)"], ["cloud_only", "Cloud (OpenRouter)"]].map(([k, l]) => `<option value="${k}"${s.llm.mode === k ? " selected" : ""}>${l}</option>`).join("")}</select></label>
        <label>Serveur local (compatible OpenAI)<input id="fs-llm-url" value="${fe(s.llm.local_base_url)}" placeholder="${fe(ai?.local.base_url || "http://localhost:11434/v1")}" /></label>
        <label>Modèle<input id="fs-llm-model" list="fs-llm-models" value="${fe(s.llm.local_model)}" placeholder="${fe(ai?.local.model || "qwen2.5vl:7b")}" /></label>
        <datalist id="fs-llm-models">${(ai?.local.models || []).map((m) => `<option value="${fe(m)}"></option>`).join("")}</datalist>
        <label class="wz-check"><input type="checkbox" id="fs-llm-cloud"${s.llm.allow_cloud ? " checked" : ""} /> Autoriser le cloud si le local ne répond pas</label>
        <p class="muted">${ai ? (ai.local.reachable ? `🟢 ${fe(ai.local.base_url)} répond · modèles : ${fe(ai.local.models.join(", ") || "—")}` : `🟠 ${fe(ai.local.base_url)} ne répond pas. Avec Ollama : <code>ollama pull qwen2.5vl:7b</code>, et pour Docker, lancer Ollama avec <code>OLLAMA_HOST=0.0.0.0</code>.`) : ""} ${ai?.cloud.configured ? "· ☁️ OpenRouter configuré" : "· ☁️ pas de clé OpenRouter"}</p>
        <div class="modal-actions"><button class="btn btn-primary btn-sm" id="fs-save-llm">Enregistrer</button></div>
      </div>
    </div>
    <div class="fin-grid2">
      <div class="card"><h3 class="fin-h3">🌐 Données en ligne</h3>
        ${[["prices", "Cours des actions et ETF (Yahoo / Stooq)"], ["fx", "Taux de change (BCE)"], ["cpi", "Inflation suisse (BNS)"]].map(([k, l]) => `<label class="wz-check"><input type="checkbox" data-online="${k}"${s.online[k] ? " checked" : ""} /> ${l}</label>`).join("")}
        <p class="muted">Seuls des symboles et des codes de devise sortent, jamais un montant. Mise à jour chaque jour à 18:30.${cpi?.month ? ` Dernière inflation connue : ${fcNum(cpi.yoy_pct, { dec: 1 })} % (${cpi.month}), moyenne sur 10 ans ${fcNum(cpi.avg_10y_pct, { dec: 1 })} %.` : ""}</p>
        <div class="modal-actions"><button class="btn btn-ghost btn-sm" id="fs-refresh">Actualiser maintenant</button><button class="btn btn-primary btn-sm" id="fs-save-online">Enregistrer</button></div>
      </div>
      <div class="card"><div class="fin-cardhead"><h3 class="fin-h3">🔍 Contrôle du moteur fiscal</h3><button class="btn btn-ghost btn-sm" id="fs-add-bill">+ Décision de taxation</button></div>
        ${cal.length ? `<table class="fin-cmp"><thead><tr><th>Année</th><th>Qui</th><th>Revenu imposable</th><th>Facture ICC</th><th>Moteur</th><th>Écart</th></tr></thead><tbody>${cal.map((c) => `<tr><td>${c.year}</td><td>${fe(c.member)}</td><td>${chf(c.taxable_income)}</td><td>${chf(c.icc_bill)}</td><td>${chf(c.icc_engine)}</td><td>${c.icc_gap_pct != null ? `${fcNum(c.icc_gap_pct, { dec: 2 })} %` : "—"}</td></tr>`).join("")}</tbody></table>`
          : `<p class="muted">Glissez votre dernière décision de taxation dans Documents (ou saisissez-la) : le moteur refait le calcul sur le même revenu imposable et affiche l'écart.</p>`}
      </div>
    </div>
    <div class="fin-grid2">
      <div class="card"><h3 class="fin-h3">🔐 Sauvegarde chiffrée</h3>
        <p>Les finances ne vont jamais sur GitHub. Pour les emporter sur un autre ordinateur : exportez un fichier chiffré, puis importez-le là-bas avec la même phrase de passe.</p>
        <div class="plan-actions"><button class="btn btn-primary btn-sm" id="fs-export">Exporter…</button><button class="btn btn-ghost btn-sm" id="fs-import">Importer…</button></div>
      </div>
      <div class="card"><h3 class="fin-h3">📚 Sources</h3><ul class="fin-sources">${fin.status.sources.map((x) => `<li>${fe(x)}</li>`).join("")}</ul>
        <button class="btn btn-ghost btn-sm fin-danger-btn" id="fs-reset">Tout effacer…</button></div>
    </div>`;
  $("#fs-save-hh").onclick = async () => {
    const patch = { civil_status: $("#fs-civil").value, commune: $("#fs-commune").value, communal_coefficient: finParse($("#fs-coef").value) ?? 78.5, spending_split: $("#fs-spend").value };
    if ($("#fs-split")) patch.child_tax_split = (finParse($("#fs-split").value) ?? 50) / 100;
    await api.put("/api/finance/settings", patch); toast("Enregistré");
  };
  $("#fs-save-cc").onclick = async () => {
    const cc2 = {}; body.querySelectorAll("[data-cc]").forEach((i) => { cc2[i.dataset.cc] = finParse(i.value) ?? 0; });
    await api.put("/api/finance/settings", { child_costs: cc2 }); toast("Enregistré");
  };
  $("#fs-save-llm").onclick = async () => {
    await api.put("/api/finance/settings", { llm: { mode: $("#fs-llm-mode").value, local_base_url: $("#fs-llm-url").value.trim(), local_model: $("#fs-llm-model").value.trim(), allow_cloud: $("#fs-llm-cloud").checked } });
    toast("Enregistré"); finRenderTab();
  };
  $("#fs-save-online").onclick = async () => {
    const o = {}; body.querySelectorAll("[data-online]").forEach((i) => { o[i.dataset.online] = i.checked; });
    await api.put("/api/finance/settings", { online: o }); toast("Enregistré");
  };
  $("#fs-refresh").onclick = async (e) => { e.target.disabled = true; try { await api.post("/api/finance/market/refresh"); toast("Données à jour"); finRenderTab(); } catch (err) { toast(err.message, true); } };
  body.querySelectorAll("[data-mem]").forEach((b) => b.onclick = () => finMemberModal(finMember(+b.dataset.mem)));
  $("#fs-add-child").onclick = () => finMemberModal({ kind: "child", emoji: "👶", meta: {} });
  $("#fs-add-adult").onclick = () => finMemberModal({ kind: "adult", emoji: "🙂", meta: {} });
  $("#fs-add-income").onclick = () => finIncomeModal(null, incomes);
  body.querySelectorAll("[data-inc]").forEach((b) => b.onclick = () => finIncomeModal(incomes.find((i) => i.id === +b.dataset.inc)));
  $("#fs-add-bill").onclick = () => finGo("docs");
  $("#fs-export").onclick = () => finBackupModal("export");
  $("#fs-import").onclick = () => finBackupModal("import");
  $("#fs-reset").onclick = () => {
    const dlg = finModal(`<h3>Tout effacer ?</h3><p>Personnes, comptes, opérations, documents, XP… Tout disparaît de cette machine. Tapez <b>EFFACER</b> pour confirmer.</p>
      <input id="fs-reset-word" autocomplete="off" /><div class="modal-actions"><button class="btn btn-ghost" data-close>Annuler</button><button class="btn btn-danger" id="fs-reset-ok">Effacer</button></div>`);
    $("#fs-reset-ok").onclick = async () => {
      try { await api.post(`/api/finance/reset?confirm=${encodeURIComponent($("#fs-reset-word").value)}`); dlg.close(); fin.tab = "summit"; finRefresh(); }
      catch (e) { toast(e.message, true); }
    };
  };
}

function finMemberModal(m) {
  const meta = m.meta || {};
  const adult = m.kind === "adult";
  const dlg = finModal(`
    <h3>${m.id ? fe(m.name) : adult ? "Nouvel adulte" : "Nouvel enfant"}</h3>
    <div class="field-row"><label class="wz-emoji">Emoji<input id="fm-emoji" value="${fe(m.emoji || "🙂")}" maxlength="4" /></label><label>Prénom<input id="fm-name" value="${fe(m.name || "")}" /></label></div>
    <label>${adult ? "Date de naissance" : "Date de naissance (ou prévue)"}<input type="date" id="fm-birth" value="${fe(m.birth_date || "")}" /></label>
    ${adult ? `
      <div class="field-row"><label>Arrêter de travailler à <small class="muted">(vide = au plus tôt)</small><input id="fm-target" type="text" inputmode="numeric" value="${m.retire_age_target ?? ""}" /></label>
        <label>Années AVS déjà cotisées<input id="fm-avsy" type="text" inputmode="numeric" value="${meta.avs_past_years ?? ""}" placeholder="dès 21 ans" /></label></div>
      <div class="field-row"><label>Revenu moyen passé (AVS)${finMoneyInput("fm-avsi", meta.avs_past_avg_income)}</label>
        <label>Primes d'assurance / an <small class="muted">(LAMal…)</small>${finMoneyInput("fm-ins", meta.insurance_annual ?? 5400)}</label></div>
      <div class="field-row"><label>Trajets domicile-travail / an${finMoneyInput("fm-commute", meta.commute ?? 0)}</label>
        <label>3a prévu / an <small class="muted">(vide = le maximum)</small>${finMoneyInput("fm-p3a", meta.p3a_annual)}</label></div>
      <div class="field-row"><label>Taux AANP (%)<input id="fm-aanp" type="text" inputmode="decimal" value="${meta.aanp_rate != null ? meta.aanp_rate * 100 : 1}" /></label>
        <label class="wz-check"><input type="checkbox" id="fm-meals"${meta.meals_out !== false ? " checked" : ""} /> Repas de midi à l'extérieur</label></div>` : `
      <label>Parent qui porte <small class="muted">(congé maternité)</small><select id="fm-bp"><option value="">—</option>${finAdults().map((a) => `<option value="${a.id}"${meta.birth_parent_id === a.id ? " selected" : ""}>${a.emoji} ${fe(a.name)}</option>`).join("")}</select></label>`}
    <div class="modal-actions">${m.id ? `<button type="button" class="btn btn-ghost fin-danger-btn" id="fm-del">Retirer</button>` : ""}
      <button type="button" class="btn btn-ghost" data-close>Annuler</button><button type="button" class="btn btn-primary" id="fm-save">Enregistrer</button></div>`);
  $("#fm-save").onclick = async () => {
    const birth = $("#fm-birth").value || null;
    const body = { name: $("#fm-name").value || "?", emoji: $("#fm-emoji").value || "🙂", kind: m.kind, birth_date: birth,
      expected: !!(birth && birth > new Date().toISOString().slice(0, 10)), color: "", meta: {} };
    if (adult) {
      body.retire_age_target = finParse($("#fm-target").value) || null;
      const mm = body.meta;
      if (finParse($("#fm-avsy").value) != null) mm.avs_past_years = finParse($("#fm-avsy").value);
      if (finParse($("#fm-avsi").value) != null) mm.avs_past_avg_income = finParse($("#fm-avsi").value);
      mm.insurance_annual = finParse($("#fm-ins").value) ?? 5400;
      mm.commute = finParse($("#fm-commute").value) ?? 0;
      mm.p3a_annual = finParse($("#fm-p3a").value);
      mm.aanp_rate = (finParse($("#fm-aanp").value) ?? 1) / 100;
      mm.meals_out = $("#fm-meals").checked;
    } else if ($("#fm-bp").value) body.meta.birth_parent_id = +$("#fm-bp").value;
    try {
      if (m.id) await api.put(`/api/finance/members/${m.id}`, body); else await api.post("/api/finance/members", body);
      dlg.close(); fin.members = await api.get("/api/finance/members"); finRenderScope(); finRenderTab();
    } catch (e) { toast(e.message, true); }
  };
  if ($("#fm-del")) $("#fm-del").onclick = async () => {
    if (await finConfirm(`Retirer ${fe(m.name)} ? Ses revenus disparaissent aussi.`, { ok: "Retirer", danger: true })) {
      await api.del(`/api/finance/members/${m.id}`); fin.members = await api.get("/api/finance/members"); finRenderScope(); finRenderTab();
    }
  };
}

function finIncomeModal(inc) {
  const i = inc || { kind: "salary", member_id: finAdults()[0]?.id, work_pct: 100 };
  const dlg = finModal(`
    <h3>${inc ? "Modifier le revenu" : "Nouveau revenu"}</h3>
    <div class="field-row"><label>Qui<select id="fi-who">${finAdults().map((m) => `<option value="${m.id}"${i.member_id === m.id ? " selected" : ""}>${m.emoji} ${fe(m.name)}</option>`).join("")}</select></label>
      <label>Type<select id="fi-kind">${[["salary", "Salaire"], ["side", "Revenu annexe (indépendant)"], ["rental", "Location"], ["other", "Autre"]].map(([k, l]) => `<option value="${k}"${i.kind === k ? " selected" : ""}>${l}</option>`).join("")}</select></label></div>
    <div class="field-row"><label>Brut annuel (13e compris)${finMoneyInput("fi-gross", i.gross_annual)}</label><label>Taux d'activité (%)<input id="fi-pct" type="text" inputmode="numeric" value="${i.work_pct}" /></label></div>
    <div class="field-row"><label>Cotisation LPP salarié / an${finMoneyInput("fi-lpp", i.lpp_employee_annual)}</label><label>Employeur<input id="fi-emp" value="${fe(i.employer || "")}" /></label></div>
    <div class="field-row"><label>Depuis<input type="date" id="fi-start" value="${fe(i.start_date || "")}" /></label><label>Jusqu'au<input type="date" id="fi-end" value="${fe(i.end_date || "")}" /></label></div>
    <div class="modal-actions">${inc ? `<button type="button" class="btn btn-ghost fin-danger-btn" id="fi-del">Supprimer</button>` : ""}
      <button type="button" class="btn btn-ghost" data-close>Annuler</button><button type="button" class="btn btn-primary" id="fi-save">Enregistrer</button></div>`);
  $("#fi-save").onclick = async () => {
    const body = { member_id: +$("#fi-who").value, kind: $("#fi-kind").value, gross_annual: finParse($("#fi-gross").value) || 0, work_pct: finParse($("#fi-pct").value) ?? 100,
      lpp_employee_annual: finParse($("#fi-lpp").value), employer: $("#fi-emp").value, start_date: $("#fi-start").value || null, end_date: $("#fi-end").value || null, label: "", notes: "" };
    try { if (inc) await api.put(`/api/finance/incomes/${inc.id}`, body); else await api.post("/api/finance/incomes", body); dlg.close(); finRenderTab(); }
    catch (e) { toast(e.message, true); }
  };
  if ($("#fi-del")) $("#fi-del").onclick = async () => { await api.del(`/api/finance/incomes/${inc.id}`); dlg.close(); finRenderTab(); };
}

function finBackupModal(mode) {
  const dlg = finModal(mode === "export" ? `
    <h3>🔐 Exporter les finances</h3>
    <p>Choisissez une phrase de passe (8 caractères au moins). <b>Sans elle, le fichier est illisible</b> — même pour nous.</p>
    <label>Phrase de passe<input type="password" id="fb-pass" autocomplete="new-password" /></label>
    <label>Encore une fois<input type="password" id="fb-pass2" autocomplete="new-password" /></label>
    <div class="modal-actions"><button class="btn btn-ghost" data-close>Annuler</button><button class="btn btn-primary" id="fb-go">Télécharger</button></div>` : `
    <h3>📥 Importer une sauvegarde</h3>
    <p class="fin-stale">Les finances actuelles de cette machine seront remplacées (une copie de sécurité est gardée).</p>
    <label>Fichier .perrufin<input type="file" id="fb-file" accept=".perrufin" /></label>
    <label>Phrase de passe<input type="password" id="fb-pass" autocomplete="current-password" /></label>
    <div class="modal-actions"><button class="btn btn-ghost" data-close>Annuler</button><button class="btn btn-primary" id="fb-go">Importer</button></div>`);
  $("#fb-go").onclick = async () => {
    const pass = $("#fb-pass").value;
    try {
      if (mode === "export") {
        if (pass !== $("#fb-pass2").value) { toast("Les deux phrases diffèrent", true); return; }
        const res = await fetch("/api/finance/backup/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passphrase: pass }) });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Erreur");
        const blob = await res.blob();
        const name = (res.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1] || "finances.perrufin";
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a"); a.href = url; a.download = name; a.click();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        toast("Sauvegarde téléchargée 🔐");
      } else {
        const f = $("#fb-file").files[0];
        if (!f) { toast("Choisissez le fichier", true); return; }
        const fd = new FormData(); fd.append("file", f); fd.append("passphrase", pass);
        const res = await fetch("/api/finance/backup/import", { method: "POST", body: fd });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Erreur");
        toast("Finances restaurées ✅");
        finRefresh();
      }
      dlg.close();
    } catch (e) { toast(e.message, true); }
  };
}

/* ------------------------------------------------------------------ start */

if (document.getElementById("view-finance")?.classList.contains("active")) finOnShow();
