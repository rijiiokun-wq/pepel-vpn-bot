"""
Centralized logging configuration for the shop bot.
Configure once at startup; all modules use getLogger(__name__).

Best practices:
- Single place for format and level
- LOG_LEVEL from env (INFO, DEBUG, WARNING, ERROR); e.g. in docker-compose: environment: LOG_LEVEL: INFO
- Readable one-line format, optional JSON for production
- Third-party loggers tuned to reduce noise
"""
import logging
import os
import sys

# Default: one-line format, easy to grep. %(short_name)s set by ShortNameFilter.
DEFAULT_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(short_name)s | %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Short logger names for readability (strip shop_bot. prefix in display)
class ShortNameFilter(logging.Filter):
    """Shorten logger name to last two parts, e.g. shop_bot.data_manager.scheduler -> data_manager.scheduler."""
    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        if name.startswith("shop_bot."):
            parts = name.split(".")
            record.short_name = ".".join(parts[-2:]) if len(parts) > 2 else parts[-1]
        else:
            record.short_name = name
        return True


class SafeFormatter(logging.Formatter):
    """Formatter that uses short_name when present, else name."""
    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "short_name"):
            record.short_name = record.name
        return super().format(record)


def get_log_level() -> int:
    """Log level from env LOG_LEVEL (DEBUG, INFO, WARNING, ERROR). Default: INFO."""
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def setup_logging(
    *,
    level: int | None = None,
    format_string: str = DEFAULT_FORMAT,
    date_format: str = DATE_FORMAT,
    use_short_names: bool = True,
    quiet_third_party: bool = True,
) -> None:
    """
    Configure root logger. Call once at application startup (e.g. in __main__).

    Args:
        level: Log level (default: from LOG_LEVEL env or INFO)
        format_string: Log line format
        date_format: Date format in asctime
        use_short_names: Use shortened logger names in output
        quiet_third_party: Set werkzeug/urllib3 etc. to WARNING to reduce noise
    """
    if level is None:
        level = get_log_level()

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers when Flask reimports or tests call setup twice
    if root.handlers:
        for h in root.handlers[:]:
            root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    formatter = SafeFormatter(format_string, datefmt=date_format)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    if use_short_names:
        handler.addFilter(ShortNameFilter())

    if quiet_third_party:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Ensure our app loggers inherit level (they use root if not set)
    logging.getLogger("shop_bot").setLevel(level)
