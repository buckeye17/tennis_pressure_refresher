/* Canister Monitor dashboard. Plain JS, no build step. The API speaks kPa, degC and
 * UTC epoch seconds; everything here converts at the edge for display. */
(() => {
  "use strict";

  const PRESSURE = {
    psi: { perKpa: 1 / 6.894757, digits: 1 },
    kPa: { perKpa: 1, digits: 0 },
    bar: { perKpa: 1 / 100, digits: 2 },
  };
  const PALETTE = ["#2563eb", "#059669", "#db2777", "#7c3aed", "#0891b2", "#65a30d", "#dc2626", "#9333ea"];
  const KIND_LABELS = {
    note: "Note", fill: "Filled", vent: "Vented", balls_added: "Balls added", balls_removed: "Balls removed",
  };
  const POLL_CANISTERS_MS = 30_000;
  const POLL_READINGS_MS = 60_000;
  const PREFS_KEY = "canister-monitor-prefs";

  // --- state and preferences ----------------------------------------------------

  const state = {
    units: document.documentElement.dataset.defaultUnits || "psi",
    tempUnit: (navigator.language || "").toLowerCase() === "en-us" ? "F" : "C",
    compensated: false,
    range: 86400,
    canisters: [],
    settings: null,
    series: [],
    events: [],
    skew: 0, // server clock minus browser clock, seconds
    colors: new Map(),
  };

  function loadPrefs() {
    try {
      const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
      if (saved.units in PRESSURE) state.units = saved.units;
      if (saved.tempUnit === "C" || saved.tempUnit === "F") state.tempUnit = saved.tempUnit;
      if (typeof saved.compensated === "boolean") state.compensated = saved.compensated;
      if (Number.isFinite(saved.range) || saved.range === "fill") state.range = saved.range;
    } catch (_) { /* storage unavailable: keep defaults */ }
  }

  function savePrefs() {
    try {
      const { units, tempUnit, compensated, range } = state;
      localStorage.setItem(PREFS_KEY, JSON.stringify({ units, tempUnit, compensated, range }));
    } catch (_) { /* ignore */ }
  }

  // --- formatting ---------------------------------------------------------------

  const $ = (sel) => document.querySelector(sel);
  const serverNow = () => Date.now() / 1000 + state.skew;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "style") node.style.cssText = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c != null && c !== false) node.append(c instanceof Node ? c : String(c));
    }
    return node;
  }

  const toPressure = (kpa) => (kpa == null ? null : kpa * PRESSURE[state.units].perKpa);
  const fmtPressure = (v) => (v == null ? "—" : v.toFixed(PRESSURE[state.units].digits));
  const toTemp = (c) => (c == null ? null : state.tempUnit === "F" ? c * 9 / 5 + 32 : c);
  const fmtTemp = (c) => (c == null ? "—" : `${toTemp(c).toFixed(state.tempUnit === "F" ? 0 : 1)} °${state.tempUnit}`);

  function fmtAgo(seconds) {
    if (seconds == null) return "never";
    const s = Math.max(0, seconds);
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400 * 2) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }

  const fmtDateTime = (ts) =>
    new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

  function colorFor(mac) {
    if (!state.colors.has(mac)) state.colors.set(mac, PALETTE[state.colors.size % PALETTE.length]);
    return state.colors.get(mac);
  }

  const nameFor = (mac) => {
    const c = state.canisters.find((x) => x.mac === mac);
    return (c && c.canister) || mac;
  };

  // --- API ---------------------------------------------------------------------------

  async function api(path, options) {
    const resp = await fetch(path, options);
    if (!resp.ok) {
      let message = `${resp.status} ${resp.statusText}`;
      try { message = (await resp.json()).error || message; } catch (_) { /* not JSON */ }
      throw new Error(message);
    }
    return resp.status === 204 ? null : resp.json();
  }

  /** Start of the selected range in epoch seconds, or null for "All". */
  function rangeStart() {
    if (state.range === "fill") return lastFill();
    return state.range > 0 ? serverNow() - state.range : null;
  }

  function rangeQuery() {
    const params = new URLSearchParams();
    const from = rangeStart();
    if (from != null) params.set("from", String(Math.floor(from)));
    return params;
  }

  function setStatus(ok, text) {
    const status = $("#status");
    status.classList.toggle("ok", ok);
    status.classList.toggle("error", !ok);
    $("#status-text").textContent = text;
  }

  // --- trends ------------------------------------------------------------------------

  function fmtDays(seconds) {
    const d = seconds / 86400;
    return d < 1 ? `${Math.max(1, Math.round(seconds / 3600))} h` : `${d.toFixed(1)} days`;
  }

  /** One line describing the trend, plus a CSS modifier. */
  function trendLine(trend) {
    if (!trend) return null;
    const perDay = toPressure(trend.slope_kpa_per_day);
    const digits = Math.max(1, PRESSURE[state.units].digits + 1);
    const rate = perDay == null ? "" : `${Math.abs(perDay).toFixed(digits)} ${state.units}/day`;
    const since = serverNow() - (trend.levelled_since ?? serverNow());
    const after = trend.session_source === "fill" ? "after fill" : "since first reading";
    const age = trend.session_start == null ? "" : fmtDays(serverNow() - trend.session_start);
    switch (trend.status) {
      case "insufficient_data":
        return { text: `Trend: needs ~${Math.round(trend.window_s / 7200)} h of data`, mod: "" };
      case "falling":
        return { text: `▼ ${rate} · still dropping`, mod: "" };
      case "rising":
        return { text: `▲ ${rate} · rising`, mod: "" };
      case "levelling_off":
        return { text: `Levelling off · flat ${fmtDays(since)}`, mod: "flat" };
      case "levelled_off": // levelled_since is only looked back ~plateau_hours, so no duration
        return { text: "✓ Levelled off", mod: "flat" };
      case "leak_suspected":
        return { text: `▼ ${rate} · still dropping ${age} ${after} — check for a leak`, mod: "leak" };
      default:
        return null;
    }
  }

  /** Most recent fill across canisters (for the "Since fill" range), or null. */
  function lastFill() {
    const fills = state.canisters
      .map((c) => c.trend)
      .filter((t) => t && t.session_source === "fill")
      .map((t) => t.session_start);
    return fills.length ? Math.max(...fills) : null;
  }

  // --- cards ---------------------------------------------------------------------------

  function renderCards() {
    const root = $("#cards");
    root.replaceChildren();
    if (state.canisters.length === 0) {
      root.append(el("p", { class: "no-sensors" },
        "No sensors yet. Start the collector, or list sensors in config.toml."));
      return;
    }
    const unit = state.units;
    for (const c of state.canisters) {
      const r = c.reading;
      const comp = r && r.compensated_gauge_kpa;
      const shown = r && (state.compensated && comp != null ? comp : r.gauge_kpa);
      const other = r && (state.compensated ? r.gauge_kpa : comp);
      const age = r ? serverNow() - r.ts : null;
      const battery = r && (r.battery_pct != null ? `${Math.round(r.battery_pct)} %`
        : r.battery_v != null ? `${r.battery_v.toFixed(2)} V` : "—");
      const trend = trendLine(c.trend);
      const filled = c.trend && c.trend.session_source === "fill"
        ? `Filled ${fmtDays(serverNow() - c.trend.session_start)} ago` : null;

      root.append(el("article", { class: `card${c.stale ? " stale" : ""}`, style: `--c:${colorFor(c.mac)}` },
        el("h3", {}, c.canister || "Unassigned sensor"),
        el("div", { class: "big" }, r ? fmtPressure(toPressure(shown)) : "—", el("small", {}, unit)),
        r && other != null
          ? el("div", { class: "sub" }, `${state.compensated ? "Raw" : "Compensated"} ${fmtPressure(toPressure(other))} ${unit}`)
          : el("div", { class: "sub" }, r && state.compensated ? "No temperature: showing raw" : " "),
        el("dl", { class: "meta" },
          el("div", {}, el("dt", {}, "Temp"), el("dd", {}, r ? fmtTemp(r.temp_c) : "—")),
          el("div", {}, el("dt", {}, "Battery"), el("dd", {}, battery || "—")),
          el("div", {}, el("dt", {}, "Signal"), el("dd", {}, c.rssi != null ? `${c.rssi} dBm` : "—"))),
        trend ? el("div", { class: `trend ${trend.mod}` }, trend.text) : null,
        filled ? el("div", { class: "session" }, filled) : null,
        el("div", { class: "age" },
          r ? `${c.stale ? "Stale · " : ""}updated ${fmtAgo(age)}` : c.last_seen ? "Heard, no reading yet" : "Never heard"),
        c.assigned ? null : el("p", { class: "hint" },
          "MAC ", el("code", {}, c.mac), " — add it to config.toml under [[sensors]] to name it."),
      ));
    }
  }

  function renderEventCanisterOptions() {
    const select = $("#event-mac");
    const current = select.value;
    select.replaceChildren(el("option", { value: "" }, "All canisters"),
      ...state.canisters.map((c) => el("option", { value: c.mac }, c.canister || c.mac)));
    select.value = state.canisters.some((c) => c.mac === current) ? current : "";
  }

  // --- charts --------------------------------------------------------------------------

  const charts = { pressure: null, temp: null, key: "" };

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /** Merge per-sensor columns onto one shared, sorted x axis. Missing points are
   * `undefined` (uPlot spans them); real nulls (e.g. no temperature) stay gaps. */
  function align(field, convert) {
    const xsSet = new Set();
    for (const s of state.series) for (const t of s.ts) xsSet.add(t);
    const xs = Array.from(xsSet).sort((a, b) => a - b);
    const index = new Map(xs.map((t, i) => [t, i]));
    const ys = state.series.map((s) => {
      const out = new Array(xs.length).fill(undefined);
      s.ts.forEach((t, i) => { out[index.get(t)] = convert(s[field][i]); });
      return out;
    });
    return [xs, ...ys];
  }

  function eventsPlugin(tipHost) {
    const tip = el("div", { class: "tip", hidden: true });
    tipHost.append(tip);
    const nearest = (u, left) => {
      let best = null;
      for (const ev of state.events) {
        const x = u.valToPos(ev.ts, "x");
        const d = Math.abs(x - left);
        if (d <= 6 && (!best || d < best.d)) best = { ev, d, x };
      }
      return best;
    };
    return {
      hooks: {
        draw: [(u) => {
          const { ctx, bbox } = u;
          ctx.save();
          ctx.lineWidth = Math.max(1, devicePixelRatio);
          ctx.setLineDash([4 * devicePixelRatio, 4 * devicePixelRatio]);
          for (const ev of state.events) {
            const x = Math.round(u.valToPos(ev.ts, "x", true));
            if (x < bbox.left || x > bbox.left + bbox.width) continue;
            ctx.strokeStyle = ev.mac ? colorFor(ev.mac) : cssVar("--muted");
            ctx.beginPath();
            ctx.moveTo(x, bbox.top);
            ctx.lineTo(x, bbox.top + bbox.height);
            ctx.stroke();
          }
          ctx.restore();
        }],
        setCursor: [(u) => {
          const hit = u.cursor.left >= 0 && nearest(u, u.cursor.left);
          if (!hit) { tip.hidden = true; return; }
          const { ev } = hit;
          tip.textContent = `${fmtDateTime(ev.ts)} · ${KIND_LABELS[ev.kind] || ev.kind}`
            + `${ev.mac ? ` · ${nameFor(ev.mac)}` : ""}${ev.note ? ` — ${ev.note}` : ""}`;
          tip.hidden = false;
          // Center on the marker, but keep the whole tip inside the chart.
          const maxLeft = tipHost.clientWidth - tip.offsetWidth;
          const left = u.over.offsetLeft + hit.x - tip.offsetWidth / 2;
          tip.style.left = `${Math.max(0, Math.min(maxLeft, left))}px`;
          tip.style.top = `${u.over.offsetTop + 4}px`;
        }],
      },
    };
  }

  function chartOptions(host, height, valueLabel, fmt) {
    const axisColor = cssVar("--muted");
    const grid = { stroke: cssVar("--grid"), width: 1 };
    return {
      width: host.clientWidth,
      height,
      cursor: { sync: { key: "canisters" }, points: { size: 6 } },
      scales: { x: { time: true } },
      legend: { live: true },
      axes: [
        { stroke: axisColor, grid, ticks: grid },
        { stroke: axisColor, grid, ticks: grid, size: 54, values: (u, vals) => vals.map(fmt) },
      ],
      series: [
        {},
        ...state.series.map((s) => ({
          label: s.canister || s.mac,
          stroke: colorFor(s.mac),
          width: 2,
          paths: uPlot.paths.stepped({ align: 1 }),
          points: { show: false },
          value: (u, v) => (v == null ? "—" : `${fmt(v)} ${valueLabel}`),
        })),
      ],
      plugins: [eventsPlugin(host)],
    };
  }

  function xRange() {
    const now = serverNow();
    const from = rangeStart();
    if (from != null) return [from, now];
    const xs = state.series.flatMap((s) => s.ts);
    return xs.length ? [Math.min(...xs), now] : [now - 86400, now];
  }

  function renderCharts() {
    const field = state.compensated ? "compensated_gauge_kpa" : "gauge_kpa";
    const pData = align(field, toPressure);
    const tData = align("temp_c", toTemp);
    const hasData = pData[0].length > 0;
    for (const id of ["#chart-pressure", "#chart-temp"]) $(`${id} .empty`).hidden = hasData;
    $("#pressure-title").textContent =
      `${state.compensated ? "Temperature-compensated gauge pressure" : "Gauge pressure"} (${state.units})`;

    const key = [state.units, state.tempUnit, state.compensated, ...state.series.map((s) => s.mac)].join("|");
    if (charts.pressure && charts.key === key) {
      charts.pressure.setData(pData, false);
      charts.temp.setData(tData, false);
    } else {
      charts.pressure?.destroy();
      charts.temp?.destroy();
      const pHost = $("#chart-pressure");
      const tHost = $("#chart-temp");
      const pDigits = PRESSURE[state.units].digits;
      charts.pressure = new uPlot(
        chartOptions(pHost, pHost.clientWidth < 500 ? 240 : 300, state.units, (v) => v.toFixed(pDigits)),
        pData, pHost);
      charts.temp = new uPlot(
        chartOptions(tHost, 170, `°${state.tempUnit}`, (v) => v.toFixed(state.tempUnit === "F" ? 0 : 1)),
        tData, tHost);
      charts.key = key;
    }
    const [min, max] = xRange();
    for (const c of [charts.pressure, charts.temp]) c.setScale("x", { min, max });
  }

  function resizeCharts() {
    for (const [c, host] of [[charts.pressure, "#chart-pressure"], [charts.temp, "#chart-temp"]]) {
      if (c) c.setSize({ width: $(host).clientWidth, height: c.height });
    }
  }

  // --- events (notes) ------------------------------------------------------------------

  function renderEvents() {
    const list = $("#events");
    list.replaceChildren();
    const events = [...state.events].sort((a, b) => b.ts - a.ts);
    if (events.length === 0) {
      list.append(el("li", { class: "none" }, "No notes in this time range."));
      return;
    }
    for (const ev of events) {
      list.append(el("li", {},
        el("span", { class: "when" }, fmtDateTime(ev.ts)),
        el("span", { class: "what" },
          el("span", { class: "kind" }, KIND_LABELS[ev.kind] || ev.kind),
          ev.note || "",
          " ",
          el("span", { class: "who" }, ev.mac ? `· ${nameFor(ev.mac)}` : "· all canisters")),
        el("button", {
          class: "del", type: "button", title: "Delete note", "aria-label": "Delete note",
          onclick: () => deleteEvent(ev),
        }, "✕")));
    }
  }

  async function deleteEvent(ev) {
    if (!confirm(`Delete this note?\n\n${fmtDateTime(ev.ts)} ${KIND_LABELS[ev.kind] || ev.kind} ${ev.note || ""}`)) return;
    try {
      await api(`/api/events/${ev.id}`, { method: "DELETE" });
      await refreshReadings();
    } catch (e) {
      $("#event-error").textContent = `Could not delete: ${e.message}`;
    }
  }

  async function addEvent(e) {
    e.preventDefault();
    const err = $("#event-error");
    err.textContent = "";
    const body = { kind: $("#event-kind").value };
    const mac = $("#event-mac").value;
    const note = $("#event-note").value.trim();
    const when = $("#event-when").value;
    if (mac) body.mac = mac;
    if (note) body.note = note;
    if (when) body.ts = new Date(when).getTime() / 1000;
    try {
      await api("/api/events", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      $("#event-note").value = "";
      $("#event-when").value = "";
      await refreshReadings();
    } catch (ex) {
      err.textContent = `Could not add note: ${ex.message}`;
    }
  }

  // --- refresh loops ------------------------------------------------------------------

  async function refreshCanisters() {
    try {
      const sent = Date.now() / 1000;
      const body = await api("/api/canisters");
      state.skew = body.now - (sent + Date.now() / 1000) / 2;
      state.settings = body.settings;
      state.canisters = body.canisters;
      for (const c of state.canisters) colorFor(c.mac); // stable colors in card order
      $("#range-fill").disabled = lastFill() == null;
      renderCards();
      renderEventCanisterOptions();
      setStatus(true, `Updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}`);
    } catch (e) {
      setStatus(false, `Offline — retrying (${e.message})`);
    }
  }

  async function refreshReadings() {
    const params = rangeQuery();
    $("#export").href = `/api/export.csv?${params}`;
    const width = $("#chart-pressure").clientWidth || 800;
    params.set("max_points", String(Math.max(200, Math.min(4000, Math.round(width * 1.5)))));
    try {
      const [readings, events] = await Promise.all([
        api(`/api/readings?${params}`),
        api(`/api/events?${rangeQuery()}`),
      ]);
      state.series = readings.series;
      state.events = events.events;
      renderCharts();
      renderEvents();
    } catch (e) {
      setStatus(false, `Offline — retrying (${e.message})`);
    }
  }

  // --- controls -----------------------------------------------------------------------

  function bindSegment(id, get, set) {
    const root = $(id);
    const sync = () => {
      for (const b of root.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.value === String(get())));
    };
    root.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      set(b.dataset.value);
      sync();
      savePrefs();
    });
    sync();
  }

  function init() {
    loadPrefs();
    bindSegment("#units", () => state.units, (v) => { state.units = v; renderCards(); renderCharts(); });
    bindSegment("#temp-units", () => state.tempUnit, (v) => { state.tempUnit = v; renderCards(); renderCharts(); });
    bindSegment("#ranges", () => state.range, (v) => {
      state.range = v === "fill" ? "fill" : Number(v);
      refreshReadings();
    });
    const comp = $("#compensated");
    comp.checked = state.compensated;
    comp.addEventListener("change", () => {
      state.compensated = comp.checked;
      savePrefs();
      renderCards();
      renderCharts();
    });
    $("#event-form").addEventListener("submit", addEvent);

    new ResizeObserver(resizeCharts).observe($("main"));
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      charts.key = ""; // rebuild charts with the new theme colors
      renderCharts();
    });

    refreshCanisters().then(refreshReadings);
    setInterval(refreshCanisters, POLL_CANISTERS_MS);
    setInterval(refreshReadings, POLL_READINGS_MS);
    setInterval(renderCards, 15_000); // keep "updated X ago" current
  }

  init();
})();
