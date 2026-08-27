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
  grocery: [],
  fridge: [],
  wishPeople: [],
  wishes: [],
  currentPlanId: null,
  currentWeek: 1,
  currentPersonId: null,
  wishFilter: "all",
  wishMode: "manual",  // saisie de l'envie : manual | ai
  giftMode: false,     // false = vue sans spoiler (par défaut, pour le/la destinataire)
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

    const wishBox = $("#home-wishlist");
    const birthdays = s.wishlist.upcoming_birthdays;
    if (!s.wishlist.people) {
      wishBox.innerHTML = `<p class="muted">Aucune liste d'envies — créez-en une dans l'onglet Envies.</p>`;
    } else {
      wishBox.innerHTML = (birthdays.length
        ? birthdays.map((b) => `
          <div class="home-item">
            <div class="info">
              <strong>${b.emoji} ${b.name}</strong>
              <small>🎂 ${b.days_until_birthday === 0 ? "c'est aujourd'hui !" : `dans ${b.days_until_birthday} jours`}</small>
            </div>
          </div>`).join("")
        : `<p class="muted">Aucun anniversaire dans les 60 jours.</p>`)
        + `<p class="muted fridge-hint">${s.wishlist.open_wishes} envie(s) encore à offrir sur ${s.wishlist.total_wishes}.</p>`;
    }

    $("#home-stats").innerHTML = `
      <div class="stat"><div class="num">${s.plants.total}</div><div class="lbl">Plantes</div></div>
      <div class="stat"><div class="num">${s.plants.due_count}</div><div class="lbl">Arrosages dus</div></div>
      <div class="stat"><div class="num">${s.meals.dishes_in_library}</div><div class="lbl">Plats</div></div>
      <div class="stat"><div class="num">${s.meals.active_plan ? s.meals.active_plan.weeks : 0}</div><div class="lbl">Semaines planifiées</div></div>
      <div class="stat"><div class="num">${s.grocery.items_on_list}</div><div class="lbl">Courses à faire</div></div>
      <div class="stat"><div class="num">${s.grocery.fridge_items}</div><div class="lbl">Dans le frigo</div></div>
      <div class="stat"><div class="num">${s.wishlist.open_wishes}</div><div class="lbl">Envies à offrir</div></div>
      <div class="stat"><div class="num">${s.wishlist.people}</div><div class="lbl">Listes d'envies</div></div>`;
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

async function waterAllPlants() {
  if (!state.plants.length) return toast("Aucune plante à arroser", true);
  if (!confirm(`Marquer les ${state.plants.length} plantes comme arrosées maintenant ?`)) return;
  try {
    const res = await api.post("/api/plants/water-all", {});
    state.notified.clear();
    toast(`${res.count} plantes arrosées 💧`);
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
$("#water-all-btn").addEventListener("click", waterAllPlants);

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

function renderGroceryDishSelect() {
  const sel = $("#grocery-dish-select");
  if (!sel) return;
  sel.innerHTML = state.dishes.length
    ? state.dishes.map((d) => `<option value="${d.id}">${d.name}</option>`).join("")
    : `<option value="">Aucun plat</option>`;
}

function renderDishes() {
  renderGroceryDishSelect();
  const list = $("#dishes-list");
  if (state.dishes.length === 0) {
    list.innerHTML = `<p class="muted">Ajoutez vos plats favoris ici, puis placez-les sur le planning.</p>`;
    return;
  }
  list.innerHTML = state.dishes.map((d) => {
    const ingCount = (d.ingredients || "").split("\n").filter((l) => l.trim()).length;
    return `
    <div class="dish-item">
      ${d.photo ? `<img class="dish-thumb" src="/api/dishes/${d.id}/photo" alt="${d.name}" loading="lazy" />` : ""}
      <div>
        ${d.name}
        <small>${CAT_LABELS[d.category] || d.category}${d.prep_time_minutes ? " · " + d.prep_time_minutes + " min" : ""}${ingCount ? ` · ${ingCount} ingrédient${ingCount > 1 ? "s" : ""}` : ""}</small>
      </div>
      <div>
        ${d.recipe_url ? `<a class="icon-btn" href="${d.recipe_url}" target="_blank" title="Recette">🔗</a>` : ""}
        <button class="icon-btn" onclick="openDishModal(${d.id})" title="Modifier">✏️</button>
        <button class="icon-btn" onclick="deleteDish(${d.id})" title="Supprimer">🗑️</button>
      </div>
    </div>`;
  }).join("");
}

/* Mode de saisie d'un plat : "manual" (défaut) ou "ai" (pré-remplissage par photo) */
let dishFormMode = "manual";

function setDishMode(mode) {
  dishFormMode = mode === "ai" ? "ai" : "manual";
  document.querySelectorAll("#dish-mode-choice .mode-option").forEach((b) => {
    const active = b.dataset.mode === dishFormMode;
    b.classList.toggle("is-active", active);
    b.setAttribute("aria-pressed", active ? "true" : "false");
  });
  $("#dish-photo-btn").textContent =
    dishFormMode === "ai" ? "📷 Identifier par photo" : "📷 Ajouter une photo";
  $("#dish-photo-status").textContent = "";
}

function openDishModal(id = null) {
  const form = $("#dish-form");
  form.reset();
  setDishMode("manual");
  $("#dish-id").value = id || "";
  $("#dish-photo-token").value = "";
  $("#dish-photo-status").textContent = "";
  const preview = $("#dish-photo-preview");
  preview.classList.add("hidden");
  preview.removeAttribute("src");
  $("#dish-modal-title").textContent = id ? "Modifier le plat" : "Ajouter un plat";
  if (id) {
    const d = state.dishes.find((x) => x.id === id);
    if (d) {
      $("#dish-name").value = d.name;
      $("#dish-category").value = d.category;
      $("#dish-prep").value = d.prep_time_minutes ?? "";
      $("#dish-url").value = d.recipe_url;
      $("#dish-ingredients").value = d.ingredients || "";
      $("#dish-notes").value = d.notes;
      if (d.photo) {
        preview.src = `/api/dishes/${d.id}/photo`;
        preview.classList.remove("hidden");
      }
    }
  }
  $("#dish-modal").showModal();
}

/* ---- Photo du plat : simple ajout (manuel) ou identification (IA) ---- */

document.querySelectorAll("#dish-mode-choice .mode-option").forEach((b) => {
  b.addEventListener("click", () => setDishMode(b.dataset.mode));
});

$("#dish-photo-btn").addEventListener("click", () => $("#dish-photo-input").click());

$("#dish-photo-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // allow re-picking the same file
  if (!file) return;

  const useAi = dishFormMode === "ai";
  const status = $("#dish-photo-status");
  const btn = $("#dish-photo-btn");
  const preview = $("#dish-photo-preview");
  try {
    const blob = await downscaleImage(file);
    preview.src = URL.createObjectURL(blob);
    preview.classList.remove("hidden");
    status.textContent = useAi ? "🔍 Identification en cours…" : "⏳ Envoi de la photo…";
    btn.disabled = true;

    const fd = new FormData();
    fd.append("file", blob, "plat.jpg");
    const url = useAi ? "/api/dishes/identify" : "/api/dishes/photo";
    const res = await fetch(url, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Erreur ${res.status}`);

    if (useAi) {
      if (data.name) $("#dish-name").value = data.name;
      if (data.category) $("#dish-category").value = data.category;
      if (data.prep_time_minutes) $("#dish-prep").value = data.prep_time_minutes;
      if (data.ingredients) $("#dish-ingredients").value = data.ingredients;
      if (data.notes) $("#dish-notes").value = data.notes;
    }
    $("#dish-photo-token").value = data.photo_token || "";
    status.textContent = useAi
      ? "✅ Plat identifié — vérifiez et ajustez si besoin."
      : "✅ Photo ajoutée.";
  } catch (err) {
    status.textContent = "";
    preview.classList.add("hidden");
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

$("#dish-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#dish-id").value;
  const prep = $("#dish-prep").value;
  const payload = {
    name: $("#dish-name").value.trim(),
    category: $("#dish-category").value,
    prep_time_minutes: prep ? parseInt(prep, 10) : null,
    recipe_url: $("#dish-url").value.trim(),
    ingredients: $("#dish-ingredients").value.trim(),
    notes: $("#dish-notes").value.trim(),
    photo: $("#dish-photo-token").value || null,
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
  const gsel = $("#grocery-plan-select");
  if (gsel) {
    const active = (state.plans.find((p) => p.active) || state.plans[0])?.id;
    gsel.innerHTML = state.plans.length
      ? state.plans.map((p) => `<option value="${p.id}" ${p.id === active ? "selected" : ""}>${p.name}</option>`).join("")
      : `<option value="">Aucun plan</option>`;
  }
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

/* ---------------- Courses & Frigo ---------------- */

async function loadGrocery() {
  try {
    state.grocery = await api.get("/api/grocery");
    renderGrocery();
  } catch (e) { toast(e.message, true); }
}

async function loadFridge() {
  try {
    state.fridge = await api.get("/api/fridge");
    renderFridge();
  } catch (e) { toast(e.message, true); }
}

function renderGrocery() {
  const list = $("#grocery-list");
  if (state.grocery.length === 0) {
    list.innerHTML = `<p class="muted">Liste vide. Ajoutez des articles ou générez-la depuis un plan de repas.</p>`;
    return;
  }
  list.innerHTML = state.grocery.map((g) => {
    const src = g.source === "plan" ? `🍽️ ${g.dishes || "plan"}`
      : g.source === "dish" ? `🍲 ${g.dishes || "plat"}`
      : "manuel";
    return `
    <div class="grocery-item">
      <div class="info">
        <strong>${g.name}</strong>
        <small>${[g.quantity, src].filter(Boolean).join(" · ")}</small>
      </div>
      <div class="grocery-actions">
        <button class="btn btn-primary btn-sm" onclick="buyGroceryItem(${g.id})" title="Marquer comme acheté → frigo">✓ Acheté</button>
        <button class="icon-btn" onclick="deleteGroceryItem(${g.id})" title="Retirer de la liste">🗑️</button>
      </div>
    </div>`;
  }).join("");
}

function renderFridge() {
  const list = $("#fridge-list");
  if (state.fridge.length === 0) {
    list.innerHTML = `<p class="muted">Frigo vide pour l'instant.</p>`;
    return;
  }
  list.innerHTML = state.fridge.map((f) => `
    <div class="grocery-item">
      <div class="info">
        <strong>${f.name}</strong>
        <small>${[f.quantity, f.source === "grocery" ? "acheté" : "ajouté", fmtDate(f.added_at)].filter(Boolean).join(" · ")}</small>
      </div>
      <div class="grocery-actions">
        <button class="icon-btn" onclick="deleteFridgeItem(${f.id})" title="Consommé / retirer">🗑️</button>
      </div>
    </div>`).join("");
}

$("#grocery-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api.post("/api/grocery", {
      name: $("#grocery-name").value.trim(),
      quantity: $("#grocery-qty").value.trim(),
    });
    e.target.reset();
    toast("Article ajouté 🛒");
    await Promise.all([loadGrocery(), loadHome()]);
  } catch (e2) { toast(e2.message, true); }
});

