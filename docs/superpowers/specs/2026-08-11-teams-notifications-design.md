# Replace Email Notifications with Microsoft Teams

**Date:** 2026-08-11
**Status:** Draft — pending user review

## Goal

Replace the `s1_reporter` role's Gmail SMTP email notifications (offline-device alerts, upload-failure alerts, daily reports, monthly reports) with Microsoft Teams messages, posted via a Power Automate Workflow webhook. Full cutover — email is removed once Teams delivery is verified in production.

## Background

`roles/s1_reporter/` is a Dockerized Python service with two scripts that each contain their own duplicate `send_email()` (stdlib `smtplib`/`email.mime`, no shared notifier module, no templating engine — raw f-string HTML):

- `report.py` — daily report (06:00), monthly report (1st @ 06:30), offline-device alerts/recoveries (every 20 min). Daily/monthly reports embed matplotlib chart PNGs as inline CID attachments (daily volume, good-read trend, hourly pattern) plus HTML tables (per-customer anomalies, 7-day/30-day summaries, storage usage).
- `upload_monitor.py` — upload-failure alerts/recoveries (every 20 min, device has `not_sent > 0` on ≥3 consecutive packets while scanning).

All alerting is suppressed on weekends. Credentials live in Ansible Vault (`vault_s1_reporter_smtp_user`, `vault_s1_reporter_smtp_pass`, `vault_s1_reporter_report_to`), non-secret schedule config in `roles/s1_reporter/defaults/main.yml`, injected as container env vars via `roles/s1_reporter/templates/docker-compose.s1_reporter.yml.j2`.

Public services (Grafana, dashboards) are exposed via a single Cloudflare Tunnel (`roles/cloudflared/`, one shared `cloudflare_tunnel_token`). Public hostname routing is configured entirely in the Cloudflare Zero Trust dashboard, not in this repo — there is no local nginx/reverse-proxy role.

## Decisions

1. **Delivery mechanism:** Power Automate Workflow webhook (not the legacy/deprecated Incoming Webhook connector). Confirmed working end-to-end during this design session:
   - Webhook posts to the **Systems-One** team, **Reporting** channel (groupId `5a8f60bc-e705-4bed-99ac-8ae23c332176`, channelId `19:efeC-qWPDOSgvSKHkwkItEoNRuWCXkMTt8XW0Incjeg1@thread.tacv2`).
   - The flow's trigger (`Request`/`TeamsWebhook` kind) requires payloads shaped as:
     ```json
     {
       "type": "message",
       "attachments": [
         {
           "contentType": "application/vnd.microsoft.card.adaptive",
           "content": { "$schema": "...", "type": "AdaptiveCard", "version": "1.4", "body": [ ... ] }
         }
       ]
     }
     ```
   - The flow's action is `PostCardToConversation` (`shared_teams` connector), `messageBody` set from a `Body` variable populated off the trigger's `attachments[0].content` — i.e. it relays whatever Adaptive Card we send.
   - Webhook URL is a bearer credential (`sig=` query param) — stored only in Ansible Vault as `vault_s1_reporter_teams_webhook_url`, never committed in plaintext.

2. **Channel layout:** One channel, one webhook, for all notification types (alerts and reports). Card title/color distinguishes severity (red = alert, green = recovery, blue/neutral = report digest).

3. **Chart images:** Base64/data-URI images in Adaptive Cards are not viable — Teams caps total card size at 28KB (a chart PNG alone typically exceeds this once base64-encoded), and data-URI images are known to silently fail to render on Teams Desktop/Web (only work in the Adaptive Card designer and Teams mobile). Instead:
   - `report.py` continues generating chart PNGs, but writes them to a shared volume instead of emailing them, with unguessable UUID filenames (e.g. `a1b2c3d4-....png`).
   - A new lightweight static-file container (e.g. `nginx:alpine`) is added, mounting that shared volume read-only, attached to the existing `infra` Docker network (same network `cloudflared` uses).
   - A new Cloudflare Tunnel **Public Hostname** is added in the Cloudflare Zero Trust dashboard (manual step, not Ansible-managed, consistent with how Grafana/other services are exposed) pointing at this container — e.g. `charts.<existing-domain>` — routing to it over the shared tunnel.
   - Adaptive Card `Image` elements reference `https://charts.<domain>/<uuid>.png`.
   - A retention/cleanup job (run from the existing `entrypoint.sh` poll loop) deletes chart files older than **14 days** (configurable via a new `s1_reporter_chart_retention_days` default var) — bounds public exposure window and disk usage. UUID filenames mean charts aren't discoverable without the exact link, but are not access-controlled; treat them as effectively public for the retention window.

4. **Cutover:** Full replacement now. Once Teams delivery is verified working in production, remove `vault_s1_reporter_smtp_user`/`_smtp_pass`/`_report_to`, the `smtplib`/`email.mime` code, and the CID inline-attachment logic.

## Architecture

- **New shared module** `roles/s1_reporter/files/teams_notifier.py`: single `post_to_teams(card: dict)` function — POST to `TEAMS_WEBHOOK_URL` with retry/backoff (3 attempts), replacing the duplicate `send_email()` in `report.py` and `upload_monitor.py`.
- **Adaptive Card builders** (in `report.py`/`upload_monitor.py` or a shared `cards.py`): one function per message type — offline alert, offline recovery, upload-failure alert, upload-failure recovery, daily report, monthly report. Each returns the full `{"type": "message", "attachments": [...]}` envelope.
- **New role addition**: static chart-file server (nginx or equivalent) in `roles/s1_reporter/templates/docker-compose.s1_reporter.yml.j2`, sharing a named volume with the `s1_reporter` container.
- **Config**: `TEAMS_WEBHOOK_URL` env var (from `vault_s1_reporter_teams_webhook_url`), `CHART_PUBLIC_BASE_URL` env var (e.g. `https://charts.<domain>`), `s1_reporter_chart_retention_days` default var — all wired the same way current SMTP vars are.

## Message design

- **Offline / upload-failure alerts & recoveries:** compact Adaptive Card — `TextBlock` title (colored via `Container`/`ColumnSet` styling: attention=red for alert, good=green for recovery), device/customer name, condition, timestamp.
- **Daily / monthly reports:** KPI `FactSet` (total items, avg good-read %, active/offline device counts) + a compact `Table` element (per-customer or per-device rows, depending on size) + chart `Image` elements pointing at the hosted PNG URLs. If a report would produce an oversized card (many customers), split into multiple cards posted sequentially rather than one giant payload.

## Error handling

- Webhook POST failure (network error, non-2xx): retry 3x with backoff, then log the failure and the full card JSON to stdout (visible via `docker logs`) so nothing is silently lost. No email fallback (full cutover).
- Chart-hosting container down / volume write failure: report generation continues, card omits the `Image` element and includes a text note that the chart wasn't available, rather than failing the whole report.

## Testing

- Unit tests for Adaptive Card builder functions (data in → correct card JSON out), independent of the network layer.
- Manual verification: trigger a real webhook POST (as already done during this design session) and confirm rendering in the **Reporting** channel before removing email code.

## Open items requiring a decision at implementation time

- Exact subdomain to request for the chart-hosting Cloudflare Public Hostname (e.g. `charts.<domain>`) — needs the existing domain name and for someone with Cloudflare Zero Trust dashboard access to add the route.
- Confirm 14-day chart retention default is acceptable (easy to change via Ansible var, not a hard constraint).
