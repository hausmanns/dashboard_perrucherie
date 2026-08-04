/* Dashboard de la Perrucherie — frontend (vanilla JS, no build step). */

const $ = (sel) => document.querySelector(sel);
const api = {
  async req(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Erreur ${res.status}`);
    }
    return res.status === 204 ? null : res.json();
  },
  get: (url) => api.req("GET", url),
  post: (url, body) => api.req("POST", url, body),
  put: (url, body) => api.req("PUT", url, body),
  del: (url) => api.req("DELETE", url),
};

const DAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"];
const PLANT_EMOJIS = ["🌿", "🪴", "🌱", "🌵", "🍀", "🌾", "🌺", "🌻"];

const state = {
  plants: [],
  dishes: [],
  plans: [],
  currentPlanId: null,
  currentWeek: 1,
  notified: new Set(), // plant ids already notified this session
};

/* ---------------- Utils ---------------- */

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  setTimeout(() => el.classList.add("hidden"), 2800);
}

function plantEmoji(name) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PLANT_EMOJIS[h % PLANT_EMOJIS.length];
}

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
}

function statusLabel(p) {
  switch (p.status) {
    case "overdue": return `En retard de ${Math.abs(Math.floor(p.days_until_watering))} j 💧`;
    case "due_soon": return "À arroser bientôt 💧";
    case "never_watered": return "Jamais arrosée";
    default: return `OK · dans ${Math.ceil(p.days_until_watering)} j`;
  }
}

function closeModal(id) { $(id).close(); }
document.querySelectorAll("[data-close]").forEach((b) =>
  b.addEventListener("click", () => b.closest("dialog").close())
);

/* ---------------- Navigation ---------------- */

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    tab.classList.add("active");
    $(`#view-${tab.dataset.view}`).classList.add("active");
  })
);

/* ---------------- Accueil ---------------- */

async function loadHome() {
  try {
    const s = await api.get("/api/summary");

    const dueBox = $("#home-due-plants");
    if (s.plants.due.length === 0) {
      dueBox.innerHTML = `<p class="muted">Toutes les plantes sont heureuses 🌿</p>`;
    } else {
      dueBox.innerHTML = s.plants.due.map((p) => `
        <div class="home-item">
          <div class="info">
            <strong>${plantEmoji(p.name)} ${p.name}</strong>
            <small>${statusLabel(p)}${p.location ? " · " + p.location : ""}</small>
          </div>
          <button class="btn btn-primary btn-sm" onclick="waterPlant(${p.id})">Arroser</button>
        </div>`).join("");
    }

    const mealBox = $("#home-today-meals");
    if (!s.meals.active_plan) {
      mealBox.innerHTML = `<p class="muted">Aucun plan actif — créez-en un dans l'onglet Repas.</p>`;
    } else if (s.meals.today.length === 0) {
      mealBox.innerHTML = `<p class="muted">Rien de prévu aujourd'hui dans « ${s.meals.active_plan.name} ».</p>`;
    } else {
      mealBox.innerHTML = s.meals.today.map((m) => `
        <div class="home-item"><div class="info"><strong>🍽️ ${m.dish}</strong><small>Repas ${m.slot_index}</small></div></div>
      `).join("");
    }

    $("#home-stats").innerHTML = `
      <div class="stat"><div class="num">${s.plants.total}</div><div class="lbl">Plantes</div></div>
      <div class="stat"><div class="num">${s.plants.due_count}</div><div class="lbl">Arrosages dus</div></div>
      <div class="stat"><div class="num">${s.meals.dishes_in_library}</div><div class="lbl">Plats</div></div>
      <div class="stat"><div class="num">${s.meals.active_plan ? s.meals.active_plan.weeks : 0}</div><div class="lbl">Semaines planifiées</div></div>`;
  } catch (e) { toast(e.message, true); }
}

/* ---------------- Plantes ---------------- */

async function loadPlants() {
  try {
    state.plants = await api.get("/api/plants");
    renderPlants();
    updateBadge();
  } catch (e) { toast(e.message, true); }
}

function updateBadge() {
  const due = state.plants.filter((p) => ["overdue", "due_soon", "never_watered"].includes(p.status));
  const badge = $("#plants-badge");
  badge.textContent = due.length;
  badge.classList.toggle("hidden", due.length === 0);
}

