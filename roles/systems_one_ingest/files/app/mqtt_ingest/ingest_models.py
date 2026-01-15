from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, cast


@dataclass(frozen=True)
class TopicInfo:
    prefix: str
    customer: str
    location: str
    machine: str
    subtype: str


@dataclass(frozen=True)
class IngestEvent:
    prefix: str
    customer: str
    location: str
    machine: str
    subtype: str

    serial_number: str | None
    ts_ms: int | None

    qos: int | None
    retain: bool | None

    payload: Mapping[str, Any] | None

    @property
    def ts_iso(self) -> str | None:
        if self.ts_ms is None:
            return None
        try:
            dt = datetime.fromtimestamp(self.ts_ms / 1000.0, tz=timezone.utc)
        except Exception:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_log_line(self) -> str:
        # Human-friendly one-liner for logs.
        prefix = self.prefix
        loc = f"{self.customer}/{self.location}/{self.machine}"
        retain = " R" if self.retain else ""
        ts = self.ts_iso or "?"
        serial = self.serial_number or "?"

        if self.payload is None:
            return f"{prefix} {loc} {self.subtype}{retain} ts={ts} sn={serial} payload=<unparsed>"

        if self.subtype == "status":
            status = _safe_str(self.payload.get("device_status"))
            os_version = _safe_str(self.payload.get("device_os_version"))
            os_part = (
                f' os="{_short_os(os_version)}"' if os_version else ""
            )
            return (
                f"{prefix} {loc} status{retain} ts={ts} sn={serial} "
                f"state={_short_state(status)}{os_part}"
            )

        if self.subtype == "statistics":
            stats = self.payload.get("statistics")
            if isinstance(stats, Mapping):
                stats_map = cast(Mapping[str, Any], stats)
                total = _safe_int(stats_map.get("total_items"))
                good = _safe_int(stats_map.get("good_reads"))
                no_reads = _safe_int(stats_map.get("no_reads"))
                out_of_spec = _safe_int(stats_map.get("out_of_spec"))
                success = _safe_int(stats_map.get("success"))
                sent = _safe_int(stats_map.get("sent"))
                return (
                    f"{prefix} {loc} statistics{retain} ts={ts} sn={serial} "
                    f"total={_fmt_int(total)} good={_fmt_int(good)} nrd={_fmt_int(no_reads)} "
                    f"oos={_fmt_int(out_of_spec)} ok={_fmt_int(success)} sent={_fmt_int(sent)}"
                )
            return f"{prefix} {loc} statistics{retain} ts={ts} sn={serial} statistics=<invalid>"

        if self.subtype == "storage":
            storage = self.payload.get("storage")
            if isinstance(storage, Mapping):
                storage_map = cast(Mapping[str, Any], storage)
                parts: list[str] = []
                for drive, info in storage_map.items():
                    if not isinstance(info, Mapping):
                        continue
                    info_map = cast(Mapping[str, Any], info)
                    used_pct = _safe_float(info_map.get("used_pct"))
                    used_gb = _safe_float(info_map.get("used_gb"))
                    total_gb = _safe_float(info_map.get("total_gb"))
                    parts.append(
                        f"{drive}: {(_fmt_float(used_pct) + '%') if used_pct is not None else '?'} "
                        f"({(_fmt_float(used_gb) + 'GB') if used_gb is not None else '?'}"
                        f"/{(_fmt_float(total_gb) + 'GB') if total_gb is not None else '?'})"
                    )
                joined = ", ".join(parts) if parts else "<empty>"
                return f"{prefix} {loc} storage{retain} ts={ts} sn={serial} {joined}"
            return f"{prefix} {loc} storage{retain} ts={ts} sn={serial} storage=<invalid>"

        # Unknown subtype: keep it short but show keys.
        keys = ",".join(sorted(self.payload.keys()))
        return f"{prefix} {loc} {self.subtype}{retain} ts={ts} sn={serial} keys=[{keys}]"


def parse_topic(topic: str) -> TopicInfo | None:
    # Expected: systems-one/customer/location/machine/subtype
    parts = [p for p in (topic or "").strip("/").split("/") if p]
    if len(parts) < 5:
        return None
    prefix, customer, location, machine = parts[0], parts[1], parts[2], parts[3]
    subtype = "/".join(parts[4:])
    return TopicInfo(
        prefix=prefix,
        customer=customer,
        location=location,
        machine=machine,
        subtype=subtype,
    )


def parse_payload(payload_bytes: bytes) -> Mapping[str, Any] | None:
    if not payload_bytes:
        return None
    try:
        text = payload_bytes.decode("utf-8", errors="strict")
    except Exception:
        text = payload_bytes.decode("utf-8", errors="replace")

    try:
        obj = json.loads(text)
    except Exception:
        return None

    if isinstance(obj, Mapping):
        return cast(Mapping[str, Any], obj)
    return None


def build_event(
    *,
    topic: str,
    payload_bytes: bytes,
    qos: int | None = None,
    retain: bool | None = None,
) -> IngestEvent | None:
    info = parse_topic(topic)
    if info is None:
        return None

    payload = parse_payload(payload_bytes)
    serial: str | None = None
    ts_ms: int | None = None

    if payload is not None:
        serial = _safe_str(payload.get("serial_number"))
        ts_ms = _safe_int(payload.get("ts"))

    return IngestEvent(
        prefix=info.prefix,
        customer=info.customer,
        location=info.location,
        machine=info.machine,
        subtype=info.subtype,
        serial_number=serial,
        ts_ms=ts_ms,
        qos=qos,
        retain=retain,
        payload=payload,
    )


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except Exception:
        return None


def _fmt_int(value: int | None) -> str:
    return "?" if value is None else str(value)


def _fmt_float(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _squash_ws(value: str) -> str:
    return " ".join(value.split())


def _short_os(value: str) -> str:
    text = _squash_ws(value)
    if text.lower().startswith("microsoft "):
        text = text[len("microsoft ") :]
    return _shorten(text, 48)


def _shorten(value: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(value) <= max_len:
        return value
    if max_len <= 1:
        return value[:max_len]
    return value[: max_len - 1] + "…"


def _short_state(value: str | None) -> str:
    if not value:
        return "?"
    v = _squash_ws(value).lower()
    if v == "online":
        return "ON"
    if v == "offline":
        return "OFF"
    return _shorten(v, 12)