$("#fridge-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api.post("/api/fridge", {
      name: $("#fridge-name").value.trim(),
      quantity: $("#fridge-qty").value.trim(),
    });
    e.target.reset();
    toast("Ajouté au frigo 🧊");
    await Promise.all([loadFridge(), loadHome()]);
  } catch (e2) { toast(e2.message, true); }
});

async function buyGroceryItem(id) {
  try {
    await api.post(`/api/grocery/${id}/buy`, {});
    toast("Acheté — direction le frigo 🧊");
    await Promise.all([loadGrocery(), loadFridge(), loadHome()]);
  } catch (e) { toast(e.message, true); }
}

async function deleteGroceryItem(id) {
  try {
    await api.del(`/api/grocery/${id}`);
    await Promise.all([loadGrocery(), loadHome()]);
  } catch (e) { toast(e.message, true); }
}

async function deleteFridgeItem(id) {
  try {
    await api.del(`/api/fridge/${id}`);
    await Promise.all([loadFridge(), loadHome()]);
  } catch (e) { toast(e.message, true); }
}

$("#grocery-generate-btn").addEventListener("click", async () => {
  const planId = parseInt($("#grocery-plan-select").value, 10);
  if (!planId) return toast("Créez d'abord un plan de repas (onglet Repas)", true);
  if (!confirm("Remplacer les articles générés depuis ce plan ? (vos articles manuels sont conservés)")) return;
  try {
    const res = await api.post("/api/grocery/from-plan", { plan_id: planId });
    toast(res.count ? `${res.count} ingrédients ajoutés depuis « ${res.plan_name} » 🪄`
                    : "Aucun ingrédient trouvé — remplissez les ingrédients des plats du plan.");
    await Promise.all([loadGrocery(), loadHome()]);
  } catch (e) { toast(e.message, true); }
});

$("#grocery-dish-btn").addEventListener("click", async () => {
  const dishId = parseInt($("#grocery-dish-select").value, 10);
  if (!dishId) return toast("Ajoutez d'abord des plats avec des ingrédients (onglet Repas)", true);
  try {
    const res = await api.post("/api/grocery/from-dish", { dish_id: dishId });
    toast(res.count ? `${res.count} ingrédients ajoutés depuis « ${res.dish_name} » 🪄`
                    : "Aucun ingrédient — ajoutez des ingrédients à ce plat (onglet Repas).");
    await Promise.all([loadGrocery(), loadHome()]);
  } catch (e) { toast(e.message, true); }
});

/* ---------------- Envies & cadeaux ---------------- */

/* Deux lectures de la même liste :
   - vue sans spoiler (par défaut) : l'API masque qui a réservé/acheté quoi,
     c'est la vue que le/la destinataire peut regarder sans rien gâcher ;
   - vue cadeau (bouton 🎁) : tout est visible, pour celui/celle qui offre. */