function renderPlants() {
  const grid = $("#plants-grid");
  if (state.plants.length === 0) {
    grid.innerHTML = `<p class="muted">Aucune plante pour l'instant. Ajoutez votre première ! 🌱</p>`;
    return;
  }
  grid.innerHTML = state.plants.map((p) => {
    let pct = null, barClass = "";
    if (p.last_watered_at && p.days_until_watering !== null) {
      const consumed = p.watering_frequency_days - Math.max(p.days_until_watering, 0);
      pct = Math.min(100, Math.round((consumed / p.watering_frequency_days) * 100));
      if (p.status === "overdue") barClass = "danger";
      else if (p.status === "due_soon") barClass = "warn";
    }
    return `
    <div class="card plant-card">
      ${p.photo
        ? `<img class="plant-photo" src="/api/plants/${p.id}/photo" alt="${p.name}" loading="lazy" />`
        : `<span class="emoji">${plantEmoji(p.name)}</span>`}
      <h3>${p.name}</h3>
      ${p.species ? `<div class="species">${p.species}</div>` : ""}
      ${p.location ? `<div class="location">📍 ${p.location}</div>` : ""}
      <span class="status-pill status-${p.status}">${statusLabel(p)}</span>
      ${pct !== null ? `<div class="progress ${barClass}"><div style="width:${pct}%"></div></div>` : ""}
      <div class="location">💧 tous les ${p.watering_frequency_days} j · dernier : ${fmtDate(p.last_watered_at)}</div>
      ${p.notes ? `<div class="notes">${p.notes}</div>` : ""}
      <div class="actions">
        <button class="btn btn-primary btn-sm" onclick="waterPlant(${p.id})">💧 Arroser</button>
        <button class="btn btn-ghost btn-sm" onclick="openPlantModal(${p.id})">✏️</button>
        <button class="btn btn-ghost btn-sm" onclick="deletePlant(${p.id})">🗑️</button>
      </div>
    </div>`;
  }).join("");
}

async function waterPlant(id) {
  try {
    await api.post(`/api/plants/${id}/water`, {});
    state.notified.delete(id);
    toast("Plante arrosée 💧");
    await Promise.all([loadPlants(), loadHome()]);
  } catch (e) { toast(e.message, true); }
}

function openPlantModal(id = null) {
  const form = $("#plant-form");
  form.reset();
  $("#plant-id").value = id || "";
  $("#plant-photo-token").value = "";
  $("#plant-photo-status").textContent = "";
  const preview = $("#plant-photo-preview");
  preview.classList.add("hidden");
  preview.removeAttribute("src");
  $("#plant-modal-title").textContent = id ? "Modifier la plante" : "Ajouter une plante";
  if (id) {
    const p = state.plants.find((x) => x.id === id);
    if (p) {
      $("#plant-name").value = p.name;
      $("#plant-species").value = p.species;
      $("#plant-location").value = p.location;
      $("#plant-freq").value = p.watering_frequency_days;
      $("#plant-last").value = p.last_watered_at ? p.last_watered_at.slice(0, 16) : "";
      $("#plant-notes").value = p.notes;
      if (p.photo) {
        preview.src = `/api/plants/${p.id}/photo`;
        preview.classList.remove("hidden");
      }
    }
  }
  $("#plant-modal").showModal();
}

/* ---- Identification par photo (vision LLM) ---- */

// Downscale phone photos before upload (faster, smaller, no backend deps).
function downscaleImage(file, maxSize = 1024, quality = 0.82) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(img.src);
      const scale = Math.min(1, maxSize / Math.max(img.width, img.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Image illisible"))), "image/jpeg", quality);
    };
    img.onerror = () => reject(new Error("Image illisible"));
    img.src = URL.createObjectURL(file);
  });
}

$("#plant-photo-btn").addEventListener("click", () => $("#plant-photo-input").click());

