/* WIT Forms SPA — search-first, schema-driven. No form layout is hardcoded;
 * everything renders from the schema returned by /api/forms/<id>. */
(() => {
  "use strict";

  const state = {
    user: null,
    config: { owner_cc_email: "", csrf_token: "", email_enabled: false },
    schema: null,      // active form schema
    formId: null,
    profiles: { agency: [], client: [] },
  };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) n.setAttribute(k, v);
    }
    for (const kid of kids) if (kid != null) n.append(kid);
    return n;
  };

  // ---- API helpers ----
  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (opts.body) headers["Content-Type"] = "application/json";
    if (["POST", "PUT", "PATCH", "DELETE"].includes((opts.method || "GET").toUpperCase())) {
      headers["X-CSRF-Token"] = state.config.csrf_token;
    }
    const res = await fetch(path, { ...opts, headers });
    return res;
  }
  async function apiJson(path, opts) {
    const res = await api(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { data, status: res.status });
    return data;
  }

  function toast(msg, kind = "") {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast " + kind;
    setTimeout(() => t.classList.add("hidden"), 3200);
  }

  // ---- Auth bootstrap ----
  async function boot() {
    try {
      const me = await fetch("/auth/me");
      if (me.status === 401) return showLogin();
      state.user = await me.json();
    } catch { return showLogin(); }

    try { state.config = await apiJson("/api/config"); } catch {}
    $("#user-email").textContent = state.user.email;
    $("#login-view").classList.add("hidden");
    $("#app-view").classList.remove("hidden");
    wireGlobal();
    loadResults("");
    loadProfiles();
  }

  function showLogin() {
    $("#app-view").classList.add("hidden");
    $("#login-view").classList.remove("hidden");
    const err = new URLSearchParams(location.search).get("error");
    if (err) { const e = $("#login-error"); e.textContent = err; e.classList.remove("hidden"); }
  }

  function wireGlobal() {
    $("#logout-btn").addEventListener("click", async () => {
      await api("/auth/logout", { method: "POST" });
      location.href = "/";
    });
    $("#search-box").addEventListener("input", debounce((e) => loadResults(e.target.value), 180));
    $("#back-btn").addEventListener("click", showSearch);
    $("#preview-btn").addEventListener("click", onPreview);
    $("#download-btn").addEventListener("click", () => onAction("download"));
    $("#print-btn").addEventListener("click", () => onAction("print"));
    $("#email-btn").addEventListener("click", openEmailDialog);
    $("#save-profile-btn").addEventListener("click", openProfileDialog);
    $("#agency-select").addEventListener("change", () => applyProfile("agency"));
    $("#client-select").addEventListener("change", () => applyProfile("client"));
    wireEmailDialog();
    wireProfileDialog();
  }

  function debounce(fn, ms) {
    let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  // ---- Catalog / search (M3) ----
  async function loadResults(q) {
    const { forms } = await apiJson("/api/forms?q=" + encodeURIComponent(q || ""));
    const list = $("#results");
    list.innerHTML = "";
    $("#results-empty").classList.toggle("hidden", forms.length > 0);
    for (const f of forms) {
      list.append(el("li", { class: "result-item", onclick: () => openForm(f.id) },
        el("span", { class: "result-num" }, "ACORD " + f.acord_number),
        el("span", { class: "result-title" }, f.title),
        f.category ? el("span", { class: "badge" }, f.category) : null,
      ));
    }
  }

  function showSearch() {
    $("#form-view").classList.add("hidden");
    $("#search-view").classList.remove("hidden");
  }

  // ---- Open + render form (M3) ----
  async function openForm(formId) {
    state.schema = await apiJson("/api/forms/" + formId);
    state.formId = formId;
    const meta = state.schema._meta;
    $("#form-title").textContent = `ACORD ${meta.acord_number} — ${meta.title}`;
    $("#form-sub").textContent = meta.edition ? `Edition ${meta.edition}` : "";
    $("#preview-pane").classList.add("hidden");
    $("#validation-summary").classList.add("hidden");
    renderForm();
    populateProfileSelects();
    $("#search-view").classList.add("hidden");
    $("#form-view").classList.remove("hidden");
    window.scrollTo(0, 0);
  }

  function renderForm() {
    const form = $("#dynamic-form");
    form.innerHTML = "";
    const schema = state.schema;

    // Insurer A–F reference table (rendered once, drives insurer_ref dropdowns).
    if (schema.insurers && schema.insurers.rows) form.append(renderInsurers(schema.insurers));

    for (const section of schema.sections) form.append(renderSection(section));
    refreshConditionalVisibility();
  }

  function renderInsurers(insurers) {
    const body = el("div", { class: "section-body" });
    const table = el("table", { class: "insurer-table" });
    table.append(el("tr", {},
      el("th", {}, "#"), el("th", {}, "Insurer name"), el("th", {}, "NAIC #")));
    for (const row of insurers.rows) {
      table.append(el("tr", {},
        el("td", { class: "insurer-letter" }, row.letter),
        el("td", {}, el("input", { type: "text", "data-insurer-name": row.letter,
          oninput: refreshInsurerOptions })),
        el("td", {}, el("input", { type: "text", "data-insurer-naic": row.letter })),
      ));
    }
    body.append(table);
    return el("section", { class: "section" },
      el("div", { class: "section-head" }, el("h3", {}, insurers.label || "Insurers Affording Coverage")),
      body);
  }

  function renderSection(section) {
    const head = el("div", { class: "section-head" }, el("h3", {}, section.label));
    const body = el("div", { class: "section-body" });

    // Optional coverage block toggle.
    if (section.optional_block) {
      const tog = section.include_toggle;
      const cb = el("input", { type: "checkbox", id: "tog_" + tog.key, "data-key": tog.key,
        onchange: refreshConditionalVisibility });
      head.append(el("label", { class: "toggle-row" }, cb,
        el("span", {}, tog.label || ("Include " + section.label))));
    }

    const core = [], rare = [];
    for (const f of section.fields) (f.priority === "rare" ? rare : core).push(renderField(f));
    core.forEach((n) => body.append(n));

    const sectionEl = el("section", { class: "section", "data-section": section.id }, head, body);
    if (section.optional_block) sectionEl.dataset.toggle = section.include_toggle.key;

    if (rare.length) {
      const rareWrap = el("div", { class: "section-body rare-fields collapsed" });
      rare.forEach((n) => rareWrap.append(n));
      const btn = el("button", { type: "button", class: "more-toggle" },
        `+ ${rare.length} more field${rare.length > 1 ? "s" : ""}`);
      btn.addEventListener("click", () => {
        rareWrap.classList.toggle("collapsed");
        btn.textContent = rareWrap.classList.contains("collapsed")
          ? `+ ${rare.length} more fields` : "− Hide extra fields";
      });
      sectionEl.append(btn, rareWrap);
    }
    return sectionEl;
  }

  function renderField(f) {
    const id = "fld_" + f.key;
    const wrap = el("div", { class: "field" + (isWide(f) ? " full" : ""), "data-field": f.key });
    if (f.show_if) wrap.dataset.showIf = f.show_if;

    const labelText = f.label + (f.required ? " " : "");
    const label = el("label", { for: id }, labelText);
    if (f.required) label.append(el("span", { class: "req" }, "*"));
    if (f.priority === "rare") label.append(el("span", { class: "pill" }, "  (rare)"));
    wrap.append(label);

    let input;
    switch (f.type) {
      case "textarea":
        input = el("textarea", { id, rows: "3", "data-key": f.key }); break;
      case "state":
        input = stateSelect(id, f.key); break;
      case "select":
        input = el("select", { id, "data-key": f.key });
        input.append(el("option", { value: "" }, "—"));
        (f.options || []).forEach((o) => input.append(el("option", { value: o.value ?? o.label }, o.label)));
        break;
      case "checkbox":
        input = el("input", { type: "checkbox", id, "data-key": f.key }); break;
      case "yn_code":
        input = el("select", { id, "data-key": f.key });
        ["", "Y", "N"].forEach((v) => input.append(el("option", { value: v }, v || "—")));
        break;
      case "insurer_ref":
        input = el("select", { id, "data-key": f.key, "data-insurer-ref": "1" });
        input.append(el("option", { value: "" }, "— select insurer —"));
        break;
      case "radio_group":
        input = renderRadioGroup(f); break;
      default:
        input = el("input", { type: inputType(f.type), id, "data-key": f.key,
          inputmode: f.type === "currency" || f.type === "number" ? "decimal" : null,
          placeholder: placeholderFor(f.type) });
    }
    wrap.append(input);
    wrap.append(el("div", { class: "field-err" }));
    return wrap;
  }

  function renderRadioGroup(f) {
    const row = el("div", { class: "radio-row", "data-key": f.key, "data-radio-group": "1" });
    for (const opt of f.options) {
      const r = el("input", { type: "radio", name: "radio_" + f.key, value: opt.label,
        onchange: refreshConditionalVisibility });
      if (opt.reveals) r.dataset.reveals = opt.reveals;
      row.append(el("label", {}, r, el("span", {}, opt.label)));
    }
    return row;
  }

  const US_STATES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split(" ");
  function stateSelect(id, key) {
    const s = el("select", { id, "data-key": key });
    s.append(el("option", { value: "" }, "—"));
    US_STATES.forEach((st) => s.append(el("option", { value: st }, st)));
    return s;
  }

  const isWide = (f) => f.type === "textarea";
  const inputType = (t) => ({ date: "text", phone: "tel", email: "email", number: "text", currency: "text" }[t] || "text");
  const placeholderFor = (t) => ({ date: "MM/DD/YYYY", currency: "$0", phone: "(555) 555-5555" }[t] || "");

  // ---- Conditional visibility (optional blocks, show_if, reveals) ----
  function refreshConditionalVisibility() {
    const answers = collectAnswers();
    // Optional block sections.
    document.querySelectorAll(".section[data-toggle]").forEach((sec) => {
      const on = !!answers[sec.dataset.toggle];
      sec.querySelectorAll(".section-body, .more-toggle, .rare-fields").forEach((b) => {
        if (!b.classList.contains("section-head")) b.style.opacity = on ? "1" : "0.45";
      });
      sec.querySelectorAll("input, select, textarea").forEach((inp) => {
        if (inp.dataset.key === sec.dataset.toggle) return;
        inp.disabled = !on;
      });
    });
    // show_if fields + radio reveals.
    document.querySelectorAll(".field[data-show-if]").forEach((fld) => {
      const cond = fld.dataset.showIf;
      let visible = !!answers[cond];
      // A radio option may reveal a field via its `reveals` key; if any checked
      // radio reveals this field, show it regardless of the virtual show_if flag.
      document.querySelectorAll("input[type=radio][data-reveals]:checked").forEach((r) => {
        if (r.dataset.reveals === fld.dataset.field) visible = true;
      });
      fld.style.display = visible ? "" : "none";
    });
    refreshInsurerOptions();
  }

  // ---- Insurer dropdowns ----
  function refreshInsurerOptions() {
    const filled = [];
    document.querySelectorAll("input[data-insurer-name]").forEach((inp) => {
      if (inp.value.trim()) filled.push({ letter: inp.dataset.insurerName, name: inp.value.trim() });
    });
    document.querySelectorAll("select[data-insurer-ref]").forEach((sel) => {
      const cur = sel.value;
      sel.innerHTML = "";
      sel.append(el("option", { value: "" }, "— select insurer —"));
      filled.forEach((i) => sel.append(el("option", { value: i.letter }, `${i.letter} — ${i.name}`)));
      sel.value = cur;
    });
  }

  // ---- Collect answers ----
  function collectAnswers() {
    const answers = {};
    document.querySelectorAll("[data-key]").forEach((inp) => {
      const key = inp.dataset.key;
      if (inp.type === "checkbox") answers[key] = inp.checked;
      else if (inp.value !== "") answers[key] = inp.value;
    });
    document.querySelectorAll("[data-radio-group]").forEach((g) => {
      const checked = g.querySelector("input[type=radio]:checked");
      if (checked) answers[g.dataset.key] = checked.value;
    });
    // Insurers table.
    const insurers = {};
    document.querySelectorAll("input[data-insurer-name]").forEach((inp) => {
      const letter = inp.dataset.insurerName;
      if (inp.value.trim()) {
        const naic = document.querySelector(`input[data-insurer-naic="${letter}"]`);
        insurers[letter] = { name: inp.value.trim(), naic: naic ? naic.value.trim() : "" };
      }
    });
    if (Object.keys(insurers).length) answers._insurers = insurers;
    return answers;
  }

  // ---- Profiles (M5) ----
  async function loadProfiles() {
    try {
      const a = await apiJson("/api/profiles?type=agency");
      const c = await apiJson("/api/profiles?type=client");
      state.profiles.agency = a.profiles; state.profiles.client = c.profiles;
    } catch {}
  }
  function populateProfileSelects() {
    for (const t of ["agency", "client"]) {
      const sel = $(`#${t}-select`);
      sel.innerHTML = "";
      sel.append(el("option", { value: "" }, "— none —"));
      state.profiles[t].forEach((p) => sel.append(el("option", { value: p.id }, p.name)));
    }
  }
  function applyProfile(type) {
    const id = $(`#${type}-select`).value;
    if (!id) return;
    const prof = state.profiles[type].find((p) => String(p.id) === String(id));
    if (!prof) return;
    for (const [k, v] of Object.entries(prof.data || {})) {
      const inp = document.querySelector(`[data-key="${cssEscape(k)}"]`);
      if (inp && !inp.disabled) {
        if (inp.type === "checkbox") inp.checked = !!v; else inp.value = v;
      }
    }
    refreshConditionalVisibility();
    toast(`Applied ${prof.name}`, "success");
  }
  const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s);

  // ---- Validation display ----
  function showValidation(fields) {
    document.querySelectorAll(".field.invalid").forEach((f) => f.classList.remove("invalid"));
    document.querySelectorAll(".field-err").forEach((e) => (e.textContent = ""));
    const box = $("#validation-summary");
    if (!fields || !fields.length) { box.classList.add("hidden"); return true; }
    box.innerHTML = "";
    box.append(el("strong", {}, "Please fix the following:"));
    const ul = el("ul");
    fields.forEach((f) => {
      ul.append(el("li", {}, `${f.label}: ${f.error}`));
      const fld = document.querySelector(`.field[data-field="${cssEscape(f.key)}"]`);
      if (fld) { fld.classList.add("invalid"); const e = fld.querySelector(".field-err"); if (e) e.textContent = f.error; }
    });
    box.append(ul);
    box.classList.remove("hidden");
    box.scrollIntoView({ behavior: "smooth", block: "center" });
    return false;
  }

  function payload() {
    return { answers: collectAnswers() };
  }

  // ---- Preview + actions (M4) ----
  async function onPreview() {
    setBusy(true);
    try {
      const res = await api(`/api/forms/${state.formId}/preview`, {
        method: "POST", body: JSON.stringify(payload()),
      });
      if (res.status === 422) { const d = await res.json(); showValidation(d.fields); return; }
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || res.statusText); }
      showValidation([]);
      const blob = await res.blob();
      $("#preview-frame").src = URL.createObjectURL(blob);
      $("#preview-pane").classList.remove("hidden");
      $("#preview-pane").scrollIntoView({ behavior: "smooth" });
    } catch (e) { toast(e.message, "error"); }
    finally { setBusy(false); }
  }

  async function onAction(action) {
    setBusy(true);
    try {
      const res = await api(`/api/forms/${state.formId}/${action}`, {
        method: "POST", body: JSON.stringify(payload()),
      });
      if (res.status === 422) { const d = await res.json(); showValidation(d.fields); return; }
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || res.statusText); }
      showValidation([]);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      if (action === "download") {
        const a = el("a", { href: url, download: `ACORD_${state.schema._meta.acord_number}.pdf` });
        document.body.append(a); a.click(); a.remove();
        toast("Downloaded", "success");
      } else if (action === "print") {
        const w = window.open(url);
        if (w) w.addEventListener("load", () => w.print());
        toast("Opening print dialog…", "success");
      }
    } catch (e) { toast(e.message, "error"); }
    finally { setBusy(false); }
  }

  function setBusy(b) {
    ["#preview-btn", "#download-btn", "#print-btn", "#email-btn"].forEach((s) => ($(s).disabled = b));
  }

  // ---- Email dialog ----
  function openEmailDialog() {
    $("#email-cc").value = state.config.owner_cc_email + " (locked)";
    $("#email-to").value = "";
    $("#email-message").value = "";
    $("#email-error").classList.add("hidden");
    if (!state.config.email_enabled) {
      $("#email-error").textContent = "Email transport is not configured yet (TODO from Logan).";
      $("#email-error").classList.remove("hidden");
    }
    $("#email-dialog").showModal();
  }
  function wireEmailDialog() {
    $("#email-form").addEventListener("submit", async (e) => {
      const btn = e.submitter && e.submitter.value;
      if (btn !== "send") return; // cancel closes
      e.preventDefault();
      const recipients = $("#email-to").value.split(",").map((s) => s.trim()).filter(Boolean);
      if (!recipients.length) { showEmailError("Add at least one recipient."); return; }
      try {
        const res = await api(`/api/forms/${state.formId}/email`, {
          method: "POST",
          body: JSON.stringify({ ...payload(), recipients, message: $("#email-message").value }),
        });
        const d = await res.json().catch(() => ({}));
        if (res.status === 422) { $("#email-dialog").close(); showValidation(d.fields); return; }
        if (!res.ok) { showEmailError(d.error || res.statusText); return; }
        $("#email-dialog").close();
        toast(`Emailed to ${d.to.join(", ")} (cc ${d.cc.join(", ") || "—"})`, "success");
      } catch (err) { showEmailError(err.message); }
    });
  }
  function showEmailError(msg) { const e = $("#email-error"); e.textContent = msg; e.classList.remove("hidden"); }

  // ---- Save-profile dialog ----
  function openProfileDialog() { $("#profile-name").value = ""; $("#profile-dialog").showModal(); }
  function wireProfileDialog() {
    $("#profile-form").addEventListener("submit", async (e) => {
      if (!e.submitter || e.submitter.value !== "save") return;
      e.preventDefault();
      const type = $("#profile-type").value;
      const name = $("#profile-name").value.trim();
      if (!name) return;
      // Collect only keys belonging to sections with prefill_from == type.
      const keys = new Set();
      for (const s of state.schema.sections)
        if (s.prefill_from === type) s.fields.forEach((f) => keys.add(f.key));
      const all = collectAnswers();
      const data = {};
      for (const k of keys) if (k in all) data[k] = all[k];
      try {
        const prof = await apiJson("/api/profiles", { method: "POST", body: JSON.stringify({ type, name, data }) });
        state.profiles[type].push(prof);
        populateProfileSelects();
        $("#profile-dialog").close();
        toast(`Saved profile ${prof.name}`, "success");
      } catch (err) { toast(err.message, "error"); }
    });
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
