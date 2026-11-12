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
    note: "Note", fill: "Filled", leak_test: "Leak test (filled, no balls)", vent: "Vented",
    balls_added: "Balls added", balls_removed: "Balls removed",
  };
  const POLL_CANISTERS_MS = 30_000;
  const POLL_READINGS_MS = 60_000;
  const TABLE_PAGE = 100;
  const TABLE_MAX = 1000; // server's per-request limit
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
    table: { mac: "", rows: [], next: null, seq: 0 },
    lengthUnit: document.documentElement.dataset.lengthUnits === "cm" ? "cm" : "in",
    bounce: {
      settings: null, setups: [], batches: [], references: [], conversions: [],
      expanded: new Set(), showRetired: false,
      pictures: new Set(), // video tests whose drop pictures are shown
    },
  };

  function loadPrefs() {
    try {
      const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
      if (saved.units in PRESSURE) state.units = saved.units;
      if (saved.tempUnit === "C" || saved.tempUnit === "F") state.tempUnit = saved.tempUnit;
      if (typeof saved.compensated === "boolean") state.compensated = saved.compensated;
      if (Number.isFinite(saved.range) || saved.range === "fill") state.range = saved.range;
      if (saved.lengthUnit === "in" || saved.lengthUnit === "cm") state.lengthUnit = saved.lengthUnit;
    } catch (_) { /* storage unavailable: keep defaults */ }
  }

  function savePrefs() {
    try {
      const { units, tempUnit, compensated, range, lengthUnit } = state;
      localStorage.setItem(PREFS_KEY, JSON.stringify({ units, tempUnit, compensated, range, lengthUnit }));
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
  const fmtDateTimeSec = (ts) =>
    new Date(ts * 1000).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit",
    });

  const fmtBattery = (r) =>
    r.battery_pct != null ? `${Math.round(r.battery_pct)} %` : r.battery_v != null ? `${r.battery_v.toFixed(2)} V` : "—";

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
      const battery = r && fmtBattery(r);
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

  function renderCanisterOptions(select) {
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

  // --- table -------------------------------------------------------------------------

  /** Fetch the table's first page, or the next page when `more`. A refresh re-fetches
   * as many rows as are showing, so polling doesn't undo "Load more". */
  async function loadTable(more = false) {
    const t = state.table;
    const seq = ++t.seq;
    const params = rangeQuery();
    if (t.mac) params.set("mac", t.mac);
    if (more) params.set("before", t.next);
    params.set("limit", String(more ? TABLE_PAGE : Math.min(TABLE_MAX, Math.max(TABLE_PAGE, t.rows.length))));
    const body = await api(`/api/readings/table?${params}`);
    if (seq !== t.seq) return; // a newer load replaced this one
    t.rows = more ? t.rows.concat(body.rows) : body.rows;
    t.next = body.next;
    renderTable();
  }

  function renderTable() {
    const { rows, next } = state.table;
    const unit = state.units;
    $("#th-pressure").textContent = `${state.compensated ? "Compensated" : "Gauge"} (${unit})`;
    $("#th-other").textContent = `${state.compensated ? "Raw" : "Compensated"} (${unit})`;
    $("#th-temp").textContent = `Temp (°${state.tempUnit})`;
    const fmtT = (c) => (c == null ? "—" : toTemp(c).toFixed(state.tempUnit === "F" ? 0 : 1));

    const body = $("#table-body");
    body.replaceChildren();
    if (rows.length === 0) {
      body.append(el("tr", {}, el("td", { class: "none", colspan: 6 }, "No readings in this range.")));
    }
    for (const r of rows) {
      const comp = r.compensated_gauge_kpa;
      const shown = state.compensated && comp != null ? comp : r.gauge_kpa;
      const other = state.compensated ? r.gauge_kpa : comp;
      body.append(el("tr", {},
        el("td", { class: "when" }, fmtDateTimeSec(r.ts)),
        el("td", {}, el("span", { class: "swatch", style: `--c:${colorFor(r.mac)}` }), r.canister || r.mac),
        el("td", { class: "num" }, fmtPressure(toPressure(shown))),
        el("td", { class: "num muted" }, fmtPressure(toPressure(other))),
        el("td", { class: "num" }, fmtT(r.temp_c)),
        el("td", { class: "num" }, fmtBattery(r))));
    }
    $("#table-count").textContent = rows.length
      ? `${rows.length} reading${rows.length === 1 ? "" : "s"}, newest first${next ? "" : " (all in range)"}` : "";
    $("#table-more").hidden = !next;
  }

  async function loadMore() {
    const button = $("#table-more");
    button.disabled = true;
    try {
      await loadTable(true);
    } catch (e) {
      setStatus(false, `Could not load more readings (${e.message})`);
    } finally {
      button.disabled = false;
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
          el("span", { class: "who" }, ev.mac ? `· ${nameFor(ev.mac)}` : "· all canisters",
            ev.batch_id ? ` · ${batchLabel(ev.batch_id)}` : "")),
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
    const batch = $("#event-batch");
    if (!batch.hidden && batch.value) body.batch_id = Number(batch.value);
    try {
      await api("/api/events", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      $("#event-note").value = "";
      $("#event-when").value = "";
      await refreshReadings();
      if (body.batch_id) await refreshBounce(); // where the batch is changed
    } catch (ex) {
      err.textContent = `Could not add note: ${ex.message}`;
    }
  }

  // --- balls and bounce tests (PLAN.md 6.2) -------------------------------------------
  // The API speaks cm and degC; inputs and labels use the chosen length and temp units.

  const LENGTH = { in: 1 / 2.54, cm: 1 };
  const CONTEXT_LABELS = {
    new: "New, straight from the can", before: "Before a canister session",
    after: "After a canister session", after_play: "After play", other: "Other",
  };
  const CONTEXT_SHORT = { new: "New", before: "Before", after: "After", after_play: "After play", other: "Other" };
  const DROPS_PER_BALL = 3;
  const SAMPLE_ALL_UP_TO = 12; // matches the server's default
  const DEFAULT_SAMPLE = 10;

  const toLen = (cm) => (cm == null ? null : cm * LENGTH[state.lengthUnit]);
  const fromLen = (v) => v / LENGTH[state.lengthUnit];
  const fmtLen = (cm) => (cm == null ? "—" : `${toLen(cm).toFixed(1)} ${state.lengthUnit}`);
  const fromTempInput = (v) => (state.tempUnit === "F" ? (v - 32) * 5 / 9 : v);
  /** A typed number (a decimal comma is fine), or NaN. */
  const parseNum = (text) => (text.trim() === "" ? NaN : Number(text.trim().replace(",", ".")));
  const pad = (n) => String(n).padStart(2, "0");
  const toDateInput = (ts) => {
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  };
  const toDateTimeInput = (ts) => {
    const d = new Date(ts * 1000);
    return `${toDateInput(ts)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  const batchName = (b) => `${b.brand} ${b.model}`;
  const setupById = (id) => state.bounce.setups.find((s) => s.id === id);
  const batchById = (id) => state.bounce.batches.find((b) => b.id === id);
  const batchLabel = (id) => {
    const b = batchById(id);
    return b ? `${batchName(b)} (opened ${fmtDate(b.opened_ts)})` : "deleted batch";
  };
  const send = (path, method, body) =>
    api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  function median(xs) {
    const s = [...xs].sort((a, b) => a - b);
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  /** Room temperature to pre-fill: the batch's canister if it's in one, else any fresh reading. */
  function roomTempC(batch) {
    const fresh = state.canisters.filter((c) => c.reading && !c.stale && c.reading.temp_c != null);
    const mac = batch.location && batch.location.mac;
    const own = fresh.find((c) => c.mac === mac);
    return (own || fresh[0])?.reading.temp_c ?? null;
  }

  function renderBounce() {
    for (const n of document.querySelectorAll(".len-unit")) n.textContent = state.lengthUnit;
    $("#balls-setup-hint").hidden = state.bounce.setups.length > 0;
    renderBatches();
    renderSetups();
    renderReferences();
    renderConversions();
    renderBatchOptions();
  }

  function renderBatches() {
    const root = $("#batches");
    const all = state.bounce.batches;
    const shown = all.filter((b) => state.bounce.showRetired || b.retired_ts == null);
    $("#show-retired-wrap").hidden = !all.some((b) => b.retired_ts != null);
    root.replaceChildren(...shown.map(batchCard));
    if (shown.length === 0) {
      root.append(el("p", { class: "none" }, all.length
        ? "All batches are retired."
        : "No ball batches yet. Add one when you open new balls, and test them before they go in a canister."));
    }
  }

  function locationText(b) {
    if (!b.location) return null;
    if (!b.location.in_canister) return "Out of canister";
    return `In ${b.location.mac ? nameFor(b.location.mac) : "a canister"}`;
  }

  function testLine(t) {
    const s = t.summary;
    const spread = s.sd_cm != null ? ` ±${toLen(s.sd_cm).toFixed(1)}` : "";
    const k = s.drops_fit?.outliers || 0;
    return `${fmtDate(t.ts)} · ${CONTEXT_SHORT[t.context] || t.context} · average ${fmtLen(s.mean_cm)}${spread}`
      + ` · ${s.balls} ball${s.balls === 1 ? "" : "s"}`
      + (k ? ` · ${misfits(k)}` : "");
  }

  const misfits = (k) => (k === 1 ? "1 drop doesn't fit the others" : `${k} drops don't fit the others`);

  /** "Ball 6, drop 2: 101.8 cm, 4.3 sd below the other drops" for each outlier of a test. */
  function outlierLines(t) {
    const fit = t.summary.drops_fit;
    const seen = new Map();
    const lines = [];
    for (const d of t.drops) {
      const n = (seen.get(d.ball_no) || 0) + 1;
      seen.set(d.ball_no, n);
      if (d.outlier_z == null) continue;
      const off = d.rebound_cm - fit.mean_cm;
      lines.push(`Ball ${d.ball_no}, drop ${n}: ${fmtLen(d.rebound_cm)}, ${Math.abs(d.outlier_z).toFixed(1)} sd `
        + `${off < 0 ? "below" : "above"} the other drops (${fmtLen(fit.mean_cm)} ±${toLen(fit.sd_cm).toFixed(1)})`);
    }
    return lines;
  }

  function batchCard(b) {
    const open = state.bounce.expanded.has(b.id);
    const latest = b.tests[b.tests.length - 1];
    const sample = b.sample_size === b.n_balls ? `testing all ${b.n_balls}` : `testing balls 1–${b.sample_size}`;
    const where = locationText(b);
    const noSetup = state.bounce.setups.length === 0;
    return el("article", { class: `batch${b.retired_ts != null ? " retired" : ""}` },
      el("div", { class: "batch-head" },
        el("h3", {}, batchName(b)),
        where ? el("span", { class: "tag" }, where) : null,
        b.retired_ts != null ? el("span", { class: "tag" }, `Retired ${fmtDate(b.retired_ts)}`) : null),
      el("div", { class: "sub" }, `Opened ${fmtDate(b.opened_ts)} · ${b.n_balls} balls · ${sample}`
        + (b.reference_override_cm != null ? ` · new-ball value ${fmtLen(b.reference_override_cm)} (entered)` : "")),
      b.note ? el("div", { class: "sub" }, b.note) : null,
      el("div", { class: "latest" }, latest
        ? `Latest: ${testLine(latest)}`
        : "No tests yet. Start with the new balls, straight from the can."),
      el("div", { class: "actions" },
        el("button", {
          type: "button", class: "primary", disabled: noSetup,
          title: noSetup ? "Add a test setup first (below)" : null, onclick: () => openTest(b),
        }, "Bounce test"),
        b.tests.length
          ? el("button", { type: "button", "aria-expanded": String(open), onclick: () => toggleTests(b.id) },
            `${open ? "Hide" : "Show"} tests (${b.tests.length})`)
          : null,
        el("button", { type: "button", onclick: () => openBatch(b) }, "Edit"),
        el("button", { type: "button", onclick: () => retireBatch(b) }, b.retired_ts == null ? "Retire" : "Un-retire"),
        el("button", { type: "button", class: "danger", onclick: () => deleteBatch(b) }, "Delete")),
      open ? testsTable(b) : null);
  }

  function testsTable(b) {
    const tDigits = state.tempUnit === "F" ? 0 : 1;
    const heads = ["When", "Stage", "Setup", `Temp (°${state.tempUnit})`, `Average (${state.lengthUnit})`, "Spread", "Balls", ""];
    return el("div", { class: "table-wrap tests-wrap" },
      el("table", { class: "readings" },
        el("thead", {}, el("tr", {},
          ...heads.map((h, i) => el("th", { scope: "col", class: i >= 3 && i <= 6 ? "num" : null }, h)))),
        el("tbody", {}, ...[...b.tests].reverse().flatMap((t) => [el("tr", {},
          el("td", { class: "when" }, fmtDateTime(t.ts)),
          el("td", {}, CONTEXT_SHORT[t.context] || t.context, t.source === "video"
            ? el("span", { class: "tag video-tag", title: t.video ? `Measured from video: ${t.video.filename}` : "Measured from video" }, "video")
            : null),
          el("td", {}, setupById(t.setup_id)?.name ?? "?"),
          el("td", { class: "num" }, t.temp_c == null ? "—" : toTemp(t.temp_c).toFixed(tDigits)),
          el("td", { class: "num" }, t.summary.mean_cm == null ? "—" : toLen(t.summary.mean_cm).toFixed(1)),
          el("td", { class: "num muted" }, t.summary.sd_cm == null ? "—" : `±${toLen(t.summary.sd_cm).toFixed(1)}`),
          el("td", { class: "num" }, t.summary.balls, ...(() => {
            const lines = outlierLines(t);
            return lines.length ? [el("span", {
              class: "flag", role: "img", title: lines.join("\n"),
              "aria-label": `${misfits(lines.length)}: ${lines.join("; ")}`,
            }, " ⚠")] : [];
          })()),
          el("td", { class: "row-actions" },
            t.video?.drops.some((d) => d.image)
              ? el("button", {
                type: "button", class: "link", "aria-expanded": String(state.bounce.pictures.has(t.id)),
                onclick: () => togglePictures(t.id),
              }, state.bounce.pictures.has(t.id) ? "Hide pictures" : "Pictures")
              : null,
            el("button", { type: "button", class: "link", onclick: () => openTest(b, t) }, "Edit"),
            el("button", {
              type: "button", class: "del", title: "Delete test", "aria-label": "Delete test",
              onclick: () => removeItem(`/api/bounce/tests/${t.id}`, `Delete the ${CONTEXT_SHORT[t.context] || t.context} test from ${fmtDateTime(t.ts)}?`),
            }, "✕"))),
        state.bounce.pictures.has(t.id) && t.video ? picturesRow(t, heads.length) : null,
        ].filter(Boolean)))));
  }

  /** A video test's drops as pictures, in the order they were dropped, each with its
   *  full-resolution apex close-up under it; rejected ones dimmed. */
  function picturesRow(t, columns) {
    const seen = new Map();
    const figures = t.video.drops.map((d) => {
      const n = (seen.get(d.ball_no) || 0) + 1;
      seen.set(d.ball_no, n);
      const height = d.edited_cm ?? d.rebound_cm;
      const src = `/api/bounce/video-drops/${d.id}.jpg`;
      const closeup = `/api/bounce/video-drops/${d.id}/closeup.jpg`;
      return el("figure", { class: `drop-pic${d.kept ? "" : " rejected"}`, title: d.warnings.join("\n") || null },
        d.image
          ? el("a", { href: src, target: "_blank", rel: "noopener" },
            el("img", { src, alt: `Ball ${d.ball_no}, drop ${n}`, loading: "lazy" }))
          : el("div", { class: "no-pic" }, "no picture"),
        d.closeup
          ? el("a", { href: closeup, target: "_blank", rel: "noopener", class: "closeup" },
            el("img", { src: closeup, alt: `Ball ${d.ball_no}, drop ${n}: the ball at its apex`, loading: "lazy",
              title: "The ball at its apex, full resolution. The green line is where the measurement put the bottom of the ball." }))
          : null,
        el("figcaption", {}, `Ball ${d.ball_no}.${n} · ${height == null ? "—" : fmtLen(height)}`,
          d.edited_cm != null ? el("span", { class: "tag" }, "edited") : null,
          d.kept ? null : el("span", { class: "tag" }, "rejected"),
          d.warnings.length ? el("span", { class: "flag", "aria-label": d.warnings.join("; ") }, " ⚠") : null));
    });
    const cal = t.video.calibration ? `calibrated (${t.video.calibration})` : "not calibrated";
    return el("tr", { class: "pictures-row" }, el("td", { colspan: columns },
      el("div", { class: "sub" }, `${t.video.filename} · ${cal}`),
      el("div", { class: "drop-pics" }, ...figures)));
  }

  function togglePictures(id) {
    if (!state.bounce.pictures.delete(id)) state.bounce.pictures.add(id);
    renderBatches();
  }

  function toggleTests(id) {
    if (!state.bounce.expanded.delete(id)) state.bounce.expanded.add(id);
    renderBatches();
  }

  /** Fill a <select> with setups; keeps the current (or preferred) choice. Returns how many. */
  function setupOptions(select, filter = () => true, preferred = null) {
    const current = preferred ?? select.value;
    const setups = state.bounce.setups.filter(filter);
    select.replaceChildren(...setups.map((s) => el("option", { value: s.id }, `${s.name} (${fmtLen(s.drop_cm)})`)));
    if (setups.some((s) => String(s.id) === String(current))) select.value = String(current);
    return setups.length;
  }

  function delButton(label, onclick) {
    return el("button", { type: "button", class: "del", title: label, "aria-label": label, onclick }, "✕");
  }

  function renderSetups() {
    const list = $("#setups");
    list.replaceChildren(...state.bounce.setups.map((s) => el("li", {},
      el("span", {}, el("strong", {}, s.name), ` · drops from ${fmtLen(s.drop_cm)}`,
        s.mid_mark_cm != null ? ` · mid mark ${fmtLen(s.mid_mark_cm)}` : "",
        s.is_itf ? " · ITF method" : "", s.note ? ` · ${s.note}` : ""),
      el("span", {},
        el("button", { type: "button", class: "link", onclick: () => editMidMark(s) },
          s.mid_mark_cm != null ? "Mid mark" : "Add mid mark"),
        s.used
          ? el("span", { class: "muted small" }, "in use")
          : delButton("Delete setup", () => removeItem(`/api/bounce/setups/${s.id}`, `Delete the setup "${s.name}"?`))))));
    if (!state.bounce.setups.length) list.append(el("li", { class: "none" }, "No setups yet."));
    setupOptions($("#ref-setup"));
    const nonItf = setupOptions($("#conv-setup"), (s) => !s.is_itf);
    $("#conv-form").hidden = nonItf === 0;
    $("#conv-none").hidden = nonItf > 0 || state.bounce.setups.length === 0;
    $("#ref-form").hidden = state.bounce.setups.length === 0;
  }

  function renderReferences() {
    const refs = state.bounce.references;
    const list = $("#references");
    list.replaceChildren(...refs.map((r) => el("li", {},
      el("span", {}, el("strong", {}, `${r.brand} ${r.model}`),
        ` · ${setupById(r.setup_id)?.name ?? "?"} · ${fmtLen(r.rebound_cm)}`),
      delButton("Delete value", () => removeItem(`/api/bounce/references/${r.id}`, `Delete the value for ${r.brand} ${r.model}?`)))));
    if (!refs.length) list.append(el("li", { class: "none" }, "None entered."));
    const brands = new Set(), models = new Set();
    for (const x of [...state.bounce.batches, ...refs]) { brands.add(x.brand); models.add(x.model); }
    $("#brand-list").replaceChildren(...[...brands].sort().map((v) => el("option", { value: v })));
    $("#model-list").replaceChildren(...[...models].sort().map((v) => el("option", { value: v })));
  }

  function renderConversions() {
    const convs = state.bounce.conversions;
    const list = $("#conversions");
    list.replaceChildren(...convs.map((c) => el("li", {},
      el("span", {}, `${fmtDate(c.ts)} · ${setupById(c.setup_id)?.name ?? "?"}: `,
        el("strong", {}, fmtLen(c.rebound_cm)), " → ITF ", el("strong", {}, fmtLen(c.itf_rebound_cm)),
        c.note ? ` · ${c.note}` : ""),
      delButton("Delete pair", () => removeItem(`/api/bounce/conversions/${c.id}`, "Delete this pair?")))));
    if (!convs.length) list.append(el("li", { class: "none" }, "No pairs yet."));
  }

  function renderBatchOptions() {
    const select = $("#event-batch");
    const current = select.value;
    const active = state.bounce.batches.filter((b) => b.retired_ts == null);
    select.replaceChildren(el("option", { value: "" }, "No batch"),
      ...active.map((b) => el("option", { value: b.id }, batchLabel(b.id))));
    select.value = active.some((b) => String(b.id) === current) ? current : "";
  }

  function syncEventBatch() {
    const kind = $("#event-kind").value;
    $("#event-batch").hidden = !(kind === "balls_added" || kind === "balls_removed");
  }

  async function removeItem(path, question) {
    if (!confirm(question)) return;
    $("#balls-error").textContent = "";
    try {
      await api(path, { method: "DELETE" });
      await refreshBounce();
    } catch (e) {
      $("#balls-error").textContent = `Could not delete: ${e.message}`;
    }
  }

  async function submitForm(e, errorSel, action) {
    e.preventDefault();
    const err = $(errorSel);
    err.textContent = "";
    try {
      await action();
      await refreshBounce();
    } catch (ex) {
      err.textContent = ex.message;
    }
  }

  /** A required length input, in cm. Throws a readable error, in the user's units. */
  function lengthField(sel, what, maxCm = null) {
    const v = parseNum($(sel).value);
    if (!(v > 0)) throw new Error(`${what} must be a number above 0`);
    if (maxCm != null && fromLen(v) > maxCm + 1e-6) {
      throw new Error(`${what} can't be higher than the drop height (${fmtLen(maxCm)}). Wrong units?`);
    }
    return fromLen(v);
  }

  /** The mid mark is a hint for the video app; changing it doesn't touch tests already taken. */
  async function editMidMark(s) {
    const current = s.mid_mark_cm == null ? "" : toLen(s.mid_mark_cm).toFixed(1);
    const typed = prompt(`Mid mark for "${s.name}" in ${state.lengthUnit}: the height of the second mark ` +
      `on the wall, near typical rebounds. Leave empty to remove it.`, current);
    if (typed == null) return;
    $("#balls-error").textContent = "";
    try {
      const v = typed.trim() === "" ? null : parseNum(typed);
      if (v != null && !(v > 0 && fromLen(v) < s.drop_cm)) {
        throw new Error(`The mid mark must be between the floor and the drop height (${fmtLen(s.drop_cm)})`);
      }
      await send(`/api/bounce/setups/${s.id}`, "PATCH", { mid_mark_cm: v == null ? null : fromLen(v) });
      await refreshBounce();
    } catch (e) {
      $("#balls-error").textContent = `Could not set the mid mark: ${e.message}`;
    }
  }

  function addSetup(e) {
    return submitForm(e, "#setup-error", async () => {
      const drop = lengthField("#setup-drop", "Drop height");
      const midText = $("#setup-mid").value.trim();
      const mid = midText === "" ? null : lengthField("#setup-mid", "Mid mark", drop);
      if (mid != null && mid >= drop) throw new Error("The mid mark must be below the drop height");
      await send("/api/bounce/setups", "POST", {
        name: $("#setup-name").value,
        drop_cm: drop,
        is_itf: $("#setup-itf").checked,
        note: $("#setup-note").value.trim() || null,
        mid_mark_cm: mid,
      });
      e.target.reset();
      $("#setup-drop").readOnly = false;
    });
  }

  function syncItfSetup() {
    const itf = $("#setup-itf").checked;
    const drop = $("#setup-drop");
    if (itf) drop.value = toLen(state.bounce.settings?.itf_drop_cm ?? 254).toFixed(1);
    drop.readOnly = itf;
  }

  function addReference(e) {
    return submitForm(e, "#ref-error", async () => {
      await send("/api/bounce/references", "POST", {
        brand: $("#ref-brand").value,
        model: $("#ref-model").value,
        setup_id: Number($("#ref-setup").value),
        rebound_cm: lengthField("#ref-rebound", "Rebound", setupById(Number($("#ref-setup").value))?.drop_cm),
      });
      $("#ref-rebound").value = "";
    });
  }

  function addConversion(e) {
    return submitForm(e, "#conv-error", async () => {
      await send("/api/bounce/conversions", "POST", {
        setup_id: Number($("#conv-setup").value),
        rebound_cm: lengthField("#conv-rebound", "Your rebound", setupById(Number($("#conv-setup").value))?.drop_cm),
        itf_rebound_cm: lengthField("#conv-itf", "ITF rebound", state.bounce.settings?.itf_drop_cm),
      });
      $("#conv-rebound").value = "";
      $("#conv-itf").value = "";
      $("#conv-rebound").focus(); // next ball
    });
  }

  // Batch dialog (new and edit).

  function updateSampleHint() {
    const n = parseInt($("#batch-n").value, 10);
    const sample = $("#batch-sample");
    if (sample.dataset.auto && n > 0) sample.value = n <= SAMPLE_ALL_UP_TO ? n : DEFAULT_SAMPLE;
    const k = parseInt(sample.value, 10);
    $("#batch-sample-hint").textContent = !(n > 0 && k > 0) ? ""
      : k >= n ? `Number all ${n} balls 1–${n} with a marker.`
        : `Pick ${k} balls at random, number them 1–${k} with a marker, and test those same balls every time.`;
  }

  function openBatch(b = null) {
    const f = $("#batch-form");
    f.reset();
    f.dataset.id = b ? b.id : "";
    $("#batch-title").textContent = b ? `Edit ${batchName(b)}` : "New ball batch";
    $("#batch-brand").value = b ? b.brand : "";
    $("#batch-model").value = b ? b.model : "";
    $("#batch-opened").value = toDateInput(b ? b.opened_ts : serverNow());
    $("#batch-n").value = b ? b.n_balls : "";
    $("#batch-sample").value = b ? b.sample_size : "";
    $("#batch-sample").dataset.auto = b ? "" : "1";
    $("#batch-ref").value = b && b.reference_override_cm != null ? toLen(b.reference_override_cm).toFixed(1) : "";
    $("#batch-note").value = b?.note ?? "";
    $("#batch-error").textContent = "";
    updateSampleHint();
    $("#batch-dialog").showModal();
  }

  async function saveBatch(e) {
    e.preventDefault();
    const f = $("#batch-form");
    const err = $("#batch-error");
    err.textContent = "";
    const existing = f.dataset.id ? batchById(Number(f.dataset.id)) : null;
    const n = parseInt($("#batch-n").value, 10);
    const sample = parseInt($("#batch-sample").value, 10);
    const refText = $("#batch-ref").value;
    const ref = refText.trim() ? parseNum(refText) : null;
    if (!(n > 0 && sample > 0)) { err.textContent = "Enter how many balls are in the batch and how many to test."; return; }
    if (ref != null && !(ref > 0)) { err.textContent = "The new-ball rebound must be a number, or left empty."; return; }
    // Keep the original time of day unless the date was changed.
    const opened = $("#batch-opened").value;
    const was = existing ? existing.opened_ts : serverNow();
    const openedTs = opened === toDateInput(was) ? was : new Date(`${opened}T12:00`).getTime() / 1000;
    const body = {
      brand: $("#batch-brand").value,
      model: $("#batch-model").value,
      opened_ts: openedTs,
      n_balls: n,
      sample_size: sample,
      reference_override_cm: ref == null ? null : fromLen(ref),
      note: $("#batch-note").value.trim() || null,
    };
    try {
      if (existing) await send(`/api/bounce/batches/${existing.id}`, "PATCH", body);
      else await send("/api/bounce/batches", "POST", body);
      $("#batch-dialog").close();
      await refreshBounce();
    } catch (ex) {
      err.textContent = ex.message;
    }
  }

  async function retireBatch(b) {
    const retired = b.retired_ts == null;
    if (retired && !confirm(`Retire ${batchName(b)}? It will be hidden; its tests are kept.`)) return;
    try {
      await send(`/api/bounce/batches/${b.id}`, "PATCH", { retired_ts: retired ? serverNow() : null });
      await refreshBounce();
    } catch (e) {
      $("#balls-error").textContent = `Could not update: ${e.message}`;
    }
  }

  function deleteBatch(b) {
    const tests = b.tests.length ? ` and its ${b.tests.length} test${b.tests.length === 1 ? "" : "s"}` : "";
    return removeItem(`/api/bounce/batches/${b.id}`,
      `Delete ${batchName(b)} (opened ${fmtDate(b.opened_ts)})${tests}? This can't be undone. To keep the data, retire it instead.`);
  }

  // Bounce test dialog: one row per numbered ball, DROPS_PER_BALL boxes each.

  function openTest(b, t = null) {
    const f = $("#test-form");
    f.reset();
    f.dataset.batch = b.id;
    f.dataset.test = t ? t.id : "";
    $("#test-title").textContent = `${t ? "Edit test" : "Bounce test"} · ${batchName(b)}`;
    const last = b.tests[b.tests.length - 1];
    setupOptions($("#test-setup"), () => true, t ? t.setup_id : last ? last.setup_id : null);
    const ctx = $("#test-context");
    ctx.replaceChildren(...state.bounce.settings.contexts.map((c) => el("option", { value: c }, CONTEXT_LABELS[c] || c)));
    ctx.value = t ? t.context : !b.tests.length ? "new" : b.location?.in_canister ? "after" : "before";
    $("#test-when").value = t ? toDateTimeInput(t.ts) : "";
    const temp = t ? t.temp_c : roomTempC(b);
    $("#test-temp").value = temp == null ? "" : toTemp(temp).toFixed(state.tempUnit === "F" ? 0 : 1);
    $("#test-temp-unit").textContent = `°${state.tempUnit}`;
    $("#test-note").value = t?.note ?? "";

    const byBall = new Map();
    for (const d of t ? t.drops : []) {
      if (!byBall.has(d.ball_no)) byBall.set(d.ball_no, []);
      byBall.get(d.ball_no).push(d.rebound_cm);
    }
    const balls = [...new Set([...Array.from({ length: b.sample_size }, (_, i) => i + 1), ...byBall.keys()])]
      .sort((x, y) => x - y);
    const cols = Math.max(DROPS_PER_BALL, ...[...byBall.values()].map((v) => v.length));
    const grid = $("#test-grid");
    grid.style.setProperty("--drops", cols);
    grid.replaceChildren(
      el("div", { class: "grid-head", "aria-hidden": "true" }, el("span", {}, "Ball"),
        ...Array.from({ length: cols }, (_, i) => el("span", {}, `Drop ${i + 1}`)), el("span", {}, "Median")),
      ...balls.map((n) => el("div", { class: "ball-row", "data-ball": n },
        el("label", { for: `drop-${n}-0` }, `#${n}`),
        ...Array.from({ length: cols }, (_, i) => {
          const cm = byBall.get(n)?.[i];
          return el("input", {
            id: `drop-${n}-${i}`, inputmode: "decimal", autocomplete: "off", enterkeyhint: "next",
            "aria-label": `Ball ${n}, drop ${i + 1}, ${state.lengthUnit}`,
            value: cm == null ? null : toLen(cm).toFixed(1),
          });
        }),
        el("output", { class: "median" }, "—"))));
    $("#test-error").textContent = "";
    f.dataset.dirty = "";
    outlierNote = "";
    updateTestSummary();
    $("#test-dialog").showModal();
  }

  /** Read the ball grid: per-ball valid values, flagging boxes that aren't numbers or are
   * higher than the setup's drop height (usually the wrong units). */
  function readGrid() {
    const rows = [];
    let bad = 0;
    const drop = toLen(setupById(Number($("#test-setup").value))?.drop_cm ?? Infinity);
    for (const row of document.querySelectorAll("#test-grid .ball-row")) {
      const vals = [];
      const inputs = [];
      for (const input of row.querySelectorAll("input")) {
        const empty = input.value.trim() === "";
        const v = parseNum(input.value);
        const ok = empty || (v > 0 && v <= drop + 1e-6);
        input.setAttribute("aria-invalid", String(!ok));
        if (!ok) bad++;
        else if (!empty) { vals.push(v); inputs.push(input); }
      }
      rows.push({ ball: Number(row.dataset.ball), vals, inputs, row });
    }
    return { rows, bad };
  }

  // Drops that don't fit the others, checked by the Pi (bounce.fit_normal) a moment
  // after typing stops. Only a hint: a test with outliers saves like any other.
  let outlierTimer = null;
  let outlierSeq = 0;
  let outlierNote = "";

  function scheduleOutlierCheck() {
    clearTimeout(outlierTimer);
    outlierTimer = setTimeout(checkOutliers, 300);
  }

  async function checkOutliers() {
    const { rows } = readGrid();
    const cells = rows.flatMap((r) => r.vals.map((v, i) => ({ v, input: r.inputs[i] })));
    const seq = ++outlierSeq;
    let res;
    try {
      res = await api("/api/bounce/outliers", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rebounds_cm: cells.map((c) => fromLen(c.v)) }),
      });
    } catch (_) {
      return; // the Pi is unreachable: saving will say so
    }
    if (seq !== outlierSeq) return; // typed again since
    for (const input of document.querySelectorAll("#test-grid input")) {
      input.classList.remove("outlier");
      input.removeAttribute("title");
    }
    const fit = res.drops_fit;
    let k = 0;
    cells.forEach((c, i) => {
      const z = res.outlier_z[i];
      if (z == null) return;
      k++;
      c.input.classList.add("outlier");
      c.input.title = `${Math.abs(z).toFixed(1)} sd ${z < 0 ? "below" : "above"} the other drops `
        + `(${fmtLen(fit.mean_cm)} ±${toLen(fit.sd_cm).toFixed(1)}): a typo?`;
    });
    outlierNote = k ? ` · ${misfits(k)} (highlighted): a typo?` : "";
    updateTestSummary(false);
  }

  function updateTestSummary(recheck = true) {
    if (recheck) scheduleOutlierCheck();
    const { rows, bad } = readGrid();
    const meds = [];
    for (const r of rows) {
      const m = r.vals.length ? median(r.vals) : null;
      r.row.querySelector(".median").textContent = m == null ? "—" : m.toFixed(1);
      if (m != null) meds.push(m);
    }
    const n = meds.length;
    const mean = n ? meds.reduce((a, b) => a + b, 0) / n : null;
    const sd = n > 1 ? Math.sqrt(meds.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1)) : null;
    const problems = bad ? ` · ${bad} box${bad === 1 ? " needs" : "es need"} fixing` : "";
    $("#test-summary").textContent = n
      ? `${n} ball${n === 1 ? "" : "s"} · average ${mean.toFixed(1)} ${state.lengthUnit}`
        + `${sd != null ? ` · spread ±${sd.toFixed(1)}` : ""}${outlierNote}${problems}`
      : `No drops entered yet${problems}.`;
  }

  async function saveTest(e) {
    e.preventDefault();
    const f = $("#test-form");
    const err = $("#test-error");
    err.textContent = "";
    const { rows, bad } = readGrid();
    if (bad) {
      err.textContent = "Fix the highlighted boxes: each must be a number above 0 and no higher than the drop height.";
      return;
    }
    const drops = rows.flatMap((r) => r.vals.map((v) => ({ ball_no: r.ball, rebound_cm: fromLen(v) })));
    if (!drops.length) { err.textContent = "Enter at least one drop."; return; }
    if (!$("#test-setup").value) { err.textContent = "Choose a setup."; return; }
    const tempText = $("#test-temp").value;
    const temp = tempText.trim() === "" ? null : parseNum(tempText);
    if (temp != null && !Number.isFinite(temp)) { err.textContent = "Room temp must be a number, or left empty."; return; }
    const body = {
      batch_id: Number(f.dataset.batch),
      setup_id: Number($("#test-setup").value),
      context: $("#test-context").value,
      temp_c: temp == null ? null : fromTempInput(temp),
      note: $("#test-note").value.trim() || null,
      drops,
    };
    const when = $("#test-when").value;
    if (when) body.ts = new Date(when).getTime() / 1000;
    const save = $("#test-save");
    save.disabled = true;
    try {
      if (f.dataset.test) await send(`/api/bounce/tests/${f.dataset.test}`, "PUT", body);
      else await send("/api/bounce/tests", "POST", body);
      f.dataset.dirty = "";
      $("#test-dialog").close();
      state.bounce.expanded.add(body.batch_id);
      await refreshBounce();
    } catch (ex) {
      err.textContent = ex.message;
    } finally {
      save.disabled = false;
    }
  }

  function bindTestDialog() {
    const grid = $("#test-grid");
    grid.addEventListener("input", () => {
      $("#test-form").dataset.dirty = "1";
      updateTestSummary();
    });
    // Enter moves to the next box, then to Save, so a test can be typed without the mouse.
    grid.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.target.tagName !== "INPUT") return;
      e.preventDefault();
      const inputs = [...grid.querySelectorAll("input")];
      (inputs[inputs.indexOf(e.target) + 1] || $("#test-save")).focus();
    });
    $("#test-setup").addEventListener("change", updateTestSummary); // drop height limit
    $("#test-form").addEventListener("submit", saveTest);
    $("#test-dialog").addEventListener("cancel", (e) => {
      if ($("#test-form").dataset.dirty && !confirm("Discard the drops you've entered?")) e.preventDefault();
    });
  }

  async function refreshBounce() {
    try {
      Object.assign(state.bounce, await api("/api/bounce"));
      renderBounce();
    } catch (e) {
      setStatus(false, `Offline — retrying (${e.message})`);
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
      renderCanisterOptions($("#event-mac"));
      renderCanisterOptions($("#table-mac"));
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
        loadTable(),
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
    bindSegment("#units", () => state.units, (v) => { state.units = v; renderCards(); renderCharts(); renderTable(); });
    bindSegment("#temp-units", () => state.tempUnit, (v) => {
      state.tempUnit = v; renderCards(); renderCharts(); renderTable(); renderBatches();
    });
    bindSegment("#length-units", () => state.lengthUnit, (v) => { state.lengthUnit = v; renderBounce(); });
    bindSegment("#ranges", () => state.range, (v) => {
      state.range = v === "fill" ? "fill" : Number(v);
      state.table.rows = []; // new range: start from one page again
      refreshReadings();
    });
    const comp = $("#compensated");
    comp.checked = state.compensated;
    comp.addEventListener("change", () => {
      state.compensated = comp.checked;
      savePrefs();
      renderCards();
      renderCharts();
      renderTable();
    });
    $("#event-form").addEventListener("submit", addEvent);
    $("#event-kind").addEventListener("change", syncEventBatch);
    syncEventBatch();

    $("#batch-new").addEventListener("click", () => openBatch());
    $("#batch-form").addEventListener("submit", saveBatch);
    $("#batch-n").addEventListener("input", updateSampleHint);
    $("#batch-sample").addEventListener("input", (e) => { e.target.dataset.auto = ""; updateSampleHint(); });
    for (const b of document.querySelectorAll("dialog [data-close]")) {
      b.addEventListener("click", () => {
        const dialog = b.closest("dialog");
        const ev = new Event("cancel", { cancelable: true });
        if (dialog.dispatchEvent(ev)) dialog.close();
      });
    }
    bindTestDialog();
    $("#setup-form").addEventListener("submit", addSetup);
    $("#setup-itf").addEventListener("change", syncItfSetup);
    $("#ref-form").addEventListener("submit", addReference);
    $("#conv-form").addEventListener("submit", addConversion);
    $("#show-retired").addEventListener("change", (e) => { state.bounce.showRetired = e.target.checked; renderBatches(); });
    $("#table-mac").addEventListener("change", (e) => {
      state.table.mac = e.target.value;
      state.table.rows = [];
      loadTable().catch((ex) => setStatus(false, `Offline — retrying (${ex.message})`));
    });
    $("#table-more").addEventListener("click", loadMore);

    new ResizeObserver(resizeCharts).observe($("main"));
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      charts.key = ""; // rebuild charts with the new theme colors
      renderCharts();
    });

    // Bounce data first: notes name batches.
    refreshBounce().then(refreshCanisters).then(refreshReadings);
    setInterval(refreshCanisters, POLL_CANISTERS_MS);
    setInterval(refreshReadings, POLL_READINGS_MS);
    setInterval(refreshBounce, POLL_READINGS_MS); // other devices may add tests
    setInterval(renderCards, 15_000); // keep "updated X ago" current
  }

  init();
})();
