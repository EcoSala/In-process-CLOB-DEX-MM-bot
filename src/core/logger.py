import logging
import re
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from rich.logging import RichHandler
except ImportError:
    RichHandler = None  # type: ignore[misc,assignment]

_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 14
_FILL_MAX_BYTES = 10 * 1024 * 1024
_FILL_BACKUP_COUNT = 10


def setup_logging(level: str = "INFO", log_file: str | None = "logs/mm.log") -> None:
    """Stdout (journald) + rotating file log. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt_plain = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_types = (logging.StreamHandler,)
    if RichHandler is not None:
        stream_types = (RichHandler, logging.StreamHandler)
    has_stream = any(
        isinstance(h, stream_types) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )
    if not has_stream:
        if RichHandler is not None:
            root.addHandler(RichHandler(rich_tracebacks=True, show_path=False))
        else:
            sh = logging.StreamHandler()
            sh.setFormatter(fmt_plain)
            root.addHandler(sh)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        resolved = str(path.resolve())
        already = any(getattr(h, "baseFilename", None) == resolved for h in root.handlers)
        if not already:
            fh = RotatingFileHandler(
                path,
                mode="a",
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            fh.setFormatter(fmt_plain)
            root.addHandler(fh)


class StripAnsiFormatter(logging.Formatter):
    """Formatter that strips ANSI color codes from log messages"""

    ANSI_ESCAPE = re.compile(r"\033\[[0-9;]*m")

    def format(self, record):
        message = super().format(record)
        return self.ANSI_ESCAPE.sub("", message)


def setup_fill_logger(enabled: bool = True, log_file: str = "logs/fills.log") -> logging.Logger:
    """
    Dedicated fill logger. Appends across restarts and rotates by size.
    Never truncates on start.
    """
    fill_logger = logging.getLogger("mm.fill")
    fill_logger.setLevel(logging.INFO if enabled else logging.CRITICAL)
    fill_logger.propagate = False
    fill_logger.handlers.clear()

    if enabled:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            mode="a",
            maxBytes=_FILL_MAX_BYTES,
            backupCount=_FILL_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        fill_logger.addHandler(file_handler)

    return fill_logger


def spawn_fill_monitor_window(log_file: str = "logs/fills.log") -> None:
    """Windows-only PowerShell tail of the fill log. No-op on Linux."""
    if sys.platform != "win32":
        return

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    Path(log_file).touch(exist_ok=True)
    abs_log_path = Path(log_file).resolve()
    ps_command = f"Get-Content '{abs_log_path}' -Wait"

    try:
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", ps_command],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        logging.getLogger("mm").info(f"Opened fill monitor window for: {abs_log_path}")
    except Exception as e:
        logging.getLogger("mm").warning(f"Failed to open fill monitor window: {e}")


def spawn_order_ladder_window(log_file: str = "orders_ladder.log") -> None:
    if sys.platform != "win32":
        return

    Path(log_file).touch(exist_ok=True)
    abs_log_path = Path(log_file).resolve()
    ps_command = f"""
while ($true) {{
    Clear-Host
    Get-Content '{abs_log_path}'
    Start-Sleep -Milliseconds 250
}}
"""
    try:
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", ps_command],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        logging.getLogger("mm").info(f"Opened order ladder window for: {abs_log_path}")
    except Exception as e:
        logging.getLogger("mm").warning(f"Failed to open order ladder window: {e}")
