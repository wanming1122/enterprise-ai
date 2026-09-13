"""服务器管理工具（M6-T6，MCP 风格只读探查）：AI 助手经工具调用查询服务器状态。

安全边界：
- 全部动作只读，不提供任何写操作；
- 文件浏览/读取限定在项目目录内（resolve 后校验防路径穿越）；
- 读取仅限文本文件且单次 ≤100KB；
- 仅持有 ai:server_admin 权限的账号，其 AI 助手会话才会注入该工具。
"""
import logging
import os
import platform
import shutil
import string
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # backend/app/services → 项目根
MAX_READ_CHARS = 4000   # 单次读取返回给模型的字符上限
MAX_FILE_BYTES = 100_000  # 可读文件大小上限
ALLOWED_ACTIONS = ("system_info", "disk", "process", "network", "file_list", "file_read")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _sandbox_resolve(rel: str | None) -> Path:
    """项目目录沙箱：相对路径解析并校验不越界。"""
    base = PROJECT_ROOT.resolve()
    target = (base / (rel or "")).resolve()
    if target != base and base not in target.parents:
        raise ValueError("路径越界：仅允许访问项目目录内的文件")
    return target


def _disk_lines() -> list[str]:
    lines: list[str] = []
    if os.name == "nt":
        drives = [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    else:
        drives = ["/"]
    for drive in drives:
        usage = shutil.disk_usage(drive)
        percent = usage.used / usage.total * 100 if usage.total else 0
        lines.append(
            f"{drive} 总 {_human(usage.total)}，已用 {_human(usage.used)}（{percent:.0f}%），"
            f"可用 {_human(usage.free)}"
        )
    return lines


def _process_lines() -> list[str]:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15
        ).stdout
        rows = [ln for ln in out.splitlines() if ln.strip()]
        return rows[:40]
    out = subprocess.run(
        ["ps", "aux", "--sort=-%mem"], capture_output=True, text=True, timeout=15
    ).stdout
    return out.splitlines()[:41]


def _network_lines() -> list[str]:
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=15).stdout
    listening: list[str] = []
    state_count: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        state = parts[-2] if os.name == "nt" and len(parts) >= 5 else (parts[3] if len(parts) > 3 else "")
        state_count[state or "OTHER"] = state_count.get(state or "OTHER", 0) + 1
        if "LISTEN" in line.upper() and len(listening) < 30:
            listening.append(line.strip())
    summary = [f"连接状态统计：{state_count}"] if state_count else []
    return summary + ["监听中的端口："] + (listening or ["（未发现监听端口）"])


def run_action(action: str, params: dict | None = None) -> str:
    """执行只读探查动作，返回给模型阅读的文本结果。"""
    params = params or {}
    if action not in ALLOWED_ACTIONS:
        return f"不支持的操作 {action}。可用操作：{', '.join(ALLOWED_ACTIONS)}"

    try:
        if action == "system_info":
            return (
                f"操作系统：{platform.platform()}\n"
                f"Python：{platform.python_version()}\n"
                f"CPU 逻辑核数：{os.cpu_count()}\n"
                f"项目目录：{PROJECT_ROOT}\n"
                f"磁盘：{'；'.join(_disk_lines())}"
            )

        if action == "disk":
            return "\n".join(_disk_lines())

        if action == "process":
            return "进程列表（前 40 条）：\n" + "\n".join(_process_lines())

        if action == "network":
            return "\n".join(_network_lines())

        if action == "file_list":
            target = _sandbox_resolve(params.get("path"))
            if not target.is_dir():
                return f"{target} 不是目录"
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            lines = [
                f"{'[目录]' if e.is_dir() else '[文件]'} {e.name}"
                + ("" if e.is_dir() else f"（{_human(e.stat().st_size)}）")
                for e in entries[:100]
            ]
            more = f"\n…（共 {len(entries)} 项，仅显示前 100）" if len(entries) > 100 else ""
            return f"{target} 目录内容：\n" + "\n".join(lines) + more

        if action == "file_read":
            target = _sandbox_resolve(params.get("path"))
            if not target.is_file():
                return f"{target.name} 不是文件"
            if target.stat().st_size > MAX_FILE_BYTES:
                return f"文件过大（{_human(target.stat().st_size)}），仅支持读取 100KB 内的文本文件"
            raw = target.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="ignore")
            if "\x00" in text[:200]:
                return "该文件疑似二进制文件，不支持读取"
            return f"{target.name}（前 {MAX_READ_CHARS} 字符）：\n{text[:MAX_READ_CHARS]}"

        return f"不支持的操作 {action}"
    except ValueError as exc:
        # 路径越界拒绝（沙箱拦截），可能来自模型被诱导构造的越权路径，留痕便于安全审计
        logger.warning("server_admin 沙箱拦截 action=%s params=%s: %s", action, params, exc)
        return str(exc)
    except Exception as exc:  # noqa: BLE001 工具结果兜底，异常信息交给模型转述
        logger.exception("server_admin 动作执行异常 action=%s params=%s", action, params)
        return f"执行失败：{exc}"
