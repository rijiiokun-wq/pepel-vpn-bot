"""Daily XLSX report for leads collected by the proxy gate bot."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo


MOSCOW_TZ = ZoneInfo("Europe/Moscow")
APP_DB = Path(os.environ.get("PROXY_GATE_LEAD_REPORT_DB") or os.environ.get("SHOP_DB_FILE") or "/app/project/users.db")
STATE_DB = Path(
    os.environ.get("PROXY_GATE_LEAD_REPORT_STATE_DB") or "/app/project/runtime/proxy_gate_lead_reporter.sqlite"
)
CHAT_ID = str(os.environ.get("PROXY_GATE_LEAD_REPORT_CHAT_ID") or "5045142059").strip()
REPORT_HOUR = int(os.environ.get("PROXY_GATE_LEAD_REPORT_HOUR") or "12")
REPORT_MINUTE = int(os.environ.get("PROXY_GATE_LEAD_REPORT_MINUTE") or "0")
RETRY_SECONDS = int(os.environ.get("PROXY_GATE_LEAD_REPORT_RETRY_SECONDS") or "1800")
ALL_SHEET_LIMIT = int(os.environ.get("PROXY_GATE_LEAD_REPORT_ALL_SHEET_LIMIT") or "50000")
LOG_FILE = Path(os.environ.get("PROXY_GATE_LEAD_REPORT_LOG_FILE") or "/app/project/runtime/proxy_gate_lead_reporter.log")


LEAD_COLUMNS = [
    ("user_id", "Telegram ID"),
    ("username", "Username"),
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("first_seen_at_msk", "Первый визит МСК"),
    ("last_seen_at_msk", "Последний визит МСК"),
    ("joined_confirmed_at_msk", "Подписка подтверждена МСК"),
    ("last_event", "Последнее событие"),
    ("last_status", "Статус Telegram"),
    ("prompt_count", "Показов"),
    ("check_count", "Проверок"),
    ("last_error", "Ошибка"),
    ("lead_state", "Состояние"),
]


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=str(os.environ.get("LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_db_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_msk(value: Any) -> str:
    parsed = parse_db_timestamp(value)
    if not parsed:
        return ""
    return parsed.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S")


def db_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def app_conn() -> sqlite3.Connection:
    uri = f"file:{APP_DB}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def state_conn() -> sqlite3.Connection:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_runs (
            date_key TEXT PRIMARY KEY,
            period_start_utc TEXT NOT NULL,
            period_end_utc TEXT NOT NULL,
            sent_at_utc TEXT,
            lead_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error TEXT
        )
        """
    )
    conn.commit()
    return conn


def get_bot_token(conn: sqlite3.Connection) -> str:
    env_token = str(os.environ.get("PROXY_GATE_BOT_TOKEN") or "").strip()
    if env_token:
        return env_token
    row = conn.execute("SELECT value FROM bot_settings WHERE key = 'proxy_gate_bot_token'").fetchone()
    token = str(row["value"] or "").strip() if row else ""
    if token:
        return token
    raise RuntimeError("proxy_gate_bot_token is not configured")


def row_to_lead(row: sqlite3.Row) -> dict[str, Any]:
    confirmed = bool(str(row["joined_confirmed_at"] or "").strip())
    return {
        "user_id": row["user_id"],
        "username": row["username"] or "",
        "first_name": row["first_name"] or "",
        "last_name": row["last_name"] or "",
        "first_seen_at_msk": format_msk(row["first_seen_at"]),
        "last_seen_at_msk": format_msk(row["last_seen_at"]),
        "joined_confirmed_at_msk": format_msk(row["joined_confirmed_at"]),
        "last_event": row["last_event"] or "",
        "last_status": row["last_status"] or "",
        "prompt_count": row["prompt_count"] or 0,
        "check_count": row["check_count"] or 0,
        "last_error": row["last_error"] or "",
        "lead_state": "Подтвержден" if confirmed else "Не подтвержден",
    }


