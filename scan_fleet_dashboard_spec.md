# Scan Fleet Monitoring Dashboard — Implementation Spec

A monitoring web app for a fleet of barcode-scanning machines across multiple
customers. Machines emit a telemetry packet every **5 minutes**. Some capture
weight, some capture dimensions, some are hand scanners; they all share one
reporting structure (`dbo.device_statistics`). This spec describes three pages
plus a per-device drill-down, and the exact aggregations behind each chart.

Hand this whole document to the coding agent. Where it says **DECIDE**, a
product decision is still open — pick a default and leave a `TODO` comment.

---

## 1. Data model (existing SQL Server schema)

The agent should treat these tables as read-only sources. Do not alter schema.

**`dbo.devices`** — one row per machine.
`id` (PK), `serial_number`, `customer`, `location`, `machine_name`, timestamps.
The unique key is `(customer, location, machine_name)`. Display name in the UI
is `location + " / " + machine_name` (e.g. `DC-Chicago / Line-01`).

**`dbo.device_statistics`** — the core telemetry, one row per packet (~every 5
min per device). Relevant columns:

| Column | Type | Meaning |
|---|---|---|
| `device_id` | int (FK → devices.id) | which machine |
| `ts_datetime` | datetime2 | packet timestamp (use this for all time bucketing) |
| `ts_epoch` | bigint | same instant as epoch; ignore in favour of `ts_datetime` |
| `total_items` | int | items seen in the interval |
| `good_read` | int | successfully read |
| `no_read` | int | failed to read |
| `no_dimension` | int | dimension not captured |
| `no_weight` | int | weight not captured |
| `item_out_of_spec` | int | out of spec |
| `more_than_1_item` | int | multiple items in one read |
| `data_sent` / `not_sent` | int | upstream delivery counts |
| `image_sent` / `image_not_sent` | int | image delivery counts |
| `hand_scanned` | int (nullable) | items processed by hand scanner |
| `complete` | int (nullable) | fully processed items |
| `created_at` | datetime2 | row insert time |

**`dbo.device_storage_status`** — latest storage snapshot per device+drive.
`device_id`, `drive`, `total_gb`, `free_gb`, `used_gb`, `usage_percent`,
`ts_datetime`. Unique on `(device_id, drive)`.

**`dbo.device_os_metrics`** — CPU/memory/temperature snapshot per device.
`cpu_percent`, `mem_usage_pct`, `temp_celsius`, `ts_datetime`, etc.

**`dbo.device_status`** — online/offline state per device: `status`,
`offline_since`, `ts_datetime`.

**`dbo.device_application_status`** — is the app running: `application_running`
(bit), `stopped_since`, `ts_datetime`.

**`dbo.alert_thresholds`** — per-(customer, machine, metric) thresholds:
`metric`, `direction` ('low'/'high'), `warn_value`, `bad_value`, plus baseline
stats. This is the source of the target/threshold lines. If no row exists for a
machine+metric, fall back to a global default (see §6).

**`dbo.customer_login_map`** — maps a login to a `customer`; use it to scope
what a non-admin user can see (row-level filter by `customer`).

---

## 2. Core aggregation rules

All charts are built from `device_statistics` rolled up into time buckets.
Bucket boundaries are computed on `ts_datetime`.

**Daily bucket** (default for performance charts): group by
`CAST(ts_datetime AS date)`. Each daily value is the **sum** of the counts
across that day's ~288 packets.

**Hourly bucket** (throughput): group by `DATEADD(hour, DATEDIFF(hour, 0, ts_datetime), 0)`.
Each hourly value is the sum across that hour's 12 packets.

**Percentage metrics** (performance tab): for each bucket, compute
`metric_pct = 100.0 * SUM(metric) / NULLIF(SUM(total_items), 0)`.
Applies to `good_read`, `no_read`, `no_dimension`, `no_weight`,
`item_out_of_spec`. Guard against divide-by-zero (bucket with 0 total_items →
render as a gap, not 0).

**Good read % headline** = `100.0 * SUM(good_read) / SUM(total_items)` over the
selected range.

