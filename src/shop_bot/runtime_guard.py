from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shop_bot.errors import BotError, send_critical_alert_nonblocking

logger = logging.getLogger(__name__)

_DEFAULT_RUNTIME_STATE_DIR = Path("/app/data/runtime")
_FALLBACK_RUNTIME_STATE_DIR = Path(".runtime")
_UNLIMITED_CGROUP_VALUES = {"", "max"}


@dataclass(frozen=True)
class MemorySnapshot:
    rss_bytes: int | None = None
    cgroup_current_bytes: int | None = None
    cgroup_limit_bytes: int | None = None
    system_total_bytes: int | None = None

    @property
    def used_bytes(self) -> int | None:
        return self.cgroup_current_bytes or self.rss_bytes

    @property
    def limit_bytes(self) -> int | None:
        if self.cgroup_limit_bytes and self.cgroup_limit_bytes > 0:
            return self.cgroup_limit_bytes
        return self.system_total_bytes

    @property
    def usage_percent(self) -> float | None:
        used = self.used_bytes
        limit = self.limit_bytes
        if not used or not limit or limit <= 0:
            return None
        return used / limit * 100.0

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["used_bytes"] = self.used_bytes
        data["limit_bytes"] = self.limit_bytes
        data["usage_percent"] = self.usage_percent
        return data


def env_flag(name: str, *, default: bool = True) -> bool:
    raw = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def env_positive_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def runtime_state_dir() -> Path:
    raw = str(os.environ.get("RUNTIME_STATE_DIR", "")).strip()
    if raw:
        return Path(raw).expanduser()
    if _DEFAULT_RUNTIME_STATE_DIR.exists() or _DEFAULT_RUNTIME_STATE_DIR.parent.exists():
        return _DEFAULT_RUNTIME_STATE_DIR
    return _FALLBACK_RUNTIME_STATE_DIR


def runtime_marker_path(source: str, *, state_dir: Path | None = None) -> Path:
    normalized = _normalize_source(source)
    return (state_dir or runtime_state_dir()) / f"{normalized}.json"


def _normalize_source(source: str) -> str:
    normalized = str(source or "main").strip().lower()
    return normalized if normalized in {"main", "reserve"} else "main"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Runtime marker read failed path=%s error=%s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def mark_runtime_started(source: str, *, state_dir: Path | None = None) -> dict[str, Any] | None:
    path = runtime_marker_path(source, state_dir=state_dir)
    previous = _read_json(path)
    snapshot = get_memory_snapshot()
    marker = {
        "source": _normalize_source(source),
        "pid": os.getpid(),
        "server": socket.gethostname(),
        "started_at": _now_iso(),
        "last_heartbeat_at": _now_iso(),
        "graceful_shutdown": False,
        "shutdown_requested": False,
        "shutdown_requested_at": None,
        "shutdown_signal": None,
        "last_memory": snapshot.to_dict(),
    }
    try:
        _atomic_write_json(path, marker)
    except OSError as exc:
        logger.warning("Runtime marker write failed path=%s error=%s", path, exc)
    return previous


def mark_runtime_heartbeat(source: str, *, state_dir: Path | None = None) -> None:
    path = runtime_marker_path(source, state_dir=state_dir)
    marker = _read_json(path) or {}
    marker.update(
        {
            "source": _normalize_source(source),
            "pid": os.getpid(),
            "server": socket.gethostname(),
            "last_heartbeat_at": _now_iso(),
            "graceful_shutdown": False,
            "last_memory": get_memory_snapshot().to_dict(),
        }
    )
    try:
        _atomic_write_json(path, marker)
    except OSError as exc:
        logger.warning("Runtime heartbeat write failed path=%s error=%s", path, exc)


def mark_runtime_graceful_shutdown(
    source: str,
    *,
    signal_name: str | None = None,
    state_dir: Path | None = None,
) -> None:
    path = runtime_marker_path(source, state_dir=state_dir)
    marker = _read_json(path) or {}
    marker.update(
        {
            "source": _normalize_source(source),
            "pid": os.getpid(),
            "server": socket.gethostname(),
            "last_heartbeat_at": _now_iso(),
            "graceful_shutdown": True,
            "shutdown_signal": signal_name,
            "shutdown_at": _now_iso(),
            "last_memory": get_memory_snapshot().to_dict(),
        }
    )
    try:
        _atomic_write_json(path, marker)
    except OSError as exc:
        logger.warning("Runtime graceful marker write failed path=%s error=%s", path, exc)