def load_period_leads(conn: sqlite3.Connection, period_start: datetime, period_end: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT user_id, username, first_name, last_name, bot_source, first_seen_at, last_seen_at,
               last_event, last_status, last_checked_at, joined_confirmed_at,
               prompt_count, check_count, last_error
        FROM channel_gate_users
        WHERE bot_source = 'proxy_gate'
          AND (
              first_seen_at >= ?
              OR last_seen_at >= ?
              OR joined_confirmed_at >= ?
          )
          AND (
              first_seen_at < ?
              OR last_seen_at < ?
              OR joined_confirmed_at < ?
          )
        ORDER BY COALESCE(joined_confirmed_at, last_seen_at, first_seen_at), user_id
        """,
        (
            db_ts(period_start),
            db_ts(period_start),
            db_ts(period_start),
            db_ts(period_end),
            db_ts(period_end),
            db_ts(period_end),
        ),
    ).fetchall()
    return [row_to_lead(row) for row in rows]


def load_all_leads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT user_id, username, first_name, last_name, bot_source, first_seen_at, last_seen_at,
               last_event, last_status, last_checked_at, joined_confirmed_at,
               prompt_count, check_count, last_error
        FROM channel_gate_users
        WHERE bot_source = 'proxy_gate'
        ORDER BY COALESCE(joined_confirmed_at, last_seen_at, first_seen_at), user_id
        LIMIT ?
        """,
        (ALL_SHEET_LIMIT,),
    ).fetchall()
    return [row_to_lead(row) for row in rows]


