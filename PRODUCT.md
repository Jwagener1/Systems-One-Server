# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Internal Systems One staff (ops/engineering). They monitor the deployed scan-station fleet from a desk or remotely — checking device health, investigating alerts, and verifying that ingestion and services on s1_server are healthy. No customer-facing or kiosk audience is confirmed.

## Product Purpose

Fleet uptime assurance for Systems One's remote monitoring platform. Scan stations and devices at customer sites publish telemetry over MQTT; the platform ingests it into SQL Server and surfaces health through dashboards and reports. Success is detecting offline or degraded devices quickly — before customers notice — and responding fast.

## Operating Context

- This repository is the Ansible deployment for the production server (`s1_server`, reachable via Cloudflare Tunnel at sysone.co.za). Services run as Docker containers; Ansible runs on-box.
- Data path: devices → Mosquitto MQTT broker → `mqtt_ingestor` (Python) → Microsoft SQL Server (`S1_Remote_Monitoring` DB) → dashboards/reports.
- UI surfaces deployed by roles in this repo:
  - `marketing_display` — FastAPI + static dark status page ("S1 Remote Monitoring", index + history views, Chart.js).
  - `scan_fleet_dashboard` — FastAPI dashboard with auth, thresholds, and throughput views.
  - `s1_dashboard` — autologin kiosk-style Docker status display on the server.
  - Grafana — broker/system health dashboards.
- Supporting services: Node-RED, `s1_reporter` (reports, charts, Microsoft Teams notifications), backups, cloudflared.
- Fleet scale at time of writing: ~19 registered devices across multiple customers and locations (from the production database).

## Capabilities and Constraints

- Monitoring covers device online/offline state, telemetry metrics, broker health statistics, and ingestion pipeline health (spool, batch writes, Prometheus metrics on the ingestor).
- Alerting/reporting goes to Microsoft Teams via `s1_reporter`.
- Deployment constraint: all apps are deployed via Ansible roles into Docker on a single production host; UI apps are plain Python (FastAPI) with static HTML/CSS/JS front-ends — no JS build toolchain or SPA framework in use.
- Terminology: "devices" / "scan stations" (e.g. `SNOWSOFT_JHB_SCANSTATION1_...`) identified by customer, location, and station name.

## Brand Commitments

The existing identity is binding for future design work: the Systems One / "S1 Remote Monitoring" name and the incumbent dark status-dashboard look — Inter typeface, dark panels (`#0f1117` background family), and the green/red/amber/blue status palette as used in `roles/marketing_display/files/app/static/`. Refinements should stay within this world.

## Evidence on Hand

- Live production telemetry and history in the `S1_Remote_Monitoring` SQL Server database (real device names, customers, and locations).
- Existing shipped UI: `roles/marketing_display/files/app/static/index.html` and `history.html` are the visual reference for the committed look.
- No marketing claims, testimonials, or benchmarks exist in this repo; future work must not fabricate any.

## Product Principles

1. Truth over polish: dashboards show real fleet state; never decorate at the cost of accurate, current status.
2. Glanceable health first: an operator must read overall fleet health in seconds; detail (per-device, history) is one step deeper.
3. Status color is semantic: green/red/amber carry meaning and are reserved for state, not decoration.
4. Operationally boring by design: plain, dependency-light front-ends that survive unattended on a single server.
