import os
import time
import uuid


def save_chart(png_bytes, chart_dir, public_base_url):
    os.makedirs(chart_dir, exist_ok=True)
    filename = f"{uuid.uuid4()}.png"
    with open(os.path.join(chart_dir, filename), "wb") as f:
        f.write(png_bytes)
    return f"{public_base_url.rstrip('/')}/{filename}"


def cleanup_old_charts(chart_dir, retention_days):
    if not os.path.isdir(chart_dir):
        return 0
    cutoff = time.time() - (retention_days * 86400)
    deleted = 0
    for name in os.listdir(chart_dir):
        path = os.path.join(chart_dir, name)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)
            deleted += 1
    return deleted