def load_summary(conn: sqlite3.Connection, period_start: datetime, period_end: datetime) -> list[tuple[str, Any]]:
    total = conn.execute("SELECT COUNT(*) AS c FROM channel_gate_users WHERE bot_source = 'proxy_gate'").fetchone()["c"]
    confirmed = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM channel_gate_users
        WHERE bot_source = 'proxy_gate' AND joined_confirmed_at IS NOT NULL
        """
    ).fetchone()["c"]
    period_new = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM channel_gate_users
        WHERE bot_source = 'proxy_gate' AND first_seen_at >= ? AND first_seen_at < ?
        """,
        (db_ts(period_start), db_ts(period_end)),
    ).fetchone()["c"]
    period_confirmed = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM channel_gate_users
        WHERE bot_source = 'proxy_gate'
          AND joined_confirmed_at IS NOT NULL
          AND joined_confirmed_at >= ?
          AND joined_confirmed_at < ?
        """,
        (db_ts(period_start), db_ts(period_end)),
    ).fetchone()["c"]
    return [
        ("Период с", period_start.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S МСК")),
        ("Период по", period_end.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S МСК")),
        ("Новых лидов за период", period_new),
        ("Подтвердили подписку за период", period_confirmed),
        ("Всего лидов ppsstbot", total),
        ("Всего подтвердили подписку", confirmed),
        ("Всего не подтвердили", int(total or 0) - int(confirmed or 0)),
    ]


def safe_sheet_name(name: str) -> str:
    return name[:31]


def column_letter(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def cell_xml(row_idx: int, col_idx: int, value: Any) -> str:
    ref = f"{column_letter(col_idx)}{row_idx}"
    text = "" if value is None else str(value)
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'


def sheet_xml(headers: list[str], rows: list[list[Any]]) -> str:
    body: list[str] = []
    header_cells = "".join(cell_xml(1, idx, header) for idx, header in enumerate(headers, start=1))
    body.append(f'<row r="1">{header_cells}</row>')
    for row_idx, row in enumerate(rows, start=2):
        cells = "".join(cell_xml(row_idx, col_idx, value) for col_idx, value in enumerate(row, start=1))
        body.append(f'<row r="{row_idx}">{cells}</row>')
    width_cols = "".join(f'<col min="{i}" max="{i}" width="22" customWidth="1"/>' for i in range(1, len(headers) + 1))
    dimension = f"A1:{column_letter(max(len(headers), 1))}{max(len(rows) + 1, 1)}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="{dimension}"/><cols>{width_cols}</cols><sheetData>{"".join(body)}</sheetData></worksheet>'
    )


def workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{escape(safe_sheet_name(name))}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def workbook_rels_xml(sheet_count: int) -> str:
    rels = "".join(
        '<Relationship '
        f'Id="rId{idx}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{idx}.xml"/>'
        for idx in range(1, sheet_count + 1)
    )
    rels += (
        '<Relationship Id="rIdStyles" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


def content_types_xml(sheet_count: int) -> str:
    sheets = "".join(
        '<Override '
        f'PartName="/xl/worksheets/sheet{idx}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for idx in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{sheets}</Types>"
    )


def build_xlsx(path: Path, *, period_leads: list[dict[str, Any]], all_leads: list[dict[str, Any]], summary: list[tuple[str, Any]]) -> None:
    lead_headers = [title for _, title in LEAD_COLUMNS]
    period_rows = [[lead.get(key, "") for key, _title in LEAD_COLUMNS] for lead in period_leads]
    all_rows = [[lead.get(key, "") for key, _title in LEAD_COLUMNS] for lead in all_leads]
    summary_rows = [[key, value] for key, value in summary]
    sheets = [
        ("Новые лиды", lead_headers, period_rows),
        ("Все лиды", lead_headers, all_rows),
        ("Сводка", ["Показатель", "Значение"], summary_rows),
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml(len(sheets)))
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr("xl/workbook.xml", workbook_xml([name for name, _headers, _rows in sheets]))
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml(len(sheets)))
        archive.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            '</styleSheet>',
        )
        for idx, (_name, headers, rows) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml(headers, rows))


def multipart_form(fields: dict[str, str], file_field: str, file_path: Path, filename: str) -> tuple[bytes, str]:
    boundary = f"----pepelbot{int(time.time() * 1000)}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def send_document(token: str, path: Path, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    payload, boundary = multipart_form(
        {
            "chat_id": CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML",
        },
        "document",
        path,
        path.name,
    )
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
            data = json.loads(raw)
            if not data.get("ok"):
                logging.error("Telegram sendDocument failed: %s", raw)
                return False
            return True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logging.error("Telegram sendDocument exception: %s", exc)
        return False


def latest_success_end(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        """
        SELECT period_end_utc
        FROM report_runs
        WHERE status = 'sent'
        ORDER BY sent_at_utc DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return parse_db_timestamp(row["period_end_utc"])


def already_sent(conn: sqlite3.Connection, date_key: str) -> bool:
    row = conn.execute("SELECT status FROM report_runs WHERE date_key = ?", (date_key,)).fetchone()
    return bool(row and row["status"] == "sent")


def mark_run(
    conn: sqlite3.Connection,
    *,
    date_key: str,
    period_start: datetime,
    period_end: datetime,
    lead_count: int,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO report_runs (
            date_key, period_start_utc, period_end_utc, sent_at_utc, lead_count, status, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date_key,
            db_ts(period_start),
            db_ts(period_end),
            db_ts(utc_now()) if status == "sent" else None,
            int(lead_count),
            status,
            error,
        ),
    )
    conn.commit()


def build_caption(period_start: datetime, period_end: datetime, lead_count: int, total_count: int) -> str:
    start = period_start.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    end = period_end.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    return (
        "📊 <b>Лиды ppsstbot</b>\n"
        f"Период: <b>{html.escape(start)} — {html.escape(end)} МСК</b>\n"
        f"Новых/обновленных за период: <b>{lead_count}</b>\n"
        f"Всего в базе ppsstbot: <b>{total_count}</b>"
    )