const WISH_CATEGORIES = {
  tech: ["💻", "Tech"], maison: ["🏠", "Maison"], vetements: ["👕", "Vêtements"],
  livres: ["📚", "Livres"], sport: ["🏃", "Sport"], loisirs: ["🎲", "Loisirs"],
  beaute: ["💄", "Beauté"], cuisine: ["🍳", "Cuisine"], voyage: ["✈️", "Voyage"],
  experience: ["🎟️", "Expérience"], autre: ["🎁", "Autre"],
};
const WISH_STATUS_LABELS = {
  wanted: "À offrir", reserved: "Réservé 🔒", bought: "Acheté 🎀",
  received: "Offert ✅", archived: "Archivé",
};
const WISH_FILTERS = [
  ["all", "Toutes"], ["wanted", "À offrir"], ["reserved", "Réservées"],
  ["bought", "Achetées"], ["received", "Offertes"],
];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// N'accepte que des liens web : évite un href "javascript:" venu d'un champ texte.
function safeUrl(url) {
  return /^https?:\/\//i.test(url || "") ? esc(url) : "";
}

function fmtPrice(item) {
  return item.price ? `${item.price.toLocaleString("fr-CH")} ${esc(item.currency)}` : "";
}

function wishStars(priority) { return "⭐".repeat(Math.max(1, Math.min(3, priority))); }

function giftQuery() { return state.giftMode ? "" : "?hide_reservations=true"; }

async function loadWishlist() {
  try {
    state.wishPeople = await api.get(`/api/wishlist/overview${giftQuery()}`);
    if (!state.wishPeople.some((p) => p.id === state.currentPersonId)) {
      state.currentPersonId = state.wishPeople.length ? state.wishPeople[0].id : null;
    }
    renderPeople();
    await loadWishes();
  } catch (e) { toast(e.message, true); }
}

async function loadWishes() {
  const grid = $("#wishes-grid");
  if (!state.currentPersonId) {
    $("#wishlist-head").innerHTML = "";
    $("#wish-filters").innerHTML = "";
    grid.innerHTML = `<p class="muted">Créez d'abord une personne, puis ajoutez ses envies 🎁</p>`;
    return;
  }
  try {
    const q = state.giftMode ? "" : "&hide_reservations=true";
    state.wishes = await api.get(`/api/wishlist/items?person_id=${state.currentPersonId}${q}`);
    renderWishlistHead();
    renderWishFilters();
    renderWishes();
  } catch (e) { toast(e.message, true); }
}

function renderPeople() {
  const box = $("#people-list");
  if (!state.wishPeople.length) {
    box.innerHTML = `<p class="muted">Personne pour l'instant. Ajoutez qui vous voulez gâter 🎁</p>`;
    return;
  }
  box.innerHTML = state.wishPeople.map((p) => {
    const bday = p.days_until_birthday === null ? ""
      : p.days_until_birthday === 0 ? "🎂 c'est aujourd'hui !"
      : `🎂 dans ${p.days_until_birthday} j`;
    const budget = p.estimated_budget ? ` · ~${p.estimated_budget.toLocaleString("fr-CH")} ${esc(p.currency)}` : "";
    return `
    <button class="person-item ${p.id === state.currentPersonId ? "active" : ""}"
            onclick="selectPerson(${p.id})">
      <span class="person-emoji">${esc(p.emoji)}</span>
      <span class="info">
        <strong>${esc(p.name)}</strong>
        <small>${p.open_items} envie(s) à offrir${budget}</small>
        ${bday ? `<small>${bday}</small>` : ""}
      </span>
    </button>`;
  }).join("");
}

function renderWishlistHead() {
  const person = state.wishPeople.find((p) => p.id === state.currentPersonId);
  if (!person) return;
  const offered = person.counts.received || 0;
  $("#wishlist-head").innerHTML = `
    <div class="section-head wishlist-head">
      <div>
        <h3>${esc(person.emoji)} Envies de ${esc(person.name)}</h3>
        <p class="muted">
          ${person.total_items} envie(s) · ${person.open_items} encore à offrir · ${offered} déjà offerte(s)
          ${person.estimated_budget ? ` · budget restant ~${person.estimated_budget.toLocaleString("fr-CH")} ${esc(person.currency)}` : ""}
        </p>
        ${person.notes ? `<p class="muted person-notes">📝 ${esc(person.notes)}</p>` : ""}
      </div>
      <div class="plan-actions">
        <button class="btn btn-ghost btn-sm" onclick="openPersonModal(${person.id})">✏️ Fiche</button>
        <button class="btn btn-ghost btn-sm" onclick="deletePerson(${person.id})">🗑️</button>
      </div>
    </div>`;
}

function renderWishFilters() {
  $("#wish-filters").innerHTML = WISH_FILTERS
    // Sans le mode cadeau, l'API renvoie les réservations comme « à offrir » :
    // proposer ces filtres n'aurait aucun sens.
    .filter(([key]) => state.giftMode || !["reserved", "bought"].includes(key))
    .map(([key, label]) => `
      <button class="week-tab ${state.wishFilter === key ? "active" : ""}"
              onclick="setWishFilter('${key}')">${label}</button>`).join("");
}

function setWishFilter(filter) {
  state.wishFilter = filter;
  renderWishFilters();
  renderWishes();
}

function selectPerson(id) {
  state.currentPersonId = id;
  renderPeople();
  loadWishes();
}

