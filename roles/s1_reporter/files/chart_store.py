import os
import time
import uuid


def save_chart(png_bytes, chart_dir, public_base_url):
    """Write a chart PNG and return its public URL.

    Returns None when public_base_url is empty — the file is still written, but there
    is no way to build a URL Teams can resolve, so the caller must omit the image
    rather than emit a broken relative path.
    """
    os.makedirs(chart_dir, exist_ok=True)
    filename = f"{uuid.uuid4()}.png"
    with open(os.path.join(chart_dir, filename), "wb") as f:
        f.write(png_bytes)
    if not public_base_url:
        return None
    return f"{public_base_url.rstrip('/')}/{filename}"


def cleanup_old_charts(chart_dir, retention_days):
    if not os.path.isdir(chart_dir):
        return 0
    cutoff = time.time() - (retention_days * 86400)
    deleted = 0
    for name in os.listdir(chart_dir):
        path = os.path.join(chart_dir, name)
        if os.path.isfile(path) and name.endswith(".png") and os.path.getmtime(path) < cutoff:
            os.remove(path)
            deleted += 1
    return deleted
