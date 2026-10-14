# Canister Pressure Monitor — Implementation Plan

This document is the source of truth for building this project. It is written for coding agents (and humans) working in this repo. Read it fully before starting any task, and follow the phase order unless told otherwise.

---

## 1. Project summary

**Purpose:** Monitor the air pressure inside canisters used to re-pressurize flat tennis balls, so the owner can see how the re-pressurization process is progressing over several days.

**How:** Bluetooth Low Energy (BLE) tire-pressure sensors (TPMS valve caps) are screwed onto the canisters' Schrader valves. A Raspberry Pi 3 Model B+ listens for the sensors' BLE broadcasts, decodes pressure and temperature, stores every reading in SQLite, and serves a simple web dashboard on the home LAN.

**Hardware:**

| Item | Notes |
|---|---|
| Raspberry Pi 3 Model B+ | Built-in Bluetooth 4.2 (BLE) and dual-band Wi-Fi + Ethernet |
| 4x BLE TPMS external sensors | Sold as "B-Qtech Wireless Bluetooth TPMS with 4 External Sensors for RV Trailer". Protocol not documented by the vendor (see §5) |
| microSD card (16 GB+) and 5V/2.5A PSU | Raspberry Pi OS Lite 64-bit |

**Users:** One person on a home LAN. No authentication, no internet exposure.

---

## 2. Goals and non-goals

### Goals

1. Capture every pressure/temperature broadcast from up to ~8 sensors, continuously, for days at a time.
2. Store data durably so a reboot or crash loses nothing already recorded.
3. Provide a LAN web dashboard with current values and history charts per canister.
4. Provide temperature-compensated pressure so real trends can be separated from day/night temperature swings.
5. Allow CSV export and simple annotations ("filled to 30 psi", "added 3 balls").
6. Run unattended: start on boot, recover from Bluetooth stalls.
7. Predict how the balls bounce (Phase 8, §6.2): whether they rebound like new and whether they would pass the ITF rebound test, from canister pressure and occasional bounce tests.

### Non-goals

- No cloud services, accounts, or authentication.
- No internet dependency at runtime (all JS/CSS is vendored locally).
- No Home Assistant or MQTT integration in the initial scope (possible later; see §12).
- No writing to or configuring the sensors. They are broadcast-only.

---

## 3. Key facts about the sensors (read before coding)

1. **Broadcast-only.** These sensors do not accept connections. They put readings into BLE *advertisement packets* (manufacturer data and/or service UUIDs). The collector only needs to scan and decode. Never attempt to connect or pair.
2. **Irregular timing.** Sensors typically broadcast when pressure or temperature changes, and much less often (possibly as rarely as ~hourly) when stable. **Do not assume a reading every minute.** Treat each decoded packet as an event with a timestamp; the UI shows "last known value" and its age.
3. **Accuracy.** Tire sensors are typically ±1.5–3 psi absolute accuracy with ~0.1–1 psi resolution. Repeatability is better than accuracy; trends are the point.
4. **Gauge vs absolute.** Some formats report gauge pressure, some report absolute. Decoders must normalize to a single canonical unit (see §6).
5. **Temperature is the sensor's temperature**, not necessarily the gas temperature. It's the best available proxy for compensation.
6. **Exact format is unknown** until Phase 0 captures real packets. Decoders for two known generic formats are specified in §5 as starting hypotheses.

---

## 4. Architecture

Two independent processes share one SQLite database (WAL mode):

```
 ┌──────────────┐  BLE adverts   ┌───────────────────────┐
 │ TPMS sensors │ ─────────────▶ │ collector (asyncio)   │
 └──────────────┘                │  bleak scanner        │
                                 │  decoder registry     │
                                 │  writes raw + decoded │
                                 └──────────┬────────────┘
                                            │ SQLite (WAL)
                                 ┌──────────▼────────────┐   HTTP   ┌─────────┐
                                 │ web (Flask+waitress)  │ ───────▶ │ Browser │
                                 │  JSON API + dashboard │          └─────────┘
                                 └───────────────────────┘
```

**Why two processes:** bleak is asyncio-based and Flask is synchronous. Keeping them separate avoids threading bugs, lets the web server restart without losing scans, and makes each piece testable on its own. The web process opens the DB **read-mostly** (it only writes annotations).

**Key design rule: always store the raw advertisement.** Every relevant advert's raw bytes are saved before decoding. If a decoder turns out to be wrong, data can be re-decoded later with `tools/redecode.py`. Nothing is lost while the protocol is still being figured out.

### Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | Pi OS Bookworm ships 3.11; use `tomllib` from stdlib |
| BLE | `bleak` | Linux/BlueZ backend via D-Bus |
| Web | `flask` + `waitress` | waitress is pure Python, production-safe |
| DB | `sqlite3` (stdlib) | WAL mode, no ORM |
| Charts | `uPlot` (vendored) | Small, fast, native time axes |
| Config | TOML | `config.toml`, example in repo |
| Tests | `pytest` | |
| Lint/format | `ruff` | |
| Video (laptop only) | `opencv-python`, `numpy` | Optional extra `video` for the bounce-video tool (§6.3); never installed on the Pi |

Keep runtime dependencies to **bleak, flask, waitress**. Do not add others without a clear reason recorded in the PR/commit message. The optional `video` extra is the one exception: it is only for the laptop tool.

---

## 5. Sensor protocol and decoders

### 5.1 Decoder interface

```python
# src/canister_monitor/decoders/base.py
from dataclasses import dataclass, field
from typing import Protocol

@dataclass(frozen=True)
class Advert:
    ts: float                         # UTC epoch seconds
    mac: str                          # "AA:BB:CC:DD:EE:FF", uppercase
    name: str | None
    rssi: int | None
    manufacturer_data: dict[int, bytes]   # as bleak provides it (see gotcha below)
    service_uuids: list[str]
    service_data: dict[str, bytes]

@dataclass(frozen=True)
class Reading:
    ts: float
    mac: str
    pressure_kpa_abs: float           # canonical: absolute pressure in kPa
    temp_c: float | None
    battery_pct: float | None = None
    battery_v: float | None = None
    flags: dict = field(default_factory=dict)  # e.g. {"flat": True, "status": 0x28}
    decoder: str = ""

class Decoder(Protocol):
    name: str
    pressure_reference: str          # "gauge" or "absolute" (as reported by the sensor)
    def matches(self, adv: Advert) -> bool: ...
    def decode(self, adv: Advert, atmospheric_kpa: float) -> Reading | None: ...
```

Decoders are registered in `decoders/__init__.py` and enabled by name in config. `decode()` must never raise on malformed input; return `None` and let the caller log it.

### 5.2 Bleak gotcha: manufacturer data keys

Bleak splits manufacturer-specific data into `{company_id: payload}`, where `company_id` is the **first two bytes, little-endian**, and `payload` is the rest. Some cheap sensors don't use a real company ID; their first two bytes are sensor data. Decoders for such formats must rebuild the full byte string:

```python
full = cid.to_bytes(2, "little") + payload
```

### 5.3 Format "br_27a5" (hypothesis — strong evidence)

Identification: local name `"BR"` and/or 16-bit service UUID `0x27a5` (`"000027a5-0000-1000-8000-00805f9b34fb"` in bleak). Manufacturer data is 7 bytes total (after rebuilding as above):

| Byte(s) | Field | Encoding |
|---|---|---|
| 0 | status/flags | bitfield, meaning partly unknown; store raw in `flags["status"]` |
| 1 | battery | volts × 10 (0x1E → 3.0 V) |
| 2 | temperature | °C, unsigned (signedness unconfirmed; treat as int8 if values look wrong below 0 °C) |
| 3–4 | pressure | **absolute**, big-endian uint16, units of 0.1 psi |
| 5–6 | checksum | algorithm unknown; store, don't validate |

Conversion: `pressure_kpa_abs = raw * 0.1 * 6.894757`.

Test fixtures (from a public forum post, cross-checked against the vendor app):

| Full mfr bytes (hex) | bleak key / payload | App showed | Expected decode |
|---|---|---|---|
| `281E11014D8511` | `0x1E28` / `11014D8511` | 1.3 bar, 17 °C, 3.0 V | 33.3 psi abs ≈ 18.6 psi gauge ≈ 1.28 bar; 17 °C; 3.0 V |
| `401E1501FA0FF8` | `0x1E40` / `1501FA0FF8` | 2.5 bar, 21 °C, 3.0 V | 50.6 psi abs ≈ 35.9 psi gauge ≈ 2.48 bar; 21 °C; 3.0 V |

### 5.4 Format "tpms_0100" (hypothesis — needs confirmation)