$("#plant-photo-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // allow re-picking the same file
  if (!file) return;

  const status = $("#plant-photo-status");
  const btn = $("#plant-photo-btn");
  const preview = $("#plant-photo-preview");
  try {
    const blob = await downscaleImage(file);
    preview.src = URL.createObjectURL(blob);
    preview.classList.remove("hidden");
    status.textContent = "🔍 Identification en cours…";
    btn.disabled = true;

    const fd = new FormData();
    fd.append("file", blob, "plante.jpg");
    const res = await fetch("/api/plants/identify", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Erreur ${res.status}`);

    if (data.name) $("#plant-name").value = data.name;
    if (data.species) $("#plant-species").value = data.species;
    if (data.watering_frequency_days) $("#plant-freq").value = data.watering_frequency_days;
    if (data.notes) $("#plant-notes").value = data.notes;
    $("#plant-photo-token").value = data.photo_token || "";
    status.textContent = "✅ Plante identifiée — vérifiez et ajustez si besoin.";
  } catch (err) {
    status.textContent = "";
    preview.classList.add("hidden");
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

$("#plant-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#plant-id").value;
  const lastVal = $("#plant-last").value;
  const payload = {
    name: $("#plant-name").value.trim(),
    species: $("#plant-species").value.trim(),
    location: $("#plant-location").value.trim(),
    watering_frequency_days: parseInt($("#plant-freq").value, 10),
    last_watered_at: lastVal ? lastVal + ":00" : null,
    notes: $("#plant-notes").value.trim(),
    photo: $("#plant-photo-token").value || null,
  };
  try {
    if (id) await api.put(`/api/plants/${id}`, payload);
    else await api.post("/api/plants", payload);
    closeModal("#plant-modal");
    toast("Plante enregistrée 🌿");
    await Promise.all([loadPlants(), loadHome()]);
  } catch (e) { toast(e.message, true); }
});

async function deletePlant(id) {
  const p = state.plants.find((x) => x.id === id);
  if (!confirm(`Supprimer « ${p?.name} » ?`)) return;
  try {
    await api.del(`/api/plants/${id}`);
    toast("Plante supprimée");
    await Promise.all([loadPlants(), loadHome()]);
  } catch (e) { toast(e.message, true); }
}

$("#add-plant-btn").addEventListener("click", () => openPlantModal());

/* ---------------- Notifications ---------------- */

$("#notif-btn").addEventListener("click", async () => {
  if (!("Notification" in window)) return toast("Notifications non supportées", true);
  const perm = await Notification.requestPermission();
  toast(perm === "granted" ? "Notifications activées 🔔" : "Notifications refusées", perm !== "granted");
  checkNotifications();
});

async function checkNotifications() {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const due = await api.get("/api/plants/due");
    for (const p of due) {
      if (!state.notified.has(p.id)) {
        state.notified.add(p.id);
        new Notification("🌿 La Perrucherie", {
          body: `${p.name} a besoin d'eau ! (${statusLabel(p)})`,
          tag: `plant-${p.id}`,
        });
      }
    }
  } catch { /* silencieux */ }
}

/* ---------------- Repas : plats ---------------- */

async function loadDishes() {
  try {
    state.dishes = await api.get("/api/dishes");
    renderDishes();
  } catch (e) { toast(e.message, true); }
}

const CAT_LABELS = { "petit-dej": "Petit-déj", dejeuner: "Déjeuner", diner: "Dîner", snack: "Snack" };

function renderDishes() {
  const list = $("#dishes-list");
  if (state.dishes.length === 0) {
    list.innerHTML = `<p class="muted">Ajoutez vos plats favoris ici, puis placez-les sur le planning.</p>`;
    return;
  }
  list.innerHTML = state.dishes.map((d) => `
    <div class="dish-item">
      <div>
        ${d.name}
        <small>${CAT_LABELS[d.category] || d.category}${d.prep_time_minutes ? " · " + d.prep_time_minutes + " min" : ""}</small>
      </div>
      <div>
        ${d.recipe_url ? `<a class="icon-btn" href="${d.recipe_url}" target="_blank" title="Recette">🔗</a>` : ""}
        <button class="icon-btn" onclick="openDishModal(${d.id})" title="Modifier">✏️</button>
        <button class="icon-btn" onclick="deleteDish(${d.id})" title="Supprimer">🗑️</button>
      </div>
    </div>`).join("");
}

