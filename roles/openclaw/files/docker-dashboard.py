#!/usr/bin/env python3
"""
⚙️ S1 Docker Dashboard — Full screen, modern style
"""

import subprocess
import time
import os
import json
import glob
from datetime import datetime

# ── ANSI ──────────────────────────────────────────────────────────────────────
RESET     = "\033[0m"
BOLD      = "\033[1m"
DIM       = "\033[2m"
BG_BAR    = "\033[48;5;236m"

FG_CYAN   = "\033[38;5;87m"
FG_GREEN  = "\033[38;5;84m"
FG_YELLOW = "\033[38;5;220m"
FG_RED    = "\033[38;5;203m"
FG_ORANGE = "\033[38;5;214m"
FG_WHITE  = "\033[38;5;252m"
FG_DIM    = "\033[38;5;240m"
FG_ACCENT = "\033[38;5;213m"
FG_PURPLE = "\033[38;5;177m"


def clr(text, *codes):
    return "".join(codes) + text + RESET


def _strip_ansi(s):
    import re
    return re.sub(r'\033\[[0-9;]*m', '', s)


def pad(text, width):
    visible = len(_strip_ansi(text))
    if visible < width:
        return text + " " * (width - visible)
    return text


def truncate(text, max_len):
    if len(text) > max_len:
        return text[:max_len - 1] + "…"
    return text


def bar(pct, width=24):
    filled = min(int(width * pct / 100), width)
    empty  = width - filled
    fc = FG_RED if pct > 85 else (FG_ORANGE if pct > 60 else FG_GREEN)
    return BG_BAR + fc + "█" * filled + FG_DIM + "░" * empty + RESET


# ── Data fetchers ──────────────────────────────────────────────────────────────

def get_disk_stats(mount="/"):
    st = os.statvfs(mount)
    total = st.f_blocks * st.f_frsize / (1024**3)
    free  = st.f_bavail * st.f_frsize / (1024**3)
    used  = total - free
    pct   = used / total * 100 if total > 0 else 0
    return {"total": total, "used": used, "free": free, "pct": pct, "mount": mount}


def get_system_stats():
    with open("/proc/loadavg") as f:
        parts = f.read().split()
    load1, load5, load15 = parts[0], parts[1], parts[2]

    with open("/proc/uptime") as f:
        secs = float(f.read().split()[0])
    days  = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    mins  = int((secs % 3600) // 60)
    uptime = f"{days}d {hours}h {mins}m"

    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0])
    mem_total  = mem["MemTotal"] / 1024
    mem_used   = (mem["MemTotal"] - mem["MemFree"] - mem["Buffers"] - mem["Cached"]) / 1024
    mem_pct    = mem_used / mem_total * 100
    swap_total = mem["SwapTotal"] / 1024
    swap_used  = (mem["SwapTotal"] - mem["SwapFree"]) / 1024
    swap_pct   = swap_used / swap_total * 100 if swap_total > 0 else 0

    def read_cpu():
        with open("/proc/stat") as f:
            line = f.readline()
        v = list(map(int, line.split()[1:]))
        return v[3], sum(v)

    i1, t1 = read_cpu()
    time.sleep(0.3)
    i2, t2 = read_cpu()
    cpu_pct = 100.0 * (1 - (i2 - i1) / max(t2 - t1, 1))

    return {
        "cpu_pct": cpu_pct, "load": f"{load1}  {load5}  {load15}",
        "mem_used": mem_used, "mem_total": mem_total, "mem_pct": mem_pct,
        "swap_used": swap_used, "swap_total": swap_total, "swap_pct": swap_pct,
        "uptime": uptime,
    }


def get_container_stats():
    try:
        r = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"],
            capture_output=True, text=True, timeout=10
        )
        out = {}
        for line in r.stdout.strip().splitlines():
            p = line.split("\t")
            if len(p) >= 4:
                out[p[0]] = (p[1], p[2], p[3])
        return out
    except Exception:
        return {}