Identification: local name starts with `"TPMS"`, manufacturer company ID `0x0100` (256). Payload (bleak `payload`, i.e. after the 2-byte company ID) is 16 bytes:

| Byte(s) | Field | Encoding |
|---|---|---|
| 0–5 | sensor address | byte 0 is often sensor position (0x80, 0x81, …) |
| 6–9 | pressure | **gauge**, little-endian uint32, Pascals |
| 10–13 | temperature | little-endian int32, °C × 100 |
| 14 | battery | percent |
| 15 | flat/alarm flag | 0 or 1 |

Conversion: `pressure_kpa_abs = raw_pa / 1000 + atmospheric_kpa`.

No verified fixtures yet. Write tests from synthetic payloads built from this table, and mark them `# TODO: replace with captured fixture`.

### 5.5 Unknown formats

If Phase 0 shows neither format matches, add a new decoder module following the same interface, document its byte table in this section, and add captured fixtures. Use the syringe trick (screw a sensor onto a valve on a large syringe) or the canister itself at two known pressures (measured with a gauge) to identify bytes.

### 5.6 Format "fbb0_ac00" (B-Qtech sensors — confirmed in Phase 0)

The B-Qtech sensors match **neither** §5.3 nor §5.4. Captured 2026-10-08 from sensor `30:94:A8:11:11:11` on Windows (bleak/WinRT), with values cross-checked against the vendor phone app. Fixtures: `tests/fixtures/captured/fbb0_ac00_sensor1.jsonl`.

Identification: no local name; 16-bit service UUID `0xfbb0` (`"0000fbb0-0000-1000-8000-00805f9b34fb"`); manufacturer data is 17 bytes total, which bleak reports as company ID `0x00AC` (172) plus a 15-byte payload. The sensor's own MAC appears byte-reversed at the end. Indices below are into the **full** 17 bytes (`full = cid.to_bytes(2, "little") + payload`):

| Byte(s) | Field | Encoding / evidence |
|---|---|---|
| 0–1 | header | `AC 00`, constant |
| 2 | unknown | Tracks temperature at ≈0.8 count/°C: 143 at 13 °C, 145 at 14 °C, 150 at ~21 °C, 156 at 29 °C. Battery voltage (rises when warm) or a second temperature reading — **unconfirmed** |
| 3 | **pressure, gauge** | uint8, ≈ **0.4558 psi (≈ 3.1426 kPa) per count**, 0 at ambient. Four app pairs bound the scale to 0.4555–0.4561 psi/count. Resolution ≈ 0.46 psi; max ≈ 116 psi |
| 4 | **temperature** | uint8, **°C + 55**. Verified at 13, 14 and 29 °C; every app reading (55.4–87.8 °F) was a whole °C converted to °F |
| 5 | flags | `00` normal; `01` at 0 psi (unscrewed); `02` right after the sensor is screwed onto pressure (seen twice). Store raw in `flags["status"]` |
| 6 | unknown | `0A`, constant so far |
| 7 | unknown | changes on every new reading (counter or random) |
| 8 | unknown | `00`, constant so far |
| 9 | check byte? | changes on every new reading; algorithm unknown — store, don't validate |
| 10 | unknown | `28`, constant so far |
| 11–16 | sensor MAC | reversed, e.g. `11 11 11 A8 94 30` for `30:94:A8:11:11:11` |

Conversion: `pressure_kpa_abs = full[3] * 3.1426 + atmospheric_kpa`; `temp_c = full[4] - 55`.

Verified pairs (bytes 3 → app): `66`=102 → 46.5 psi; `63`=99 → 45.1 psi; `49`=73 → 33.3 psi; `27`=39 → 17.8 psi; `00` → unscrewed. Note: right after bleeding air the app briefly showed 17.3 psi from a packet the laptop missed, then 17.8 once the gas re-warmed — wait ~5 min after a pressure change before pairing readings.

Broadcast behaviour (laptop view): short bursts every ~4.5–6.5 min when stable; every ~9 s for ~45 s after a change; **stops broadcasting at 0 psi** (the app stalls too). The phone app does not block broadcasts — the laptop received packets while the app was connected and open. The Windows laptop missed many bursts the phone caught (e.g. 03:54–04:00, phone updated 4+ times, laptop heard none) — likely Windows' partial scan duty cycle / Wi-Fi coexistence. **Verify on the Pi that it catches every update**; if not, try Ethernet instead of Wi-Fi.

Battery: the vendor app shows only a bar (near full on 2026-10-08), no number. Candidates are byte 2 (voltage?), byte 6 (`0A`, a 0–10 level?) or byte 10 (`28`). Resolve by comparing sensors whose app bars differ, or by swapping in a partly drained cell.

Still to confirm: byte 2's meaning, the battery field, temperature over a wider range (incl. below 0 °C, see §13 Q5), the other three sensors' MACs and whether they share this format.

---

## 6. Units, compensation, and derived values

**Canonical storage:** absolute pressure in kPa, temperature in °C, timestamps as UTC epoch seconds (REAL). All conversions to psi/bar and local time happen in the UI or export layer.

**Config values:**
- `atmospheric_kpa` — local ambient pressure (default 101.325; user may set for elevation).
- `reference_temp_c` — temperature to normalize to (default 20.0).

**Formulas** (implement in `compensation.py`, fully unit-tested):

```
gauge_kpa       = abs_kpa - atmospheric_kpa
compensated_abs = abs_kpa * (reference_temp_c + 273.15) / (temp_c + 273.15)
compensated_gauge = compensated_abs - atmospheric_kpa
psi = kpa / 6.894757 ; bar = kpa / 100
```

Compensation must use **absolute** pressure and **Kelvin**. If `temp_c` is missing, compensated values are `None`.

**Trend (Phase 7):** slope of compensated gauge pressure over a trailing window (default **24 h**) via least-squares, reported in psi/day. "Plateau" when |slope| stays below a configurable threshold (default **0.3 psi/day**) for a configurable duration (default 12 h). Interpretation: as air diffuses into the balls, canister pressure declines and levels off; a decline that never levels off may indicate a leak — flagged when still falling `leak_hint_after_hours` (default **96 h**) after the latest `fill` event (or the first reading if there is none).

Why not the original 6 h / 0.2 psi/day: with the B-Qtech sensors' 0.46 psi steps, the 6 h slope of a canister that has truly levelled off wobbles by up to ±1.3 psi/day, so it could never stay under 0.2 for 12 h. Measured on 7 simulated days: max |slope| on flat canisters was ±1.3 (6 h), ±0.4 (12 h), ±0.2 (24 h), while the 1.5 psi/day leak read −1.4 to −1.6 at 24 h. One healthy simulated canister was still at −0.43 psi/day on day 3, hence the 96 h leak hint. All four are `[trends]` settings; revisit with real data.

Statuses: `insufficient_data` (fewer than 6 readings, or they span less than half the window), `falling`, `rising`, `levelling_off` (flat for less than `plateau_hours`), `levelled_off`, `leak_suspected`.

### 6.1 Leak calibration (Phase 8, `leak.py`)

**Goal:** subtract each canister's leak from a ball cycle, so the rest of the pressure drop measures the air that went into the balls. Since bounce tests were added (§6.2), this mass balance is a **cross-check**, not the main prediction. It is most informative for large batches, because the canister notices the balls in proportion to the air they hold relative to its free volume. The canister is used in two modes: **maintain** (filled to ~20 psi, to hold the balls at pressure) and **recharge** (filled to ~50 psi, to add pressure; the balls do not reach canister pressure).

**Leak tests.** The canister is filled with no balls and left alone. The user logs a `leak_test` event at the fill. The test runs until the next `fill`, `leak_test`, `vent` or `balls_added` event for that canister (or all canisters). Each test starts with the canister fully vented, lid opened and closed, refilled, and the sensor re-seated, just like a real cycle, so tests sample seal variation. Recommended 7-day protocol: 50 psi for 60 h, 20 psi for 60 h, 50 psi for 48 h (three seatings, both pressures). In simulation, every 7-day split of 2–4 tests did about equally well: ±0.07–0.08 psi/day at 20 psi and ±0.12–0.17 at 50. One 7-day test is no better and says nothing about seal variation. Extra time: a fourth test at 50 psi (3 days) gave the best gain at 50 psi (±0.10). Beyond ~10 days, testing adds little, because the remaining error is mostly the seal variation itself.