def mark_runtime_shutdown_requested(
    source: str,
    *,
    signal_name: str | None = None,
    state_dir: Path | None = None,
) -> None:
    path = runtime_marker_path(source, state_dir=state_dir)
    marker = _read_json(path) or {}
    marker.update(
        {
            "source": _normalize_source(source),
            "pid": os.getpid(),
            "server": socket.gethostname(),
            "last_heartbeat_at": _now_iso(),
            "graceful_shutdown": False,
            "shutdown_requested": True,
            "shutdown_signal": signal_name,
            "shutdown_requested_at": _now_iso(),
            "last_memory": get_memory_snapshot().to_dict(),
        }
    )
    try:
        _atomic_write_json(path, marker)
    except OSError as exc:
        logger.warning("Runtime shutdown-request marker write failed path=%s error=%s", path, exc)


def previous_exit_was_unclean(previous_marker: dict[str, Any] | None) -> bool:
    if not previous_marker:
        return False
    if bool(previous_marker.get("graceful_shutdown")):
        return False
    if bool(previous_marker.get("shutdown_requested")):
        shutdown_requested_at = _parse_iso_datetime(previous_marker.get("shutdown_requested_at"))
        if shutdown_requested_at is None:
            return True
        grace_seconds = env_positive_int("RUNTIME_SHUTDOWN_REQUEST_GRACE_SECONDS", 300, minimum=0)
        age_seconds = (datetime.now(UTC) - shutdown_requested_at).total_seconds()
        if age_seconds <= grace_seconds:
            return False
    return True


def build_unclean_exit_alert_details(previous_marker: dict[str, Any]) -> str:
    source = str(previous_marker.get("source") or "unknown")
    pid = str(previous_marker.get("pid") or "unknown")
    server = str(previous_marker.get("server") or socket.gethostname())
    last_heartbeat = str(previous_marker.get("last_heartbeat_at") or "unknown")
    started_at = str(previous_marker.get("started_at") or "unknown")
    last_memory = previous_marker.get("last_memory")
    memory_line = ""
    if isinstance(last_memory, dict):
        percent = last_memory.get("usage_percent")
        used = last_memory.get("used_bytes")
        limit = last_memory.get("limit_bytes")
        memory_line = (
            f"\nMemory: used={_format_bytes(_safe_int(used))} "
            f"limit={_format_bytes(_safe_int(limit))} "
            f"usage={_format_percent(percent)}"
        )
    return (
        f"source={source} reason=suspected_oom_kill pid={pid} server={server} "
        f"\nStarted: {started_at}"
        f"\nLast heartbeat: {last_heartbeat}"
        f"{memory_line}"
        "\nПредыдущий процесс завершился без graceful shutdown marker. "
        "Вероятные причины: OOM kill, SIGKILL, crash контейнера или reboot сервера."
    )


def alert_on_unclean_previous_exit(
    previous_marker: dict[str, Any] | None,
    *,
    source: str,
) -> bool:
    if not previous_exit_was_unclean(previous_marker):
        return False
    cooldown = env_positive_int("RUNTIME_UNCLEAN_EXIT_ALERT_COOLDOWN_SECONDS", 300, minimum=0)
    if not _cooldown_allows(source, "unclean-exit", cooldown):
        return False
    send_critical_alert_nonblocking(
        BotError.BOT_PROCESS_UNCLEAN_EXIT,
        details=build_unclean_exit_alert_details(previous_marker or {}),
        respect_cooldown=False,
    )
    _mark_cooldown(source, "unclean-exit")
    return True


async def runtime_guard_loop(source: str) -> None:
    interval = env_positive_int("RUNTIME_HEARTBEAT_INTERVAL_SECONDS", 10, minimum=1)
    while True:
        mark_runtime_heartbeat(source)
        emit_memory_pressure_alert_if_needed(reason="runtime_guard")
        await asyncio.sleep(interval)


def get_memory_snapshot() -> MemorySnapshot:
    rss = _read_proc_status_rss()
    cgroup_current, cgroup_limit = _read_cgroup_memory()
    system_total = _read_meminfo_total()
    return MemorySnapshot(
        rss_bytes=rss,
        cgroup_current_bytes=cgroup_current,
        cgroup_limit_bytes=cgroup_limit,
        system_total_bytes=system_total,
    )


def memory_usage_percent(snapshot: MemorySnapshot | None = None) -> float | None:
    return (snapshot or get_memory_snapshot()).usage_percent


def memory_pressure_level(snapshot: MemorySnapshot | None = None) -> str | None:
    snap = snapshot or get_memory_snapshot()
    percent = snap.usage_percent
    if percent is None:
        return None
    critical = env_positive_int("RUNTIME_MEMORY_CRITICAL_PERCENT", 90, minimum=1)
    warning = env_positive_int("RUNTIME_MEMORY_WARNING_PERCENT", 80, minimum=1)
    if percent >= critical:
        return "critical"
    if percent >= warning:
        return "warning"
    return None


def is_memory_pressure_critical(snapshot: MemorySnapshot | None = None) -> bool:
    return memory_pressure_level(snapshot) == "critical"