def run_report(*, date_key: str | None = None, force: bool = False, period_hours: int = 24, dry_run: Path | None = None) -> bool:
    period_end = utc_now()
    local_date_key = date_key or period_end.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d")
    with state_conn() as state:
        if not force and not dry_run and already_sent(state, local_date_key):
            logging.info("Report for %s has already been sent; skipping.", local_date_key)
            return True
        last_end = latest_success_end(state)
        period_start = last_end if last_end and not force else period_end - timedelta(hours=period_hours)

        with app_conn() as app:
            token = get_bot_token(app)
            period_leads = load_period_leads(app, period_start, period_end)
            all_leads = load_all_leads(app)
            summary = load_summary(app, period_start, period_end)

        output_path = dry_run
        temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if output_path is None:
            temp_dir = tempfile.TemporaryDirectory(prefix="proxy_gate_leads_")
            output_path = Path(temp_dir.name) / f"ppsstbot_leads_{local_date_key}.xlsx"
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            build_xlsx(output_path, period_leads=period_leads, all_leads=all_leads, summary=summary)
            if dry_run:
                logging.info("Dry-run report created: %s", output_path)
                return True
            caption = build_caption(period_start, period_end, len(period_leads), len(all_leads))
            if send_document(token, output_path, caption):
                mark_run(
                    state,
                    date_key=local_date_key,
                    period_start=period_start,
                    period_end=period_end,
                    lead_count=len(period_leads),
                    status="sent",
                )
                logging.info("Report sent: date_key=%s leads=%s total=%s", local_date_key, len(period_leads), len(all_leads))
                return True
            mark_run(
                state,
                date_key=local_date_key,
                period_start=period_start,
                period_end=period_end,
                lead_count=len(period_leads),
                status="send_failed",
                error="telegram_send_failed",
            )
            return False
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()


def next_run_at(now: datetime) -> datetime:
    local_now = now.astimezone(MOSCOW_TZ)
    target = local_now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return target.astimezone(UTC)


def loop_forever() -> None:
    logging.info(
        "Proxy gate lead reporter started: chat_id=%s schedule=%02d:%02d MSK db=%s",
        CHAT_ID,
        REPORT_HOUR,
        REPORT_MINUTE,
        APP_DB,
    )
    while True:
        target = next_run_at(utc_now())
        sleep_seconds = max(1.0, (target - utc_now()).total_seconds())
        logging.info("Next report at %s MSK", target.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S"))
        while sleep_seconds > 0:
            nap = min(sleep_seconds, 3600.0)
            time.sleep(nap)
            sleep_seconds = max(0.0, (target - utc_now()).total_seconds())
        date_key = target.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d")
        while True:
            try:
                if run_report(date_key=date_key):
                    break
            except Exception as exc:
                logging.exception("Report run failed: %s", exc)
            retry_at = utc_now() + timedelta(seconds=max(RETRY_SECONDS, 60))
            if retry_at.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d") != date_key:
                logging.error("Report for %s was not sent before the next day window.", date_key)
                break
            logging.warning("Report for %s was not sent; retrying in %s seconds.", date_key, RETRY_SECONDS)
            time.sleep(max(RETRY_SECONDS, 60))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send daily XLSX reports for ppsstbot leads.")
    parser.add_argument("--send-now", action="store_true", help="Send the report immediately and exit.")
    parser.add_argument("--force", action="store_true", help="Ignore duplicate date guard for --send-now.")
    parser.add_argument("--period-hours", type=int, default=24, help="Lookback period for forced/manual report.")
    parser.add_argument("--dry-run", type=Path, help="Build XLSX at this path without sending to Telegram.")
    return parser.parse_args()


def main() -> None:
    setup_logging()
    args = parse_args()
    if args.dry_run:
        ok = run_report(force=True, period_hours=args.period_hours, dry_run=args.dry_run)
        raise SystemExit(0 if ok else 1)
    if args.send_now:
        key = f"manual-{utc_now().strftime('%Y%m%d%H%M%S')}" if args.force else None
        ok = run_report(date_key=key, force=args.force, period_hours=args.period_hours)
        raise SystemExit(0 if ok else 1)
    loop_forever()


if __name__ == "__main__":
    main()