function renderWishes() {
  const grid = $("#wishes-grid");
  const items = state.wishes.filter((w) =>
    state.wishFilter === "all" ? w.status !== "archived" : w.status === state.wishFilter);
  if (!items.length) {
    grid.innerHTML = `<p class="muted">Aucune envie ici. Ajoutez-en une avec « + Envie » 🎁</p>`;
    return;
  }
  grid.innerHTML = items.map((w) => {
    const [emoji, catLabel] = WISH_CATEGORIES[w.category] || WISH_CATEGORIES.autre;
    const chips = [
      [catLabel, emoji],
      [w.size && `Taille ${w.size}`, "📏"],
      [w.color, "🎨"],
      [w.quantity > 1 && `×${w.quantity}`, "🔢"],
      [w.shop, "🏬"],
      [w.occasion, "📅"],
      [w.target_date && `avant le ${fmtDate(w.target_date)}${
        w.days_until_target !== null && w.days_until_target >= 0 ? ` (${w.days_until_target} j)` : " ⏰"}`, "⏳"],
    ].filter(([v]) => v).map(([v, ic]) => `<span class="chip">${ic} ${esc(v)}</span>`).join("");

    const taker = w.bought_by || w.reserved_by;
    const url = safeUrl(w.url);
    return `
    <div class="card wish-card wish-${w.status}">
      <div class="wish-media">
        ${w.photo
          ? `<img class="wish-photo" src="/api/wishlist/items/${w.id}/photo" alt="${esc(w.name)}" loading="lazy" />`
          : `<span class="wish-emoji">${emoji}</span>`}
        <span class="wish-stars" title="Niveau d'envie">${wishStars(w.priority)}</span>
      </div>
      <h3>${esc(w.name)}</h3>
      ${w.price ? `<div class="wish-price">${fmtPrice(w)}</div>` : ""}
      ${w.description ? `<p class="notes">${esc(w.description)}</p>` : ""}
      <div class="wish-chips">${chips}</div>
      <span class="status-pill wish-pill-${w.status}">${WISH_STATUS_LABELS[w.status]}</span>
      ${state.giftMode && taker ? `<div class="location">🤫 ${esc(taker)} s'en occupe</div>` : ""}
      ${w.notes ? `<div class="notes">📝 ${esc(w.notes)}</div>` : ""}
      <div class="actions">
        ${url ? `<a class="btn btn-ghost btn-sm" href="${url}" target="_blank" rel="noopener">🔗 Voir</a>` : ""}
        ${state.giftMode ? giftActions(w) : ""}
        <button class="btn btn-ghost btn-sm btn-icon" onclick="openWishModal(${w.id})" title="Modifier">✏️</button>
        <button class="btn btn-ghost btn-sm btn-icon" onclick="deleteWish(${w.id})" title="Supprimer">🗑️</button>
      </div>
    </div>`;
  }).join("");
}

function giftActions(w) {
  if (w.status === "received") return "";
  if (w.status === "bought") {
    return `<button class="btn btn-primary btn-sm" onclick="wishAction(${w.id}, 'received')">🎉 Offert</button>
            <button class="btn btn-ghost btn-sm btn-icon" onclick="wishAction(${w.id}, 'unreserve')" title="Libérer">↩️</button>`;
  }
  if (w.status === "reserved") {
    return `<button class="btn btn-primary btn-sm" onclick="wishAction(${w.id}, 'bought')">🛍️ Acheté</button>
            <button class="btn btn-ghost btn-sm btn-icon" onclick="wishAction(${w.id}, 'unreserve')" title="Libérer">↩️</button>`;
  }
  return `<button class="btn btn-primary btn-sm" onclick="wishAction(${w.id}, 'reserve')">🤝 Je m'en occupe</button>`;
}

// Mémorise qui offre pour ne pas redemander le prénom à chaque réservation.
function gifterName() {
  let name = localStorage.getItem("gifterName") || "";
  if (!name) {
    name = (prompt("Qui s'en occupe ? (votre prénom)") || "").trim();
    if (name) localStorage.setItem("gifterName", name);
  }
  return name;
}

async function wishAction(id, action) {
  try {
    const body = ["reserve", "bought"].includes(action) ? { by: gifterName() } : {};
    await api.post(`/api/wishlist/items/${id}/${action}`, body);
    toast({ reserve: "Réservé 🤝", unreserve: "Réservation annulée",
            bought: "Cadeau acheté 🛍️", received: "Cadeau offert 🎉" }[action]);
    await Promise.all([loadWishlist(), loadHome()]);
  } catch (e) { toast(e.message, true); }
}

async function deleteWish(id) {
  const wish = state.wishes.find((w) => w.id === id);
  if (!confirm(`Supprimer « ${wish ? wish.name : "cette envie"} » ?`)) return;
  try {
    await api.del(`/api/wishlist/items/${id}`);
    toast("Envie supprimée");
    await Promise.all([loadWishlist(), loadHome()]);
  } catch (e) { toast(e.message, true); }
}

$("#gift-mode-btn").addEventListener("click", async () => {
  state.giftMode = !state.giftMode;
  const btn = $("#gift-mode-btn");
  btn.textContent = state.giftMode ? "🎁 Mode cadeau" : "🙈 Vue sans spoiler";
  btn.classList.toggle("btn-primary", state.giftMode);
  btn.classList.toggle("btn-ghost", !state.giftMode);
  btn.setAttribute("aria-pressed", String(state.giftMode));
  if (!state.giftMode && ["reserved", "bought"].includes(state.wishFilter)) state.wishFilter = "all";
  if (state.giftMode) toast("Mode cadeau : les réservations sont visibles 🤫");
  await loadWishlist();
});

/* ---- Personnes ---- */

function openPersonModal(id = null) {
  const form = $("#person-form");
  form.reset();
  $("#person-id").value = id || "";
  $("#person-modal-title").textContent = id ? "Modifier la fiche" : "Nouvelle liste d'envies";
  if (id) {
    const p = state.wishPeople.find((x) => x.id === id);
    if (p) {
      $("#person-name").value = p.name;
      $("#person-emoji").value = p.emoji || "";
      $("#person-birthday").value = p.birthday || "";
      $("#person-notes").value = p.notes || "";
    }
  }
  $("#person-modal").showModal();
}

$("#add-person-btn").addEventListener("click", () => openPersonModal());

$("#person-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#person-id").value;
  const payload = {
    name: $("#person-name").value.trim(),
    emoji: $("#person-emoji").value.trim() || "🎁",
    birthday: $("#person-birthday").value || null,
    notes: $("#person-notes").value.trim(),
  };
  try {
    const saved = id ? await api.put(`/api/wishlist/people/${id}`, payload)
                     : await api.post("/api/wishlist/people", payload);
    state.currentPersonId = saved.id;
    closeModal("#person-modal");
    toast("Fiche enregistrée 🎁");
    await Promise.all([loadWishlist(), loadHome()]);
  } catch (e2) { toast(e2.message, true); }
});

async function deletePerson(id) {
  const person = state.wishPeople.find((p) => p.id === id);
  if (!confirm(`Supprimer ${person ? person.name : "cette personne"} et toutes ses envies ?`)) return;
  try {
    await api.del(`/api/wishlist/people/${id}`);
    state.currentPersonId = null;
    toast("Liste supprimée");
    await Promise.all([loadWishlist(), loadHome()]);
  } catch (e) { toast(e.message, true); }
}

/* ---- Envies ---- */

function setWishMode(mode) {
  state.wishMode = mode;
  document.querySelectorAll("#wish-mode-choice .mode-option").forEach((opt) => {
    const active = opt.dataset.mode === mode;
    opt.classList.toggle("is-active", active);
    opt.setAttribute("aria-pressed", String(active));
  });
  $("#wish-photo-btn").textContent = mode === "ai" ? "📷 Identifier par photo" : "📷 Ajouter une photo";
  $("#wish-photo-status").textContent = "";
}

document.querySelectorAll("#wish-mode-choice .mode-option").forEach((opt) =>
  opt.addEventListener("click", () => setWishMode(opt.dataset.mode))
);