**DECIDE — "parcels" definition for Throughput.** Either `total_items`
(everything seen) or `complete`/`good_read` (successfully processed). Default to
`total_items` and expose it as a config constant `THROUGHPUT_METRIC`.

**Missing-packet detection** (used in throughput shift summary): expected
packets per day per device = `288` (1440 min / 5). Missed = `288 − COUNT(packets that day)`.

**Timezone:** `ts_datetime` values — **DECIDE** whether stored UTC or local.
Assume UTC in the DB, convert to the customer's local zone for display. Put the
conversion in one place.

---

## 3. Pages / routes

```
/                         → redirect to /machines/performance
/machines/performance     → Tab 1: Scan performance (small multiples)
/machines/throughput      → Tab 2: Throughput
/machines/health          → Tab 3: Device health (fleet table)  [lower priority]
/machines/:deviceId       → Single-device drill-down (diagnostics)
```

The three `/machines/*` tab routes share one layout: sidebar + top header + a
tab bar + a shared filter row (date range + customer filter). Only the content
below the tab bar changes.

---

## 4. Shared layout / chrome

**Sidebar** (fixed, ~220px, dark navy `#14213d`): logo, nav items (Dashboard,
Machines [active], Scan Log, Alerts, Thresholds, Reports, Admin), user footer.
Active item highlighted `#2154bf`.

**Header** (white, ~76px): page title, search box, user avatar/menu.

**Tab bar**: `Scan performance | Throughput | Device health`. Active tab has a
teal underline `#2f8fa0` and teal text `#0d7969`. Clicking a tab swaps content
without full reload (client-side route).

**Filter row** (right-aligned, on the tab bar line):
- **Date range picker** — presets: Today, Last 7 days, Last 14 days, Last 30
  days, Custom. Drives the `from`/`to` of every query on the page. Default
  Last 30 days on performance, Last 14 days on throughput.
- **Customer filter** — "All customers" + one entry per distinct
  `devices.customer` the user is allowed to see. Filters which machines appear.

**Palette** (use CSS variables):
```
--teal    #2f8fa0   good_read / primary series
--orange  #e07b39   no_read
--plum    #8a4b74   no_dimension
--slate   #5b6b8c   no_weight
--red     #c0392b   item_out_of_spec / below-target / threshold line
--navy    #14213d   sidebar
--blue    #2563eb   interactive accents
--text    #3a3a3a   --muted #8a8e94   --border #dee1e5   --card #ffffff   --page #f3f4f6
```
Series colours are **fixed by metric**, never by position — filtering must not
recolour a line.

Charting library: **DECIDE** (Recharts / ECharts / Chart.js all fine). Spec is
library-agnostic; requirements below are what matters.

---

## 5. Tab 1 — Scan performance (small multiples)

**Layout:** a responsive grid of mini-charts, **one per machine**, up to
**12 per screen (3 columns × 4 rows)**. Beyond 12, **paginate** (page controls
below the grid) — DECIDE paginate vs infinite scroll; default paginate.

**Each mini-chart:**
- Header: machine display name (bold), customer (muted, smaller), and the
  current period good_read % right-aligned. Colour that % **red** when below
  target, teal when at/above.
- Plot: 5 line series, all as **% of total_items**, daily buckets:
  `good_read %` (teal, thicker), `no_read %` (orange), `no_dimension %` (plum),
  `no_weight %` (slate), `item_out_of_spec %` (red).
- **Y-axis is symlog** (symmetric log), `linthresh ≈ 10`, ticks at
  `[0, 2, 5, 10, 50, 100]`. This is the key detail: good_read sits ~90–99%
  while errors sit ~0.5–6%, and a linear axis crushes the error lines. Symlog
  keeps both readable on one plot.
- **Target line:** horizontal dashed red line at the good_read target
  (from `alert_thresholds`, metric `good_read`, else default 93). Label it.
- X-axis: dates, ~4 ticks, formatted `M/D`.
- Small point markers on each series.

**Shared legend** above the grid (not repeated per chart): the 5 metrics + the
target line. Keeps each cell clean.

**Interaction:** clicking anywhere on a mini-chart navigates to
`/machines/:deviceId` (the drill-down). Hover state: subtle border highlight +
"open device" affordance.

