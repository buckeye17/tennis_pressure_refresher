/* Ball Bounce Tracker, the laptop app. Plain JS, no build step.
 * The server does the capture and analysis; this page shows its state, sends
 * commands, and converts lengths (the API speaks cm) for display. */
(() => {
  "use strict";

  const POLL_MS = 250;
  const PREFS_KEY = "ball-bounce-tracker-prefs";
  const OLD_PREFS_KEY = "canister-video-prefs";  // before the rename: read if the new key is unset
  const CM_PER_IN = 2.54;
  const CONTEXT_LABELS = {
    new: "New balls", before: "Before refill", after: "After refill",
    after_play: "After play", other: "Other",
  };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else n.setAttribute(k, v === true ? "" : v);
    }
    n.append(...kids.filter((k) => k != null));
    return n;
  };

  // --- preferences (per browser; conveniences only) ------------------------------

  const prefs = (() => {
    try {
      const saved = localStorage.getItem(PREFS_KEY) ?? localStorage.getItem(OLD_PREFS_KEY);
      return JSON.parse(saved) || {};
    } catch { return {}; }
  })();
  const savePrefs = () => {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* private window */ }
  };
  prefs.mid = prefs.mid || {};
  prefs.rotation = prefs.rotation || {}; // per camera
  prefs.area = prefs.area || {}; // tracking area per camera and rotation: "key|rotation"
  const areaKey = (camera, rotation) => `${camera}|${rotation || 0}`;
  const savedArea = (camera, rotation) => prefs.area[areaKey(camera, rotation)] || null;

  const ui = {
    units: prefs.units || null, // null until the Pi says, then "in" or "cm"
    pi: null,
    state: { phase: "idle", session: null },
    previewSession: null,
    cameraKey: null,
    reviewKey: null,
    liveKey: null,
    busy: false,
    frameDuration: 0,
    pick: null,
  };

  // --- lengths --------------------------------------------------------------------

  const units = () => ui.units || "in";
  const toUnit = (cm) => (units() === "in" ? cm / CM_PER_IN : cm);
  const fromUnit = (v) => (units() === "in" ? v * CM_PER_IN : v);
  const fmt = (cm, digits) => {
    if (cm == null) return "–";
    const d = digits ?? (units() === "in" ? 2 : 1);
    return `${toUnit(cm).toFixed(d)} ${units()}`;
  };
  const inputCm = (input) => {
    const v = parseFloat(input.value);
    return Number.isFinite(v) && v > 0 ? fromUnit(v) : null;
  };
  const setInputCm = (input, cm) => {
    input.value = cm == null ? "" : (+toUnit(cm).toFixed(units() === "in" ? 2 : 1)).toString();
  };

  function setUnits(u) {
    const mid = inputCm($("#mid-mark"));
    const drop = inputCm($("#drop-height"));
    ui.units = u;
    prefs.units = u;
    savePrefs();
    setInputCm($("#mid-mark"), mid);
    setInputCm($("#drop-height"), drop);
    document.querySelectorAll("[data-units]").forEach((b) => b.classList.toggle("on", b.dataset.units === u));
    document.querySelectorAll(".unit-label").forEach((s) => (s.textContent = `(${u})`));
    ui.reviewKey = ui.liveKey = null; // re-render tables in the new units
    render();
  }

  // --- talking to the server -------------------------------------------------------

  async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `${resp.status} ${resp.statusText}`);
    return data;
  }

  async function act(fn) {
    if (ui.busy) return;
    ui.busy = true;
    document.body.classList.add("busy");
    try {
      const st = await fn();
      if (st && "phase" in st) apply(st);
    } catch (e) {
      alert(e.message);
    } finally {
      ui.busy = false;
      document.body.classList.remove("busy");
    }
  }

  // --- the Pi and set-up -------------------------------------------------------------

  async function loadPi() {
    const pill = $("#pi-status");
    pill.textContent = "Pi: checking…";
    pill.className = "pill";
    try {
      ui.pi = await api("GET", "/api/pi");
    } catch (e) {
      ui.pi = { ok: false, error: e.message };
    }
    const host = (() => { try { return new URL(document.documentElement.dataset.piUrl).host; } catch { return "Pi"; } })();
    pill.textContent = ui.pi.ok ? `Pi: connected (${host})` : `Pi: not reachable (${host})`;
    pill.className = `pill ${ui.pi.ok ? "ok" : "bad"}`;
    pill.title = ui.pi.error || "";
    $("#pi-retry").hidden = ui.pi.ok;
    if (!ui.units) setUnits(ui.pi.settings?.display_units === "cm" ? "cm" : "in");
    fillSetup();
  }

  function fillSetup() {
    const pi = ui.pi;
    const online = !!pi?.ok;
    $("#pi-fields").hidden = !online;
    $("#manual-fields").hidden = online;
    if (!online) {
      if (prefs.dropCm && !$("#drop-height").value) setInputCm($("#drop-height"), prefs.dropCm);
      setMidDefault();
      return;
    }
    const batches = [...pi.batches].sort((a, b) => b.opened_ts - a.opened_ts);
    const batchSel = $("#batch");
    batchSel.replaceChildren(
      ...batches.map((b) => el("option", { value: b.id },
        `${b.brand} ${b.model} · opened ${new Date(b.opened_ts * 1000).toLocaleDateString()} · ${b.sample_size} balls`)),
    );
    if (!batches.length) batchSel.append(el("option", { value: "" }, "No batches: add one on the dashboard"));
    if (batches.some((b) => b.id === prefs.batchId)) batchSel.value = prefs.batchId;

    const setupSel = $("#setup");
    setupSel.replaceChildren(
      ...pi.setups.map((s) => el("option", { value: s.id }, `${s.name} · ${fmt(s.drop_cm, 1)} drop`)),
    );
    if (!pi.setups.length) setupSel.append(el("option", { value: "" }, "No setups: add one on the dashboard"));
    if (pi.setups.some((s) => s.id === prefs.setupId)) setupSel.value = prefs.setupId;

    const ctx = $("#context");
    ctx.replaceChildren(...(pi.settings.contexts || Object.keys(CONTEXT_LABELS)).map(
      (c) => el("option", { value: c }, CONTEXT_LABELS[c] || c)));
    guessContext();
    setMidDefault();
  }

  const batch = () => ui.pi?.batches?.find((b) => b.id === +$("#batch").value) || null;
  const setup = () => ui.pi?.setups?.find((s) => s.id === +$("#setup").value) || null;

  function guessContext() {
    const b = batch();
    if (b) $("#context").value = !b.tests ? "new" : b.in_canister ? "after" : "before";
  }

  function setMidDefault() {
    const s = setup();
    const key = s ? `setup-${s.id}` : "manual";
    const cm = s?.mid_mark_cm ?? prefs.mid[key] ?? null;
    setInputCm($("#mid-mark"), cm);
    const note = $("#setup-note");
    note.textContent = s?.mid_mark_cm != null ? "Mid mark from the setup." : "";
  }

  function params() {
    const err = (msg) => { $("#setup-error").textContent = msg; $("#setup-error").hidden = false; return null; };
    $("#setup-error").hidden = true;
    const online = !!ui.pi?.ok;
    const s = online ? setup() : null;
    const b = online ? batch() : null;
    if (online && !s) return err("Pick a setup (add one on the dashboard first).");
    if (online && !b) return err("Pick a batch (add one on the dashboard first).");
    const drop = online ? s.drop_cm : inputCm($("#drop-height"));
    if (!drop) return err("Enter the drop height.");
    const mid = inputCm($("#mid-mark"));
    if (mid != null && mid >= drop) return err("The mid mark must be below the drop height.");
    const perBall = parseInt($("#drops-per-ball").value, 10);
    if (!(perBall >= 1 && perBall <= 20)) return err("Drops per ball: 1 to 20.");
    if (mid == null && !confirm("No mid mark: heights will assume a level camera (about 0.5 cm off per degree of tilt). Go on?")) return null;
    prefs.mid[s ? `setup-${s.id}` : "manual"] = mid;
    if (!online) prefs.dropCm = drop;
    if (b) prefs.batchId = b.id;
    if (s) prefs.setupId = s.id;
    savePrefs();
    return {
      drop_height_cm: drop,
      mid_mark_cm: mid,
      drops_per_ball: perBall,
      batch_id: b?.id ?? null,
      batch_label: b ? `${b.brand} ${b.model}` : null,
      sample_size: b?.sample_size ?? null,
      setup_id: s?.id ?? null,
      setup_name: s?.name ?? null,
      context: online ? $("#context").value : null,
    };
  }

  // --- sources ----------------------------------------------------------------------

  async function loadCameras(refresh = false) {
    const box = $("#cameras");
    box.replaceChildren(el("p", { class: "muted small" }, "Looking for cameras…"));
    try {
      const cams = await api("GET", `/api/cameras${refresh ? "?refresh=1" : ""}`);
      box.replaceChildren(...cams.map((c) => el("button", {
        type: "button",
        onclick: () => {
          ui.cameraKey = c.key;
          const rotation = prefs.rotation[c.key] || 0;
          act(() => api("POST", "/api/camera", { key: c.key, rotation, area: savedArea(c.key, rotation) }));
        },
      }, el("span", {}, c.label), el("span", { class: "muted small" }, `${c.width}×${c.height}`))));
      if (!cams.length) box.append(el("p", { class: "muted small" }, "No webcam found."));
    } catch (e) {
      box.replaceChildren(el("p", { class: "error small" }, e.message));
    }
  }

  function uploadClip(file) {
    const p = params();
    if (!p) return;
    const bar = $("#upload-bar");
    const text = $("#upload-text");
    $("#upload").hidden = false;
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload?name=${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      bar.value = e.loaded / e.total;
      text.textContent = `Copying ${file.name}: ${(e.loaded / 1e6).toFixed(0)} of ${(e.total / 1e6).toFixed(0)} MB`;
    };
    xhr.onerror = () => { text.textContent = "The upload failed."; };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status !== 201) {
        text.textContent = data.error || `Upload failed (${xhr.status})`;
        return;
      }
      $("#upload").hidden = true;
      act(() => api("POST", "/api/clip", { file: data.file, params: p }));
    };
    bar.value = 0;
    text.textContent = `Copying ${file.name}…`;
    xhr.send(file);
  }

  // --- rendering --------------------------------------------------------------------

  function apply(st) {
    ui.state = st;
    render();
  }

  function render() {
    const st = ui.state;
    const phase = st.phase;
    const sourcing = phase === "idle";
    const running = ["framing", "live", "analysing"].includes(phase);
    $("#setup-card").hidden = !(sourcing || phase === "framing");
    $("#source-card").hidden = !sourcing;
    $("#live-card").hidden = !running;
    $("#review-card").hidden = phase !== "review";
    $("#error-card").hidden = phase !== "error";
    $("#main").classList.toggle("wide", phase === "review");
    $("#notice").hidden = !st.notice;
    $("#notice").textContent = st.notice || "";

    renderPreview(st, running);
    if (running) renderLive(st);
    if (phase === "review") renderReview(st);
    if (phase === "error") {
      $("#error-text").textContent = st.error;
      $("#error-reanalyse").hidden = !st.file || !st.params || !st.duration_s; // not if unreadable
    }
  }

  function renderPreview(st, running) {
    const img = $("#preview");
    const want = running ? st.session : null;
    if (ui.previewSession !== want) {
      ui.previewSession = want;
      if (want == null) img.removeAttribute("src");
      else img.src = `/preview.mjpg?session=${want}`;
    }
    $("#preview-frame").hidden = want == null;
    $("#placeholder").hidden = want != null;
    previewArea.show(st.area ?? null);
    $("#preview-wrap").hidden = st.phase === "review";
    $("#review-wrap").hidden = st.phase !== "review";
  }

  function renderLive(st) {
    const phase = st.phase;
    $("#framing-actions").hidden = phase !== "framing";
    $("#live-actions").hidden = phase !== "live";
    $("#analysing").hidden = phase !== "analysing";
    $("#provisional").hidden = phase !== "live";
    $("#pick-hint").hidden = phase === "analysing";
    const canDraw = st.kind === "camera" && (phase === "framing" || phase === "live");
    $("#area-controls").hidden = !canDraw;
    if (!canDraw) previewArea.cancel();

    const pr = st.prompt;
    if (phase === "framing") {
      $("#prompt-text").textContent = "Frame the shot";
      $("#prompt-detail").textContent = "The whole drop and the floor in view, the drop line near the middle. Then Start.";
      setWarn(null);
    } else {
      $("#prompt-text").textContent = pr?.text || (phase === "analysing" ? "Analysing the clip" : "");
      $("#prompt-detail").textContent = pr?.detail || "";
      setWarn(pr?.warn);
    }

    const src = st.source || {};
    $("#rotation-field").hidden = phase !== "framing";
    if (phase === "framing" && document.activeElement !== $("#rotation")) $("#rotation").value = src.rotation ?? 0;
    const bits = [];
    if (st.kind === "camera") {
      bits.push(el("span", {}, `${src.width}×${src.height}`));
      if (src.fps != null) bits.push(el("span", { class: src.fps_warning ? "bad" : "" }, `${src.fps.toFixed(0)} fps`));
      if (st.preview_fps != null) bits.push(el("span", {}, `preview ${st.preview_fps.toFixed(0)} fps`));
      bits.push(el("span", { class: st.ball_seen ? "good" : "bad" }, st.ball_seen ? "ball tracked" : "ball not found"));
      if (phase === "live" && src.recording) bits.push(el("span", {}, `● recording ${src.recording}`));
      if (src.skipped) bits.push(el("span", { class: "bad" }, `${src.skipped} frames skipped`));
      if (src.recording_lost) bits.push(el("span", { class: "bad" }, `${src.recording_lost} frames not recorded`));
      if (src.recording_error) bits.push(el("span", { class: "bad" }, src.recording_error));
      if (src.fps_warning && phase !== "analysing") setWarn([pr?.warn, src.fps_warning].filter(Boolean).join(" "));
    } else {
      bits.push(el("span", {}, st.file || ""));
      if (src.width) bits.push(el("span", {}, `${src.width}×${src.height}, ${src.fps?.toFixed(0)} fps`));
    }
    if (st.calibration) bits.push(el("span", { class: "good" }, `calibrated (${st.calibration.method})`));
    $("#live-status").replaceChildren(...bits);

    if (phase === "analysing") {
      const p = st.progress || { done: 0, total: 0 };
      $("#analyse-bar").value = p.total ? p.done / p.total : 0;
      $("#analyse-text").textContent = p.total ? `Frame ${p.done} of ${p.total}` : `Frame ${p.done}`;
    }

    const drops = st.drops || [];
    const key = `${st.session}|${units()}|${drops.map((d) => `${d.seq}:${d.ball_no}:${d.rebound_cm}`).join(",")}`;
    if (key !== ui.liveKey) {
      ui.liveKey = key;
      const tbody = $("#live-drops tbody");
      tbody.replaceChildren(...drops.map((d, i) => el("tr", { class: i && d.ball_no !== drops[i - 1].ball_no ? "newball" : null },
        el("td", {}, `${d.ball_no}`),
        el("td", {}, `${d.drop}`),
        el("td", { class: "num" }, fmt(d.rebound_cm)),
        el("td", {}, d.warnings.length ? el("span", { class: "warn-icon", title: d.warnings.join("\n") }, "⚠") : null),
      )));
      const last = tbody.lastElementChild;
      if (last) last.scrollIntoView({ block: "nearest" });
    }
    $("#live-drops").hidden = !drops.length;
  }

  function setWarn(text) {
    $("#prompt-warn").hidden = !text;
    $("#prompt-warn").textContent = text || "";
  }

  function renderReview(st) {
    const rev = st.review;
    const p = st.params || {};
    const what = [p.batch_label, p.setup_name, CONTEXT_LABELS[p.context] || p.context,
      `${fmt(p.drop_height_cm, 1)} drop`, st.file].filter(Boolean);
    $("#review-what").textContent = what.join(" · ");
    $("#reanalyse-open").hidden = !st.file;

    const s = rev.summary;
    $("#medians tbody").replaceChildren(...s.medians.map((m) => el("tr", {},
      el("td", {}, `${m.ball_no}`), el("td", { class: "num" }, fmt(m.rebound_cm)), el("td", { class: "num" }, `${m.drops}`))));
    $("#review-mean").textContent = s.balls
      ? `Average ${fmt(s.mean_cm)} over ${s.balls} ball${s.balls > 1 ? "s" : ""}${s.sd_cm != null ? ` (± ${fmt(s.sd_cm)})` : ""}`
      : "No drops kept.";
    const fit = s.drops_fit;
    let fitText = "";
    if (fit) {
      const k = fit.outliers;
      fitText = `Drops: ${fmt(fit.mean_cm)} ± ${fmt(fit.sd_cm)} over ${fit.drops - k} kept` + (!fit.tested
        ? ", too few to check for outliers."
        : k ? `, leaving out ${k} outlier${k > 1 ? "s" : ""} (highlighted).` : "; no outliers.");
    }
    $("#review-fit").textContent = fitText;

    const key = `${st.session}|${rev.version}|${units()}`;
    if (key === ui.reviewKey) return;
    ui.reviewKey = key;
    const maxBall = Math.max(p.sample_size || 0, ...rev.drops.map((d) => d.ball_no + 1));
    const ballOptions = (cur) => Array.from({ length: maxBall }, (_, i) =>
      el("option", { value: i + 1, selected: i + 1 === cur }, `${i + 1}`));
    const tbody = $("#review tbody");
    const focused = document.activeElement?.dataset?.focus;
    tbody.replaceChildren(...rev.drops.map((d, i) => {
      const height = el("input", {
        type: "number", step: "0.01", min: "0", class: d.edited_cm != null ? "edited" : null,
        "aria-label": `Height of drop ${d.seq + 1}`, "data-focus": `h${d.seq}`,
        onchange: (e) => {
          const v = e.target.value.trim();
          const cm = v === "" ? null : fromUnit(parseFloat(v));
          if (v !== "" && !(cm > 0)) { alert("Type a height, or clear the box to use the measurement."); return; }
          edit(d.seq, { edited_cm: cm });
        },
      });
      setInputCm(height, d.edited_cm ?? d.height_cm);
      const notes = d.warnings.map((w) => el("li", {}, w));
      if (d.outlier_z != null && fit) {
        const off = d.height_cm - fit.mean_cm;
        notes.unshift(el("li", { class: "outlier" },
          `Outlier: ${fmt(Math.abs(off))} ${off < 0 ? "below" : "above"} the other drops' average ` +
          `(${Math.abs(d.outlier_z).toFixed(1)} sd). Check the picture: correct the height or untick Keep.`));
      }
      const rowClass = [d.kept ? null : "rejected", d.outlier_z != null ? "outlier" : null,
        i && d.ball_no !== rev.drops[i - 1].ball_no ? "newball" : null].filter(Boolean).join(" ") || null;
      return el("tr", { class: rowClass },
        el("td", {}, d.image
          ? el("img", { src: `/api/drops/${d.seq}.jpg?s=${st.session}`, alt: `Drop ${d.seq + 1}`, loading: "lazy", onclick: (e) => showPicture(e.target.src) })
          : el("span", { class: "muted small" }, "no picture")),
        el("td", {}, el("select", { "aria-label": "Ball", "data-focus": `b${d.seq}`, onchange: (e) => edit(d.seq, { ball_no: +e.target.value }) }, ...ballOptions(d.ball_no))),
        el("td", { class: "num" }, fmt(d.measured_cm)),
        el("td", { class: "height" }, height, d.edited_cm != null
          ? el("button", { class: "link small", type: "button", title: "Use the measurement", onclick: () => edit(d.seq, { edited_cm: null }) }, " ↺")
          : null, d.edited_cm != null ? el("span", { class: "tag" }, "edited") : null),
        el("td", { class: "keep" }, el("input", { type: "checkbox", checked: d.kept, "aria-label": "Keep this drop", "data-focus": `k${d.seq}`, onchange: (e) => edit(d.seq, { kept: e.target.checked }) })),
        el("td", {}, notes.length ? el("ul", {}, ...notes) : null),
      );
    }));
    if (focused) tbody.querySelector(`[data-focus="${focused}"]`)?.focus();
    if (!rev.drops.length) tbody.append(el("tr", {}, el("td", { colspan: 6, class: "muted" }, "No drops were found. Try Analyse again and click the ball.")));
  }

  const edit = (seq, changes) => act(() => api("POST", `/api/review/${seq}`, changes));

  function showPicture(src) {
    $("#picture-img").src = src;
    $("#picture").showModal();
  }

  // --- analyse again ---------------------------------------------------------------------

  function openReanalyse() {
    const st = ui.state;
    ui.pick = null;
    $("#frame-mark").hidden = true;
    $("#reanalyse-pick").disabled = true;
    dialogArea.reset(st.area ?? null);
    const range = $("#frame-t");
    range.max = Math.max(st.duration_s || 0, 0.1);
    range.value = Math.min(1, range.max);
    loadFrame();
    $("#reanalyse").showModal();
  }

  function loadFrame() {
    const t = +$("#frame-t").value;
    $("#frame-time").textContent = `${t.toFixed(1)} s`;
    $("#frame").src = `/api/frame.jpg?t=${t}&s=${ui.state.session}`;
    ui.pick = null;
    $("#frame-mark").hidden = true;
    $("#reanalyse-pick").disabled = true;
  }

  function reanalyse(withPick) {
    if (ui.state.review?.drops.some((d) => d.edited_cm != null || !d.kept) && !confirm("Your review edits will be lost. Analyse again?")) return;
    $("#reanalyse").close();
    act(() => api("POST", "/api/reanalyse", { pick: withPick ? ui.pick : null, area: dialogArea.area() }));
  }

  // --- tracking area ---------------------------------------------------------------------
  // A polygon around where the ball moves; the server ignores yellow blobs outside it.
  // Corners are fractions of the picture, like clicks.

  const SVG_NS = "http://www.w3.org/2000/svg";
  const CLOSE_PX = 12; // a click this near the first corner closes the polygon

  function areaTool({ frame, img, controls, commit }) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "area-svg");
    svg.setAttribute("viewBox", "0 0 1 1");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("aria-hidden", "true");
    frame.append(svg);
    let area = null; // the polygon in use
    let points = null; // corners so far while drawing, else null

    const pathOf = (pts, close) =>
      pts.map(([x, y], i) => `${i ? "L" : "M"}${x} ${y}`).join(" ") + (close ? " Z" : "");

    function draw() {
      const shapes = [];
      const shape = (d, cls) => {
        const p = document.createElementNS(SVG_NS, "path");
        p.setAttribute("d", d);
        p.setAttribute("class", cls);
        p.setAttribute("vector-effect", "non-scaling-stroke");
        shapes.push(p);
      };
      if (points) {
        if (points.length > 1) shape(pathOf(points, false), "area-line");
      } else if (area) {
        shape(`M0 0H1V1H0Z ${pathOf(area, true)}`, "area-shade");
        shape(pathOf(area, true), "area-line");
      }
      svg.replaceChildren(...shapes);
      frame.querySelectorAll(".area-dot").forEach((d) => d.remove());
      (points || []).forEach(([x, y], i) => frame.append(el("span", {
        class: i ? "area-dot" : "area-dot first", style: `left: ${x * 100}%; top: ${y * 100}%`,
      })));
      renderControls();
    }

    function renderControls() {
      const button = (text, onclick, attrs = {}) => el("button", { type: "button", onclick, ...attrs }, text);
      if (points) {
        controls.replaceChildren(
          el("p", { class: "muted small" }, "Click the corners of the area where the ball moves: " +
            "from the floor to above the drop mark, with room for your hand. Click the first corner, " +
            "or Done, to finish."),
          el("div", { class: "actions" },
            button("Done", finish, { class: "primary", disabled: points.length < 3 }),
            button("Undo corner", () => { points.pop(); draw(); }, { disabled: !points.length }),
            button("Cancel", cancel)),
        );
        return;
      }
      controls.replaceChildren(
        el("div", { class: "actions" },
          button(area ? "Redraw tracking area" : "Draw tracking area", start),
          area ? button("Clear", () => { area = null; draw(); commit(null); }) : null),
        el("p", { class: "muted small" }, area
          ? "The ball is looked for only inside the area."
          : "If something else yellow gets picked up, draw an area around where the ball moves."),
      );
    }

    function start() { points = []; draw(); }
    function cancel() { if (points) { points = null; draw(); } }
    function finish() {
      if (!points || points.length < 3) return;
      area = points;
      points = null;
      draw();
      commit(area);
    }

    // Returns true if the click was used for drawing (so it isn't also a colour pick).
    function click(e) {
      if (!points) return false;
      const f = clickFraction(e, img);
      const r = img.getBoundingClientRect();
      const first = points[0];
      if (points.length >= 3 && Math.hypot((f.x - first[0]) * r.width, (f.y - first[1]) * r.height) <= CLOSE_PX) {
        finish();
      } else {
        points.push([+f.x.toFixed(4), +f.y.toFixed(4)]);
        draw();
      }
      return true;
    }

    // The server's area; ignored while drawing, so a poll doesn't wipe the corners.
    function show(next) {
      if (points || JSON.stringify(next) === JSON.stringify(area)) return;
      area = next;
      draw();
    }

    function reset(next) { points = null; area = next; draw(); }

    document.addEventListener("keydown", (e) => {
      if (points && e.key === "Escape" && frame.offsetParent) { e.preventDefault(); cancel(); }
    });
    draw();
    return { click, show, reset, cancel, area: () => area };
  }

  const previewArea = areaTool({
    frame: $("#preview-frame"),
    img: $("#preview"),
    controls: $("#area-controls"),
    commit: (area) => act(async () => {
      const st = await api("POST", "/api/area", { area });
      if (ui.cameraKey != null) {
        const key = areaKey(ui.cameraKey, st.source?.rotation);
        if (area) prefs.area[key] = area; else delete prefs.area[key];
        savePrefs();
      }
      return st;
    }),
  });

  const dialogArea = areaTool({
    frame: $("#frame").parentElement,
    img: $("#frame"),
    controls: $("#reanalyse-area"),
    commit: () => {}, // sent with Analyse again
  });

  // --- wiring -------------------------------------------------------------------------

  function clickFraction(e, img) {
    const r = img.getBoundingClientRect();
    return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
  }

  function wire() {
    document.querySelectorAll("[data-units]").forEach((b) => b.addEventListener("click", () => setUnits(b.dataset.units)));
    $("#pi-retry").addEventListener("click", loadPi);
    $("#batch").addEventListener("change", guessContext);
    $("#setup").addEventListener("change", setMidDefault);
    $("#cameras-refresh").addEventListener("click", () => loadCameras(true));

    const zone = $("#dropzone");
    $("#clip-file").addEventListener("change", (e) => { if (e.target.files[0]) uploadClip(e.target.files[0]); e.target.value = ""; });
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("over"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("over");
      const f = e.dataTransfer.files[0];
      if (f) uploadClip(f);
    });

    $("#start").addEventListener("click", () => {
      const p = params();
      if (p) act(() => api("POST", "/api/start", p));
    });
    $("#rotation").addEventListener("change", (e) => {
      const rotation = +e.target.value;
      if (ui.cameraKey != null) { prefs.rotation[ui.cameraKey] = rotation; savePrefs(); }
      previewArea.cancel();
      const area = ui.cameraKey != null ? savedArea(ui.cameraKey, rotation) : null;
      act(() => api("POST", "/api/rotate", { rotation, area }));
    });
    $("#close-camera").addEventListener("click", () => act(() => api("POST", "/api/close")));
    document.querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => {
      const cmd = b.dataset.cmd;
      if (cmd === "finish" && !confirm("Finish the session and review the drops?")) return;
      act(() => api("POST", `/api/command/${cmd}`));
    }));
    $("#cancel").addEventListener("click", () => act(() => api("POST", "/api/close")));
    $("#preview").addEventListener("click", (e) => {
      if (previewArea.click(e)) return;
      if (!["framing", "live", "analysing"].includes(ui.state.phase)) return;
      act(() => api("POST", "/api/pick", clickFraction(e, e.target)));
    });

    $("#new-session").addEventListener("click", () => {
      if (!confirm("Start a new session? This review hasn't been saved (the video stays in recordings/).")) return;
      act(() => api("POST", "/api/close"));
    });
    $("#error-back").addEventListener("click", () => act(() => api("POST", "/api/close")));
    $("#reanalyse-open").addEventListener("click", openReanalyse);
    $("#error-reanalyse").addEventListener("click", openReanalyse);
    $("#frame-t").addEventListener("change", loadFrame);
    $("#frame-t").addEventListener("input", () => { $("#frame-time").textContent = `${(+$("#frame-t").value).toFixed(1)} s`; });
    $("#frame").addEventListener("click", (e) => {
      if (dialogArea.click(e)) return;
      const f = clickFraction(e, e.target);
      ui.pick = { t: +$("#frame-t").value, ...f };
      const mark = $("#frame-mark");
      mark.style.left = `${f.x * 100}%`;
      mark.style.top = `${f.y * 100}%`;
      mark.hidden = false;
      $("#reanalyse-pick").disabled = false;
    });
    $("#reanalyse-pick").addEventListener("click", () => reanalyse(true));
    $("#reanalyse-default").addEventListener("click", () => reanalyse(false));

    // Live shortcuts, so a hand that's holding a ball can reach one key.
    document.addEventListener("keydown", (e) => {
      if (ui.state.phase !== "live" || e.ctrlKey || e.metaKey || e.altKey) return;
      if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
      const cmd = { u: "undo", n: "next_ball", r: "recalibrate" }[e.key.toLowerCase()];
      if (cmd) act(() => api("POST", `/api/command/${cmd}`));
      if (e.key.toLowerCase() === "f") $("[data-cmd=finish]").click();
    });
  }

  async function poll() {
    try {
      if (!ui.busy) apply(await api("GET", "/api/state"));
    } catch { /* the server may be restarting */ }
    setTimeout(poll, POLL_MS);
  }

  wire();
  if (ui.units) setUnits(ui.units);
  loadPi();
  loadCameras();
  poll();
})();