function openWishModal(id = null) {
  if (!state.wishPeople.length) return toast("Créez d'abord une personne", true);
  const form = $("#wish-form");
  form.reset();
  setWishMode("manual");
  $("#wish-id").value = id || "";
  $("#wish-photo-token").value = "";
  const preview = $("#wish-photo-preview");
  preview.classList.add("hidden");
  preview.removeAttribute("src");
  $("#wish-person").innerHTML = state.wishPeople
    .map((p) => `<option value="${p.id}">${esc(p.emoji)} ${esc(p.name)}</option>`).join("");
  $("#wish-person").value = state.currentPersonId || state.wishPeople[0].id;
  $("#wish-modal-title").textContent = id ? "Modifier l'envie" : "Ajouter une envie";
  $("#wish-currency").value = "CHF";
  $("#wish-quantity").value = 1;

  if (id) {
    const w = state.wishes.find((x) => x.id === id);
    if (w) {
      $("#wish-person").value = w.person_id;
      $("#wish-name").value = w.name;
      $("#wish-description").value = w.description || "";
      $("#wish-category").value = w.category;
      $("#wish-priority").value = w.priority;
      $("#wish-price").value = w.price ?? "";
      $("#wish-currency").value = w.currency || "CHF";
      $("#wish-quantity").value = w.quantity;
      $("#wish-size").value = w.size || "";
      $("#wish-color").value = w.color || "";
      $("#wish-shop").value = w.shop || "";
      $("#wish-url").value = w.url || "";
      $("#wish-occasion").value = w.occasion || "";
      $("#wish-target").value = w.target_date || "";
      $("#wish-notes").value = w.notes || "";
      if (w.photo) {
        preview.src = `/api/wishlist/items/${w.id}/photo`;
        preview.classList.remove("hidden");
      }
    }
  }
  $("#wish-modal").showModal();
}

$("#add-wish-btn").addEventListener("click", () => openWishModal());
$("#wish-photo-btn").addEventListener("click", () => $("#wish-photo-input").click());