**Per session** (`fit_session`): hourly means of compensated gauge pressure, skipping the first 2 h (the gas cools after pumping), fitted with a line plus a daily sine and cosine (temperature compensation is imperfect because the sensor follows the air faster than the gas does). Rate = −slope / mean gauge, as a fraction per day. Its standard error is widened for autocorrelated residuals and for **weather**. The sensor reads gauge pressure, so atmospheric changes appear in the readings. Typical drift (0.8 kPa sd, 36 h correlation) looks like a slope of about 0.05 psi/day over three days. Nothing within one session can separate that from a leak, so its expected effect on the slope is added to the error. Without it, simulated errors were 3–4× too small.

**Across sessions** (`fit_calibration`): ln(rate) = μ + β·ln(P / 30 psi) + u, with u ~ N(0, τ²) per session. β is how the leak scales with pressure: 0 when flow is proportional to gauge pressure (permeation), about −0.3 for an orifice. It is fitted only when the tests span ≥1.5× in pressure, with a weak N(0, 1) prior. Each session's measurement error is integrated out numerically, so negative or noisy rates are fine. τ is averaged over a grid with a half-Cauchy(0.5) prior instead of taken at its best fit: with six noisy tests the best fit was τ ≈ 0 most of the time, and predictions covered 45% of new sessions instead of 68%. A uniform prior gave 85–100% (too wide). Half-Cauchy came out near 68% for true τ of 0.4–0.8 and still erred wide (85–98%) for tight seals (τ 0.1), which is the safe direction. Prediction for a new session: median rate and ±1 sd range, `Calibration.rate_range`.

**Simulation** (`tools/leak_sim.py`, `simulate.leak_test_readings`). Each test goes through the real encoder and decoder (0.46 psi steps, whole °C), with OU weather (0.8 kPa, 36 h), OU daily-mean temperature (1.5 °C, 48 h), ±5 °C daily cycle and a 1 h lag of gas temperature. Truth is a rate of 0.005/day at 30 psi (0.15 psi/day), β −0.3. Results, 60 trials each:

| Protocol (psi:hours) | Conditions | True τ | Fitted τ (median) | β (median ± sd) | New session inside ±1 sd (expect 68%) | Leak prediction error, psi/day rms (20 / 50 psi) |
|---|---|---|---|---|---|---|
| 50:168, 3× 20:72, 2× 50:72 | weather | 0.4 | 0.30 | −0.12 ± 0.41 | 80% / 67% | 0.068 / 0.107 |
| same | weather | 0.1 | 0.20 | −0.16 ± 0.33 | 88% / 98% | 0.034 / 0.031 |
| same | barometer | 0.4 | 0.42 | −0.31 ± 0.33 | 75% / 80% | 0.056 / 0.090 |
| 50:168, 3× 20:120, 2× 50:72 | weather | 0.4 | 0.34 | −0.26 ± 0.35 | 80% / 72% | 0.056 / 0.116 |
| 50:168, 5× 24 h | weather | 0.4 | 0.37 | −0.00 ± 0.40 | 82% / 85% | 0.090 / 0.137 (median rate 30% low) |

Findings:
- **24 h tests are too short.** They biased the rate low by 30% and doubled its uncertainty. Use 3–5 days.
- **Weather limits the 20 psi tests.** A 72 h test at 20 psi measures its rate to only about ±55% (±0.06 psi/day). β is barely identifiable without a barometer, which recovers it without bias.
- **When seals vary (τ ≈ 0.4), that variation is the floor.** Predicting a new session's leak is uncertain by ±0.06–0.11 psi/day however well μ is known. Over a 7-day recharge cycle that is about 0.4–0.8 psi of canister pressure. With repeatable seals (τ ≈ 0.1) it is ±0.03 psi/day.
- The leak magnitude here is a guess. Real errors scale with the real leak.

### 6.2 Bounce model and predictions (Phase 8, `bounce.py`)

**Goal:** predict, at any time, how a batch of balls bounces, and report two two-sided criteria as the **share of the batch** that meets them:

| | Like-new | ITF rebound |
|---|---|---|
| Question | Do they bounce like new balls of this kind, tested the same way? | Would they pass the ITF rebound test? |
| Pass band | New-ball mean rebound ± `like_new_tolerance_pct` (default ±5%) | 134.62–147.32 cm (53–58 in) from 254 cm (100 in) onto concrete at 20 °C. Configurable, e.g. 48–53 in for high-altitude balls |
| Needs | Tests at one fixed setup (drop height, surface, method), used consistently | Tests at ITF conditions, **or** a one-time conversion between the user's setup and ITF conditions |

Both bands are two-sided. Over-pressurized balls bounce too high, and a long recharge can overshoot, so the app warns before the upper limit as well as reporting when balls are back in the band. "ITF rebound" covers only the rebound criterion: ITF also sets limits on deformation, mass and size, which the app does not predict.

