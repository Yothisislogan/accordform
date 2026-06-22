/* WIT Forms SPA — search-first, schema-driven. */
(() => {
  "use strict";

  const state = {
    user: null,
    config: { owner_cc_email: "", csrf_token: "", email_enabled: false },
    schema: null,
    formId: null,
    profiles: { agency: [], client: [] },
    dirty: false,            // unsaved input in the current form
    category: "",            // active category-chip filter
    carryOver: null,         // shared header carried into a companion form
    navObserver: null,       // IntersectionObserver for active section
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

  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (opts.body) headers["Content-Type"] = "application/json";
    if (["POST", "PUT", "PATCH", "DELETE"].includes((opts.method || "GET").toUpperCase())) {
      headers["X-CSRF-Token"] = state.config.csrf_token;
    }
    return fetch(path, { ...opts, headers });
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
    t.classList.remove("hidden");
    setTimeout(() => t.classList.add("hidden"), 3600);
  }

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
    $("#back-btn").addEventListener("click", () => { if (confirmDiscard()) showSearch(); });
    // Mark the form dirty on any input, and keep the required-remaining counter live.
    $("#dynamic-form").addEventListener("input", () => { state.dirty = true; updateReqCounter(); });
    $("#dynamic-form").addEventListener("change", () => { state.dirty = true; updateReqCounter(); });
    // Guard against losing typed data on reload/close.
    window.addEventListener("beforeunload", (e) => {
      if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
    });
    $("#preview-btn").addEventListener("click", onPreview);
    $("#download-btn").addEventListener("click", () => onAction("download"));
    $("#print-btn").addEventListener("click", () => onAction("print"));
    $("#email-btn").addEventListener("click", onUseOwnEmail);          // local download
    wireProfileDialog();  // saved-info ("Use saved" / "+ Save") lives inline per block
  }

  function debounce(fn, ms) {
    let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  async function loadResults(q) {
    const list = $("#results"), empty = $("#results-empty");
    empty.classList.add("hidden");
    list.innerHTML = "";
    list.append(el("li", { class: "state-msg" }, "Loading forms…"));
    let forms;
    try {
      ({ forms } = await apiJson("/api/forms?q=" + encodeURIComponent(q || "")));
    } catch (e) {
      list.innerHTML = "";
      empty.textContent = "Couldn’t load forms: " + e.message;
      empty.classList.remove("hidden");
      return;
    }
    buildCategoryChips(forms);                          // chips reflect the q result set
    if (state.category) forms = forms.filter((f) => f.category === state.category);
    list.innerHTML = "";
    empty.textContent = "No forms match your search.";
    empty.classList.toggle("hidden", forms.length > 0);
    for (const f of forms) {
      list.append(el("li", { class: "result-item", role: "button", tabindex: "0",
        onclick: () => openForm(f.id),
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openForm(f.id); } } },
        el("span", { class: "result-num" }, "ACORD " + f.acord_number),
        el("span", { class: "result-title" }, f.title),
        f.category ? el("span", { class: "badge" }, f.category) : null,
      ));
    }
  }

  // Category chips under the search box; clicking filters the catalog.
  function buildCategoryChips(forms) {
    const wrap = $("#category-chips");
    wrap.innerHTML = "";
    const cats = [...new Set(forms.map((f) => f.category).filter(Boolean))].sort();
    if (cats.length <= 1) { wrap.classList.add("hidden"); return; }
    wrap.classList.remove("hidden");
    const chip = (label, value) => {
      const c = el("button", { type: "button", class: "chip" + (state.category === value ? " active" : "") }, label);
      c.addEventListener("click", () => {
        state.category = state.category === value ? "" : value;
        loadResults($("#search-box").value);
      });
      return c;
    };
    wrap.append(chip("All", ""));
    cats.forEach((cat) => wrap.append(chip(cat, cat)));
  }

  function showSearch() {
    $("#form-view").classList.add("hidden");
    $("#search-view").classList.remove("hidden");
    state.dirty = false;
  }

  function confirmDiscard() {
    if (!state.dirty) return true;
    return window.confirm("Discard the info you've entered on this form?");
  }

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
    buildSectionNav();
    // Apply any shared header carried from a 125 hub launch, then clear it.
    if (state.carryOver) { applyCarryOver(state.carryOver); state.carryOver = null; }
    state.dirty = false;
    updateReqCounter();
    $("#search-view").classList.add("hidden");
    $("#form-view").classList.remove("hidden");
    window.scrollTo(0, 0);
  }

  function renderForm() {
    const form = $("#dynamic-form");
    form.innerHTML = "";
    const schema = state.schema;
    if (schema.insurers && schema.insurers.rows) form.append(renderInsurers(schema.insurers));
    for (const section of schema.sections) form.append(renderSection(section));
    // ACORD 125 "Sections Attached" hub: a top-level block (a list, in the
    // verified 125 schema) of checkbox+premium rows. Rendered + filled like
    // everything else; emits flat pdf_field values.
    if (schema.sections_attached) form.append(renderSectionsAttached(schema.sections_attached));
    refreshConditionalVisibility();
  }

  // Tolerant of the block being a bare list (verified 125) or an object wrapper.
  function attachedRows(block) {
    return Array.isArray(block) ? block : (block.rows || block.items || block.sections || []);
  }

  function renderSectionsAttached(block) {
    const body = el("div", { class: "section-body" });
    const table = el("table", { class: "insurer-table" });
    table.append(el("tr", {}, el("th", {}, "Attach"), el("th", {}, "Section"), el("th", {}, "")));
    for (const row of attachedRows(block)) {
      const cb = el("input", { type: "checkbox", "data-attached-ind": row.indicator_pdf_field || "" });
      // Companion-form launch: enabled once the section is checked. Opens the
      // companion form carrying the shared header (agency + insured + carriers).
      let launchCell = el("span", { class: "muted" }, "—");
      if (row.attaches_form) {
        const launch = el("button", { type: "button", class: "btn btn-secondary btn-sm attach-launch", disabled: "" },
          `Start ACORD ${row.attaches_form} →`);
        launch.addEventListener("click", () => startCompanion(row.attaches_form));
        cb.addEventListener("change", () => { launch.disabled = !cb.checked; });
        launchCell = launch;
      }
      const premium = row.premium_pdf_field
        ? el("input", { type: "text", inputmode: "decimal", placeholder: "Premium $0",
            "data-attached-prem": row.premium_pdf_field })
        : null;
      table.append(el("tr", {},
        el("td", {}, cb),
        el("td", {}, (row.label || row.indicator_pdf_field || ""), premium ? el("div", {}, premium) : null),
        el("td", { class: "attach-launch" }, launchCell),
      ));
    }
    body.append(table);
    return el("section", { class: "section", id: "sec-sections_attached", "data-section": "sections_attached" },
      el("div", { class: "section-head" },
        el("h3", {}, (Array.isArray(block) ? null : block.label) || "Sections Attached")),
      body);
  }

  // Carry the shared header (agency + client blocks + insurers) into a companion
  // form, then open it. Best-effort: applies values to any matching field key.
  async function startCompanion(acordNumber) {
    const answers = collectAnswers();
    const carry = {};
    for (const section of state.schema.sections) {
      if (section.prefill_from) for (const f of section.fields)
        if (f.key in answers) carry[f.key] = answers[f.key];
    }
    if (answers._insurers) carry._insurers = answers._insurers;
    state.carryOver = carry;
    try {
      const id = await formIdForNumber(acordNumber);
      if (!id) { toast(`ACORD ${acordNumber} isn't in the catalog yet`, "error"); state.carryOver = null; return; }
      state.dirty = false;            // we already captured what we need
      await openForm(id);
      toast(`Opened ACORD ${acordNumber} with the shared header pre-filled`, "success");
    } catch (e) { state.carryOver = null; toast(e.message, "error"); }
  }

  async function formIdForNumber(n) {
    const { forms } = await apiJson("/api/forms?q=" + encodeURIComponent(n));
    const hit = forms.find((f) => String(f.acord_number) === String(n));
    return hit ? hit.id : null;
  }

  function applyCarryOver(carry) {
    for (const [k, v] of Object.entries(carry)) {
      if (k === "_insurers") {
        for (const [letter, info] of Object.entries(v)) {
          const nm = document.querySelector(`input[data-insurer-name="${letter}"]`);
          const naic = document.querySelector(`input[data-insurer-naic="${letter}"]`);
          if (nm && info.name) nm.value = info.name;
          if (naic && info.naic) naic.value = info.naic;
        }
        continue;
      }
      const inp = document.querySelector(`[data-key="${cssEscape(k)}"]`);
      if (inp && !inp.disabled) { if (inp.type === "checkbox") inp.checked = !!v; else inp.value = v; }
    }
    refreshConditionalVisibility();
  }

  // Sticky section nav (left rail desktop / chip-scroller mobile) built from the
  // rendered sections; the active section highlights as you scroll.
  function buildSectionNav() {
    const nav = $("#section-nav");
    nav.innerHTML = "";
    const sections = [...document.querySelectorAll("#dynamic-form .section")];
    if (sections.length <= 1) { nav.classList.add("hidden"); return; }
    nav.classList.remove("hidden");
    const ul = el("ul");
    for (const sec of sections) {
      const h3 = sec.querySelector(".section-head h3");
      const a = el("a", { href: "#" + sec.id }, h3 ? h3.textContent : sec.dataset.section);
      a.addEventListener("click", (e) => { e.preventDefault(); sec.scrollIntoView({ behavior: "smooth", block: "start" }); });
      ul.append(el("li", {}, a));
    }
    nav.append(ul);
    if (state.navObserver) state.navObserver.disconnect();
    state.navObserver = new IntersectionObserver((entries) => {
      for (const en of entries) {
        if (!en.isIntersecting) continue;
        nav.querySelectorAll("a").forEach((a) =>
          a.classList.toggle("active", a.getAttribute("href") === "#" + en.target.id));
      }
    }, { rootMargin: "-72px 0px -60% 0px", threshold: 0 });
    sections.forEach((sec) => state.navObserver.observe(sec));
  }

  // Live "N required fields remaining" counter (visible required fields only).
  function updateReqCounter() {
    const counter = $("#req-counter");
    if (!state.schema) { counter.textContent = ""; return; }
    const answers = collectAnswers();
    let remaining = 0;
    for (const section of state.schema.sections) {
      for (const f of section.fields) {
        if (!f.required || !fieldVisible(f.key)) continue;
        const v = answers[f.key];
        if (v === undefined || v === null || String(v).trim() === "") remaining++;
      }
    }
    if (remaining === 0) { counter.textContent = "All required fields complete"; counter.className = "req-counter done"; }
    else { counter.textContent = `${remaining} required field${remaining === 1 ? "" : "s"} remaining`; counter.className = "req-counter todo"; }
  }

  function renderInsurers(insurers) {
    const body = el("div", { class: "section-body" });
    const table = el("table", { class: "insurer-table" });
    table.append(el("tr", {}, el("th", {}, "#"), el("th", {}, "Insurer name"), el("th", {}, "NAIC #")));
    for (const row of insurers.rows) {
      table.append(el("tr", {},
        el("td", { class: "insurer-letter" }, row.letter),
        el("td", {}, el("input", { type: "text", "data-insurer-name": row.letter, oninput: refreshInsurerOptions })),
        el("td", {}, el("input", { type: "text", "data-insurer-naic": row.letter })),
      ));
    }
    body.append(table);
    return el("section", { class: "section", id: "sec-insurers", "data-section": "insurers" },
      el("div", { class: "section-head" }, el("h3", {}, insurers.label || "Insurers Affording Coverage")), body);
  }

  function renderSection(section) {
    const head = el("div", { class: "section-head" }, el("h3", {}, section.label));
    const body = el("div", { class: "section-body" });

    // Inline "saved info" for reusable blocks (agency/client): a Use-saved
    // dropdown + a one-click "+ Save", right where the data lives. No jargon —
    // the control sits on the block it fills. Shared across the team.
    if (section.prefill_from) {
      const type = section.prefill_from;
      const sel = el("select", { class: "prefill-select", "data-prefill-type": type,
        title: "Fill this block from saved info" });
      sel.append(el("option", { value: "" }, "Use saved…"));
      sel.addEventListener("change", () => applyProfileToSection(type, sel.value, section));
      const saveBtn = el("button", { type: "button", class: "btn btn-ghost btn-sm" }, "+ Save");
      saveBtn.addEventListener("click", () => openSaveProfile(type, section, sel));
      head.append(el("span", { class: "prefill-controls" }, sel, saveBtn));
    }

    if (section.optional_block) {
      const tog = section.include_toggle;
      const cb = el("input", { type: "checkbox", id: "tog_" + tog.key, "data-key": tog.key, onchange: refreshConditionalVisibility });
      head.append(el("label", { class: "toggle-row" }, cb, el("span", {}, tog.label || ("Include " + section.label))));
    }

    const core = [], rare = [];
    for (const f of section.fields) (f.priority === "rare" ? rare : core).push(renderField(f));
    core.forEach((n) => body.append(n));

    const sectionEl = el("section", { class: "section", id: "sec-" + section.id, "data-section": section.id }, head, body);
    if (section.optional_block) sectionEl.dataset.toggle = section.include_toggle.key;

    if (rare.length) {
      const rareWrap = el("div", { class: "section-body rare-fields collapsed" });
      rare.forEach((n) => rareWrap.append(n));
      const btn = el("button", { type: "button", class: "more-toggle" }, `+ ${rare.length} more field${rare.length > 1 ? "s" : ""}`);
      btn.addEventListener("click", () => {
        rareWrap.classList.toggle("collapsed");
        btn.textContent = rareWrap.classList.contains("collapsed") ? `+ ${rare.length} more fields` : "− Hide extra fields";
      });
      sectionEl.append(btn, rareWrap);
    }
    return sectionEl;
  }

  function renderField(f) {
    const id = "fld_" + f.key;
    const wrap = el("div", { class: "field" + (isWide(f) ? " full" : ""), "data-field": f.key });
    if (f.show_if) wrap.dataset.showIf = f.show_if;

    const label = el("label", { for: id }, f.label + (f.required ? " " : ""));
    if (f.required) label.append(el("span", { class: "req", title: "required" }, "*"));
    if (f.priority === "rare") label.append(el("span", { class: "pill" }, "  (rare)"));
    if (f.attaches_form) label.append(el("span", { class: "pill" }, `  opens ${f.attaches_form}`));
    // Per-field help from the schema (FieldNameAlt tooltip) — insurance fields are cryptic.
    const help = f.help || f.tooltip || f.alt;
    if (help) label.append(el("span", { class: "hint", title: help, tabindex: "0",
      role: "img", "aria-label": "Help: " + help }, "?"));
    wrap.append(label);

    let input;
    switch (f.type) {
      case "textarea": input = el("textarea", { id, rows: "3", "data-key": f.key }); break;
      case "state": input = stateSelect(id, f.key); break;
      case "select":
        input = el("select", { id, "data-key": f.key });
        input.append(el("option", { value: "" }, "—"));
        (f.options || []).forEach((o) => input.append(el("option", { value: o.value ?? o.label }, o.label)));
        break;
      case "checkbox": input = el("input", { type: "checkbox", id, "data-key": f.key, onchange: refreshConditionalVisibility }); break;
      case "yn_code":
        input = el("select", { id, "data-key": f.key });
        ["", "Y", "N"].forEach((v) => input.append(el("option", { value: v }, v || "—")));
        break;
      case "insurer_ref":
        input = el("select", { id, "data-key": f.key, "data-insurer-ref": "1" });
        input.append(el("option", { value: "" }, "— select insurer —"));
        break;
      case "radio_group": input = renderRadioGroup(f); break;
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
      const r = el("input", { type: "radio", name: "radio_" + f.key, value: opt.label, onchange: refreshConditionalVisibility });
      if (opt.reveals) r.dataset.reveals = opt.reveals;
      row.append(el("label", {}, r, el("span", {}, opt.label)));
    }
    return row;
  }

  const US_STATES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split(" ");
  function stateSelect(id, key) {
    const s = el("select", { id, "data-key": key });
    s.append(el("option", { value: "" }, "—"));
    US_STATES.forEach((st) => s.append(el("option", { value: st }, st)));
    return s;
  }

  const isWide = (f) => f.type === "textarea";
  const inputType = (t) => ({ date: "text", phone: "tel", email: "email", number: "text", currency: "text" }[t] || "text");
  const placeholderFor = (t) => ({ date: "MM/DD/YYYY", currency: "$0", phone: "(555) 555-5555" }[t] || "");

  function refreshConditionalVisibility() {
    const answers = collectAnswers();
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
    document.querySelectorAll(".field[data-show-if]").forEach((fld) => {
      const cond = fld.dataset.showIf;
      let visible = !!answers[cond];
      document.querySelectorAll("input[type=radio][data-reveals]:checked").forEach((r) => {
        if (r.dataset.reveals === fld.dataset.field) visible = true;
      });
      fld.style.display = visible ? "" : "none";
    });
    refreshInsurerOptions();
  }

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

  async function loadProfiles() {
    try {
      const a = await apiJson("/api/profiles?type=agency");
      const c = await apiJson("/api/profiles?type=client");
      state.profiles.agency = a.profiles; state.profiles.client = c.profiles;
    } catch {}
  }

  // Fill every inline "Use saved…" dropdown from the loaded profiles of its type.
  function populateProfileSelects() {
    document.querySelectorAll("select[data-prefill-type]").forEach((sel) => {
      const t = sel.dataset.prefillType;
      const cur = sel.value;
      sel.innerHTML = "";
      sel.append(el("option", { value: "" }, "Use saved…"));
      (state.profiles[t] || []).forEach((p) => sel.append(el("option", { value: p.id }, p.name)));
      sel.value = cur;
    });
  }

  function applyProfileToSection(type, id, section) {
    if (!id) return;
    const prof = (state.profiles[type] || []).find((p) => String(p.id) === String(id));
    if (!prof) return;
    for (const [k, v] of Object.entries(prof.data || {})) {
      const inp = document.querySelector(`[data-key="${cssEscape(k)}"]`);
      if (inp && !inp.disabled) {
        if (inp.type === "checkbox") inp.checked = !!v; else inp.value = v;
      }
    }
    refreshConditionalVisibility();
    toast(`Filled ${section.label} from “${prof.name}”`, "success");
  }
  const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s);

  function showValidation(fields) {
    document.querySelectorAll(".field.invalid").forEach((f) => f.classList.remove("invalid"));
    document.querySelectorAll(".field-err").forEach((e) => (e.textContent = ""));
    const box = $("#validation-summary");
    if (!fields || !fields.length) { box.classList.add("hidden"); return true; }
    box.innerHTML = "";
    box.append(el("strong", {}, "Please fix the following:"));
    const ul = el("ul");
    let firstField = null;
    fields.forEach((f) => {
      const fld = document.querySelector(`.field[data-field="${cssEscape(f.key)}"]`);
      // Each summary item links to its field (click jumps + focuses it).
      const link = el("a", { href: "#" }, `${f.label}: ${f.error}`);
      link.addEventListener("click", (e) => { e.preventDefault(); focusField(f.key); });
      ul.append(el("li", {}, link));
      if (fld) {
        fld.classList.add("invalid");
        const e = fld.querySelector(".field-err"); if (e) e.textContent = f.error;
        if (!firstField) firstField = f.key;
      }
    });
    box.append(ul);
    box.classList.remove("hidden");
    // Land the user on the first bad field, not just the summary.
    if (firstField) focusField(firstField);
    else box.scrollIntoView({ behavior: "smooth", block: "center" });
    return false;
  }

  function focusField(key) {
    const fld = document.querySelector(`.field[data-field="${cssEscape(key)}"]`);
    if (!fld) return;
    fld.scrollIntoView({ behavior: "smooth", block: "center" });
    const input = fld.querySelector("input, select, textarea");
    if (input) setTimeout(() => input.focus({ preventScroll: true }), 250);
  }

  // Integration contract (TEST-WIRE-UP §0): the front end resolves ALL schema
  // logic and sends the backend a flat { relative_pdf_field: value } map
  // (authoritative for filling). We also send the keyed `answers` so the server
  // keeps doing validation, field-usage analytics, and the audit snapshot.
  function payload() { return { answers: collectAnswers(), fields: collectFlatMap() }; }

  const _truthy = (v) =>
    v === true || ["1", "true", "yes", "y", "on", "checked"].includes(String(v).toLowerCase());

  function fieldVisible(key) {
    const dom = document.querySelector(`.field[data-field="${cssEscape(key)}"]`);
    if (!dom) return true;
    if (dom.style.display === "none") return false;
    const inp = dom.querySelector("[data-key], [data-radio-group] input");
    return !(inp && inp.disabled);
  }

  function collectFlatMap() {
    const schema = state.schema;
    const answers = collectAnswers();
    const flat = {};

    if (schema.insurers && schema.insurers.rows) {
      const ins = answers._insurers || {};
      for (const row of schema.insurers.rows) {
        const info = ins[row.letter];
        if (info && info.name) {
          flat[row.name_pdf_field] = info.name;
          if (row.naic_pdf_field && info.naic) flat[row.naic_pdf_field] = info.naic;
        }
      }
    }

    for (const section of schema.sections) {
      if (section.optional_block) {
        const tog = section.include_toggle;
        if (!_truthy(answers[tog.key])) continue;            // excluded block: emit nothing
        if (tog.pdf_field && tog.type === "checkbox") flat[tog.pdf_field] = String(tog.on_value || "1");
      }
      for (const f of section.fields) {
        if (!fieldVisible(f.key)) continue;                  // hidden by show_if/reveal/off block
        emitField(f, answers[f.key], flat);
      }
    }

    if (schema.sections_attached) {
      for (const row of attachedRows(schema.sections_attached)) {
        const cb = document.querySelector(`[data-attached-ind="${cssEscape(row.indicator_pdf_field || "")}"]`);
        if (cb && cb.checked && row.indicator_pdf_field) {
          flat[row.indicator_pdf_field] = "1";
          const prem = document.querySelector(`[data-attached-prem="${cssEscape(row.premium_pdf_field || "")}"]`);
          if (prem && prem.value.trim() && row.premium_pdf_field) flat[row.premium_pdf_field] = prem.value.trim();
        }
      }
    }
    return flat;
  }

  // Mirror of backend pdf_fill.build_field_values, resolved client-side.
  function emitField(f, v, flat) {
    switch (f.type) {
      case "checkbox":
        if (_truthy(v)) flat[f.pdf_field] = String(f.on_value || "1");
        break;
      case "radio_group":
        if (v !== undefined && v !== null && v !== "") {
          for (const opt of f.options) {
            const on = String(opt.label) === String(v) || String(opt.value) === String(v);
            flat[opt.pdf_field] = on ? String(opt.on_value || "1") : "Off";
          }
        }
        break;
      case "yn_code":
        if (v) flat[f.pdf_field] = String(v).toUpperCase().startsWith("Y") ? "Y" : "N";
        break;
      case "insurer_ref":
        if (v) flat[f.pdf_field] = String(v).trim().toUpperCase();
        break;
      default:
        if (v !== undefined && v !== null && String(v).trim() !== "")
          flat[f.pdf_field] = String(v).trim();
    }
  }

  function setPreviewStatus(kind, msg) {
    const pane = $("#preview-pane"), status = $("#preview-status"), frame = $("#preview-frame");
    pane.classList.remove("hidden");
    if (kind === "ready") { status.classList.add("hidden"); frame.classList.remove("hidden"); return; }
    frame.classList.add("hidden");
    status.className = "preview-status" + (kind === "error" ? " err" : "");
    status.innerHTML = "";
    if (kind === "loading") status.append(el("span", { class: "spinner" }), el("span", {}, msg || "Generating PDF…"));
    else status.append(el("span", {}, msg || "Couldn’t generate the PDF."));
    status.classList.remove("hidden");
  }

  async function onPreview() {
    setBusy(true);
    setPreviewStatus("loading");
    $("#preview-pane").scrollIntoView({ behavior: "smooth" });
    try {
      const res = await api(`/api/forms/${state.formId}/preview`, { method: "POST", body: JSON.stringify(payload()) });
      if (res.status === 422) {
        const d = await res.json(); $("#preview-pane").classList.add("hidden"); showValidation(d.fields); return;
      }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setPreviewStatus("error", d.error || `Preview failed (${res.status})`);
        return;
      }
      showValidation([]);
      const blob = await res.blob();
      $("#preview-frame").src = URL.createObjectURL(blob);
      setPreviewStatus("ready");
    } catch (e) { setPreviewStatus("error", e.message); }
    finally { setBusy(false); }
  }

  async function onAction(action) {
    setBusy(true);
    try {
      const res = await api(`/api/forms/${state.formId}/${action}`, { method: "POST", body: JSON.stringify(payload()) });
      if (res.status === 422) { const d = await res.json(); showValidation(d.fields); return; }
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || res.statusText); }
      showValidation([]);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      if (action === "download") {
        triggerDownload(blob, fileName());
        toast(`Downloaded ${fileName()}`, "success");
      } else if (action === "print") {
        const w = window.open(url);
        if (w) w.addEventListener("load", () => w.print());
        toast("Opening print dialog…", "success");
      }
    } catch (e) { toast(e.message, "error"); }
    finally { setBusy(false); }
  }

  async function onUseOwnEmail() {
    setBusy(true);
    try {
      const res = await api(`/api/forms/${state.formId}/download`, { method: "POST", body: JSON.stringify(payload()) });
      if (res.status === 422) { const d = await res.json(); showValidation(d.fields); return; }
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || res.statusText); }
      showValidation([]);
      const blob = await res.blob();
      const name = fileName();
      triggerDownload(blob, name);
      toast(`Downloaded ${name}. Open your email and attach it.`, "success");
    } catch (e) { toast(e.message, "error"); }
    finally { setBusy(false); }
  }

  function triggerDownload(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  function fileName() { return `ACORD_${state.schema._meta.acord_number}.pdf`; }

  function setBusy(b) {
    ["#preview-btn", "#download-btn", "#print-btn", "#email-btn"]
      .forEach((s) => ($(s).disabled = b));
  }

  let _pendingSave = null;  // {type, section, sel} set when "+ Save" is clicked

  function openSaveProfile(type, section, sel) {
    _pendingSave = { type, section, sel };
    const noun = type === "agency" ? "agency" : "client";
    $("#profile-dialog-title").textContent = `Save this ${noun}`;
    $("#profile-dialog-hint").textContent =
      `Saves the ${section.label} block so anyone on the team can reuse it. Same name updates it.`;
    $("#profile-name").value = "";
    $("#profile-dialog").showModal();
    $("#profile-name").focus();
  }

  function wireProfileDialog() {
    $("#profile-form").addEventListener("submit", async (e) => {
      if (!e.submitter || e.submitter.value !== "save") return;
      e.preventDefault();
      if (!_pendingSave) return;
      const { type, section, sel } = _pendingSave;
      const name = $("#profile-name").value.trim();
      if (!name) return;

      // Capture just this block's fields.
      const all = collectAnswers();
      const data = {};
      for (const f of section.fields) if (f.key in all) data[f.key] = all[f.key];

      // Re-using a name updates that saved entry (simple rename/overwrite).
      const existing = (state.profiles[type] || []).find(
        (p) => p.name.toLowerCase() === name.toLowerCase());
      const bodyObj = { type, name, data };
      if (existing) bodyObj.id = existing.id;

      try {
        const prof = await apiJson("/api/profiles", { method: "POST", body: JSON.stringify(bodyObj) });
        const arr = state.profiles[type] || (state.profiles[type] = []);
        const idx = arr.findIndex((p) => String(p.id) === String(prof.id));
        if (idx >= 0) arr[idx] = prof; else arr.push(prof);
        populateProfileSelects();
        if (sel) sel.value = prof.id;        // reflect what was just saved
        $("#profile-dialog").close();
        toast(`Saved “${prof.name}” — reuse it from Use saved…`, "success");
      } catch (err) { toast(err.message, "error"); }
      finally { _pendingSave = null; }
    });
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