def get_containers():
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=10
        )
        out = []
        for line in r.stdout.strip().splitlines():
            p = line.split("\t")
            if len(p) >= 2:
                out.append((p[0], p[1], p[2] if len(p) > 2 else ""))
        return out
    except Exception:
        return []


def get_health_detail(name):
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.State.Health.Status}}\t{{json .State.Health.Log}}",
             name],
            capture_output=True, text=True, timeout=5
        )
        out = r.stdout.strip()
        if not out or "\t" not in out:
            return None
        status, log_json = out.split("\t", 1)
        if status == "healthy":
            return None
        logs = json.loads(log_json)
        if logs:
            last      = logs[-1]
            output    = last.get("Output", "").strip().replace("\n", " ")
            exit_code = last.get("ExitCode", "?")
            return f"exit={exit_code}  {output}"
    except Exception:
        pass
    return None


def get_logs(container, lines=20):
    try:
        r = subprocess.run(
            ["docker", "logs", "--tail", str(lines), "--timestamps", container],
            capture_output=True, text=True, timeout=5
        )
        output = (r.stdout + r.stderr).strip()
        return output.splitlines()[-lines:] if output else []
    except Exception:
        return ["(error reading logs)"]


def get_openclaw_status():
    """Get OpenClaw gateway service status and config info."""
    result = {
        "active": False,
        "state": "unknown",
        "uptime": None,
        "model": None,
        "gateway_port": None,
        "session_count": 0,
        "last_session": None,
    }

    # Service state
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "openclaw-gateway-s1"],
            capture_output=True, text=True, timeout=5
        )
        state = r.stdout.strip()
        result["state"] = state
        result["active"] = (state == "active")
    except Exception:
        result["state"] = "error"

    # Uptime from systemd
    try:
        r = subprocess.run(
            ["systemctl", "show", "openclaw-gateway-s1",
             "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5
        )
        line = r.stdout.strip()
        if "=" in line:
            ts_str = line.split("=", 1)[1].strip()
            if ts_str and ts_str != "n/a":
                try:
                    parts = ts_str.rsplit(" ", 1)
                    ts = datetime.strptime(parts[0], "%a %Y-%m-%d %H:%M:%S")
                    elapsed = datetime.now() - ts
                    secs = int(elapsed.total_seconds())
                    d, rem = divmod(secs, 86400)
                    h, rem = divmod(rem, 3600)
                    m = rem // 60
                    if d > 0:
                        result["uptime"] = f"{d}d {h}h {m}m"
                    elif h > 0:
                        result["uptime"] = f"{h}h {m}m"
                    else:
                        result["uptime"] = f"{m}m"
                except Exception:
                    pass
    except Exception:
        pass

    # Config — model and port
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        result["model"] = (
            cfg.get("agents", {})
               .get("defaults", {})
               .get("model", {})
               .get("primary", None)
        )
        result["gateway_port"] = cfg.get("gateway", {}).get("port", None)
    except Exception:
        pass

    # Session count from session files
    sessions_dir = os.path.expanduser("~/.openclaw/agents/main/sessions")
    try:
        session_files = glob.glob(os.path.join(sessions_dir, "*.json"))
        result["session_count"] = len(session_files)
        if session_files:
            latest = max(session_files, key=os.path.getmtime)
            mtime = os.path.getmtime(latest)
            last_dt = datetime.fromtimestamp(mtime)
            ago_secs = int(time.time() - mtime)
            if ago_secs < 60:
                result["last_session"] = "just now"
            elif ago_secs < 3600:
                result["last_session"] = f"{ago_secs // 60}m ago"
            elif ago_secs < 86400:
                result["last_session"] = f"{ago_secs // 3600}h ago"
            else:
                result["last_session"] = last_dt.strftime("%d %b")
    except Exception:
        pass

    return result


# ── Render ─────────────────────────────────────────────────────────────────────

def render(cols, rows):
    sys_stats = get_system_stats()
    ctrs      = get_containers()
    stats     = get_container_stats()
    oc        = get_openclaw_status()
    now       = datetime.now().strftime("%a %d %b %Y  %H:%M:%S")
    lines     = []
    inner     = cols - 2

    def push(line=""):
        lines.append(line)

    # ── HEADER ──
    title = clr("  ⚙  S1  DOCKER DASHBOARD  ", BOLD, FG_ACCENT)
    ts    = clr(now + "  ", FG_DIM)
    gap   = inner - len(_strip_ansi(title)) - len(_strip_ansi(ts))
    push(clr("╔" + "═" * inner + "╗", FG_DIM))
    push(clr("║", FG_DIM) + title + " " * max(0, gap) + ts + clr("║", FG_DIM))
    push(clr("╠" + "═" * inner + "╣", FG_DIM))

    # ── SYSTEM STATS ──
    disk = get_disk_stats()

    def stat_row(label, b, val, extra=""):
        content = clr(f" {label:<5}", BOLD, FG_WHITE) + " " + b + " " + clr(val, BOLD, FG_CYAN) + clr(f"  {extra}", FG_DIM)
        return clr("║", FG_DIM) + pad(content, inner) + clr("║", FG_DIM)

    push(stat_row("CPU",  bar(sys_stats["cpu_pct"]),  f"{sys_stats['cpu_pct']:5.1f}%",         f"load {sys_stats['load']}   uptime {sys_stats['uptime']}"))
    push(stat_row("MEM",  bar(sys_stats["mem_pct"]),  f"{sys_stats['mem_used']:5.0f} / {sys_stats['mem_total']:.0f} MB",  f"{sys_stats['mem_pct']:.1f}%"))
    push(stat_row("SWAP", bar(sys_stats["swap_pct"]), f"{sys_stats['swap_used']:5.0f} / {sys_stats['swap_total']:.0f} MB", f"{sys_stats['swap_pct']:.1f}%"))
    push(stat_row("DISK", bar(disk["pct"]),           f"{disk['used']:.1f} / {disk['total']:.1f} GB",          f"{disk['pct']:.1f}%  {disk['mount']}"))

    # ── OPENCLAW STATUS ──
    push(clr("╠" + "═" * inner + "╣", FG_DIM))
    push(clr("║", FG_DIM) + pad(clr("  ⚙  OPENCLAW GATEWAY", BOLD, FG_PURPLE), inner) + clr("║", FG_DIM))
    push(clr("╟" + "─" * inner + "╢", FG_DIM))

    if oc["active"]:
        state_icon = clr("● ACTIVE", BOLD, FG_GREEN)
    elif oc["state"] == "inactive":
        state_icon = clr("○ INACTIVE", BOLD, FG_RED)
    elif oc["state"] == "failed":
        state_icon = clr("✗ FAILED", BOLD, FG_RED)
    else:
        state_icon = clr(f"? {oc['state'].upper()}", FG_ORANGE)

    uptime_str = f"  uptime {oc['uptime']}" if oc["uptime"] else ""
    port_str   = f"  port {oc['gateway_port']}" if oc["gateway_port"] else ""
    row1 = clr("  ", FG_DIM) + state_icon + clr(uptime_str + port_str, FG_DIM)
    push(clr("║", FG_DIM) + pad(row1, inner) + clr("║", FG_DIM))

    model_str  = truncate(oc["model"] or "unknown", 50)
    sess_str   = f"  sessions {oc['session_count']}"
    last_str   = f"  last active {oc['last_session']}" if oc["last_session"] else ""
    row2 = clr(f"  model ", FG_DIM) + clr(model_str, FG_CYAN) + clr(sess_str + last_str, FG_DIM)
    push(clr("║", FG_DIM) + pad(row2, inner) + clr("║", FG_DIM))

    # ── CONTAINERS ──
    push(clr("╠" + "═" * inner + "╣", FG_DIM))
    push(clr("║", FG_DIM) + pad(clr("  🐳  CONTAINERS", BOLD, FG_GREEN), inner) + clr("║", FG_DIM))
    push(clr("╟" + "─" * inner + "╢", FG_DIM))

    col_name = 30
    col_stat = 40
    col_cpu  = 8
    col_mem  = inner - col_name - col_stat - col_cpu - 4

    hdr = (
        clr(f"  {'NAME':<{col_name}}", BOLD, FG_DIM) +
        clr(f"{'STATUS':<{col_stat}}", BOLD, FG_DIM) +
        clr(f"{'CPU':>{col_cpu}}", BOLD, FG_DIM) +
        clr(f"  {'MEMORY':<{col_mem}}", BOLD, FG_DIM)
    )
    push(clr("║", FG_DIM) + pad(hdr, inner) + clr("║", FG_DIM))

    for name, status, image in ctrs:
        up      = "up" in status.lower()
        healthy = "healthy" in status.lower()
        exited  = "exited" in status.lower()
        icon    = clr("●", FG_GREEN if healthy else (FG_ORANGE if up else FG_RED))
        scolor  = FG_GREEN if healthy else (FG_ORANGE if up else FG_RED)
        s       = stats.get(name, ("—", "—", "—"))

        row = (
            clr("  ", FG_DIM) + icon + clr(f" {truncate(name, col_name - 2):<{col_name - 2}}", BOLD, FG_WHITE) +
            clr(f"{truncate(status, col_stat):<{col_stat}}", scolor) +
            clr(f"{s[0]:>{col_cpu}}", FG_CYAN) +
            clr(f"  {truncate(s[1], col_mem):<{col_mem}}", FG_DIM)
        )
        push(clr("║", FG_DIM) + pad(row, inner) + clr("║", FG_DIM))

        if not healthy and not exited:
            detail = get_health_detail(name)
            if detail:
                reason = clr(f"     ⚠  {truncate(detail, inner - 8)}", FG_ORANGE)
                push(clr("║", FG_DIM) + pad(reason, inner) + clr("║", FG_DIM))

    # ── MQTT LOGS ──
    push(clr("╠" + "═" * inner + "╣", FG_DIM))
    push(clr("║", FG_DIM) + pad(clr("  📨  MOSQUITTO  MQTT  LOGS", BOLD, FG_YELLOW), inner) + clr("║", FG_DIM))
    push(clr("╟" + "─" * inner + "╢", FG_DIM))

    FOOTER = 3
    log_rows = max(1, rows - len(lines) - FOOTER)
    mqtt_logs = get_logs("mosquitto", log_rows)

    for log in mqtt_logs[:log_rows]:
        parts = log.split(" ", 2)
        if len(parts) == 3 and "T" in parts[0]:
            content = clr(f"  {parts[0][11:19]} ", FG_DIM) + clr(truncate(parts[2], inner - 12), FG_WHITE)
        else:
            content = clr(f"  {truncate(log, inner - 4)}", FG_WHITE)
        push(clr("║", FG_DIM) + pad(content, inner) + clr("║", FG_DIM))

    while len(lines) < rows - FOOTER:
        push(clr("║", FG_DIM) + " " * inner + clr("║", FG_DIM))

    lines[:] = lines[:rows - FOOTER]

    # ── FOOTER ──
    push(clr("╠" + "═" * inner + "╣", FG_DIM))
    push(clr("║", FG_DIM) + pad(clr("  Ctrl+C to exit   refresh 5s  ", FG_DIM), inner) + clr("║", FG_DIM))
    push(clr("╚" + "═" * inner + "╝", FG_DIM))

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("\033[?25l\033[H\033[J", end="", flush=True)
    try:
        while True:
            try:
                sz   = os.get_terminal_size()
                cols = sz.columns
                rows = sz.lines
            except Exception:
                cols, rows = 120, 40

            output = render(cols, rows - 2)
            print("\033[H\033[J", end="")
            print("\n\n" + output, flush=True)
            time.sleep(5)

    except KeyboardInterrupt:
        print("\033[?25h\033[H\033[J")
        print("Dashboard closed.")


if __name__ == "__main__":
    main()
