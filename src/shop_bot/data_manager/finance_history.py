"""Build and load an identifier-free financial history from original exports.

Transaction amounts use RUB from the account context, not from a CSV currency
column. ``source_revenue`` retains the provider's label and is not interpreted as
commission, profit or an available balance. Refund statuses stay separate.
"""

import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_HISTORY_BYTES = 8 * 1024 * 1024
MAX_ROWS = 100_000
PROJECT_NAMES = {"pepel": "Пепел", "subscriptions": "Подписки"}
TRANSACTION_STATUSES = {"Успех", "Успешный возврат", "Истек", "Создан"}
PAYOUT_STATUSES = {"success"}
TRANSACTION_COLUMNS = {
    "Номер заказа", "Order Id", "Сумма", "Выручка", "Статус", "Время создания", "Дата платежа",
}
PAYOUT_COLUMNS = {"id", "Тип", "Статус", "Сумма выплаты", "Сумма списания", "Комиссия", "Время создания"}
DAILY_MONEY_FIELDS = ("gross_rub", "source_revenue_rub", "refund_amount_rub", "refund_source_revenue_rub")
DAILY_KEYS = {"date", "count", "refund_count", *DAILY_MONEY_FIELDS}
PROJECT_KEYS = {
    "project_id", "name", "source_sha256", "row_count", "status_counts",
    "first_created_at", "last_created_at", "first_paid_at", "last_paid_at", "daily",
}
PAYOUT_ROW_KEYS = {"date", "status", "type", "amount_field", "debit_field", "fee_field"}
API_PAYOUT_ROW_KEYS = {
    "operation_ref", "date", "date_basis", "verified_at", "status", "service",
    "currency", "amount_pay_rub", "fee_rub", "amount_receive_rub",
}


class FinanceHistoryError(ValueError):
    """An export or snapshot cannot support a complete, exact calculation."""


def _read_bounded(path, limit):
    try:
        with Path(path).open("rb") as stream:
            content = stream.read(limit + 1)
    except (OSError, TypeError, ValueError):
        raise FinanceHistoryError("Unable to read finance source") from None
    if len(content) > limit:
        raise FinanceHistoryError("Finance source exceeds the size limit")
    return content


def _read_csv(path, required_columns):
    raw = _read_bounded(path, MAX_CSV_BYTES)
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        columns = reader.fieldnames or []
        if len(columns) != len(set(columns)) or not required_columns.issubset(columns):
            raise FinanceHistoryError("Finance CSV columns are invalid")
        rows = []
        for row in reader:
            if len(rows) >= MAX_ROWS or None in row or any(value is None for value in row.values()):
                raise FinanceHistoryError("Finance CSV row structure is invalid")
            rows.append(row)
    except (UnicodeError, csv.Error):
        raise FinanceHistoryError("Finance CSV encoding or structure is invalid") from None
    return rows, hashlib.sha256(raw).hexdigest()


def _money(value, *, positive=False):
    if not isinstance(value, str) or len(value) > 64:
        raise FinanceHistoryError("Finance monetary value is invalid")
    raw = value.strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
        raise FinanceHistoryError("Finance monetary value is invalid")
    try:
        amount = Decimal(raw)
        numerator, denominator = amount.as_integer_ratio()
    except (InvalidOperation, ValueError, OverflowError):
        raise FinanceHistoryError("Finance monetary value is invalid") from None
    cents, remainder = divmod(numerator * 100, denominator)
    if remainder or cents < (1 if positive else 0):
        raise FinanceHistoryError("Finance monetary value must contain exact nonnegative kopecks")
    return cents


def _format_money(cents):
    whole, fraction = divmod(cents, 100)
    return f"{whole}.{fraction:02d}"