def emit_memory_pressure_alert_if_needed(
    *,
    source: str | None = None,
    reason: str,
    snapshot: MemorySnapshot | None = None,
) -> bool:
    if not env_flag("ENABLE_RUNTIME_GUARD", default=True):
        return False
    snap = snapshot or get_memory_snapshot()
    level = memory_pressure_level(snap)
    if level is None:
        return False
    normalized_source = _normalize_source(source or os.environ.get("BOT_SOURCE", "main"))
    cooldown = env_positive_int("RUNTIME_MEMORY_ALERT_COOLDOWN_SECONDS", 900, minimum=0)
    cooldown_kind = f"memory-{level}"
    if not _cooldown_allows(normalized_source, cooldown_kind, cooldown):
        return False
    error = (
        BotError.BOT_MEMORY_PRESSURE_CRITICAL
        if level == "critical"
        else BotError.BOT_MEMORY_PRESSURE_WARNING
    )
    send_critical_alert_nonblocking(
        error,
        details=_build_memory_alert_details(normalized_source, reason, snap),
        respect_cooldown=False,
    )
    _mark_cooldown(normalized_source, cooldown_kind)
    return True


def format_memory_snapshot(snapshot: MemorySnapshot | None = None) -> str:
    snap = snapshot or get_memory_snapshot()
    return (
        f"used={_format_bytes(snap.used_bytes)} "
        f"limit={_format_bytes(snap.limit_bytes)} "
        f"rss={_format_bytes(snap.rss_bytes)} "
        f"usage={_format_percent(snap.usage_percent)}"
    )


def _build_memory_alert_details(source: str, reason: str, snapshot: MemorySnapshot) -> str:
    return (
        f"source={source} reason={reason} "
        f"memory_used={_format_bytes(snapshot.used_bytes)} "
        f"memory_limit={_format_bytes(snapshot.limit_bytes)} "
        f"memory_usage_percent={_format_percent(snapshot.usage_percent)} "
        f"rss={_format_bytes(snapshot.rss_bytes)} "
        f"pid={os.getpid()} server={socket.gethostname()}"
        "\nПроцесс приблизился к лимиту памяти контейнера. "
        "Тяжёлые фоновые операции будут пропущены до снижения memory pressure."
    )


def _read_proc_status_rss(status_path: Path = Path("/proc/self/status")) -> int | None:
    try:
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _read_cgroup_memory() -> tuple[int | None, int | None]:
    current, limit = _read_cgroup_v2_memory()
    if current is not None or limit is not None:
        return current, limit
    return _read_cgroup_v1_memory()


def _read_cgroup_v2_memory() -> tuple[int | None, int | None]:
    path = _cgroup_v2_process_path()
    if path is None:
        return None, None
    base = Path("/sys/fs/cgroup") / path.relative_to("/")
    return _read_int_file(base / "memory.current"), _read_limit_file(base / "memory.max")


def _read_cgroup_v1_memory() -> tuple[int | None, int | None]:
    path = _cgroup_v1_memory_path()
    if path is None:
        return None, None
    base = Path("/sys/fs/cgroup/memory") / path.relative_to("/")
    return _read_int_file(base / "memory.usage_in_bytes"), _read_limit_file(base / "memory.limit_in_bytes")


def _cgroup_v2_process_path(cgroup_path: Path = Path("/proc/self/cgroup")) -> Path | None:
    try:
        for line in cgroup_path.read_text(encoding="utf-8").splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                return Path(parts[2] or "/")
    except OSError:
        return None
    return None


def _cgroup_v1_memory_path(cgroup_path: Path = Path("/proc/self/cgroup")) -> Path | None:
    try:
        for line in cgroup_path.read_text(encoding="utf-8").splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and "memory" in parts[1].split(","):
                return Path(parts[2] or "/")
    except OSError:
        return None
    return None


def _read_int_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_limit_file(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if raw in _UNLIMITED_CGROUP_VALUES:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0 or value >= (1 << 60):
        return None
    return value


def _read_meminfo_total(meminfo_path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in meminfo_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _cooldown_path(source: str, kind: str) -> Path:
    safe_kind = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in kind)
    return runtime_state_dir() / f"{_normalize_source(source)}.{safe_kind}.cooldown"


def _cooldown_allows(source: str, kind: str, cooldown_seconds: int) -> bool:
    if cooldown_seconds <= 0:
        return True
    path = _cooldown_path(source, kind)
    try:
        previous = float(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return True
    return (time.time() - previous) >= cooldown_seconds


def _mark_cooldown(source: str, kind: str) -> None:
    path = _cooldown_path(source, kind)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        logger.warning("Runtime cooldown marker write failed path=%s error=%s", path, exc)


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    mib = value / 1024 / 1024
    return f"{mib:.1f}MiB"


def _format_percent(value: object) -> str:
    try:
        if value is None:
            return "unknown"
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "unknown"


def _safe_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
