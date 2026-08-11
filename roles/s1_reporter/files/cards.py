"""Pure Adaptive Card construction for s1_reporter Teams notifications.

No I/O, no third-party imports — safe to unit test without pymssql/matplotlib.
"""


def build_card(body_elements):
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body_elements,
    }


def _text(value, weight="default", size="small", wrap=True):
    return {"type": "TextBlock", "text": str(value), "wrap": wrap, "weight": weight, "size": size}


def build_table(headers, rows):
    def _row(cells, is_header=False):
        return {
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": "stretch",
                 "items": [_text(cell, weight="bolder" if is_header else "default")]}
                for cell in cells
            ],
        }
    elements = [_row(headers, is_header=True)]
    elements.extend(_row(r) for r in rows)
    return elements


def _header(title, style):
    return {
        "type": "Container",
        "style": style,
        "items": [{"type": "TextBlock", "text": title, "size": "large", "weight": "bolder", "wrap": True}],
    }


def _device_age_label(minutes):
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes}m"


def build_offline_alert_card(offline_devices):
    count = len(offline_devices)
    body = [_header(f"🔴 S1 — {count} Device{'s' if count != 1 else ''} Not Reporting", "attention")]
    rows = [
        [d["machine_name"], d["location"], d["customer"], f"{_device_age_label(d['minutes_ago'])} ago"]
        for d in offline_devices
    ]
    body.extend(build_table(["Device", "Location", "Customer", "Overdue By"], rows))
    return build_card(body)


def build_recovery_card(recovered_devices):
    count = len(recovered_devices)
    body = [_header(f"✅ S1 — {count} Device{'s' if count != 1 else ''} Recovered", "good")]
    rows = [
        [d["machine_name"], d["location"], d["customer"], f"{d.get('downtime_minutes', 0)}m downtime"]
        for d in recovered_devices
    ]
    body.extend(build_table(["Device", "Location", "Customer", "Was Down For"], rows))
    return build_card(body)


def build_upload_alert_card(failing_devices):
    count = len(failing_devices)
    body = [_header(f"⚠️ S1 — {count} Device{'s' if count != 1 else ''} Not Uploading", "warning")]
    rows = [
        [d["machine_name"], d["location"], d["customer"], f"{d['total_not_sent']:,} unsent"]
        for d in failing_devices
    ]
    body.extend(build_table(["Device", "Location", "Customer", "Backlog"], rows))
    return build_card(body)


def build_upload_recovery_card(recovered_devices):
    count = len(recovered_devices)
    body = [_header(f"✅ S1 — {count} Device{'s' if count != 1 else ''} Uploading Again", "good")]
    rows = [[d["machine_name"], d["location"], d["customer"]] for d in recovered_devices]
    body.extend(build_table(["Device", "Location", "Customer"], rows))
    return build_card(body)


def build_customer_section_card(customer, days, anomalies, today_table, week_table,
                                 storage_table, chart_urls, kpis=None):
    body = [_header(f"🏢 {customer}", "emphasis")]

    if kpis:
        body.append({
            "type": "FactSet",
            "facts": [
                {"title": "Items Scanned", "value": f"{kpis['total_items']:,}"},
                {"title": "Avg Good Read", "value": f"{kpis['avg_good_read_pct']:.1f}%"},
                {"title": "Active Devices", "value": str(kpis["active_devices"])},
            ],
        })

    if anomalies:
        body.append({
            "type": "Container",
            "style": "attention",
            "items": [_text(f"🚨 {a}") for a in anomalies],
        })

    if today_table["rows"]:
        body.append(_text("📦 Today's Scan Summary", weight="bolder", size="medium"))
        body.extend(build_table(today_table["headers"], today_table["rows"]))

    for label, key in (("📈 Daily Volume", "volume"), ("✅ Good Read % Trend", "goodread"), ("🕐 Hourly Pattern", "hourly")):
        url = chart_urls.get(key)
        if url:
            body.append(_text(f"{label} — Last {days} Days", weight="bolder", size="medium"))
            body.append({"type": "Image", "url": url, "size": "stretch"})

    if week_table["rows"]:
        body.append(_text(f"📊 {days}-Day Summary", weight="bolder", size="medium"))
        body.extend(build_table(week_table["headers"], week_table["rows"]))

    if storage_table["rows"]:
        body.append(_text("💾 Storage Health (C: Drive)", weight="bolder", size="medium"))
        body.extend(build_table(storage_table["headers"], storage_table["rows"]))

    return build_card(body)