function openDishModal(id = null) {
  const form = $("#dish-form");
  form.reset();
  $("#dish-id").value = id || "";
  $("#dish-modal-title").textContent = id ? "Modifier le plat" : "Ajouter un plat";
  if (id) {
    const d = state.dishes.find((x) => x.id === id);
    if (d) {
      $("#dish-name").value = d.name;
      $("#dish-category").value = d.category;
      $("#dish-prep").value = d.prep_time_minutes ?? "";
      $("#dish-url").value = d.recipe_url;
      $("#dish-notes").value = d.notes;
    }
  }
  $("#dish-modal").showModal();
}

$("#dish-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#dish-id").value;
  const prep = $("#dish-prep").value;
  const payload = {
    name: $("#dish-name").value.trim(),
    category: $("#dish-category").value,
    prep_time_minutes: prep ? parseInt(prep, 10) : null,
    recipe_url: $("#dish-url").value.trim(),
    notes: $("#dish-notes").value.trim(),
  };
  try {
    if (id) await api.put(`/api/dishes/${id}`, payload);
    else await api.post("/api/dishes", payload);
    closeModal("#dish-modal");
    toast("Plat enregistré 🍽️");
    await Promise.all([loadDishes(), renderPlan()]);
  } catch (e) { toast(e.message, true); }
});

async function deleteDish(id) {
  const d = state.dishes.find((x) => x.id === id);
  if (!confirm(`Supprimer « ${d?.name} » ?`)) return;
  try {
    await api.del(`/api/dishes/${id}`);
    toast("Plat supprimé");
    await Promise.all([loadDishes(), renderPlan()]);
  } catch (e) { toast(e.message, true); }
}

$("#add-dish-btn").addEventListener("click", () => openDishModal());

/* ---------------- Repas : plans ---------------- */

async function loadPlans() {
  try {
    state.plans = await api.get("/api/meal-plans");
    if (state.plans.length && !state.plans.some((p) => p.id === state.currentPlanId)) {
      state.currentPlanId = (state.plans.find((p) => p.active) || state.plans[0]).id;
    }
    if (!state.plans.length) state.currentPlanId = null;
    renderPlanSelect();
    await renderPlan();
  } catch (e) { toast(e.message, true); }
}

function renderPlanSelect() {
  const sel = $("#plan-select");
  sel.innerHTML = state.plans.length
    ? state.plans.map((p) => `<option value="${p.id}" ${p.id === state.currentPlanId ? "selected" : ""}>${p.name}</option>`).join("")
    : `<option value="">Aucun plan</option>`;
}

$("#plan-select").addEventListener("change", (e) => {
  state.currentPlanId = parseInt(e.target.value, 10) || null;
  state.currentWeek = 1;
  renderPlan();
});

$("#add-plan-btn").addEventListener("click", () => {
  $("#plan-form").reset();
  const monday = new Date();
  monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
  $("#plan-start").value = monday.toISOString().slice(0, 10);
  $("#plan-modal").showModal();
});

$("#plan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    name: $("#plan-name").value.trim(),
    start_date: $("#plan-start").value,
    weeks: parseInt($("#plan-weeks").value, 10),
    meals_per_day: parseInt($("#plan-meals").value, 10),
  };
  try {
    const plan = await api.post("/api/meal-plans", payload);
    state.currentPlanId = plan.id;
    state.currentWeek = 1;
    closeModal("#plan-modal");
    toast("Plan créé 📅");
    await loadPlans();
    loadHome();
  } catch (e) { toast(e.message, true); }
});

$("#delete-plan-btn").addEventListener("click", async () => {
  const plan = state.plans.find((p) => p.id === state.currentPlanId);
  if (!plan || !confirm(`Supprimer le plan « ${plan.name} » ?`)) return;
  try {
    await api.del(`/api/meal-plans/${plan.id}`);
    state.currentPlanId = null;
    toast("Plan supprimé");
    await loadPlans();
    loadHome();
  } catch (e) { toast(e.message, true); }
});

function currentWeekDates(plan) {
  const start = new Date(plan.start_date + "T00:00:00");
  const offset = (state.currentWeek - 1) * 7;
  return DAYS.map((_, i) => {
    const d = new Date(start);
    d.setDate(d.getDate() + offset + i);
    return d;
  });
}