**Query (per machine, per day):**
```sql
SELECT
  d.id AS device_id,
  CAST(s.ts_datetime AS date) AS day,
  SUM(s.total_items)   AS total_items,
  SUM(s.good_read)     AS good_read,
  SUM(s.no_read)       AS no_read,
  SUM(s.no_dimension)  AS no_dimension,
  SUM(s.no_weight)     AS no_weight,
  SUM(s.item_out_of_spec) AS item_out_of_spec
FROM dbo.device_statistics s
JOIN dbo.devices d ON d.id = s.device_id
WHERE s.ts_datetime >= @from AND s.ts_datetime < @to
  AND (@customer IS NULL OR d.customer = @customer)
GROUP BY d.id, CAST(s.ts_datetime AS date)
ORDER BY d.id, day;
```
Compute the `_pct` fields in the API layer (or as expressions above). Return
one JSON object per device with its date series; the front end renders one
mini-chart per object.

---

## 6. Thresholds / target lines

For any rate chart, resolve the target like this, in order:
1. Row in `alert_thresholds` matching `(customer, machine_name, metric)`.
2. Row matching `(customer, NULL, metric)` (customer-wide).
3. Global default constant.

`direction = 'low'` means "below `warn_value`/`bad_value` is bad" (good_read).
`direction = 'high'` means "above is bad" (error metrics). Draw the dashed line
at `bad_value` (or `warn_value` — DECIDE; default `bad_value`, the hard limit).
Global default for good_read target = **93%**.

---

## 7. Tab 2 — Throughput

Packets arrive every 5 min (12/hour, 288/day). Throughput = parcels/hour.

**KPI row** (5 cards, computed over selected range):
- Fleet parcels/hour — avg hourly `SUM(THROUGHPUT_METRIC)` across machines
- Parcels today — sum since local midnight
- Peak hour — hour-of-day with max fleet parcels/hour (label like `10:00`)
- Busiest machine — highest avg parcels/hour
- Lowest-throughput machine — lowest avg parcels/hour

**Intraday profile chart** (wide): parcels/hour by hour-of-day (0–23) for the
selected machine (default: fleet total, or a picker). Overlay **today vs
yesterday** (today solid teal + light fill, yesterday dashed grey). X = hour of
day `00:00…22:00`; Y = parcels/hour.

**Shift summary card** (beside the profile): DECIDE shift windows; defaults
Shift 1 `06:00–14:00`, Shift 2 `14:00–22:00`, Overnight `22:00–06:00`. Show
parcels per shift, packets received (`COUNT` per device/day), packets missed
(`288 − count`), and the interval (5 min).

**Per-machine grid** (small multiples, same 3×4 grid as Tab 1): avg
parcels/hour per **day** per machine, area+line. Header shows machine name,
customer, and current avg `/hr`. Weekday/weekend rhythm should be visible.
Clicking → drill-down.

**DECIDE — capacity/target line** on throughput charts (like the 93% line).
Default: none, but leave the hook to add a per-machine capacity from
`alert_thresholds` (metric `throughput`).

**Hourly query:**
```sql
SELECT
  s.device_id,
  DATEADD(hour, DATEDIFF(hour, 0, s.ts_datetime), 0) AS hour_bucket,
  SUM(s.total_items) AS parcels,          -- or good_read/complete per THROUGHPUT_METRIC
  COUNT(*)           AS packet_count
FROM dbo.device_statistics s
JOIN dbo.devices d ON d.id = s.device_id
WHERE s.ts_datetime >= @from AND s.ts_datetime < @to
  AND (@customer IS NULL OR d.customer = @customer)
GROUP BY s.device_id, DATEADD(hour, DATEDIFF(hour, 0, s.ts_datetime), 0)
ORDER BY s.device_id, hour_bucket;
```
For the intraday today-vs-yesterday overlay, additionally group by
`DATEPART(hour, ts_datetime)` filtered to the two dates.

---

## 8. Single-device drill-down (`/machines/:deviceId`)

Reached from any mini-chart. This is the engineer's troubleshooting view.

- **Breadcrumb + title:** `Machines / {customer} / {display name}`, status badge
  (below target / online / offline).