**Batches and bounce tests.** A batch is a set of balls of one brand and model opened together. The user numbers a sample 1 to N: every ball if the batch has 12 or fewer (the default), otherwise 10–12 balls picked at random **once**, when the batch is new, and tested every time after (default 10). Only the sampled balls need numbering. Repeating the same balls cancels ball-to-ball differences in before/after comparisons, and ≥10 balls give a usable spread. A test is 3 drops per sampled ball, median taken, read from slow-motion video against a tape measure (reading by eye is good to only about ±1–2 in). Rebound is measured to the bottom of the ball, released with its bottom at the drop height. Test context: `new` (straight from the can), `before` / `after` a canister session (within ~30 min of opening), `after_play`, or `other`. Room temperature is recorded with each test (the canister sensor's reading if tested in the same room).

**New-ball reference** (for like-new), first available of:
1. this batch's own `new` tests;
2. `new` tests of other batches of the same brand and model, at the same setup;
3. a value the user enters: per brand and model and setup (for balls never tested new), or as an override on one batch.

The source is shown with the prediction. Without any of them, like-new is unavailable for that batch.

**Uptake model.** Air moves through the rubber at a rate proportional to the pressure difference. In bounce units, for the batch-mean rebound B:

```
dB/dt = k * (B_eq(P) - B),   B_eq(P) = a + b * P
```

- P is the compensated gauge pressure around the balls: the **measured** canister pressure while they are in a canister, 0 outside one.
- B_eq(P) is the rebound the balls would settle at if they caught up to P. **a** is the rebound of a fully flat ball, **b** the rebound per psi, and **k** how fast the balls respond (per day).
- Because P is measured, this needs neither the leak calibration nor canister or ball volumes. The same equation describes slow loss on the shelf (P = 0).
- k, a and b belong to one brand and model at one setup. They are fitted by least squares to before/after test pairs: predict each "after" mean by integrating the equation over the measured P(t) from the "before" mean, and weight each pair by its test errors. Identifying all three needs pairs at more than one pressure: maintain (~20 psi), recharge (~50 psi) and storage (0). Until they are fitted, the app reports tests only (e.g. "last test: 96% of new").
- Temperature: rebound rises with ball temperature. Each test is corrected to 20 °C with a coefficient learned from the tests, or held at 0 (and flagged) until the temperatures recorded vary enough to fit it.
- Each new test resets the batch state to the measured mean, so model error does not carry over between sessions.
- Not modelled yet: rubber ageing and felt wear (a slow drift of a). Residuals that trend downward over a batch's life would show them.

**Share passing.** Per-ball rebounds are taken as Normal(predicted batch mean, between-ball sd from the latest test), and the share inside each band is computed from that. The batch mean itself is uncertain by the fit error, so this is reported as an approximate range.

**Projections:** time until the batch mean enters the like-new or ITF band, and time until it would leave the band at the top, at the current canister pressure.

**ITF from a non-ITF setup.** Rebound is not proportional to drop height, and other surfaces absorb energy, so there is no reliable formula. Instead the user does one conversion session: about 10 balls ranging from new to flat, each dropped at the user's setup and at 254 cm onto concrete, on the same day. A straight line fitted to those pairs maps the user's rebounds to ITF rebounds. Without it, ITF predictions show as "not calibrated".

**Mass-balance cross-check** (needs canister volume and ball count). The canister pressure drop not explained by the leak gives ΔP_ball (§6.1); the bounce tests give ΔB / b. If the two disagree consistently, the leak calibration or the volumes are off. Diagnostic only.

### 6.3 Video bounce measurement (Phase 8b.2, laptop web app `canister-video`)

**Goal:** measure every drop of a bounce test from video, instead of reading heights by eye. The video can be a clip recorded on the phone, or live from the laptop's built-in webcam or an external one. Manual entry stays; the two can be mixed.

**Why a laptop tool, not the phone or the Pi.**
- A Pi 3 can't decode minutes of 1080p video in reasonable time, and uploading hundreds of MB over Wi-Fi is slow.
- In a phone browser, frame access depends on playback quirks and is hard to test.
- On the laptop, OpenCV decodes Android's H.264 and HEVC frame by frame, faster than real time, and the analysis is plain Python tested with pytest.
- The only cost is copying the video to the laptop (Quick Share, USB or Google Drive), and with a webcam there's nothing to copy.
- The user works through a **web app served by the laptop** (below): the browser is only the screen, and capture and analysis run in Python. Analysis isn't duplicated in browser JavaScript, which would be a second implementation and hard to test.
- Finished tests go to the Pi through its API.

**Camera placement (both modes).**
- Steady, at least 2–3 m from the drop line, framing the whole drop height and the floor, with the drop path near the middle of the frame. Bright light (less blur at impact); a background that isn't yellow helps.
- A webcam is landscape. To cover a 6 ft drop it needs about 3 m of distance with the camera about 1 m up; a 100 in drop needs about 4 m with the camera about 1.3 m up.
- A phone: portrait, 1× lens (not the ultrawide), 1080p, 60 fps if available. Slow motion works but isn't needed.
- Tilt is corrected by the height calibration below, so a laptop screen leaning back is fine.

**Height marks.** Two marks on the wall or door frame right at the drop line, the ball's path a few cm in front:
- the **drop height** (already used for every test);
- a **mid mark** near typical rebound heights, e.g. 36 in for a 6 ft drop or 50 in for 100 in. It is stored with the setup (`bounce_setups.mid_mark_cm`).

**Dropping.** Each session starts with three short **calibration holds**: hold a ball still for about 2 s resting on the floor, then against the mid mark, then with its bottom at the drop-height mark. After that, release each ball with its bottom at the drop-height mark, in number order, the same number of times each (default 3), and catch it after its first bounce.

**The laptop web app** (`video/app.py`).
- `canister-video` with no arguments starts a small Flask server on the laptop and opens it in the browser. It listens on `http://127.0.0.1:5100`, the laptop only (`--port`). `--pi URL` sets the Pi (default `http://canisters.local:5000`); `--no-browser` skips opening the browser.
- One session at a time. A background worker runs capture and analysis (`pipeline.Analyzer`, the same code as for clips). The page polls `/api/state` a few times a second for status, drops and progress, and shows the preview from `/preview.mjpg`.
- **1. Set up.** The page shows whether the Pi is reachable. Pick the batch and setup from the Pi's lists; drop height and mid mark come from the setup. Drops per ball defaults to 3.
- **2. Source.**
  - **A webcam:** OpenCV can't list camera names, so indices 0–4 are probed and shown with the size each delivers.
  - **A phone clip:** dragged onto the page, or chosen with a file picker.
- **3a. Live.**
  - A preview at ~10–15 fps shows the ball outlined, the calibration rows once set, and each drop's apex line. The analysis itself uses every frame the camera delivers.
  - A prompt guides the user: "Hold the ball on the floor", "…at the mid mark", "…at the drop mark", then "Ball 2, drop 1". Each height is added to a list when the ball is caught.
  - Buttons: **Undo** last drop, **Next ball**, **Recalibrate**, **Finish**. Clicking the ball in the preview teaches its colour, like `--pick`.
  - The app asks the camera for 1280×720 at 60 fps and shows the rate it actually gets. It warns below 25 fps: webcams slow down in dim light, which also blurs the ball.
  - Timestamps come from the capture clock, so a dropped frame doesn't distort the fits.
  - The session is also recorded to `recordings/<time>.mp4` (git-ignored), so it can be re-analysed as a clip. Expect about 100 MB for 3 minutes at 720p.
- **3b. Clip.** The clip is uploaded to the local server, kept in `recordings/`, and analysed with a progress bar, usually in seconds.
- **4. Review**, on the laptop: one row per drop with its picture and any warnings.
  - Each drop can be moved to another ball, rejected, or its height typed over (it is then marked as edited).
  - Per-ball medians update as you go.
  - **Save to Pi** creates the bounce test, `source = video`, with the analysis details and pictures, in one request.
  - If the Pi can't be reached, the session is kept in `recordings/<time>.json` and can be sent from the app later.
- The **command line** stays for scripting and debugging: `canister-video CLIP --drop-height 72in --mid-mark 36in --dry-run [--json] [--debug DIR]`.

**Finding the ball** (`video/detect.py`).
- Each frame is scaled to about 640 px tall.
- Optic-yellow pixels (HSV range) are grouped into blobs, and blobs of the wrong shape are dropped. The ball is picked up as the first yellow blob that **moves**, so a yellow thing sitting still in the background is ignored. From then on it's tracked as the blob nearest where it should be.
- The default yellow range can be replaced by clicking the ball: in the app's preview, or with `--pick` on the command line.
- Per frame: centre, width, and the **bottom edge to a fraction of a pixel**. Down a strip through the ball's centre, in the full-resolution frame, each row's colour is placed between the ball's colour and the background's; the edge is where it crosses halfway.
  - Motion blur smears an edge symmetrically, so the halfway point is where the ball was at mid-exposure. One measure therefore serves a ball at rest and one in flight.
  - The hand holding a ball covers its top, not its bottom.
  - Without this, whole-pixel steps showed up as ~0.7 cm steps in release heights.
- Picks up a ball that was missed for a moment while held still, at the spot it was last seen; otherwise a ball must move before it's picked up.

**Splitting into drops and measuring** (`video/measure.py`). The measurement is a streaming state machine. It takes one observation at a time (time, cx, cy, w) and emits each drop as soon as it's complete, so the live mode and a clip share the same code.
- Calibration holds are found as the first three times the ball sits still for at least 1.5 s.
- Each drop is release (at rest, then accelerating down) → impact (the lowest point, where it reverses) → rise → apex. Later bounces and the catch are ignored until the next release.
- Fit parabolas y(t) to the ball's bottom: one to the fall (release to just before impact), one to the rise (just after impact to just past the apex).
  - **release** = vertex of the fall parabola, or the resting level if the hand hid the start;
  - **floor** = where the fall and rise parabolas meet;
  - **apex** = vertex of the rise parabola.
- **Height calibration.** Along the drop line, image position maps to height by a 1-D projective map: three numbers, which absorb camera tilt and distance. The three calibration holds (floor = 0, mid mark, drop height) fix it exactly.
  - A tilted camera matters: without this, each degree of tilt adds about 0.5 cm of error at a typical apex (5° ≈ 2.4 cm for a 6 ft drop, 10° ≈ 4.9 cm), and laptop screens usually lean back 10–20°.
- **Parabolas are fitted in centimetres, not rows**, when calibrated. Through a tilted camera a fall isn't a parabola in rows; fitting rows put the floor 2 cm off at 15° tilt.
- **Rebound = apex height**, read through the map. Every release is also read through the map. A release more than 2 cm off gets a warning, and the rebound is scaled by drop height / actual release, since rebound is close to proportional to drop height.
- **At the end of the session the drop-mark row is re-anchored** on the median of all releases (the calibration hold plus every drop), and all drops are recomputed. The mark's only reference is where the user holds the ball, so the median release is a better estimate than one hold: a hold 3 cm off would otherwise skew every drop by about 1%. In live mode the numbers shown during the session are provisional, which is why drop pictures don't print the height.
- **No calibration holds** (e.g. an old clip): fall back to the linear self-calibration from each drop's own release and floor, **rebound = drop height × (floor − apex) / (floor − release)** in pixels. This is exact only for a level camera, so the review warns about it.
- Only heights are calibrated; times are used just for the shape of each fit, so slow motion is fine.
- The calibration is per session, so the camera must not move after the holds. If the floor or release positions drift, the tool asks for new holds (live) or warns (clip).
- Drops are assigned to balls in order: drops-per-ball each, starting at ball 1.

**Warnings shown in review** (in the laptop app):
- parabola fit residuals too large;
- too few frames on either side;
- release not seen;
- ball width in pixels more than 15% off what the drop's own scale predicts (6.7 cm across);
- rebound outside 30–80% of the drop height.

**Expected precision**, for a 1080p portrait clip of a 6 ft drop:

| Source of error | Size |
|---|---|
| Timing of the apex, even at 30 fps | under 2 mm, before fitting |
| Picture detail at analysis size | about 0.35 cm per pixel; fitting the apex over many frames gets well below that |
| Release 1 cm off the mark | measured and corrected; uncorrected it would be about 0.5% of the rebound |
| Camera tilt | removed by the three-point calibration; about 0.5 cm per degree without it |
| Lens distortion (webcams especially) | small near the centre of the frame; the mid mark near rebound height keeps it small at the apex |
| By eye, for comparison | about ±2.5–5 cm |

To be confirmed on real clips (§13).

**Stored on the Pi**, only once a reviewed test is saved. The video stays on the laptop.

```sql
CREATE TABLE bounce_video_analyses (
    id           INTEGER PRIMARY KEY,
    test_id      INTEGER NOT NULL UNIQUE REFERENCES bounce_tests(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    video_json   TEXT NOT NULL,          -- source, frames, fps, size, duration, calibration
    params_json  TEXT NOT NULL           -- tool version, colour range, thresholds
);

CREATE TABLE bounce_video_drops (
    id           INTEGER PRIMARY KEY,
    analysis_id  INTEGER NOT NULL REFERENCES bounce_video_analyses(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,       -- order in the clip
    ball_no      INTEGER NOT NULL,       -- after review
    rebound_cm   REAL,                   -- as measured; NULL if it couldn't be
    kept         INTEGER NOT NULL,       -- 0: rejected in review (kept for the record)
    edited_cm    REAL,                   -- the height typed over it in review, if any
    detail_json  TEXT NOT NULL,          -- release/floor/apex rows, frame times, fit residuals, warnings
    image        BLOB                    -- JPEG of the drop with its lines drawn, ~20 KB
);

ALTER TABLE bounce_tests ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';  -- manual | video
ALTER TABLE bounce_setups ADD COLUMN mid_mark_cm REAL;   -- calibration mark between floor and drop height
```

`filename` is the clip, or the saved recording for a live session. The test's drops (`bounce_drops`) are the kept drops at their reviewed heights, so everything built on tests treats video and manual tests alike. The dashboard marks video tests and shows their drop pictures.

**Install** (laptop only): `pip install -e ".[video]"` in the repo's venv. This adds OpenCV and NumPy and the `canister-video` command. The app reuses Flask, which is already a dependency. Calls to the Pi use `urllib` from the standard library.

**Testing.**
- `video/synth.py` renders artificial clips with cv2.VideoWriter: a yellow ball with known physics (including air drag), motion blur, noise, a cluttered background, the hand at release, a second bounce and a catch, calibration holds, a camera tilted up to 20°, and an occasional dropped frame.
  - The analysis must recover each rebound to within 0.5%.
  - The same synthetic frames fed one at a time through the live code path must give the same results.
- The web app is tested with Flask's test client and a **synthetic camera**: a frame source that renders a session in real time, standing in for a webcam. A full session must run through it: holds, drops, undo, finish, review edits, then Save to a fake Pi.
- Real clips go in `samples/` (git-ignored). Each is checked against a frame-by-frame reading by hand, and the results are recorded in §13.

---

## 7. Data model (SQLite)

Enable `PRAGMA journal_mode=WAL;`, `PRAGMA foreign_keys=ON;` and `PRAGMA synchronous=FULL;` (committed rows survive power loss) on every connection. Use a `schema_version` table and simple numbered migrations in `db.py`.

```sql
CREATE TABLE sensors (
    mac          TEXT PRIMARY KEY,
    canister     TEXT,              -- display name, from config
    decoder      TEXT,              -- last decoder that matched
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,     -- updated on every matching advert
    last_rssi    INTEGER
);

CREATE TABLE raw_adverts (
    id           INTEGER PRIMARY KEY,
    ts           REAL NOT NULL,
    mac          TEXT NOT NULL,
    name         TEXT,
    rssi         INTEGER,
    mfr_json     TEXT,              -- {"<company_id_int>": "<hex payload>", ...}
    service_uuids_json TEXT,
    service_data_json  TEXT
);
CREATE INDEX idx_raw_mac_ts ON raw_adverts(mac, ts);

CREATE TABLE readings (
    id               INTEGER PRIMARY KEY,
    ts               REAL NOT NULL,
    mac              TEXT NOT NULL REFERENCES sensors(mac),
    pressure_kpa_abs REAL NOT NULL,
    temp_c           REAL,
    battery_pct      REAL,
    battery_v        REAL,
    flags_json       TEXT,
    decoder          TEXT NOT NULL,
    raw_advert_id    INTEGER REFERENCES raw_adverts(id)
);
CREATE INDEX idx_readings_mac_ts ON readings(mac, ts);

CREATE TABLE events (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    mac       TEXT,                 -- NULL = applies to all canisters
    kind      TEXT NOT NULL,        -- "note", "fill", "vent", "balls_added", ...
    note      TEXT
);
```

**Ball batches and bounce tests** (migration 3, Phase 8b, §6.2). Lengths are stored in cm, the canonical unit; the UI shows inches or cm. Brand, model and setup names compare case-insensitively.

```sql
CREATE TABLE bounce_setups (            -- one per way of testing
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- e.g. "Garage slab, 6 ft"
    drop_cm    REAL NOT NULL,
    is_itf     INTEGER NOT NULL DEFAULT 0,            -- 1: 254 cm onto concrete, ITF method
    note       TEXT
);

CREATE TABLE ball_batches (
    id          INTEGER PRIMARY KEY,
    brand       TEXT NOT NULL COLLATE NOCASE,
    model       TEXT NOT NULL COLLATE NOCASE,
    opened_ts   REAL NOT NULL,
    n_balls     INTEGER NOT NULL,
    sample_size INTEGER NOT NULL,       -- balls numbered 1..sample_size get tested
    reference_override_cm REAL,         -- manual new-ball rebound for this batch, if any
    retired_ts  REAL,
    note        TEXT
);

CREATE TABLE bounce_tests (             -- one session of drops; edited and deleted as a unit
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    batch_id  INTEGER NOT NULL REFERENCES ball_batches(id) ON DELETE CASCADE,
    setup_id  INTEGER NOT NULL REFERENCES bounce_setups(id),
    context   TEXT NOT NULL,            -- new | before | after | after_play | other
    temp_c    REAL,
    note      TEXT
);

CREATE TABLE bounce_drops (
    id         INTEGER PRIMARY KEY,
    test_id    INTEGER NOT NULL REFERENCES bounce_tests(id) ON DELETE CASCADE,
    ball_no    INTEGER NOT NULL,
    rebound_cm REAL NOT NULL
);

CREATE TABLE reference_rebounds (       -- user-entered new-ball rebound per brand/model/setup
    id         INTEGER PRIMARY KEY,
    brand      TEXT NOT NULL COLLATE NOCASE,
    model      TEXT NOT NULL COLLATE NOCASE,
    setup_id   INTEGER NOT NULL REFERENCES bounce_setups(id),
    rebound_cm REAL NOT NULL,
    UNIQUE (brand, model, setup_id)
);

CREATE TABLE setup_conversions (        -- ITF conversion session: one ball, both setups
    id             INTEGER PRIMARY KEY,
    ts             REAL NOT NULL,
    setup_id       INTEGER NOT NULL REFERENCES bounce_setups(id),
    rebound_cm     REAL NOT NULL,       -- at the user's setup
    itf_rebound_cm REAL NOT NULL,
    note           TEXT
);

ALTER TABLE events ADD COLUMN batch_id INTEGER REFERENCES ball_batches(id) ON DELETE SET NULL;
-- balls_added / balls_removed name the batch, so the app knows which batch sat in which canister when
```

A setup can't be deleted once a test, reference value or conversion uses it. Deleting a batch deletes its tests; events that named it keep their note.

**Planned (Phase 8b.2, §6.3):** `bounce_video_analyses`, `bounce_video_drops`, and `bounce_tests.source`.

**Write policy (collector):**
- Store a raw advert when its payload differs from the previous one for that MAC, **or** at most once per `raw_heartbeat_minutes` (default 10) if unchanged.
- Store a reading when the decoded values differ from the previous reading for that MAC, **or** at most once per `reading_heartbeat_minutes` (default 1) if unchanged. This approximates the "once a minute" goal when the sensor broadcasts often, without bloating the DB.
- Always update `sensors.last_seen` / `last_rssi` (batch these; flush at most every 10 s).
- Only store adverts that match an enabled decoder **or** come from a MAC listed in config. In `--discover` mode, store all adverts that have manufacturer data.

Data volume is tiny (thousands of rows per day). No pruning needed.

---

## 8. Configuration

`config.example.toml` (committed). Real `config.toml` is git-ignored.

```toml
[site]
atmospheric_kpa = 101.325
reference_temp_c = 20.0
timezone_hint = "local"          # UI uses browser local time

[collector]
db_path = "data/canisters.db"           # relative to the config file
decoders = ["fbb0_ac00"]                # also available: "br_27a5", "tpms_0100"
raw_heartbeat_minutes = 10
reading_heartbeat_minutes = 1
scanner_restart_if_silent_minutes = 20   # watchdog, see §9
log_level = "INFO"

[web]
host = "0.0.0.0"
port = 5000
stale_after_minutes = 90
default_units = "psi"            # psi | kPa | bar

[trends]                         # see §6 for why these defaults
window_hours = 24
plateau_threshold_psi_per_day = 0.3
plateau_hours = 12
leak_hint_after_hours = 96

# Map sensor MACs to canister names. Unlisted sensors that match a decoder
# still get recorded and appear in the UI as "Unassigned".
[[sensors]]
mac = "AA:BB:CC:DD:EE:01"
canister = "Canister 1"

[[sensors]]
mac = "AA:BB:CC:DD:EE:02"
canister = "Canister 2"
```

Planned for Phase 8 (§6.2):

```toml
[bounce]
like_new_tolerance_pct = 5.0     # like-new band: new-ball mean +- this percentage
itf_rebound_in = [53.0, 58.0]    # [48.0, 53.0] for high-altitude balls
display_units = "in"             # in | cm
```

Bounce setups, batches, reference values and the ITF conversion are data, entered in the dashboard, not config.

Config is loaded by `config.py` into typed dataclasses with validation and clear error messages. Both processes accept `--config PATH`.

---

## 9. Components

### 9.1 Collector (`src/canister_monitor/collector.py`)

- `python -m canister_monitor.collector [--config PATH] [--db PATH] [--discover] [--duration SECONDS] [--simulate ...]` (also installed as `canister-collector`).
- Structure: a synchronous `Pipeline` (advert → rows, no asyncio/bleak) and an async `Collector` (scanner, queue, flush, watchdog). Scanners come from a factory taking a `submit(Advert)` callback, so bleak, the simulator and test fakes are interchangeable.
- Uses `BleakScanner(detection_callback=...)` in active scan mode (default).
- Callback converts bleak objects to `Advert`, then runs enabled decoders in order; first match wins.
- DB writes happen in the asyncio loop via a small queue; never block inside the bleak callback.
- **Watchdog:** if no matching adverts arrive for `scanner_restart_if_silent_minutes`, stop and restart the scanner. If restart fails 3 times, exit non-zero so systemd restarts the process.
- Handle SIGTERM/SIGINT cleanly: stop scanner, flush pending writes, close DB.
- Log one INFO line per stored reading: canister/MAC, psi gauge, °C, RSSI.
- BlueZ note: Linux may not deliver every repeated identical advert; RSSI changes usually still trigger callbacks. Logic must not depend on receiving duplicates.
- Windows note (dev only): bleak's WinRT backend reports "device out of range" as an advert with RSSI −127 repeating the last payload; the collector drops these.

### 9.2 Simulator (`src/canister_monitor/simulate.py`)

Required so agents can develop and test **without BLE hardware**.

- `--simulate` makes the collector feed synthetic `Advert`s (not bleak) into the same pipeline.
- Generates 4 virtual sensors in format `fbb0_ac00` (the real B-Qtech format, §5.6), each with: a starting pressure (e.g. 25–30 psi gauge), exponential decay toward an equilibrium, a sinusoidal daily temperature cycle (±5 °C) that modulates pressure per the ideal gas law, small noise, and broadcasts on change (a 3-packet burst) plus a heartbeat every 4.5–6.5 min, and silence at 0 psi — as observed in Phase 0. Changes are detected on the smooth physical value and noise is added only to the transmitted measurement; per-step noise made values flicker across rounding boundaries (~2,800 readings/sensor/day instead of ~280).
- `--speed N` runs simulated time N× faster (timestamps advance accordingly) so multi-day curves can be generated in minutes.
- `--start-days-ago D` starts the simulated clock D days back; it fast-forwards at `--speed` and continues in real time once it reaches now, so the dashboard sees realistic history. E.g. `--start-days-ago 3 --speed 2000` (~2 min).
- Simulated data goes to a separate database (`<db_path stem>-sim.db`, e.g. `data/canisters-sim.db`) unless `--db` is given, so it never mixes with real readings. Simulated canisters are named "Sim canister 1–4" (4 leaks).
- One simulated sensor should have a slow leak (decline that never plateaus) to exercise trend logic.

### 9.3 Web (`src/canister_monitor/web/`)

- `python -m canister_monitor.web [--config PATH] [--db PATH] [--simulate]` runs waitress (also installed as `canister-web`). `--simulate` serves the simulator's `-sim.db`.
- Flask app factory `create_app(config)` for testability.

**API** (all JSON unless noted; timestamps as UTC epoch seconds):

| Method & path | Purpose |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /api/canisters` | One entry per sensor: mac, canister name, assigned?, latest reading (abs/gauge/compensated kPa, temp, battery), last_seen, rssi, stale (bool), trend (Phase 7). Configured sensors come first (config order), including ones never heard yet; then unassigned ones. Also returns `now` and `settings` (atmospheric, reference temp, stale_after_s, default_units) for the UI. Config names override names stored at collection time |
| `GET /api/readings?mac=&from=&to=&max_points=` | Time series for one or all sensors, **columnar per sensor** (uPlot-friendly): `{series: [{mac, canister, ts[], gauge_kpa[], compensated_gauge_kpa[], temp_c[], count[]}]}`. Server downsamples (bucket average of abs pressure and temp, then compensated) to `max_points` per sensor (default 2000, max 20000) |
| `GET /api/events?from=&to=` | Annotations |
| `POST /api/events` | Create annotation `{ts?, mac?, kind, note}`; `ts` defaults to now |
| `DELETE /api/events/<id>` | Remove annotation |
| `GET /api/export.csv?from=&to=&mac=` | CSV (every reading, no downsampling): `local_time,utc_epoch,canister,mac,gauge_psi,abs_psi,compensated_gauge_psi,temp_c,battery_pct,battery_v` |
| `GET /healthz` | `{"ok": true, "db": "...", "newest_reading_age_s": N}` |

Validate query params; return 400 with `{"error": "..."}` on bad input (404 for a missing event). Event `kind` is a short lowercase word (`note`, `fill`, `vent`, `balls_added`, …).

### 9.4 Dashboard (`templates/index.html`, `static/app.js`, `static/style.css`)

Plain HTML/CSS/vanilla JS. No build step. uPlot vendored at `static/vendor/uplot/` (JS + CSS, pinned version, license file included).

Layout:
1. **Header:** title, units selector (psi / kPa / bar), temperature units (°F / °C; defaults to °F for en-US browsers, since the vendor app shows °F), "Raw vs temperature-compensated" toggle, auto-refresh indicator.
2. **Canister cards:** one per sensor. Large current gauge pressure, compensated pressure beneath, temperature, battery, RSSI, "updated 4 min ago". Card turns amber when stale. Unassigned sensors show their MAC and a hint to add them to `config.toml`.
3. **Chart:** all canisters overlaid, one line each. Time range buttons: 6 h, 24 h, 3 d, 7 d, All. Lines drawn as steps (value holds until next reading). Event annotations drawn as vertical markers with tooltips.
4. **Temperature chart:** smaller chart below, same time axis, so temperature-driven swings are obvious.
5. **Events panel:** list of annotations plus a small form to add one (canister dropdown incl. "All", kind, note).
6. **Export link** for the current time range.

Behavior: poll `/api/canisters` every 30 s and `/api/readings` every 60 s; refreshes call `setData` on existing charts so a zoom isn't reset each minute. "Updated X ago" uses the server's clock (skew measured on each poll), not the phone's. Persist units/temperature units/toggle/range choices in `localStorage`. Must work on a phone screen. Respect `prefers-color-scheme` for dark mode.

### 9.5 Tools (`tools/`)

| Script | Purpose |
|---|---|
| `scan.py` | Print every advert with manufacturer data: MAC, name, RSSI, company ID, payload hex, service UUIDs. Used in Phase 0 |
| `capture.py` | Like scan, but filtered to given MACs and writes JSON-lines fixtures to `tests/fixtures/captured/` with an optional `--label "30 psi gauge"` |
| `redecode.py` | Re-run current decoders over `raw_adverts` and rebuild `readings` (with a `--dry-run` diff summary) |
| `leak_sim.py` | Monte Carlo check of leak calibration (§6.1): simulate a leak-test protocol many times and report how well the fit recovers the truth |

---

## 10. Repository layout

```
.
├── PLAN.md                       # this file
├── README.md                     # quickstart for humans
├── pyproject.toml                # deps, ruff, pytest config, console scripts
├── config.example.toml
├── .gitignore                    # config.toml, data/, .venv/, __pycache__/
├── src/canister_monitor/
│   ├── __init__.py
│   ├── config.py
│   ├── db.py                     # connect(), migrations, queries
│   ├── write_policy.py           # raw/reading dedupe + heartbeat rules (§7)
│   ├── compensation.py
│   ├── trends.py                 # Phase 7
│   ├── leak.py                   # Phase 8
│   ├── bounce.py                 # Phase 8 (§6.2)
│   ├── video/                    # Phase 8b.2, laptop only (§6.3)
│   │   ├── detect.py             # find the ball in each frame
│   │   ├── measure.py            # split into drops, fit parabolas, rebound heights
│   │   ├── synth.py              # artificial test clips
│   │   ├── pipeline.py           # Analyzer: frames in, numbered drops and pictures out
│   │   ├── app.py                # the laptop web app (Flask): sessions, preview, review
│   │   ├── templates/, static/   # its page
│   │   └── cli.py                # canister-video: start the app, or analyze a clip
│   ├── collector.py
│   ├── simulate.py
│   ├── decoders/
│   │   ├── __init__.py           # registry
│   │   ├── base.py
│   │   ├── fbb0_ac00.py          # B-Qtech sensors (§5.6)
│   │   ├── br_27a5.py
│   │   └── tpms_0100.py
│   └── web/
│       ├── __init__.py           # create_app
│       ├── __main__.py           # waitress entrypoint
│       ├── api.py
│       ├── bounce_api.py         # Phase 8 (§6.2)
│       ├── templates/index.html
│       └── static/{app.js,style.css,vendor/uplot/...}
├── tools/{scan.py,capture.py,redecode.py,leak_sim.py}
├── tests/
│   ├── fixtures/{br_27a5.json,tpms_0100.json,captured/}
│   ├── test_decoders.py
│   ├── test_compensation.py
│   ├── test_config.py
│   ├── test_db.py
│   ├── test_write_policy.py
│   ├── test_collector_pipeline.py
│   ├── test_simulate.py
│   ├── test_trends.py
│   └── test_web.py
└── deploy/
    ├── install.sh
    ├── canister-collector.service
    └── canister-web.service
```

---

## 11. Phased implementation

Each phase lists tasks and **acceptance criteria**. A phase is done only when all criteria pass. Phases 1–5 must be completable without BLE hardware.

### Phase 0 — Hardware bring-up and protocol discovery *(human-in-the-loop)*

Tasks:
1. Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager; preset hostname `canisters`, Wi-Fi (prefer 5 GHz) or use Ethernet, SSH, username.
2. `sudo apt update && sudo apt full-upgrade -y && sudo apt install -y git python3-venv`
3. Clone repo; `python3 -m venv .venv && .venv/bin/pip install bleak`.
4. Write and run `tools/scan.py` (can be done before other phases). Screw sensors onto canisters, vary pressure, and identify the sensors' MACs and format.
5. Use `tools/capture.py` to record fixtures at ≥2 known pressures per sensor, noting the gauge reading and the vendor app's displayed values.
6. Update §5 of this plan with confirmed format details and fill in the Open Questions (§13).

Acceptance: sensor MACs known; format identified (or a new byte table documented); ≥2 labeled captured fixtures committed.

### Phase 1 — Scaffold, decoders, compensation

Tasks: `pyproject.toml`, package layout, ruff + pytest config, `config.py` with example config, `decoders/` with all three formats (`fbb0_ac00`, `br_27a5`, `tpms_0100`), `compensation.py`, tests.

Acceptance:
- `ruff check .` and `pytest` pass.
- `br_27a5` decodes both fixtures in §5.3 within ±0.1 psi and exact temp/voltage.
- `fbb0_ac00` decodes every captured fixture in `tests/fixtures/captured/` to the app's psi (±0.05) and °C.
- `tpms_0100` decodes synthetic fixtures; malformed payloads (wrong length, garbage) return `None` without raising, for all decoders.
- Compensation tests cover: identity at reference temp, known hand-calculated values, missing temperature, gauge↔absolute round-trip.

### Phase 2 — Storage layer

Tasks: `db.py` with WAL setup, migrations, and functions: `upsert_sensor_seen`, `insert_raw_advert`, `insert_reading`, `latest_readings`, `readings_between` (with downsampling), events CRUD.

Acceptance: tests use a temp DB; migrations are idempotent; heartbeat/dedupe write policy from §7 is tested with fake clocks; concurrent reader during writes works (WAL).

### Phase 3 — Collector and simulator

Tasks: `collector.py` pipeline, watchdog, graceful shutdown, `--discover`, `simulate.py` with `--speed`.

Acceptance:
- `python -m canister_monitor.collector --simulate --speed 600` produces a multi-day dataset in a few minutes.
- Pipeline test feeds a scripted list of `Advert`s and asserts the exact rows written.
- Watchdog logic tested with an injected fake scanner and clock.
- On real hardware (when available): readings appear for all assigned sensors within 10 minutes of start.

### Phase 4 — Web API

Tasks: Flask app factory, endpoints in §9.3, CSV export, input validation.

Acceptance: `test_web.py` covers every endpoint using Flask's test client against a seeded DB, including downsampling, staleness flag, unit-neutral (kPa) JSON, CSV headers, and 400 on bad params.

### Phase 5 — Dashboard

Tasks: HTML/CSS/JS per §9.4, vendored uPlot.

Acceptance (manual, against simulator data):
- Cards, both charts, range buttons, unit selector, compensated toggle, events add/delete, and export all work.
- Page loads with no network access beyond the Pi (check browser dev tools: no external requests).
- Usable on a ~375 px wide phone screen and in dark mode.

### Phase 6 — Deployment

Tasks:
- `deploy/install.sh`: creates venv at `/opt/canister-monitor/.venv`, installs package, copies config if missing, creates `data/`, installs and enables both systemd units. Idempotent.
- `canister-collector.service`: `After=bluetooth.target time-sync.target`, `Wants=time-sync.target`, `Restart=always`, `RestartSec=10`. Script enables `systemd-time-wait-sync.service` so readings aren't timestamped before NTP sync (the Pi has no RTC).
- `canister-web.service`: `Restart=always`.
- Both run as the normal user (not root); BlueZ access via D-Bus works for that user on Pi OS. Document the fix if it doesn't (add user to `bluetooth` group).
- README quickstart: install, edit config, view at `http://canisters.local:5000`.

Acceptance: after a reboot, both services start, dashboard is reachable from another LAN device, and data collection resumes without intervention. `journalctl -u canister-collector` shows readings.

As built: `/opt/canister-monitor` holds `.venv/`, `config.toml` (copied once, never overwritten, validated before services start) and `data/`. The units in `deploy/` are templates (`@USER@`, `@APP_DIR@`) rendered by `install.sh`; the service user defaults to `$SUDO_USER`. Bluetooth access: the collector unit uses `SupplementaryGroups=bluetooth` (no re-login needed) and the installer also adds the user to `bluetooth` for running `tools/scan.py` by hand. Light hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, `ProtectHome=read-only`. The package is built from a temporary copy so nothing in the clone is written as root.

**Status (2026-10-08): written but not yet run on hardware** — only `bash -n` checked. Acceptance is pending the Pi's SD card; expect to fix small issues on first install.

### Phase 7 — Analysis features *(after core works)*

Tasks: `trends.py` (slope, plateau detection per §6), trend shown on cards ("−0.8 psi/day, still dropping" / "levelled off"), optional leak hint, per-canister "session" start via a `fill` event so charts can show "time since fill".

Acceptance: simulator's leaking sensor is flagged; plateaued sensors are labeled as such; unit tests for slope math.

As built: `trends.py` is pure (no DB/web). `/api/canisters` returns `trend: {status, slope_kpa_per_day, levelled_since, session_start, session_source, window_s}`. Cards show e.g. "▼ 0.83 psi/day · still dropping", "✓ Levelled off", or (red) "still dropping 4.5 days after fill — check for a leak", plus "Filled N days ago". The chart's **Since fill** range starts at the most recent fill note. Verified on 20 simulator seeds × 7 days: 60/60 healthy canisters levelled off, 20/20 leaks flagged.

### Phase 8 — Ball bounce predictions *(in progress)*

Goal per §6.2: predict whether each batch of balls rebounds like new and within the ITF band, from canister pressure and occasional bounce tests. Leak calibration (§6.1) feeds the mass-balance cross-check.

Done (8a, leak calibration): `leak.py` (session fit, calibration across sessions), `leak_test` event kind (dashboard menu; counts as a fill for trends), `db.leak_test_spans`, `simulate.leak_test_readings`, `tools/leak_sim.py`, tests.

Next, in order. Data collection comes first because it takes the longest:

- **8b — Batches and bounce tests** *(done)*. Tables in §7 (migration 3), and dashboard forms to:
  - create a batch (brand, model, size, numbered sample);
  - define a test setup;
  - enter a bounce test (3 drops per sampled ball, with the canister sensor's temperature pre-filled);
  - enter reference rebounds and ITF conversion pairs.

  `balls_added` / `balls_removed` take a batch. Bounce data goes in the CSV export.

  Acceptance: a test can be entered on a phone in under a minute per ball, and edited or deleted.

  As built: `bounce.py` (per-ball medians, test summary), `web/bounce_api.py`:
  - `GET /api/bounce` returns everything at once: setups, batches with their tests and summaries, reference values, conversions.
  - CRUD under `/api/bounce/{setups,batches,tests,references,conversions}`, and `GET /api/bounce.csv` with one row per drop.
  - `POST /api/events` takes `batch_id`.
  - Rebounds above the setup's drop height are rejected as probably the wrong units, by both server and browser.

  Dashboard: a **Balls** panel with batch cards (location from the latest batch event, latest test, tests table), and dialogs for batch and test entry.
  - The test grid has one row per numbered ball and three drop boxes. Enter moves to the next box. Medians and the batch average update as you type.
  - Defaults: the stage is guessed (new / before / after); room temperature is pre-filled from the batch's canister sensor.
  - Unsaved drops ask before closing. Units switch between in and cm.

  Checked on desktop and at 375 px phone width.
- **8b.2 — Video measurement** (§6.3), in order:
  1. *(done)* Analysis core (`video/detect.py`; `video/measure.py` as a streaming state machine with the three-point calibration), the synthetic clip generator, and `canister-video CLIP --dry-run` printing results. No Pi needed.

     As built:
     - `video/pipeline.py` (`Analyzer`: frames in, numbered drops and pictures out; shared by clips and the coming live mode) and `video/cli.py`. Flags: `--drop-height`, `--mid-mark` (lengths with units, e.g. `72in`), `--drops-per-ball`, `--pick`, `--json`, `--debug DIR` (per-frame trace, result JSON, a picture per drop).
     - `python -m canister_monitor.video.synth OUT.mp4 --tilt 12 [--landscape]` writes a test clip and its true heights.
     - `tests/test_video.py` (19 tests, skipped without OpenCV) covers: calibration maths; the state machine on exact positions (level and 15° tilt); second bounces ignored; off-mark releases corrected and flagged; the uncalibrated fallback; rendered clips in portrait and landscape with a yellow distractor; the live path with ~6% of frames dropped; the CLI.
     - Results on rendered clips (ball physics with air drag, blur, noise, hand at holds): 10 random setups (tilt −6° to 15°, 30/60 fps, portrait and landscape) found 60/60 drops and calibrated every session. Worst error 0.26% of the rebound at 640 px or more, 0.74% at 640×360 where the ball is only 8 px across (0.19% for the same setup at 1280×720), against the 0.5% target. About 3 ms per frame on the laptop.
     - Without calibration holds, a level camera gives ~0.1%, but 10° of tilt gives ~4.5%, with a warning.
  2. **The laptop web app** (`video/app.py`): the session worker, camera probing and capture with recording, clip upload with progress, the MJPEG preview, live state and controls, and the review screen.
     - Save to Pi is disabled until step 5; review results can be downloaded as JSON meanwhile.
     - Tests use Flask's test client and a synthetic camera.
  3. Tune on real footage: the user's phone clips in `samples/` and webcam sessions. Record the results in §13.
  4. Pi side:
     - migration for the video tables, `bounce_tests.source` and `bounce_setups.mid_mark_cm` (editable in the setup form);
     - an API endpoint that accepts a reviewed video test (the test, analysis details and pictures) in one request, and one that serves drop pictures;
     - dashboard: video tests marked, pictures in the tests table, `source` in the CSV.
  5. Save to Pi from the app, keeping the session locally to send later if the Pi is offline; README section.

  Acceptance:
  - synthetic clips are measured to within 0.5%, including tilted ones;
  - real clips and webcam sessions agree with a frame-by-frame reading by hand to within ±1 cm;
  - a 10-ball test takes under 5 minutes, from copying the clip or finishing the live session to saving the test;
  - the app is usable on the laptop's own screen, and its preview stays at 10 fps or more while analysis keeps up with the camera.
- **8c — Like-new from tests.** Resolve the new-ball reference (own → same brand/model → manual, with the source shown). Show each test as a share of new and the share of sampled balls in the like-new band. A batch chart shows tests as points with the band.

  Acceptance: unit tests for reference resolution and band shares.
- **8d — Uptake model.** Fit k, a, b (and the temperature coefficient) per brand/model/setup. Predict the batch mean between tests from measured canister pressure (0 when out). Show the share of the batch passing, entry and overshoot times, and the prediction line on the batch chart.

  First extend the simulator with bounce data (balls with known k, a, b, test noise, ball-to-ball spread) and a `tools/bounce_sim.py` that checks the fit recovers them. It should also report how many sessions, and at which pressures, are needed before predictions are trustworthy.

  Acceptance: the simulator truth is recovered within its stated errors; predictions are hidden until the fit is identified.
- **8e — ITF rebound.** Use ITF setups directly, or map a non-ITF setup through the conversion fit. Correct to 20 °C. Show "not calibrated" otherwise.
- **8f — Mass-balance cross-check.** Per-canister config for internal volume. Ball count comes from the batch events, and ball volumes default to ~117 / 157 cm³ (inner / outer). Add `/api/leak-calibration`, with a panel showing each leak test's rate and error bars, and compare ΔP_ball from mass balance with ΔB / b.

---

## 12. Possible future work (not in scope now)

- MQTT publish / Home Assistant discovery.
- Alerts (e.g. ntfy or email) when pressure drops below a threshold or a sensor goes stale.
- ESP32 BLE "repeater" if the canisters are out of the Pi's radio range.
- Ambient barometric sensor (e.g. BME280 on the Pi's I²C) to replace the fixed `atmospheric_kpa`. Also removes the weather term that limits leak tests (§6.1); then set the leak fit's `weather_sd_kpa` to 0.

---

## 13. Open questions (update as answered)

| # | Question | Answer |
|---|---|---|
| 1 | Which advertisement format do the B-Qtech sensors use? | A new format, "fbb0_ac00" — see §5.6 (confirmed for sensor 1, 2026-10-08) |
| 2 | Do they report gauge or absolute pressure? | Gauge (byte 3 reads 0 when unscrewed) |
| 3 | How often do they broadcast when pressure is stable? | Bursts every ~4.5–6.5 min (≈30 min observed on Windows); every ~9 s briefly after a change; silent at 0 psi. Re-measure on the Pi over a few hours |
| 4 | MACs and canister names | Sensor 1: `30:94:A8:11:11:11`. Others TBD (guess: `…:22:22:22` etc.) |
| 5 | Is the temperature byte signed (below 0 °C behavior)? | _TBD, low priority_ |
| 6 | Local atmospheric pressure to configure | _TBD (default 101.325 kPa)_ |
| 7 | Does video detection work on real footage (the user's phone and webcams, lighting, floor, background)? Precision against a frame-by-frame reading by hand? Which webcams reach 25+ fps in the test spot's light? | _TBD: needs sample clips in `samples/` and webcam sessions (§6.3)_ |

---

## 14. Rules for coding agents

1. **Follow the phases in order.** Don't start a phase until the previous one's acceptance criteria pass.
2. **Never require BLE hardware for tests.** Use fixtures, fake scanners, and the simulator.
3. **Store raw before decoding.** Never discard raw adverts for convenience.
4. **Canonical units only in storage** (kPa absolute, °C, UTC epoch). Convert at the edges.
5. **No hardcoded MACs, paths, or ports**; everything comes from config.
6. **No runtime internet dependencies.** Vendor front-end assets with pinned versions and licenses.
7. **Keep dependencies minimal** (bleak, flask, waitress; dev: pytest, ruff).
8. Type hints on all public functions; docstrings on modules and non-obvious functions.
9. Decoders must never raise on bad input.
10. When protocol facts are confirmed or corrected, update §5 and §13 of this file in the same change.
11. Small, focused commits with messages that reference the phase (e.g. `phase-2: add reading heartbeat policy`).

---

## 15. References

- Generic "BR" / 0x27a5 BLE TPMS format and Python examples: https://github.com/andi38/TPMS
- Forum thread with raw "BR" packets and app values (source of §5.3 fixtures): https://forum.arduino.cc/t/ble-tpms-sensors-decoding/1038638
- "TPMS" / 0x0100 format decoding discussion: https://forum.espruino.com/conversations/357349
- ESP32 reverse-engineering of external BLE TPMS using a syringe test rig: https://github.com/rubfrimun/ESP32_TPMS_BLE
- bleak documentation: https://bleak.readthedocs.io/
- uPlot: https://github.com/leeoniya/uPlot