async function renderPlan() {
  const wrap = $("#plan-grid-wrap");
  const meta = $("#plan-meta");
  const weekTabs = $("#week-tabs");
  const plan = state.plans.find((p) => p.id === state.currentPlanId);

  if (!plan) {
    meta.textContent = "";
    weekTabs.innerHTML = "";
    wrap.innerHTML = `<p class="muted">Créez un plan (nombre de semaines, repas par jour) puis remplissez la grille.</p>`;
    return;
  }

  const full = await api.get(`/api/meal-plans/${plan.id}`);
  meta.textContent = `Début le ${fmtDate(plan.start_date + "T00:00:00")} · ${plan.weeks} semaine(s) · ${plan.meals_per_day} repas/jour`;

  weekTabs.innerHTML = Array.from({ length: plan.weeks }, (_, i) =>
    `<button class="week-tab ${i + 1 === state.currentWeek ? "active" : ""}" onclick="setWeek(${i + 1})">Semaine ${i + 1}</button>`
  ).join("");

  const dates = currentWeekDates(plan);
  const todayStr = new Date().toDateString();
  const SLOT_NAMES = ["Petit-déj", "Déjeuner", "Dîner", "Snack 1", "Snack 2", "Extra"];

  wrap.innerHTML = `<div class="plan-grid">` + dates.map((date, dayIdx) => {
    const slots = Array.from({ length: plan.meals_per_day }, (_, slotIdx) => {
      const entry = full.entries.find(
        (e) => e.week_index === state.currentWeek && e.day_index === dayIdx + 1 && e.slot_index === slotIdx + 1
      );
      const dishName = entry?.dish_name;
      return `
        <div class="meal-slot ${dishName ? "filled" : ""}"
             onclick="openSlotPicker(event, ${dayIdx + 1}, ${slotIdx + 1})">
          <span class="slot-label">${SLOT_NAMES[slotIdx] || "Repas " + (slotIdx + 1)}</span>
          ${dishName ? `<span class="slot-dish">${dishName}</span>` : `<span class="empty">+ ajouter</span>`}
        </div>`;
    }).join("");
    const isToday = date.toDateString() === todayStr;
    return `<div class="day-col">
      <h4 class="${isToday ? "today" : ""}">${DAYS[dayIdx]} ${date.getDate()}/${date.getMonth() + 1}</h4>
      ${slots}
    </div>`;
  }).join("") + `</div>`;
}

function setWeek(w) {
  state.currentWeek = w;
  renderPlan();
}

let pickerEl = null;
function closeSlotPicker() {
  if (pickerEl) { pickerEl.remove(); pickerEl = null; }
}

function openSlotPicker(event, dayIndex, slotIndex) {
  event.stopPropagation();
  closeSlotPicker();
  if (state.dishes.length === 0) {
    toast("Ajoutez d'abord des plats dans la bibliothèque");
    return;
  }
  pickerEl = document.createElement("div");
  pickerEl.className = "slot-picker";
  pickerEl.innerHTML =
    `<button class="clear-btn" onclick="assignDish(${dayIndex}, ${slotIndex}, null)">✕ Vider la case</button>` +
    state.dishes.map((d) =>
      `<button onclick="assignDish(${dayIndex}, ${slotIndex}, ${d.id})">${d.name} <small style="color:var(--muted)">· ${CAT_LABELS[d.category] || ""}</small></button>`
    ).join("");
  document.body.appendChild(pickerEl);
  const rect = event.currentTarget.getBoundingClientRect();
  pickerEl.style.top = Math.min(rect.bottom + 6, window.innerHeight - 320) + "px";
  pickerEl.style.left = Math.min(rect.left, window.innerWidth - 260) + "px";
  setTimeout(() => document.addEventListener("click", closeSlotPicker, { once: true }), 0);
}

async function assignDish(dayIndex, slotIndex, dishId) {
  closeSlotPicker();
  try {
    await api.put(`/api/meal-plans/${state.currentPlanId}/entry`, {
      week_index: state.currentWeek,
      day_index: dayIndex,
      slot_index: slotIndex,
      dish_id: dishId,
    });
    await renderPlan();
    loadHome();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- Init ---------------- */

(async function init() {
  await Promise.all([loadHome(), loadPlants(), loadDishes(), loadPlans()]);
  checkNotifications();
  setInterval(() => { loadPlants(); loadHome(); checkNotifications(); }, 60_000);
})();