- **Header actions:** Acknowledge alert, Restart agent, Export diagnostics
  (wire to real endpoints or stub with TODO).
- **KPI row:** good_read % (vs threshold), uptime %, CPU %, memory %,
  temperature °C, storage % — latest values from `device_os_metrics`,
  `device_storage_status`, `device_status`.
- **Read outcome breakdown:** the 6 outcome fields as either counts or % over
  the range (reuse the % logic). Good place for the finer detail (hand_scanned,
  more_than_1_item, image_sent/not_sent) that's too noisy for the fleet view.
- **Good read % vs threshold:** the same daily line + dashed target, full size,
  with the breach period shaded.
- **System health:** CPU / memory / temperature mini-trends from
  `device_os_metrics`; storage drives from `device_storage_status`.
- **Recent alerts table:** from your alerting pipeline (or derive from
  threshold breaches). Columns: time, metric, detail, severity.
- **Device info strip:** serial, firmware/OS (`device_os_status`), app status
  (`device_application_status`), network, connected-since.
- **5-minute detail:** because packets are 5-min, the drill-down (and only the
  drill-down) can expose the raw interval resolution when a user zooms into a
  single bad day. Default the drill-down charts to daily; allow switching to
  5-min for a picked day.

---

## 9. API surface (suggested)

Return JSON; do all bucketing/percentages server-side so the client just plots.

```
GET /api/machines?customer=&from=&to=
    → [{ device_id, display_name, customer, current_good_read_pct, below_target }]

GET /api/performance?customer=&from=&to=&bucket=day
    → [{ device_id, display_name, customer, target_pct,
         series: [{ date, total_items, good_read_pct, no_read_pct,
                    no_dimension_pct, no_weight_pct, item_out_of_spec_pct }] }]

GET /api/throughput/kpis?customer=&from=&to=
    → { fleet_parcels_per_hour, parcels_today, peak_hour, peak_value,
        busiest_machine, lowest_machine }

GET /api/throughput/intraday?device_id=&date=&compare=yesterday
    → { today: [{hour, parcels}], yesterday: [{hour, parcels}] }

GET /api/throughput/by-machine?customer=&from=&to=
    → [{ device_id, display_name, customer, avg_per_hour,
         series: [{ date, parcels_per_hour }] }]

GET /api/device/:id/summary
GET /api/device/:id/health?from=&to=
GET /api/device/:id/outcomes?from=&to=&bucket=day|5min
GET /api/device/:id/alerts
```

**Row-level security:** every query filters `devices.customer` by the caller's
allowed customers via `customer_login_map`. Admins see all.

**Performance:** index-friendly range scans on `device_statistics.ts_datetime`
+ `device_id`. A covering index on `(device_id, ts_datetime)` including the
count columns will help; there's already `IX_device_statistics_ts`. For large
ranges, consider a nightly daily-rollup table (`device_statistics_daily`) and
read from it for day-bucketed charts; keep raw 5-min for the drill-down. Cache
KPI responses ~30–60s.

---

## 10. Build order

1. Layout shell: sidebar, header, tab bar, filter row, routing, palette vars.
2. `/api/performance` + Tab 1 small-multiples grid (symlog, target line, shared
   legend, click-through). This is the centrepiece — get it right first.
3. Threshold resolution (§6) wired into the target line.
4. Tab 2 throughput: KPIs, intraday overlay, shift summary, per-machine grid.
5. Drill-down page with KPIs + good_read-vs-threshold + health panels.
6. Pagination, customer filtering, row-level security, caching.
7. Device health tab (fleet table) — lowest priority.

## 11. Acceptance checks

- Error % lines (0.5–6%) and good_read % (~95%) are both clearly readable in a
  single mini-chart (symlog working).
- Series colours stay fixed per metric when filtering/paginating.
- Target line reflects `alert_thresholds`, not a hardcode, when a row exists.
- A machine below target shows its % in red on the card.
- Daily buckets sum ~288 packets; hourly buckets sum ~12; missed-packet count
  is correct on a day with gaps.
- Clicking any mini-chart lands on that device's drill-down.
- Non-admin users only see their mapped customer's machines.
- Date range picker drives every query on the page.
