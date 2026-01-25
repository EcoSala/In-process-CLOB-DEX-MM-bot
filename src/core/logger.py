import logging
import re
import subprocess
import sys
from pathlib import Path
from rich.logging import RichHandler


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)]
    )


class StripAnsiFormatter(logging.Formatter):
    """Formatter that strips ANSI color codes from log messages"""
    
    # Regex to match ANSI escape sequences
    ANSI_ESCAPE = re.compile(r'\033\[[0-9;]*m')
    
    def format(self, record):
        # Get the formatted message
        message = super().format(record)
        # Strip ANSI codes
        return self.ANSI_ESCAPE.sub('', message)


def setup_fill_logger(enabled: bool = True, log_file: str = "fills.log") -> logging.Logger:
    """
    Setup a dedicated logger for execution fills.
    
    Writes fills to a file (without colors) for easy redirection and monitoring.
    Returns a logger that can be disabled via the enabled flag.
    """
    fill_logger = logging.getLogger("mm.fill")
    fill_logger.setLevel(logging.INFO if enabled else logging.CRITICAL)
    fill_logger.propagate = False  # Don't propagate to root logger
    
    # Clear any existing handlers
    fill_logger.handlers.clear()
    
    if enabled:
        # File handler: writes to file WITHOUT colors (strips ANSI codes)
        file_handler = logging.FileHandler(log_file, mode='w')  # Overwrite on restart
        file_formatter = StripAnsiFormatter("%(message)s")
        file_handler.setFormatter(file_formatter)
        fill_logger.addHandler(file_handler)
    
    return fill_logger


def spawn_fill_monitor_window(log_file: str = "fills.log") -> None:
    """
    Spawn a separate PowerShell window to display live fill logs.
    
    Opens a new PowerShell window running 'Get-Content -Wait' to tail the fill log.
    This is non-blocking and works on Windows PowerShell.
    """
    if sys.platform != "win32":
        logging.getLogger("mm").warning(
            "Fill monitor window only supported on Windows. Skipping."
        )
        return
    
    # Ensure the log file exists (empty initially)
    Path(log_file).touch(exist_ok=True)
    
    # Get absolute path for the log file
    abs_log_path = Path(log_file).resolve()
    
    # PowerShell command to tail the log file
    # -NoExit keeps the window open, -Command runs Get-Content in wait mode
    ps_command = f"Get-Content '{abs_log_path}' -Wait"
    
    # Start a new PowerShell window (non-blocking)
    # Using 'start' ensures it opens in a separate window
    try:
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", ps_command],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        logging.getLogger("mm").info(f"Opened fill monitor window for: {abs_log_path}")
    except Exception as e:
        logging.getLogger("mm").warning(f"Failed to open fill monitor window: {e}")