$("#wish-photo-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // allow re-picking the same file
  if (!file) return;

  const status = $("#wish-photo-status");
  const btn = $("#wish-photo-btn");
  const preview = $("#wish-photo-preview");
  const useAi = state.wishMode === "ai";
  try {
    const blob = await downscaleImage(file);
    preview.src = URL.createObjectURL(blob);
    preview.classList.remove("hidden");
    status.textContent = useAi ? "🔍 Identification en cours…" : "📤 Envoi de la photo…";
    btn.disabled = true;

    const fd = new FormData();
    fd.append("file", blob, "envie.jpg");
    const res = await fetch(useAi ? "/api/wishlist/identify" : "/api/wishlist/photo",
                            { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Erreur ${res.status}`);

    if (useAi) {
      if (data.name) $("#wish-name").value = data.name;
      if (data.category) $("#wish-category").value = data.category;
      if (data.price) $("#wish-price").value = data.price;
      if (data.shop) $("#wish-shop").value = data.shop;
      if (data.color) $("#wish-color").value = data.color;
      if (data.description) $("#wish-description").value = data.description;
    }
    $("#wish-photo-token").value = data.photo_token || "";
    status.textContent = useAi ? "✅ Objet identifié — vérifiez le prix et les détails."
                               : "✅ Photo prête à être enregistrée.";
  } catch (err) {
    status.textContent = "";
    preview.classList.add("hidden");
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

$("#wish-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#wish-id").value;
  const price = $("#wish-price").value;
  const payload = {
    person_id: parseInt($("#wish-person").value, 10),
    name: $("#wish-name").value.trim(),
    description: $("#wish-description").value.trim(),
    category: $("#wish-category").value,
    price: price === "" ? null : parseFloat(price),
    currency: $("#wish-currency").value.trim() || "CHF",
    url: $("#wish-url").value.trim(),
    shop: $("#wish-shop").value.trim(),
    size: $("#wish-size").value.trim(),
    color: $("#wish-color").value.trim(),
    quantity: parseInt($("#wish-quantity").value, 10) || 1,
    priority: parseInt($("#wish-priority").value, 10),
    occasion: $("#wish-occasion").value.trim(),
    target_date: $("#wish-target").value || null,
    notes: $("#wish-notes").value.trim(),
    photo: $("#wish-photo-token").value || null,
  };
  try {
    if (id) await api.put(`/api/wishlist/items/${id}`, payload);
    else await api.post("/api/wishlist/items", payload);
    state.currentPersonId = payload.person_id;
    closeModal("#wish-modal");
    toast("Envie enregistrée 🎁");
    await Promise.all([loadWishlist(), loadHome()]);
  } catch (e2) { toast(e2.message, true); }
});

/* ---------------- Rangement : cartons & objets ---------------- */

const stState = {
  boxes: [],
  items: [],
  summary: null,
  q: "",
  owner: "",
  status: "",
  category: "",
  mode: "boxes",
  open: new Set(),   // ids des cartons dépliés
};

const stEsc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const stNorm = (s) => String(s ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

/** Surligne les mots de la recherche dans un texte (et l'échappe au passage). */
function stMark(text) {
  const raw = String(text ?? "");
  const tokens = stNorm(stState.q).split(/\s+/).filter(Boolean);
  if (!tokens.length) return stEsc(raw);

  const hay = stNorm(raw);
  const hits = [];
  for (const t of tokens) {
    for (let i = hay.indexOf(t); i !== -1; i = hay.indexOf(t, i + t.length)) hits.push([i, i + t.length]);
  }
  if (!hits.length) return stEsc(raw);

  hits.sort((a, b) => a[0] - b[0]);
  let out = "", cursor = 0;
  for (const [start, end] of hits) {
    if (start < cursor) continue;
    out += stEsc(raw.slice(cursor, start)) + "<mark>" + stEsc(raw.slice(start, end)) + "</mark>";
    cursor = end;
  }
  return out + stEsc(raw.slice(cursor));
}

function stFmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

function stOwnerClass(name) {
  const idx = (stState.summary?.owners || []).findIndex((o) => o.name === name);
  return idx < 0 ? "" : "o" + (idx % 2);
}

const stFiltering = () =>
  Boolean(stState.q.trim() || stState.owner || stState.status || stState.category);

/* ---- Chargement ---- */

async function loadStorage() {
  try {
    const params = new URLSearchParams();
    if (stState.q.trim()) params.set("q", stState.q.trim());
    if (stState.owner) params.set("owner", stState.owner);
    if (stState.status) params.set("status", stState.status);
    if (stState.category) params.set("category", stState.category);

    const [boxes, items, summary] = await Promise.all([
      api.get("/api/storage/boxes"),
      api.get("/api/storage/items?" + params.toString()),
      api.get("/api/storage/summary"),
    ]);
    stState.boxes = boxes;
    stState.items = items;
    stState.summary = summary;
    renderStorage();
  } catch (e) { toast(e.message, true); }
}

/* ---- Rendu ---- */

function renderStorage() {
  stRenderFilters();
  stRenderHomeCard();

  const badge = $("#storage-badge");
  if (badge) {
    badge.textContent = stState.summary.out_count;
    badge.classList.toggle("hidden", stState.summary.out_count === 0);
  }

  const wrap = $("#storage-results");
  if (stState.items.length === 0) {
    wrap.innerHTML = `<div class="storage-empty"><span class="big">🗃️</span>${
      stFiltering()
        ? "Aucun objet ne correspond à cette recherche."
        : "Rien de rangé pour l'instant — créez un carton, puis ajoutez ce qu'il contient."
    }</div>`;
    return;
  }
  wrap.innerHTML = stState.mode === "items" ? stRenderFlat() : stRenderBoxes();
}

function stRenderFilters() {
  const s = stState.summary;

  $("#storage-owner-chips").innerHTML =
    `<button class="chip ${stState.owner === "" ? "active" : ""}" data-owner="">👥 Tous</button>` +
    s.owners.map((o) =>
      `<button class="chip ${stState.owner === o.name ? "active" : ""}" data-owner="${stEsc(o.name)}">${
        stEsc(o.name)}<span class="chip-count">${o.count}</span></button>`).join("");

  const cat = $("#storage-category");
  cat.innerHTML = `<option value="">Toutes les collections</option>` +
    s.categories.map((c) => `<option value="${stEsc(c.name)}">${stEsc(c.name)} (${c.count})</option>`).join("");
  cat.value = stState.category;

  $("#st-categories").innerHTML = s.categories.map((c) => `<option value="${stEsc(c.name)}"></option>`).join("");

  $("#storage-stats").innerHTML = `
    <span><b>${s.total_items}</b> objets</span>
    <span><b>${s.total_boxes}</b> cartons</span>
    <span class="out"><b>${s.out_count}</b> sortis</span>
    ${s.unboxed_items ? `<span><b>${s.unboxed_items}</b> sans carton</span>` : ""}
    ${stFiltering() ? `<span class="muted">→ ${stState.items.length} résultat(s)</span>` : ""}`;
}

/** Petite carte d'accueil : ce qui est sorti des cartons. */
function stRenderHomeCard() {
  const box = $("#home-storage");
  if (!box) return;
  const out = stState.summary.out;
  if (out.length === 0) {
    box.innerHTML = `<p class="muted">Tout est rangé dans les cartons 📦</p>`;
    return;
  }
  box.innerHTML = out.slice(0, 6).map((i) => `
    <div class="home-item">
      <div class="info">
        <strong>${stEsc(i.name)}</strong>
        <small>${[
          i.box_code ? "Carton " + stEsc(i.box_code) : "sans carton",
          i.out_note ? stEsc(i.out_note) : "",
          i.out_since ? "depuis le " + stFmtDate(i.out_since) : "",
        ].filter(Boolean).join(" · ")}</small>
      </div>
      <button class="btn btn-primary btn-sm" onclick="stPutBack(${i.id})">Ranger</button>
    </div>`).join("") +
    (out.length > 6 ? `<p class="muted">…et ${out.length - 6} autre(s).</p>` : "");
}

function stRenderBoxes() {
  const byBox = new Map();
  for (const item of stState.items) {
    const key = item.box_id ?? 0;
    if (!byBox.has(key)) byBox.set(key, []);
    byBox.get(key).push(item);
  }
  const filtering = stFiltering();
  const cards = stState.boxes
    .filter((b) => !filtering || byBox.has(b.id))
    .map((b) => stBoxCard(b, byBox.get(b.id) || []));
  if (byBox.has(0)) cards.push(stBoxCard(null, byBox.get(0)));
  return `<div class="boxes-grid">${cards.join("")}</div>`;
}

function stBoxCard(box, items) {
  const id = box ? box.id : 0;
  const open = stFiltering() || stState.open.has(id);
  const total = box ? box.item_count : items.length;
  const out = box ? box.out_count : items.filter((i) => i.status === "out").length;
  const shown = items.length;
  const sub = box
    ? [box.kind, box.location].filter(Boolean).join(" · ")
    : "À ranger dans un carton";

  return `
  <div class="box-card ${open ? "open" : ""}">
    <div class="box-head" onclick="stToggleBox(${id})">
      <div class="box-tag">${box ? stMark(box.code) : "?"}</div>
      <div class="box-titles">
        <strong>${box ? stMark(box.name || "Carton " + box.code) : "Sans carton"}</strong>
        <small>${sub ? stMark(sub) : "&nbsp;"}</small>
      </div>
      <div class="box-counts">
        <span class="count-pill">${shown < total ? shown + "/" + total : total} objet${total > 1 ? "s" : ""}</span>
        ${out ? `<span class="count-pill out">${out} sorti${out > 1 ? "s" : ""}</span>` : ""}
        <span class="box-chevron">▶</span>
      </div>
    </div>
    ${open ? `
    <div class="box-body">
      <div class="things">${items.map((i) => stThing(i, false)).join("") ||
        `<p class="muted" style="padding:.5rem">Carton vide.</p>`}</div>
      <div class="box-foot">
        ${box ? `<button class="btn btn-ghost btn-sm" onclick="stNewItem(${box.id})">+ Objet</button>
                 <button class="btn btn-ghost btn-sm" onclick="stEditBox(${box.id})">✏️ Carton</button>
                 <button class="btn btn-ghost btn-sm" onclick="stDeleteBox(${box.id})">🗑️</button>` : ""}
      </div>
    </div>` : ""}
  </div>`;
}

function stRenderFlat() {
  return `<div class="card"><div class="things">${
    stState.items.map((i) => stThing(i, true)).join("")}</div></div>`;
}

function stThing(item, showBox) {
  const out = item.status === "out";
  const owners = item.owners.map((o) =>
    `<span class="owner-chip ${stOwnerClass(o)}">${stEsc(o)}</span>`).join("");

  const sub = [];
  if (showBox) sub.push(item.box_code ? `📦 Carton ${stEsc(item.box_code)}${
    item.box_name ? " · " + stMark(item.box_name) : ""}` : "📦 sans carton");
  if (item.category && (!item.box_name || item.category !== item.box_name)) sub.push(stMark(item.category));
  if (item.description) sub.push(stMark(item.description));
  if (out) {
    sub.push(`🚪 ${[item.out_note ? stMark(item.out_note) : "sorti",
      item.out_since ? "depuis le " + stFmtDate(item.out_since) : ""].filter(Boolean).join(" · ")}`);
  }

  return `
  <div class="thing ${out ? "is-out" : ""}">
    <div class="thing-main">
      <span class="thing-name">
        ${stMark(item.name)}
        ${item.quantity > 1 ? `<span class="qty-badge">×${item.quantity}</span>` : ""}
        ${owners}
        ${out ? `<span class="out-pill">sorti</span>` : ""}
      </span>
      ${sub.length ? `<small class="thing-sub">${sub.join(" · ")}</small>` : ""}
    </div>
    <div class="thing-actions">
      ${out
        ? `<button class="icon-btn" title="Remettre dans le carton" onclick="stPutBack(${item.id})">📥</button>`
        : `<button class="icon-btn" title="Sortir du carton" onclick="stTakeOut(${item.id})">🚪</button>`}
      <button class="icon-btn" title="Modifier" onclick="stEditItem(${item.id})">✏️</button>
      <button class="icon-btn" title="Supprimer" onclick="stDeleteItem(${item.id})">🗑️</button>
    </div>
  </div>`;
}

/* ---- Interactions liste ---- */

function stToggleBox(id) {
  if (stFiltering()) return;                  // en recherche, tout est déjà déplié
  if (stState.open.has(id)) stState.open.delete(id); else stState.open.add(id);
  renderStorage();
}

function stSetMode(mode) {
  stState.mode = mode;
  document.querySelectorAll("#storage-mode .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode));
  renderStorage();
}

let stSearchTimer = null;
$("#storage-q").addEventListener("input", (e) => {
  stState.q = e.target.value;
  clearTimeout(stSearchTimer);
  stSearchTimer = setTimeout(loadStorage, 180);
});

$("#storage-owner-chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  stState.owner = chip.dataset.owner;
  loadStorage();
});

$("#storage-status-chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  stState.status = chip.dataset.status;
  document.querySelectorAll("#storage-status-chips .chip").forEach((c) =>
    c.classList.toggle("active", c === chip));
  loadStorage();
});

$("#storage-category").addEventListener("change", (e) => {
  stState.category = e.target.value;
  loadStorage();
});

$("#storage-mode").addEventListener("click", (e) => {
  const btn = e.target.closest(".seg-btn");
  if (btn) stSetMode(btn.dataset.mode);
});

// « / » met le curseur dans la recherche quand on est sur l'onglet Rangement.
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
  if (!$("#view-storage").classList.contains("active")) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag)) return;
  e.preventDefault();
  $("#storage-q").focus();
});

/* ---- Sortir / ranger ---- */

function stTakeOut(id) {
  const item = stState.items.find((i) => i.id === id) ||
               stState.summary.out.find((i) => i.id === id);
  $("#st-out-id").value = id;
  $("#st-out-name").textContent = item ? item.name : "";
  $("#st-out-note").value = "";
  $("#st-out-date").value = new Date().toISOString().slice(0, 10);
  $("#st-out-modal").showModal();
}

$("#st-out-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#st-out-id").value;
  const date = $("#st-out-date").value;
  try {
    await api.post(`/api/storage/items/${id}/out`, {
      note: $("#st-out-note").value.trim(),
      at: date || null,
    });
    closeModal("#st-out-modal");
    toast("Objet sorti du carton 🚪");
    await loadStorage();
  } catch (e2) { toast(e2.message, true); }
});

async function stPutBack(id) {
  try {
    await api.post(`/api/storage/items/${id}/in`, {});
    toast("Rangé 📦");
    await loadStorage();
  } catch (e) { toast(e.message, true); }
}

/* ---- Modale objet ---- */

function stBoxOptions(selected) {
  return `<option value="">— sans carton —</option>` + stState.boxes.map((b) =>
    `<option value="${b.id}" ${b.id === selected ? "selected" : ""}>Carton ${stEsc(b.code)}${
      b.name ? " · " + stEsc(b.name) : ""}</option>`).join("");
}

function stRenderOwnerToggles(selected) {
  const known = (stState.summary?.owners || []).map((o) => o.name);
  const extra = selected.filter((o) => !known.includes(o));
  $("#st-item-owners").innerHTML = known.concat(extra).map((name) =>
    `<button type="button" class="owner-toggle ${selected.includes(name) ? "active" : ""}"
             data-owner="${stEsc(name)}">${stEsc(name)}</button>`).join("");
}

$("#st-item-owners").addEventListener("click", (e) => {
  const btn = e.target.closest(".owner-toggle");
  if (btn) btn.classList.toggle("active");
});

function stOpenItemModal(item, boxId) {
  $("#st-item-title").textContent = item ? "Modifier l'objet" : "Ajouter un objet";
  $("#st-item-id").value = item ? item.id : "";
  $("#st-item-name").value = item ? item.name : "";
  $("#st-item-box").innerHTML = stBoxOptions(item ? item.box_id : boxId ?? null);
  $("#st-item-category").value = item ? item.category || "" : "";
  $("#st-item-qty").value = item ? item.quantity : 1;
  $("#st-item-desc").value = item ? item.description || "" : "";
  $("#st-item-notes").value = item ? item.notes || "" : "";
  $("#st-item-owner-extra").value = "";
  stRenderOwnerToggles(item ? item.owners : []);
  $("#st-item-modal").showModal();
}

function stNewItem(boxId) { stOpenItemModal(null, boxId); }

function stEditItem(id) {
  const item = stState.items.find((i) => i.id === id);
  if (item) stOpenItemModal(item);
}

$("#add-item-btn").addEventListener("click", () => {
  if (stState.boxes.length === 0) return toast("Créez d'abord un carton", true);
  stNewItem(null);
});

$("#st-item-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#st-item-id").value;
  const owners = [...document.querySelectorAll("#st-item-owners .owner-toggle.active")]
    .map((b) => b.dataset.owner);
  const extra = $("#st-item-owner-extra").value.trim();
  if (extra) owners.push(extra);

  const boxVal = $("#st-item-box").value;
  const current = id ? stState.items.find((i) => i.id === Number(id)) : null;
  const payload = {
    name: $("#st-item-name").value.trim(),
    box_id: boxVal ? Number(boxVal) : null,
    owner: owners.join(","),
    category: $("#st-item-category").value.trim(),
    quantity: Number($("#st-item-qty").value) || 1,
    description: $("#st-item-desc").value.trim(),
    notes: $("#st-item-notes").value.trim(),
    // le statut se pilote avec les boutons Sortir / Ranger — on le conserve tel quel
    status: current ? current.status : "stored",
    out_since: current ? current.out_since : null,
    out_note: current ? current.out_note || "" : "",
  };
  try {
    if (id) await api.put(`/api/storage/items/${id}`, payload);
    else await api.post("/api/storage/items", payload);
    closeModal("#st-item-modal");
    toast("Objet enregistré 📦");
    await loadStorage();
  } catch (e2) { toast(e2.message, true); }
});

async function stDeleteItem(id) {
  const item = stState.items.find((i) => i.id === id);
  if (!confirm(`Supprimer « ${item ? item.name : "cet objet"} » ?`)) return;
  try {
    await api.del(`/api/storage/items/${id}`);
    toast("Objet supprimé");
    await loadStorage();
  } catch (e) { toast(e.message, true); }
}

/* ---- Modale carton ---- */

function stOpenBoxModal(box) {
  $("#st-box-title").textContent = box ? "Modifier le carton" : "Nouveau carton";
  $("#st-box-id").value = box ? box.id : "";
  $("#st-box-code").value = box ? box.code : "";
  $("#st-box-name").value = box ? box.name || "" : "";
  $("#st-box-kind").value = box ? box.kind || "" : "";
  $("#st-box-location").value = box ? box.location || "" : "";
  $("#st-box-notes").value = box ? box.notes || "" : "";
  $("#st-box-modal").showModal();
}

$("#add-box-btn").addEventListener("click", () => stOpenBoxModal(null));

function stEditBox(id) {
  const box = stState.boxes.find((b) => b.id === id);
  if (box) stOpenBoxModal(box);
}

$("#st-box-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#st-box-id").value;
  const payload = {
    code: $("#st-box-code").value.trim(),
    name: $("#st-box-name").value.trim(),
    kind: $("#st-box-kind").value.trim(),
    location: $("#st-box-location").value.trim(),
    notes: $("#st-box-notes").value.trim(),
  };
  try {
    if (id) await api.put(`/api/storage/boxes/${id}`, payload);
    else await api.post("/api/storage/boxes", payload);
    closeModal("#st-box-modal");
    toast("Carton enregistré 📦");
    await loadStorage();
  } catch (e2) { toast(e2.message, true); }
});

async function stDeleteBox(id) {
  const box = stState.boxes.find((b) => b.id === id);
  if (!box) return;
  if (!confirm(`Supprimer le carton ${box.code}${box.name ? " (" + box.name + ")" : ""} ?\n` +
               `Ses ${box.item_count} objet(s) sont conservés et passent « sans carton ».`)) return;
  try {
    await api.del(`/api/storage/boxes/${id}`);
    stState.open.delete(id);
    toast("Carton supprimé");
    await loadStorage();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- Aide « (i) » des cartes d'accueil ----------------
   Ce que chaque fonctionnalité sait faire depuis Telegram, avec un exemple.
   Le contenu est du HTML statique écrit ici — rien ne vient de l'utilisateur. */

const HOME_HELP = {
  plants: {
    title: "🌿 Depuis Telegram",
    needsAI: false,
    paragraphs: [
      "Le bot envoie un rappel <b>tous les jours à 09:00 et 21:00</b> quand des plantes ont soif — rien à faire pour le recevoir.",
      "<code>/plantes</code> — l'état de l'arrosage à la demande.",
      "L'arrosage lui-même se note ici : bouton <b>Arroser</b> ci-dessous, ou <b>💧 Tout arroser</b> dans l'onglet Plantes.",
    ],
    chat: [
      ["vous", "/plantes"],
      ["bot", "💧 Arrosage — 09:00\n\nPlantes à arroser :\n\n• Monstera (salon)\n   ⚠️ en retard de 2 j\n\nTotal : 1 plante(s)"],
    ],
  },

  wishlist: {
    title: "🎁 Depuis Telegram",
    needsAI: true,
    paragraphs: [
      "<code>/envies [prénom]</code> — les listes de la maison. Le salon étant partagé, la réponse ne dit jamais <b>qui</b> s'occupe d'un cadeau : une envie déjà prise s'affiche « 🔒 pris en charge ».",
      "<code>/envie &lt;texte&gt;</code> — ajouter une envie en français normal. Le bot la crée, puis <b>demande ce qui manque</b> (le prix, un lien, la taille) une question à la fois.",
      "<code>/moi &lt;prénom&gt;</code> — une seule fois, pour que « je veux… » atterrisse sur ta liste sans qu'on te le demande.",
      "En <b>message privé</b>, la commande est facultative : écris ton envie normalement. Dans un groupe, il faut <code>/envie …</code>.",
    ],
    chat: [
      ["vous", "j'aimerais offrir un pull en laine à Léa"],
      ["bot", "🎁 Ajouté à la liste de Léa :\n• Pull en laine\n\nPour « Pull en laine », il me manque le prix,\nun lien ou le magasin et la taille."],
      ["vous", "environ 80 francs chez Zara, taille M"],
      ["bot", "✅ • Pull en laine · 80 CHF · Zara · taille M"],
    ],
  },

  storage: {
    title: "📦 Depuis Telegram",
    needsAI: true,
    paragraphs: [
      "<code>/ou &lt;objet&gt;</code> — dans quel carton il est rangé, et s'il en est sorti. <code>/cartons</code> donne l'inventaire, <code>/sortis</code> ce qui est hors carton.",
      "<code>/sorti &lt;texte&gt;</code> et <code>/range &lt;texte&gt;</code> — noter une sortie ou un retour. <b>La date est estampillée toute seule</b>, plus besoin de l'écrire.",
      "En <b>message privé</b>, dis simplement ce que tu as pris ou remis. Un même message peut concerner plusieurs objets, et si plusieurs correspondent, le bot demande lequel.",
      "« j'ai mis X dans le carton Y » crée l'objet s'il n'existait pas encore.",
    ],
    chat: [
      ["vous", "j'ai sorti le stéthoscope du carton 18, c'est pour Fetsuko"],
      ["bot", "🚪 Sorti : Stetoscope — carton 18 — Lea · pour Fetsuko"],
      ["vous", "j'ai pris les lunettes de kitesurf"],
      ["bot", "Plusieurs objets correspondent à « Lunettes kitesurf » :\n1. Lunettes kitesurf — carton 2 — Seb\n2. Lunettes kitesurf — carton 2 — Lea\nRéponds avec le numéro (ou « annuler »)."],
      ["vous", "2"],
      ["bot", "🚪 Sorti : Lunettes kitesurf — carton 2 — Lea"],
    ],
  },
};

// Ce que le bot sait faire ici et maintenant — chargé une fois, au premier clic.
let botStatus = null;

function helpChat(turns) {
  const body = turns.map(([who, text]) => {
    const label = who === "bot" ? "bot " : "vous";
    // les réponses multi-lignes s'alignent sous le texte, pas sous l'étiquette
    const lines = stEsc(text).replace(/\n/g, "\n" + " ".repeat(7));
    return `<span class="who ${who}">${label}</span> › ${lines}`;
  }).join("\n");
  return `<pre class="help-chat">${body}</pre>`;
}

function renderHomeHelp(help) {
  let warning = "";
  if (!botStatus.telegram_configured) {
    warning = "Le bot Telegram n'est pas configuré (<code>TELEGRAM_BOT_TOKEN</code>) — voir <code>TELEGRAM_BOT.md</code>.";
  } else if (help.needsAI && !botStatus.ai_configured) {
    warning = "Les commandes en langage naturel ont besoin de <code>OPENROUTER_API_KEY</code> dans <code>.env</code>. Les commandes de lecture marchent sans.";
  }
  return `<h4>${help.title}</h4>` +
    help.paragraphs.map((p) => `<p>${p}</p>`).join("") +
    (warning ? `<p class="help-warn">⚠️ ${warning}</p>` : "") +
    helpChat(help.chat);
}

async function toggleHomeHelp(btn) {
  const panel = $("#help-" + btn.dataset.help);
  if (!panel.classList.contains("hidden")) {
    panel.classList.add("hidden");
    btn.setAttribute("aria-expanded", "false");
    return;
  }
  if (!botStatus) {
    // En cas d'échec on suppose que tout est branché : mieux vaut pas d'alerte
    // qu'une fausse alerte.
    botStatus = await api.get("/api/bot/status")
      .catch(() => ({ telegram_configured: true, ai_configured: true }));
  }
  panel.innerHTML = renderHomeHelp(HOME_HELP[btn.dataset.help]);
  panel.classList.remove("hidden");
  btn.setAttribute("aria-expanded", "true");
}

document.querySelectorAll(".info-btn").forEach((btn) =>
  btn.addEventListener("click", () => toggleHomeHelp(btn)));

/* ---------------- Init ---------------- */

(async function init() {
  await Promise.all([loadHome(), loadPlants(), loadDishes(), loadPlans(), loadGrocery(), loadFridge(), loadWishlist(), loadStorage()]);
  checkNotifications();
  setInterval(() => { loadPlants(); loadHome(); checkNotifications(); }, 60_000);
})();