def _timestamp(value, *, optional=False):
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value, str):
        raise FinanceHistoryError("Finance export timestamp is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise FinanceHistoryError("Finance export timestamp is invalid") from None
    if parsed.strftime("%Y-%m-%d %H:%M:%S") != value:
        raise FinanceHistoryError("Finance export timestamp must retain its naive source format")
    return value


def _calendar_date(value):
    if not isinstance(value, str):
        raise FinanceHistoryError("Finance calendar date is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise FinanceHistoryError("Finance calendar date is invalid") from None
    if parsed.isoformat() != value:
        raise FinanceHistoryError("Finance calendar date is invalid")
    return value


def _recorded_at(value):
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise FinanceHistoryError("Finance snapshot recorded_at is invalid") from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise FinanceHistoryError("Finance snapshot recorded_at must include a timezone")
    return instant.isoformat()


def _new_daily():
    return {"count": 0, "refund_count": 0, **{key: 0 for key in DAILY_MONEY_FIELDS}}


def _unique_identifiers(rows, columns):
    result = {column: set() for column in columns}
    for row in rows:
        for column in columns:
            value = row[column].strip()
            if not value or len(value) > 512 or value in result[column]:
                raise FinanceHistoryError("Finance export identifiers are missing or duplicated")
            result[column].add(value)
    return set().union(*result.values())


def _read_transactions(csv_path, project_id):
    if not isinstance(project_id, str) or project_id not in PROJECT_NAMES:
        raise FinanceHistoryError("Unknown finance project")
    rows, digest = _read_csv(csv_path, TRANSACTION_COLUMNS)
    identifiers = _unique_identifiers(rows, ("Номер заказа", "Order Id"))
    status_counts = Counter()
    created, paid = [], []
    daily = defaultdict(_new_daily)
    for row in rows:
        status = row["Статус"]
        if status not in TRANSACTION_STATUSES:
            raise FinanceHistoryError("Unknown transaction status in finance export")
        status_counts[status] += 1
        created.append(_timestamp(row["Время создания"]))
        paid_at = _timestamp(row["Дата платежа"], optional=True)
        if paid_at is not None:
            paid.append(paid_at)
        gross = _money(row["Сумма"], positive=True)
        revenue = _money(row["Выручка"]) if row["Выручка"].strip() else None
        if status not in {"Успех", "Успешный возврат"}:
            continue
        if paid_at is None or revenue is None:
            raise FinanceHistoryError("Confirmed financial row lacks its date or source revenue")
        point = daily[paid_at[:10]]
        if status == "Успех":
            point["count"] += 1
            point["gross_rub"] += gross
            point["source_revenue_rub"] += revenue
        else:
            point["refund_count"] += 1
            point["refund_amount_rub"] += gross
            point["refund_source_revenue_rub"] += revenue
    project = {
        "project_id": project_id, "name": PROJECT_NAMES[project_id],
        "source_sha256": digest, "row_count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "first_created_at": min(created) if created else None,
        "last_created_at": max(created) if created else None,
        "first_paid_at": min(paid) if paid else None,
        "last_paid_at": max(paid) if paid else None,
        "daily": [
            {"date": day, **{key: _format_money(value) if key in DAILY_MONEY_FIELDS else value
                              for key, value in values.items()}}
            for day, values in sorted(daily.items())
        ],
    }
    return project, identifiers


def read_lava_transactions(csv_path, project_id):
    """Read one confirmed project export; return only safe daily aggregates."""
    project, _ = _read_transactions(csv_path, project_id)
    return project


def payout_operation_ref(provider_id):
    """Hash the provider payout ID, shared by CSV ``id`` and API payout records.

    Merchant order IDs are a different namespace and must not be passed here.
    Raw provider IDs are never retained in the aggregate history.
    """
    if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 512:
        raise FinanceHistoryError("Payout provider identifier is invalid")
    return hashlib.sha256(provider_id.strip().encode("utf-8")).hexdigest()


def _read_payouts(csv_path):
    rows, digest = _read_csv(csv_path, PAYOUT_COLUMNS)
    _unique_identifiers(rows, ("id",))
    safe_rows = []
    for row in rows:
        if row["Статус"] not in PAYOUT_STATUSES:
            raise FinanceHistoryError("Unknown payout status in finance export")
        if not row["Тип"].strip() or len(row["Тип"]) > 128:
            raise FinanceHistoryError("Payout type is invalid")
        fields = {target: row[source].strip() for target, source in (
            ("amount_field", "Сумма выплаты"), ("debit_field", "Сумма списания"), ("fee_field", "Комиссия"),
        )}
        for value in fields.values():
            _money(value)
        safe_rows.append({"date": _timestamp(row["Время создания"]),
                          "status": row["Статус"], "type": row["Тип"], **fields,
                          "operation_ref": payout_operation_ref(row["id"])})
    return {"source_sha256": digest, "row_count": len(rows),
            "rows": sorted(safe_rows, key=lambda row: row["date"]),
            "currency": None, "semantics_confirmed": False}


def build_history(pepel_path, subscriptions_path, payouts_path, *, recorded_at,
                  previous_history=None):
    """Read exports and retain API confirmations absent from the new payout CSV.

    An existing snapshot is validated before carrying its API rows forward.
    When the provider ID appears in CSV, that source represents the operation
    once. CSV monetary fields keep their original, unconfirmed semantics.
    """
    timestamp = _recorded_at(recorded_at)
    pepel, pepel_ids = _read_transactions(pepel_path, "pepel")
    subscriptions, subscriptions_ids = _read_transactions(subscriptions_path, "subscriptions")
    if pepel_ids & subscriptions_ids:
        raise FinanceHistoryError("Transaction identifiers overlap across projects")
    payouts = _read_payouts(payouts_path)
    if previous_history is not None:
        previous = _read_history_document(previous_history)
        if "api_rows" in previous["payouts"]:
            csv_refs = {row["operation_ref"] for row in payouts["rows"]}
            payouts["api_rows"] = [row for row in previous["payouts"]["api_rows"]
                                   if row["operation_ref"] not in csv_refs]
    snapshot = {
        "schema_version": 1, "recorded_at": timestamp,
        "date_basis": "export_calendar", "currency": "RUB", "currency_basis": "account_context",
        "projects": [pepel, subscriptions], "payouts": payouts,
    }
    _validate_history(snapshot)
    return snapshot


def _keys(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required).issubset(value)
            or not set(value).issubset(set(required) | set(optional))):
        raise FinanceHistoryError("Finance history schema is invalid")


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_ROWS:
        raise FinanceHistoryError("Finance history count is invalid")
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise FinanceHistoryError("Finance history source digest is invalid")
    return value


def _validate_project(project):
    _keys(project, PROJECT_KEYS)
    project_id = project["project_id"]
    if not isinstance(project_id, str) or project_id not in PROJECT_NAMES or project["name"] != PROJECT_NAMES[project_id]:
        raise FinanceHistoryError("Finance history project is invalid")
    _digest(project["source_sha256"])
    row_count = _count(project["row_count"])
    statuses = project["status_counts"]
    if not isinstance(statuses, dict) or not set(statuses).issubset(TRANSACTION_STATUSES):
        raise FinanceHistoryError("Finance history transaction statuses are invalid")
    if sum(_count(count) for count in statuses.values()) != row_count:
        raise FinanceHistoryError("Finance history row counts do not reconcile")
    for prefix in ("created", "paid"):
        first = _timestamp(project[f"first_{prefix}_at"], optional=True)
        last = _timestamp(project[f"last_{prefix}_at"], optional=True)
        if (first is None) != (last is None) or (first and first > last):
            raise FinanceHistoryError("Finance history timestamp bounds are invalid")
        if prefix == "created" and bool(row_count) != bool(first):
            raise FinanceHistoryError("Finance history creation bounds do not reconcile")
    if not isinstance(project["daily"], list) or len(project["daily"]) > MAX_ROWS:
        raise FinanceHistoryError("Finance history daily rows are invalid")
    seen, successes, refunds = set(), 0, 0
    for point in project["daily"]:
        _keys(point, DAILY_KEYS)
        day = _calendar_date(point["date"])
        if day in seen:
            raise FinanceHistoryError("Finance history has duplicate daily dates")
        seen.add(day)
        count, refund_count = _count(point["count"]), _count(point["refund_count"])
        if count + refund_count == 0:
            raise FinanceHistoryError("Finance history daily row has no financial records")
        money = {key: _money(point[key]) for key in DAILY_MONEY_FIELDS}
        if (count == 0 and (money["gross_rub"] or money["source_revenue_rub"])) or (count > 0 and money["gross_rub"] == 0):
            raise FinanceHistoryError("Finance history daily income does not reconcile")
        if (refund_count == 0 and (money["refund_amount_rub"] or money["refund_source_revenue_rub"])) or (refund_count > 0 and money["refund_amount_rub"] == 0):
            raise FinanceHistoryError("Finance history daily refunds do not reconcile")
        first_paid, last_paid = project["first_paid_at"], project["last_paid_at"]
        if first_paid is None or not first_paid[:10] <= day <= last_paid[:10]:
            raise FinanceHistoryError("Finance history daily dates exceed source bounds")
        successes += count
        refunds += refund_count
    if successes != statuses.get("Успех", 0) or refunds != statuses.get("Успешный возврат", 0):
        raise FinanceHistoryError("Finance history financial counts do not reconcile")


def _validate_history(history):
    _keys(history, {"schema_version", "recorded_at", "date_basis", "currency", "currency_basis", "projects", "payouts"})
    if type(history["schema_version"]) is not int or history["schema_version"] != 1:
        raise FinanceHistoryError("Unsupported finance history schema")
    if history["date_basis"] != "export_calendar" or history["currency"] != "RUB" or history["currency_basis"] != "account_context":
        raise FinanceHistoryError("Finance history units or date basis are invalid")
    _recorded_at(history["recorded_at"])
    projects = history["projects"]
    if not isinstance(projects, list) or len(projects) != 2:
        raise FinanceHistoryError("Both finance history projects are required")
    for project in projects:
        _validate_project(project)
    if {project["project_id"] for project in projects} != set(PROJECT_NAMES):
        raise FinanceHistoryError("Finance history projects are duplicated")
    payouts = history["payouts"]
    _keys(payouts, {"source_sha256", "row_count", "rows", "currency", "semantics_confirmed"},
          optional={"api_rows"})
    _digest(payouts["source_sha256"])
    if payouts["currency"] is not None or payouts["semantics_confirmed"] is not False:
        raise FinanceHistoryError("Payout currency and semantics must remain unconfirmed")
    if not isinstance(payouts["rows"], list) or _count(payouts["row_count"]) != len(payouts["rows"]):
        raise FinanceHistoryError("Finance history payout counts do not reconcile")
    operation_refs = set()
    for row in payouts["rows"]:
        _keys(row, PAYOUT_ROW_KEYS, optional={"operation_ref"})
        if "operation_ref" in row:
            reference = _digest(row["operation_ref"])
            if reference in operation_refs:
                raise FinanceHistoryError("Finance history contains duplicate payout operations")
            operation_refs.add(reference)
        _timestamp(row["date"])
        if not isinstance(row["status"], str) or row["status"] not in PAYOUT_STATUSES:
            raise FinanceHistoryError("Unknown payout status in finance history")
        if not isinstance(row["type"], str) or not row["type"].strip() or len(row["type"]) > 128:
            raise FinanceHistoryError("Finance history payout type is invalid")
        for key in ("amount_field", "debit_field", "fee_field"):
            _money(row[key])
    api_rows = payouts.get("api_rows", [])
    if not isinstance(api_rows, list) or len(api_rows) > MAX_ROWS:
        raise FinanceHistoryError("Finance API payout rows are invalid")
    for row in api_rows:
        _keys(row, API_PAYOUT_ROW_KEYS)
        reference = _digest(row["operation_ref"])
        if reference in operation_refs:
            raise FinanceHistoryError("Finance history contains duplicate payout operations")
        operation_refs.add(reference)
        _timestamp(row["date"])
        _recorded_at(row["verified_at"])
        if (row["date_basis"] != "request_moscow" or row["status"] != "success"
                or row["service"] != "trc20_payoff" or row["currency"] != "RUB"):
            raise FinanceHistoryError("Finance API payout provenance is invalid")
        amount = _money(row["amount_pay_rub"], positive=True)
        fee = _money(row["fee_rub"])
        received = _money(row["amount_receive_rub"])
        if amount != fee + received:
            raise FinanceHistoryError("Finance API payout amounts do not reconcile")


def _no_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FinanceHistoryError("Finance history contains duplicate fields")
        result[key] = value
    return result


def _read_history_document(path):
    """Read and validate stored data without adding period-derived fields."""
    raw = _read_bounded(path, MAX_HISTORY_BYTES)
    try:
        history = json.loads(raw, object_pairs_hook=_no_duplicate_json_keys)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise FinanceHistoryError("Finance history JSON is invalid") from None
    _validate_history(history)
    return history


def load_history(path, date_from=None, date_to=None):
    """Load a bounded snapshot and recompute period summaries from daily rows.

    Both dates omitted selects the entire dated history. CSV row_count and
    source_sha256 remain original source controls. API counts are separate;
    total_selected_count counts both kinds of operation exactly once.
    """
    history = _read_history_document(path)
    if (date_from is None) != (date_to is None):
        raise FinanceHistoryError("Both finance period dates are required")
    if date_from is None:
        project_dates = [point["date"] for project in history["projects"] for point in project["daily"]]
        project_dates += [project[key][:10] for project in history["projects"]
                          for key in ("first_created_at", "last_created_at") if project[key] is not None]
        payout_dates = [row["date"][:10] for row in history["payouts"]["rows"]]
        payout_dates += [row["date"][:10] for row in history["payouts"].get("api_rows", [])]
        if not project_dates and not payout_dates:
            raise FinanceHistoryError("Finance history has no dated records")
        start = min(project_dates + payout_dates)
        end = max(project_dates + payout_dates)
    else:
        start, end = _calendar_date(date_from), _calendar_date(date_to)
        if start > end:
            raise FinanceHistoryError("Finance period start exceeds end")
    for project in history["projects"]:
        points = sorted([point for point in project["daily"] if start <= point["date"] <= end], key=lambda point: point["date"])
        project["daily"] = points
        project["count"] = sum(point["count"] for point in points)
        project["refund_count"] = sum(point["refund_count"] for point in points)
        for field in DAILY_MONEY_FIELDS:
            project[field] = _format_money(sum(_money(point[field]) for point in points))
    payouts = history["payouts"]
    payouts["rows"] = [row for row in payouts["rows"] if start <= row["date"][:10] <= end]
    payouts["selected_count"] = len(payouts["rows"])
    api_rows = payouts.get("api_rows", [])
    payouts["api_row_count"] = len(api_rows)
    payouts["api_rows"] = [row for row in api_rows if start <= row["date"][:10] <= end]
    payouts["selected_api_count"] = len(payouts["api_rows"])
    payouts["total_selected_count"] = payouts["selected_count"] + payouts["selected_api_count"]
    history.update(start=start, end=end)
    return history
