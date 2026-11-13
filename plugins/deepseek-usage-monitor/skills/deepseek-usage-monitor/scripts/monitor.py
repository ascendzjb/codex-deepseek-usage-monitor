#!/usr/bin/env python3
"""Live DeepSeek usage widget for local Codex sessions."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import logging
import math
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk


APP_NAME = "DeepSeek Usage Monitor"
APP_DIR_NAME = "DeepSeekUsageMonitor"
REFRESH_SECONDS = 3
HISTORY_DAYS = 190
WIDGET_WIDTH = 560
WIDGET_HEIGHT = 272
BALL_WIDTH = 120
BALL_HEIGHT = 72
TRANSPARENT_COLOR = "#010203"
HOVER_EXPAND_SECONDS = 0.65
HOVER_COLLAPSE_SECONDS = 0.55
DRAG_THRESHOLD = 4
EDGE_SNAP_DISTANCE = 28

SCRIPT_DIR = Path(__file__).resolve().parent
PRICING_PATH = SCRIPT_DIR / "pricing.json"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SESSION_ROOT = CODEX_HOME / "sessions"
LOCAL_APP_DIR = Path(
    os.environ.get(
        "DEEPSEEK_USAGE_DATA_DIR",
        str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_DIR_NAME),
    )
)
DB_PATH = LOCAL_APP_DIR / "usage.sqlite3"
LOG_PATH = LOCAL_APP_DIR / "widget.log"
SETTINGS_PATH = LOCAL_APP_DIR / "settings.json"
PID_PATH = LOCAL_APP_DIR / "widget.pid"
STOP_PATH = LOCAL_APP_DIR / "stop.flag"
INTENTIONAL_STOP_PATH = LOCAL_APP_DIR / "intentional_stop.flag"


def ensure_app_dir() -> None:
    LOCAL_APP_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    ensure_app_dir()
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_local_zone(config: dict[str, Any]) -> Any:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    except Exception:
        return timezone(timedelta(hours=8))


@dataclass(frozen=True)
class PriceSet:
    cache_hit: float
    cache_miss: float
    output: float


class Pricing:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.local_zone = load_local_zone(config)
        self.default_model = config.get("default_model", "deepseek-flash")
        self.aliases = config.get("aliases", {})
        self.periods = sorted(
            config["pricing_periods"],
            key=lambda item: parse_iso(item["effective_from"]),
        )
        self.peak_config = config.get("peak", {})
        self.holiday_dates = set(self.peak_config.get("holiday_dates", []))
        self.peak_weekdays = set(
            self.peak_config.get("weekdays", [0, 1, 2, 3, 4])
        )
        self.peak_ranges = [
            (dt_time.fromisoformat(start), dt_time.fromisoformat(end))
            for start, end in self.peak_config.get("local_time_ranges", [])
        ]
        self.signature = hashlib.sha256(
            json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.config_warnings = self.validate_configuration(
            datetime.now(tz=self.local_zone)
        )

    def validate_configuration(self, timestamp: datetime) -> list[str]:
        warnings: list[str] = []
        current_year = timestamp.astimezone(self.local_zone).year
        years = sorted(
            {
                int(date_text[:4])
                for date_text in self.holiday_dates
                if len(date_text) >= 4 and date_text[:4].isdigit()
            }
        )
        if current_year not in years:
            covered = f"仅覆盖 {years[-1]} 年" if years else "尚未配置"
            warnings.append(f"节假日配置{covered}，当前 {current_year} 年峰谷可能不准")

        has_current_period = False
        for period in self.periods:
            start = parse_iso(period["effective_from"])
            end_value = period.get("effective_to")
            end = parse_iso(end_value) if end_value else None
            if timestamp >= start and (end is None or timestamp < end):
                has_current_period = True
                break
        if not has_current_period:
            warnings.append("当前日期没有可用的价格配置")
        return warnings

    def normalize_model(self, model: str | None) -> str:
        if not model:
            return self.default_model
        return self.aliases.get(model, model)

    def price_for(
        self, timestamp: datetime, model: str | None
    ) -> tuple[PriceSet | None, bool, str | None]:
        normalized = self.normalize_model(model)
        selected: dict[str, Any] | None = None
        for period in self.periods:
            start = parse_iso(period["effective_from"])
            end_value = period.get("effective_to")
            end = parse_iso(end_value) if end_value else None
            if timestamp >= start and (end is None or timestamp < end):
                selected = period
                break

        is_peak = self.is_peak(timestamp)
        if selected is None:
            return None, is_peak, "no pricing period"

        models = selected["models"]
        if normalized not in models:
            return None, is_peak, f"unsupported model: {normalized}"

        rates = models[normalized]["peak" if is_peak else "off_peak"]
        return (
            PriceSet(
                cache_hit=float(rates["cache_hit"]),
                cache_miss=float(rates["cache_miss"]),
                output=float(rates["output"]),
            ),
            is_peak,
            None,
        )

    def is_peak(self, timestamp: datetime) -> bool:
        local = timestamp.astimezone(self.local_zone)
        date_text = local.strftime("%Y-%m-%d")
        if date_text in self.holiday_dates:
            return False
        if local.weekday() not in self.peak_weekdays:
            return False
        current = local.time().replace(tzinfo=None)
        for start, end in self.peak_ranges:
            if start <= current < end:
                return True
        return False


def connect_db(pricing: Pricing | None = None) -> sqlite3.Connection:
    ensure_app_dir()
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS usage (
            response_id TEXT PRIMARY KEY,
            ts_utc TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            cached_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            reasoning_tokens INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS usage_ts_idx ON usage(ts_utc);

        CREATE TABLE IF NOT EXISTS turn_models (
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            model TEXT NOT NULL,
            PRIMARY KEY(session_id, turn_id)
        );

        CREATE TABLE IF NOT EXISTS file_state (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            session_id TEXT
        );

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(usage)")
    }
    new_columns = {
        "cost_cny": "REAL NOT NULL DEFAULT 0",
        "peak_cost_cny": "REAL NOT NULL DEFAULT 0",
        "off_peak_cost_cny": "REAL NOT NULL DEFAULT 0",
        "is_peak": "INTEGER NOT NULL DEFAULT 0",
        "local_date": "TEXT",
        "local_month": "TEXT",
        "priced": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in new_columns.items():
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE usage ADD COLUMN {column} {definition}"
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS usage_local_date_idx ON usage(local_date)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS usage_local_month_idx ON usage(local_month)"
    )

    if pricing is not None:
        state = connection.execute(
            "SELECT value FROM metadata WHERE key = 'pricing_signature'"
        ).fetchone()
        if state is None or state["value"] != pricing.signature:
            connection.execute("UPDATE usage SET priced = 0")
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('pricing_signature', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (pricing.signature,),
            )
            connection.commit()
    return connection


def discover_files(start_date: Any) -> list[Path]:
    if not SESSION_ROOT.exists():
        return []
    files: list[Path] = []
    current = start_date
    today = datetime.now().astimezone().date()
    while current <= today:
        folder = SESSION_ROOT / f"{current.year:04d}" / f"{current.month:02d}" / f"{current.day:02d}"
        if folder.exists():
            files.extend(folder.glob("*.jsonl"))
            files.extend(folder.glob("*.jsonl.zst"))
        current += timedelta(days=1)
    return sorted(files, key=lambda path: path.stat().st_mtime_ns)


def find_7zip() -> str | None:
    program_files = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
    ]
    candidates = [
        os.environ.get("DEEPSEEK_7ZIP_PATH"),
        shutil.which("7z.exe"),
        shutil.which("7z"),
        shutil.which("7zz"),
    ]
    candidates.extend(
        str(Path(root) / "7-Zip" / "7z.exe")
        for root in program_files
        if root
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def open_zstd_stream(path: Path) -> io.BytesIO:
    try:
        import zstandard  # type: ignore

        return io.BytesIO(zstandard.ZstdDecompressor().decompress(path.read_bytes()))
    except ImportError:
        pass

    seven_zip = find_7zip()
    if not seven_zip:
        raise RuntimeError("No zstd decompressor is available")
    result = subprocess.run(
        [seven_zip, "x", "-so", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"7-Zip could not decompress {path}: {message}")
    return io.BytesIO(result.stdout)


def calculate_usage_cost(
    pricing: Pricing,
    timestamp: datetime,
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    local = timestamp.astimezone(pricing.local_zone)
    price, is_peak, pricing_error = pricing.price_for(timestamp, model)
    if price is None:
        return {
            "cost_cny": 0.0,
            "peak_cost_cny": 0.0,
            "off_peak_cost_cny": 0.0,
            "is_peak": int(is_peak),
            "local_date": local.strftime("%Y-%m-%d"),
            "local_month": local.strftime("%Y-%m"),
            "priced": -1,
            "pricing_error": pricing_error,
        }

    miss_tokens = max(0, input_tokens - cached_tokens)
    cost = (
        cached_tokens / 1_000_000.0 * price.cache_hit
        + miss_tokens / 1_000_000.0 * price.cache_miss
        + output_tokens / 1_000_000.0 * price.output
    )
    return {
        "cost_cny": cost,
        "peak_cost_cny": cost if is_peak else 0.0,
        "off_peak_cost_cny": 0.0 if is_peak else cost,
        "is_peak": int(is_peak),
        "local_date": local.strftime("%Y-%m-%d"),
        "local_month": local.strftime("%Y-%m"),
        "priced": 1,
        "pricing_error": None,
    }


def process_session_file(
    connection: sqlite3.Connection, path: Path, pricing: Pricing
) -> bool:
    file_path = str(path.resolve())
    stat = path.stat()
    state = connection.execute(
        "SELECT size, mtime_ns, offset, session_id FROM file_state WHERE path = ?",
        (file_path,),
    ).fetchone()

    offset = int(state[2]) if state else 0
    if stat.st_size < offset or (state and stat.st_mtime_ns < int(state[1])):
        offset = 0

    session_id = state[3] if state and offset > 0 else None
    if stat.st_size == offset:
        return True

    inserted = 0
    compressed = path.name.endswith(".jsonl.zst")
    try:
        handle = open_zstd_stream(path) if compressed else path.open("rb")
    except Exception:
        logging.exception("Could not open compressed session %s", path)
        return False

    try:
        handle.seek(offset)
        while True:
            line_start = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            complete_line = raw.endswith(b"\n")
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if not complete_line:
                    handle.seek(line_start)
                    break
                logging.warning("Skipped malformed line in %s", file_path)
                continue

            record_type = record.get("type")
            payload = record.get("payload") or {}
            if record_type == "session_meta":
                session_id = payload.get("session_id") or session_id
                continue

            if record_type == "turn_context":
                turn_id = payload.get("turn_id")
                model = payload.get("model")
                if session_id and turn_id and model:
                    connection.execute(
                        """
                        INSERT INTO turn_models(session_id, turn_id, model)
                        VALUES (?, ?, ?)
                        ON CONFLICT(session_id, turn_id) DO UPDATE SET model = excluded.model
                        """,
                        (session_id, turn_id, model),
                    )
                continue

            if record_type != "token_usage_record":
                continue

            usage = payload.get("usage") or {}
            response_id = payload.get("response_id")
            timestamp_text = record.get("timestamp")
            payload_session = payload.get("session_id") or session_id
            turn_id = payload.get("turn_id")
            if not response_id or not timestamp_text or not payload_session or not turn_id:
                continue

            model_row = connection.execute(
                "SELECT model FROM turn_models WHERE session_id = ? AND turn_id = ?",
                (payload_session, turn_id),
            ).fetchone()
            model = model_row[0] if model_row else None
            if not model:
                model = "unknown"

            timestamp = parse_iso(timestamp_text).astimezone(timezone.utc)
            input_tokens = int(usage.get("input_tokens", 0))
            cached_tokens = int(usage.get("cached_input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            reasoning_tokens = int(usage.get("reasoning_output_tokens", 0))
            priced = calculate_usage_cost(
                pricing,
                timestamp,
                model,
                input_tokens,
                cached_tokens,
                output_tokens,
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO usage(
                    response_id, ts_utc, model, input_tokens, cached_tokens,
                    output_tokens, reasoning_tokens, session_id, turn_id,
                    cost_cny, peak_cost_cny, off_peak_cost_cny, is_peak,
                    local_date, local_month, priced
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response_id,
                    timestamp.isoformat(),
                    model,
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    reasoning_tokens,
                    payload_session,
                    turn_id,
                    priced["cost_cny"],
                    priced["peak_cost_cny"],
                    priced["off_peak_cost_cny"],
                    priced["is_peak"],
                    priced["local_date"],
                    priced["local_month"],
                    priced["priced"],
                ),
            )
            inserted += cursor.rowcount

        final_offset = stat.st_size if compressed else handle.tell()
    finally:
        handle.close()

    connection.execute(
        """
        INSERT INTO file_state(path, size, mtime_ns, offset, session_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            size = excluded.size,
            mtime_ns = excluded.mtime_ns,
            offset = excluded.offset,
            session_id = COALESCE(excluded.session_id, file_state.session_id)
        """,
        (file_path, stat.st_size, stat.st_mtime_ns, final_offset, session_id),
    )
    if inserted:
        logging.info("Indexed %d new usage records from %s", inserted, file_path)
    return True


def backfill_usage_costs(
    connection: sqlite3.Connection, pricing: Pricing
) -> tuple[int, int]:
    rows = connection.execute(
        """
        SELECT response_id, ts_utc, model, input_tokens, cached_tokens, output_tokens
        FROM usage
        WHERE priced = 0
        """
    ).fetchall()
    priced_count = 0
    unpriced_count = 0
    for row in rows:
        timestamp = parse_iso(row["ts_utc"])
        priced = calculate_usage_cost(
            pricing,
            timestamp,
            row["model"],
            int(row["input_tokens"]),
            int(row["cached_tokens"]),
            int(row["output_tokens"]),
        )
        connection.execute(
            """
            UPDATE usage SET
                cost_cny = ?, peak_cost_cny = ?, off_peak_cost_cny = ?,
                is_peak = ?, local_date = ?, local_month = ?, priced = ?
            WHERE response_id = ?
            """,
            (
                priced["cost_cny"],
                priced["peak_cost_cny"],
                priced["off_peak_cost_cny"],
                priced["is_peak"],
                priced["local_date"],
                priced["local_month"],
                priced["priced"],
                row["response_id"],
            ),
        )
        if priced["priced"] == 1:
            priced_count += 1
        else:
            unpriced_count += 1
            if unpriced_count <= 5:
                logging.warning(
                    "Usage record %s could not be priced: %s",
                    row["response_id"],
                    priced["pricing_error"],
                )
    return priced_count, unpriced_count


def ingest(pricing: Pricing) -> tuple[int, int]:
    now_local = datetime.now(tz=pricing.local_zone)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    seven_day_start = today_start - timedelta(days=6)
    history_start = today_start - timedelta(days=HISTORY_DAYS)
    scan_start = min(today_start, week_start, month_start, seven_day_start, history_start)
    connection = connect_db(pricing)
    files = discover_files(scan_start.date())
    skipped_compressed = 0
    try:
        for path in files:
            try:
                if not process_session_file(connection, path, pricing):
                    skipped_compressed += 1
            except (OSError, sqlite3.Error):
                logging.exception("Could not index %s", path)
                if path.name.endswith(".jsonl.zst"):
                    skipped_compressed += 1
        priced_count, unpriced_count = backfill_usage_costs(connection, pricing)
        if priced_count or unpriced_count:
            logging.info(
                "Pricing backfill completed: priced=%d unpriced=%d",
                priced_count,
                unpriced_count,
            )
        connection.commit()
    finally:
        connection.close()
    return len(files), skipped_compressed


def empty_period(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "cost": 0.0,
        "peak_cost": 0.0,
        "off_peak_cost": 0.0,
        "peak_tokens": 0,
        "off_peak_tokens": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "responses": 0,
        "cache_hit_rate": 0.0,
        "peak_cost_share": 0.0,
        "off_peak_cost_share": 0.0,
        "peak_token_share": 0.0,
        "off_peak_token_share": 0.0,
        "average_cost": 0.0,
        "unpriced_responses": 0,
        "unpriced_tokens": 0,
    }


def finalize_period(period: dict[str, Any]) -> None:
    input_tokens = int(period["input_tokens"])
    total_tokens = int(period["total_tokens"])
    total_cost = float(period["cost"])
    responses = int(period["responses"])
    period["cache_hit_rate"] = (
        int(period["cached_tokens"]) / input_tokens if input_tokens else 0.0
    )
    period["peak_cost_share"] = (
        float(period["peak_cost"]) / total_cost if total_cost else 0.0
    )
    period["off_peak_cost_share"] = (
        float(period["off_peak_cost"]) / total_cost if total_cost else 0.0
    )
    period["peak_token_share"] = (
        int(period["peak_tokens"]) / total_tokens if total_tokens else 0.0
    )
    period["off_peak_token_share"] = (
        int(period["off_peak_tokens"]) / total_tokens if total_tokens else 0.0
    )
    period["average_cost"] = total_cost / responses if responses else 0.0


def period_from_aggregate(name: str, row: sqlite3.Row) -> dict[str, Any]:
    period = empty_period(name)
    period["cost"] = float(row["cost_cny"])
    period["peak_cost"] = float(row["peak_cost_cny"])
    period["off_peak_cost"] = float(row["off_peak_cost_cny"])
    period["peak_tokens"] = int(row["peak_tokens"])
    period["off_peak_tokens"] = int(row["off_peak_tokens"])
    period["input_tokens"] = int(row["input_tokens"])
    period["cached_tokens"] = int(row["cached_tokens"])
    period["output_tokens"] = int(row["output_tokens"])
    period["reasoning_tokens"] = int(row["reasoning_tokens"])
    period["total_tokens"] = period["input_tokens"] + period["output_tokens"]
    period["responses"] = int(row["responses"])
    period["unpriced_responses"] = int(row["unpriced_responses"])
    period["unpriced_tokens"] = int(row["unpriced_tokens"])
    return period


def combine_periods(name: str, sources: Iterable[dict[str, Any]]) -> dict[str, Any]:
    result = empty_period(name)
    numeric_fields = (
        "cost",
        "peak_cost",
        "off_peak_cost",
        "peak_tokens",
        "off_peak_tokens",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "responses",
        "unpriced_responses",
        "unpriced_tokens",
    )
    for source in sources:
        for field in numeric_fields:
            result[field] += source[field]
    return result


def query_snapshot(
    pricing: Pricing, files_scanned: int = 0, compressed_skipped: int = 0
) -> dict[str, Any]:
    now_local = datetime.now(tz=pricing.local_zone)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    seven_day_start = today_start - timedelta(days=6)
    daily_start = today_start - timedelta(days=13)
    history_start = today_start - timedelta(days=HISTORY_DAYS)
    daily: dict[str, dict[str, Any]] = {}
    monthly: dict[str, dict[str, Any]] = {}

    aggregate_columns = """
        COUNT(*) AS responses,
        COALESCE(SUM(input_tokens), 0) AS input_tokens,
        COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
        COALESCE(SUM(output_tokens), 0) AS output_tokens,
        COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
        COALESCE(SUM(cost_cny), 0) AS cost_cny,
        COALESCE(SUM(peak_cost_cny), 0) AS peak_cost_cny,
        COALESCE(SUM(off_peak_cost_cny), 0) AS off_peak_cost_cny,
        COALESCE(SUM(CASE WHEN is_peak = 1 THEN input_tokens + output_tokens ELSE 0 END), 0) AS peak_tokens,
        COALESCE(SUM(CASE WHEN is_peak = 0 THEN input_tokens + output_tokens ELSE 0 END), 0) AS off_peak_tokens,
        COALESCE(SUM(CASE WHEN priced = -1 THEN 1 ELSE 0 END), 0) AS unpriced_responses,
        COALESCE(SUM(CASE WHEN priced = -1 THEN input_tokens + output_tokens ELSE 0 END), 0) AS unpriced_tokens
    """

    connection = connect_db(pricing)
    try:
        backfill_usage_costs(connection, pricing)
        connection.commit()
        daily_rows = connection.execute(
            f"""
            SELECT local_date AS bucket, {aggregate_columns}
            FROM usage
            WHERE local_date >= ?
            GROUP BY local_date
            ORDER BY local_date DESC
            """,
            (daily_start.strftime("%Y-%m-%d"),),
        ).fetchall()
        monthly_rows = connection.execute(
            f"""
            SELECT local_month AS bucket, {aggregate_columns}
            FROM usage
            WHERE local_month >= ?
            GROUP BY local_month
            ORDER BY local_month DESC
            """,
            (history_start.strftime("%Y-%m"),),
        ).fetchall()
    finally:
        connection.close()

    for row in daily_rows:
        daily[str(row["bucket"])] = period_from_aggregate(
            str(row["bucket"]), row
        )
    for row in monthly_rows:
        monthly[str(row["bucket"])] = period_from_aggregate(
            str(row["bucket"]), row
        )

    today_key = today_start.strftime("%Y-%m-%d")
    week_key = week_start.strftime("%Y-%m-%d")
    seven_key = seven_day_start.strftime("%Y-%m-%d")
    month_key = month_start.strftime("%Y-%m")
    periods = {
        "today": daily.get(today_key, empty_period("今日")),
        "week": combine_periods(
            "本周",
            (period for key, period in daily.items() if key >= week_key),
        ),
        "month": monthly.get(month_key, empty_period("本月")),
        "seven_days": combine_periods(
            "近 7 天",
            (period for key, period in daily.items() if key >= seven_key),
        ),
    }

    for period in periods.values():
        finalize_period(period)
    for period in daily.values():
        finalize_period(period)
    for period in monthly.values():
        finalize_period(period)

    return {
        "generated_at": now_local.isoformat(),
        "currency": pricing.config.get("currency", "CNY"),
        "periods": periods,
        "daily": [daily[key] for key in sorted(daily, reverse=True)],
        "monthly": [monthly[key] for key in sorted(monthly, reverse=True)],
        "files_scanned": files_scanned,
        "compressed_sessions_skipped": compressed_skipped,
        "configuration_warnings": pricing.config_warnings,
    }


def format_money(value: float) -> str:
    return f"¥{value:,.2f}"


def format_compact_money(value: float) -> str:
    if value < 10:
        return f"¥{value:.2f}"
    if value < 100:
        return f"¥{value:.1f}"
    return f"¥{value:.0f}"


def format_average_money(value: float) -> str:
    return f"¥{value:,.3f}"


def format_tokens(value: int) -> str:
    value = max(0, int(value))
    if value < 10_000:
        return f"{value:,}"
    if value < 100_000_000:
        scaled = value / 10_000
        unit = "万"
    else:
        scaled = value / 100_000_000
        unit = "亿"
    decimals = 2 if scaled < 10 else 1 if scaled < 100 else 0
    text = f"{scaled:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def format_percent(value: Any) -> str:
    return f"{float(value or 0.0) * 100:.1f}%"


def get_work_areas(root: tk.Tk) -> list[tuple[int, int, int, int, bool]]:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    areas: list[tuple[int, int, int, int, bool]] = []
    monitor_enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(RECT),
        ctypes.c_void_p,
    )

    def callback(hmonitor: Any, _hdc: Any, _rect: Any, _data: Any) -> bool:
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            work = info.rcWork
            areas.append(
                (
                    int(work.left),
                    int(work.top),
                    int(work.right),
                    int(work.bottom),
                    bool(info.dwFlags & 1),
                )
            )
        return True

    try:
        ctypes.windll.user32.EnumDisplayMonitors(
            0, 0, monitor_enum_proc(callback), 0
        )
    except Exception:
        areas = []
    if not areas:
        areas.append(
            (
                0,
                0,
                root.winfo_screenwidth(),
                root.winfo_screenheight(),
                True,
            )
        )
    return areas


def get_work_area(
    root: tk.Tk, x: int | None = None, y: int | None = None
) -> tuple[int, int, int, int]:
    areas = get_work_areas(root)
    if x is not None and y is not None:
        for left, top, right, bottom, _primary in areas:
            if left <= x < right and top <= y < bottom:
                return left, top, right, bottom
    for left, top, right, bottom, primary in areas:
        if primary:
            return left, top, right, bottom
    return areas[0][:4]


def partner_hint() -> tuple[int, int] | None:
    state_path = CODEX_HOME / ".codex-global-state.json"
    try:
        state = load_json(state_path)
        bounds = state.get("electron-avatar-overlay-bounds") or {}
        if bounds.get("x") is not None and bounds.get("y") is not None:
            return int(bounds["x"]), int(bounds["y"])
    except Exception:
        logging.debug("Could not read partner area", exc_info=True)
    return None


class UsageWidget:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.975)
        try:
            self.root.attributes("-toolwindow", True)
        except tk.TclError:
            pass

        self.root.configure(bg=TRANSPARENT_COLOR)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.expanded = False
        self.anchor_x = 0
        self.anchor_y = 0
        self.outside_since: float | None = None
        self.hover_since: float | None = None
        self.hover_armed = True
        self.last_pointer_xy: tuple[int, int] | None = None
        self.ball_pressed = False
        self.ball_drag_moved = False
        self.ball_press_root_x = 0
        self.ball_press_root_y = 0
        self.ball_phase = 0.0
        self.ball_bob_offset = 0
        self.ball_next_blink = time.monotonic() + 3.0
        self.ball_blink_until = 0.0
        self.ball_float_items: list[int] = []
        self.ball_eye_left: int | None = None
        self.ball_eye_right: int | None = None
        self.ball_left_arm: int | None = None
        self.ball_right_arm: int | None = None
        self.ball_halo: int | None = None
        self.follow_partner = True
        self.last_partner_position: tuple[int, int] | None = None
        self.last_snapshot: dict[str, Any] | None = None
        self.details_window: tk.Toplevel | None = None
        self.details_mode = "daily"
        self.details_tree: ttk.Treeview | None = None
        self.details_title: tk.Label | None = None
        self.details_daily_button: tk.Label | None = None
        self.details_monthly_button: tk.Label | None = None

        self._create_fonts()
        self._build_ui()
        self._restore_anchor()
        self._start_worker()
        self.root.after(150, self._poll_queue)
        self.root.after(100, self._poll_hover)
        self.root.after(500, self._poll_partner)
        self.root.after(80, self._animate_ball)
        self.root.after(500, self._check_stop_flag)

    def _create_fonts(self) -> None:
        family = "Microsoft YaHei UI"
        self.font_title = tkfont.Font(family=family, size=10, weight="bold")
        self.font_label = tkfont.Font(family=family, size=9)
        self.font_cost = tkfont.Font(family=family, size=19, weight="bold")
        self.font_small = tkfont.Font(family=family, size=8)
        self.font_symbol = tkfont.Font(family=family, size=13)

    def _build_ui(self) -> None:
        self.panel_border = tk.Frame(self.root, bg="#27313E", padx=1, pady=1)
        self.panel = tk.Frame(self.panel_border, bg="#11161E")
        self.panel.pack(fill="both", expand=True)

        header = tk.Frame(self.panel, bg="#11161E", height=34)
        header.pack(fill="x", padx=14, pady=(5, 0))
        header.pack_propagate(False)

        self.status_dot = tk.Canvas(header, width=12, height=12, bg="#11161E", highlightthickness=0)
        self.status_dot.create_oval(2, 2, 10, 10, fill="#21D69B", outline="")
        self.status_dot.pack(side="left", pady=(3, 0))

        title = tk.Label(
            header,
            text="DEEPSEEK 实时用量",
            fg="#F2F6FA",
            bg="#11161E",
            font=self.font_title,
        )
        title.pack(side="left", padx=(7, 0))

        close = tk.Label(
            header,
            text="×",
            fg="#718096",
            bg="#11161E",
            font=self.font_symbol,
            cursor="hand2",
            padx=6,
        )
        close.pack(side="right")
        close.bind("<Button-1>", lambda _event: self.close())
        close.bind("<Enter>", lambda _event: close.configure(fg="#FFFFFF"))
        close.bind("<Leave>", lambda _event: close.configure(fg="#718096"))

        refresh = tk.Label(
            header,
            text="↻",
            fg="#718096",
            bg="#11161E",
            font=self.font_symbol,
            cursor="hand2",
            padx=5,
        )
        refresh.pack(side="right")
        refresh.bind("<Button-1>", lambda _event: self._trigger_refresh())
        refresh.bind("<Enter>", lambda _event: refresh.configure(fg="#FFFFFF"))
        refresh.bind("<Leave>", lambda _event: refresh.configure(fg="#718096"))

        for widget in (header, title, self.status_dot):
            widget.bind("<ButtonPress-1>", self._begin_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)

        cards = tk.Frame(self.panel, bg="#11161E")
        cards.pack(fill="both", expand=True, padx=12, pady=(2, 3))
        self.cost_labels: dict[str, tk.Label] = {}
        self.token_labels: dict[str, tk.Label] = {}
        self.token_input_labels: dict[str, tk.Label] = {}
        self.token_output_labels: dict[str, tk.Label] = {}
        self.detail_labels: dict[str, tk.Label] = {}
        self.cache_labels: dict[str, tk.Label] = {}
        self.period_share_labels: dict[str, tk.Label] = {}

        for column, key in enumerate(("today", "week", "month")):
            cards.grid_columnconfigure(column, weight=1)
            card = tk.Frame(
                cards,
                bg="#171E28",
                highlightthickness=1,
                highlightbackground="#27313E",
                padx=13,
                pady=5,
            )
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0))

            label_text = {"today": "今日", "week": "本周", "month": "本月"}[key]
            tk.Label(
                card,
                text=label_text,
                fg="#8D99A8",
                bg="#171E28",
                font=self.font_label,
                anchor="w",
            ).pack(fill="x")

            cost_label = tk.Label(
                card,
                text="¥0.00",
                fg="#F5F8FC",
                bg="#171E28",
                font=self.font_cost,
                anchor="w",
            )
            cost_label.pack(fill="x", pady=(1, 0))
            self.cost_labels[key] = cost_label

            token_label = tk.Label(
                card,
                text="总 0",
                fg="#69D9B2",
                bg="#171E28",
                font=self.font_small,
                anchor="w",
            )
            token_label.pack(fill="x")
            self.token_labels[key] = token_label

            token_breakdown = tk.Frame(card, bg="#171E28")
            token_breakdown.pack(fill="x", pady=(0, 1))
            input_label = tk.Label(
                token_breakdown,
                text="入 0",
                fg="#69D9B2",
                bg="#171E28",
                font=self.font_small,
                anchor="w",
            )
            input_label.pack(side="left")
            output_label = tk.Label(
                token_breakdown,
                text="出 0",
                fg="#74C7FF",
                bg="#171E28",
                font=self.font_small,
                anchor="w",
            )
            output_label.pack(side="left", padx=(12, 0))
            self.token_input_labels[key] = input_label
            self.token_output_labels[key] = output_label

            detail_label = tk.Label(
                card,
                text="0次 · 均次¥0.00",
                fg="#687586",
                bg="#171E28",
                font=self.font_small,
                anchor="w",
            )
            detail_label.pack(fill="x")
            self.detail_labels[key] = detail_label

            cache_label = tk.Label(
                card,
                text="命中率 0.0%",
                fg="#74C7FF",
                bg="#171E28",
                font=self.font_small,
                anchor="w",
            )
            cache_label.pack(fill="x", pady=(1, 0))
            self.cache_labels[key] = cache_label

            share_label = tk.Label(
                card,
                text="高峰 0.0% · 低峰 0.0%",
                fg="#8D99A8",
                bg="#171E28",
                font=self.font_small,
                anchor="w",
            )
            share_label.pack(fill="x")
            self.period_share_labels[key] = share_label

        footer = tk.Frame(self.panel, bg="#11161E", height=20)
        footer.pack(fill="x", padx=14, pady=(0, 5))
        footer.pack_propagate(False)

        self.footer_label = tk.Label(
            footer,
            text="Codex 本地记录 · 正在索引",
            fg="#667283",
            bg="#11161E",
            font=self.font_small,
            anchor="w",
        )
        self.footer_label.pack(side="left", fill="x", expand=True)

        self.seven_day_label = tk.Label(
            footer,
            text="近7天 ¥0.00",
            fg="#9BA7B6",
            bg="#11161E",
            font=self.font_small,
            anchor="e",
        )
        self.seven_day_label.pack(side="right")

        details = tk.Label(
            footer,
            text="明细",
            fg="#69D9B2",
            bg="#11161E",
            font=self.font_small,
            cursor="hand2",
            padx=8,
        )
        details.pack(side="right")
        details.bind("<Button-1>", lambda _event: self._open_details())

        self.ball_frame = tk.Frame(self.root, bg=TRANSPARENT_COLOR)
        self.ball_canvas = tk.Canvas(
            self.ball_frame,
            width=BALL_WIDTH,
            height=BALL_HEIGHT,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
            cursor="hand2",
        )
        self.ball_canvas.pack(fill="both", expand=True)

        self.ball_halo = self.ball_canvas.create_oval(
            5,
            3,
            51,
            69,
            fill="",
            outline="#4B1D25",
            width=2,
        )
        self.ball_canvas.create_oval(16, 65, 40, 70, fill="#071014", outline="")
        self.ball_left_arm = self.ball_canvas.create_line(
            14, 40, 8, 47, fill="#FF6978", width=2, smooth=True
        )
        self.ball_right_arm = self.ball_canvas.create_line(
            42, 40, 48, 47, fill="#FF6978", width=2, smooth=True
        )

        body = self.ball_canvas.create_polygon(
            28,
            6,
            41,
            10,
            45,
            28,
            40,
            35,
            40,
            58,
            35,
            66,
            21,
            66,
            16,
            58,
            16,
            35,
            11,
            28,
            15,
            10,
            smooth=True,
            splinesteps=24,
            fill="#13212B",
            outline="#FF5E6D",
            width=2,
        )
        face = self.ball_canvas.create_oval(
            16, 16, 40, 34, fill="#091019", outline="#284B53", width=1
        )
        self.ball_eye_left = self.ball_canvas.create_oval(
            21, 23, 25, 27, fill="#C8FFF1", outline=""
        )
        self.ball_eye_right = self.ball_canvas.create_oval(
            31, 23, 35, 27, fill="#C8FFF1", outline=""
        )
        mouth = self.ball_canvas.create_line(
            26, 30, 30, 30, fill="#69D9B2", width=1
        )
        chest = self.ball_canvas.create_oval(
            24, 45, 32, 53, fill="#10312E", outline="#21D69B", width=1
        )
        antenna = self.ball_canvas.create_line(
            28, 7, 28, 2, fill="#FF7A87", width=2
        )
        self.ball_status = self.ball_canvas.create_oval(
            25, 0, 31, 6, fill="#21D69B", outline=TRANSPARENT_COLOR, width=1
        )
        self.ball_float_items = [body, face, mouth, chest, antenna, self.ball_status]
        self.ball_canvas.create_rectangle(
            62,
            45,
            116,
            67,
            fill="#151B24",
            outline="#8C2D39",
            width=1,
        )
        self.ball_canvas.create_oval(
            57, 45, 67, 67, fill="#151B24", outline="#8C2D39", width=1
        )
        self.ball_canvas.create_oval(
            111, 45, 121, 67, fill="#151B24", outline="#8C2D39", width=1
        )
        self.ball_cost_text = self.ball_canvas.create_text(
            89,
            56,
            text="¥0.00",
            fill="#FF9CA5",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.ball_canvas.bind("<ButtonPress-1>", self._begin_ball_drag)
        self.ball_canvas.bind("<B1-Motion>", self._ball_drag)
        self.ball_canvas.bind("<ButtonRelease-1>", self._end_ball_drag)

    def _animate_ball(self) -> None:
        if (
            hasattr(self, "ball_canvas")
            and not self.expanded
            and self.ball_frame.winfo_ismapped()
        ):
            self.ball_phase += 0.16
            target_offset = round(1.4 * math.sin(self.ball_phase))
            delta = target_offset - self.ball_bob_offset
            if delta:
                for item in self.ball_float_items:
                    self.ball_canvas.move(item, 0, delta)
            self.ball_bob_offset = target_offset

            arm_wave = round(1.5 * math.sin(self.ball_phase * 1.35))
            base_y = target_offset
            if self.ball_left_arm is not None:
                self.ball_canvas.coords(
                    self.ball_left_arm,
                    14,
                    40 + base_y,
                    7,
                    47 + base_y + arm_wave,
                )
            if self.ball_right_arm is not None:
                self.ball_canvas.coords(
                    self.ball_right_arm,
                    42,
                    40 + base_y,
                    49,
                    47 + base_y - arm_wave,
                )

            if self.ball_halo is not None:
                halo_colors = ["#4B1D25", "#6A2631", "#8D3140", "#6A2631"]
                halo_index = int((self.ball_phase * 0.45) % len(halo_colors))
                self.ball_canvas.itemconfigure(
                    self.ball_halo, outline=halo_colors[halo_index]
                )

            now = time.monotonic()
            if now >= self.ball_next_blink:
                self.ball_blink_until = now + 0.13
                self.ball_next_blink = now + 3.2 + (int(self.ball_phase * 10) % 5) * 0.35
            blinking = now < self.ball_blink_until
            if self.ball_eye_left is not None and self.ball_eye_right is not None:
                if blinking:
                    self.ball_canvas.coords(
                        self.ball_eye_left, 21, 25 + base_y, 25, 25 + base_y
                    )
                    self.ball_canvas.coords(
                        self.ball_eye_right, 31, 25 + base_y, 35, 25 + base_y
                    )
                else:
                    self.ball_canvas.coords(
                        self.ball_eye_left, 21, 23 + base_y, 25, 27 + base_y
                    )
                    self.ball_canvas.coords(
                        self.ball_eye_right, 31, 23 + base_y, 35, 27 + base_y
                    )

        self.root.after(80, self._animate_ball)

    def _restore_anchor(self) -> None:
        show = partner_hint()
        settings: dict[str, Any] = {}
        try:
            settings = load_json(SETTINGS_PATH)
        except Exception:
            pass
        self.follow_partner = bool(settings.get("follow_partner", True))
        self.last_partner_position = show

        point_x = show[0] if show else None
        point_y = show[1] if show else None
        left, top, right, bottom = get_work_area(self.root, point_x, point_y)
        if show and self.follow_partner:
            x = max(left + 12, show[0] - BALL_WIDTH - 12)
            y = min(bottom - BALL_HEIGHT - 12, max(top + 12, show[1]))
        elif show:
            x = show[0] - BALL_WIDTH - 12
            y = show[1]
        else:
            x = right - BALL_WIDTH - 24
            y = bottom - BALL_HEIGHT - 16

        if not self.follow_partner:
            if "ball_x" in settings and "ball_y" in settings:
                x = int(settings["ball_x"])
                y = int(settings["ball_y"])
            elif "x" in settings and "y" in settings:
                x = int(settings["x"]) + WIDGET_WIDTH - BALL_WIDTH
                y = int(settings["y"]) + WIDGET_HEIGHT - BALL_HEIGHT
            left, top, right, bottom = get_work_area(self.root, int(x), int(y))

        self.anchor_x = min(max(x, left), right - BALL_WIDTH)
        self.anchor_y = min(max(y, top), bottom - BALL_HEIGHT)
        self._show_ball()

    def _show_ball(self) -> None:
        self.expanded = False
        self.panel_border.pack_forget()
        if not self.ball_frame.winfo_ismapped():
            self.ball_frame.pack(fill="both", expand=True)
        self.root.geometry(
            f"{BALL_WIDTH}x{BALL_HEIGHT}+{self.anchor_x}+{self.anchor_y}"
        )

    def _expand_panel(self) -> None:
        if self.expanded:
            return
        left, top, right, bottom = get_work_area(
            self.root, self.anchor_x, self.anchor_y
        )
        x = self.anchor_x + BALL_WIDTH - WIDGET_WIDTH
        y = self.anchor_y + BALL_HEIGHT - WIDGET_HEIGHT
        x = min(max(x, left), right - WIDGET_WIDTH)
        y = min(max(y, top), bottom - WIDGET_HEIGHT)
        self.ball_frame.pack_forget()
        if not self.panel_border.winfo_ismapped():
            self.panel_border.pack(fill="both", expand=True)
        self.expanded = True
        self.outside_since = None
        self.root.geometry(f"{WIDGET_WIDTH}x{WIDGET_HEIGHT}+{x}+{y}")

    def _poll_partner(self) -> None:
        show = partner_hint()
        if show and self.follow_partner and show != self.last_partner_position:
            self.last_partner_position = show
            left, top, right, bottom = get_work_area(self.root, show[0], show[1])
            self.anchor_x = min(
                max(show[0] - BALL_WIDTH - 12, left), right - BALL_WIDTH
            )
            self.anchor_y = min(
                max(top + 12, min(show[1], bottom - BALL_HEIGHT - 12)),
                bottom - BALL_HEIGHT,
            )
            if self.expanded:
                x = min(
                    max(self.anchor_x + BALL_WIDTH - WIDGET_WIDTH, left),
                    right - WIDGET_WIDTH,
                )
                y = min(
                    max(self.anchor_y + BALL_HEIGHT - WIDGET_HEIGHT, top),
                    bottom - WIDGET_HEIGHT,
                )
                self.root.geometry(f"{WIDGET_WIDTH}x{WIDGET_HEIGHT}+{x}+{y}")
            else:
                self.root.geometry(
                    f"{BALL_WIDTH}x{BALL_HEIGHT}+{self.anchor_x}+{self.anchor_y}"
                )
        elif show:
            self.last_partner_position = show
        self.root.after(1500, self._poll_partner)

    def _collapse_panel(self) -> None:
        if not self.expanded:
            return
        self._show_ball()

    def _pointer_inside_widget(self) -> bool:
        pointer_x, pointer_y = self.root.winfo_pointerxy()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        return (
            x <= pointer_x < x + self.root.winfo_width()
            and y <= pointer_y < y + self.root.winfo_height()
        )

    def _pointer_inside_details(self) -> bool:
        if self.details_window is None or not self.details_window.winfo_exists():
            return False
        pointer_x, pointer_y = self.root.winfo_pointerxy()
        x = self.details_window.winfo_x()
        y = self.details_window.winfo_y()
        return (
            x <= pointer_x < x + self.details_window.winfo_width()
            and y <= pointer_y < y + self.details_window.winfo_height()
        )

    def _poll_hover(self) -> None:
        now = time.monotonic()
        inside = self._pointer_inside_widget() or self._pointer_inside_details()
        pointer = self.root.winfo_pointerxy()
        if inside:
            self.outside_since = None
            if (
                not self.expanded
                and not self.ball_pressed
                and self.hover_armed
            ):
                if pointer != self.last_pointer_xy:
                    self.hover_since = now
                elif self.hover_since is None:
                    self.hover_since = now
                elif now - self.hover_since >= HOVER_EXPAND_SECONDS:
                    self.hover_since = None
                    self._expand_panel()
            self.last_pointer_xy = pointer
        else:
            self.hover_since = None
            self.hover_armed = True
            self.last_pointer_xy = pointer
            if self.expanded:
                if self.outside_since is None:
                    self.outside_since = now
                elif now - self.outside_since >= HOVER_COLLAPSE_SECONDS:
                    self._collapse_panel()
        if self.expanded:
            self.hover_since = None
        self.root.after(100, self._poll_hover)

    def _snapped_position(
        self, x: int, y: int, width: int, height: int
    ) -> tuple[int, int, int, int, int, int]:
        left, top, right, bottom = get_work_area(self.root, x, y)
        x = min(max(x, left), right - width)
        y = min(max(y, top), bottom - height)
        if abs(x - left) <= EDGE_SNAP_DISTANCE:
            x = left
        elif abs((right - width) - x) <= EDGE_SNAP_DISTANCE:
            x = right - width
        if abs(y - top) <= EDGE_SNAP_DISTANCE:
            y = top
        elif abs((bottom - height) - y) <= EDGE_SNAP_DISTANCE:
            y = bottom - height
        return x, y, left, top, right, bottom

    def _begin_ball_drag(self, event: tk.Event[Any]) -> None:
        self.ball_pressed = True
        self.ball_drag_moved = False
        self.hover_since = None
        self.drag_offset_x = event.x_root - self.root.winfo_x()
        self.drag_offset_y = event.y_root - self.root.winfo_y()

    def _ball_drag(self, event: tk.Event[Any]) -> None:
        if not self.ball_pressed:
            return
        x = event.x_root - self.drag_offset_x
        y = event.y_root - self.drag_offset_y
        if not self.ball_drag_moved:
            if (
                abs(x - self.root.winfo_x()) <= DRAG_THRESHOLD
                and abs(y - self.root.winfo_y()) <= DRAG_THRESHOLD
            ):
                return
            self.ball_drag_moved = True
            self.follow_partner = False
            self.hover_armed = False
        left, top, right, bottom = get_work_area(
            self.root, event.x_root, event.y_root
        )
        x = min(max(x, left), right - BALL_WIDTH)
        y = min(max(y, top), bottom - BALL_HEIGHT)
        self.anchor_x = x
        self.anchor_y = y
        self.root.geometry(f"+{x}+{y}")

    def _end_ball_drag(self, event: tk.Event[Any]) -> None:
        moved = self.ball_drag_moved
        self.ball_pressed = False
        self.ball_drag_moved = False
        if not moved:
            self._expand_panel()
            return
        self.hover_armed = False
        x, y, _left, _top, _right, _bottom = self._snapped_position(
            self.root.winfo_x(),
            self.root.winfo_y(),
            BALL_WIDTH,
            BALL_HEIGHT,
        )
        self.anchor_x = x
        self.anchor_y = y
        self.follow_partner = False
        self.root.geometry(f"{BALL_WIDTH}x{BALL_HEIGHT}+{x}+{y}")
        save_json(
            SETTINGS_PATH,
            {
                "ball_x": x,
                "ball_y": y,
                "follow_partner": False,
            },
        )

    def _begin_drag(self, event: tk.Event[Any]) -> None:
        self.drag_offset_x = event.x_root - self.root.winfo_x()
        self.drag_offset_y = event.y_root - self.root.winfo_y()

    def _drag(self, event: tk.Event[Any]) -> None:
        x = event.x_root - self.drag_offset_x
        y = event.y_root - self.drag_offset_y
        width = WIDGET_WIDTH if self.expanded else BALL_WIDTH
        height = WIDGET_HEIGHT if self.expanded else BALL_HEIGHT
        left, top, right, bottom = get_work_area(
            self.root, event.x_root, event.y_root
        )
        x = min(max(x, left), right - width)
        y = min(max(y, top), bottom - height)
        self.root.geometry(f"+{x}+{y}")

    def _end_drag(self, _event: tk.Event[Any]) -> None:
        if self.expanded:
            width = WIDGET_WIDTH
            height = WIDGET_HEIGHT
        else:
            width = BALL_WIDTH
            height = BALL_HEIGHT
        x, y, *_ = self._snapped_position(
            self.root.winfo_x(), self.root.winfo_y(), width, height
        )
        self.root.geometry(f"+{x}+{y}")
        if self.expanded:
            self.anchor_x = self.root.winfo_x() + WIDGET_WIDTH - BALL_WIDTH
            self.anchor_y = self.root.winfo_y() + WIDGET_HEIGHT - BALL_HEIGHT
        else:
            self.anchor_x = self.root.winfo_x()
            self.anchor_y = self.root.winfo_y()
        self.follow_partner = False
        save_json(
            SETTINGS_PATH,
            {
                "ball_x": self.anchor_x,
                "ball_y": self.anchor_y,
                "follow_partner": False,
            },
        )

    def _trigger_refresh(self) -> None:
        self.status_dot.itemconfigure(1, fill="#F8C64B")
        if hasattr(self, "ball_status"):
            self.ball_canvas.itemconfigure(self.ball_status, fill="#F8C64B")

    def _start_worker(self) -> None:
        thread = threading.Thread(target=self._worker_loop, name="usage-indexer", daemon=True)
        thread.start()

    def _worker_loop(self) -> None:
        pricing = Pricing(load_json(PRICING_PATH))
        for warning in pricing.config_warnings:
            logging.warning("Configuration warning: %s", warning)
        while not self.stop_event.is_set():
            try:
                files_scanned, compressed_skipped = ingest(pricing)
                snapshot = query_snapshot(pricing, files_scanned, compressed_skipped)
                self.queue.put(snapshot)
            except Exception:
                logging.exception("Usage refresh failed")
                self.queue.put({"error": True, "generated_at": datetime.now().isoformat()})
            stop_deadline = time.monotonic() + REFRESH_SECONDS
            while time.monotonic() < stop_deadline and not self.stop_event.is_set():
                time.sleep(0.2)

    def _poll_queue(self) -> None:
        try:
            while True:
                snapshot = self.queue.get_nowait()
                self._apply_snapshot(snapshot)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("error"):
            self.status_dot.itemconfigure(1, fill="#F36C6C")
            self.ball_canvas.itemconfigure(self.ball_status, fill="#F36C6C")
            self.footer_label.configure(text="刷新失败 · 请查看 widget.log")
            return

        self.last_snapshot = snapshot
        periods = snapshot.get("periods", {})
        for key in ("today", "week", "month"):
            period = periods.get(key, {})
            self.cost_labels[key].configure(text=format_money(float(period.get("cost", 0.0))))
            self.token_labels[key].configure(
                text=f"总 {format_tokens(int(period.get('total_tokens', 0)))}"
            )
            self.token_input_labels[key].configure(
                text=f"入 {format_tokens(int(period.get('input_tokens', 0)))}"
            )
            self.token_output_labels[key].configure(
                text=f"出 {format_tokens(int(period.get('output_tokens', 0)))}"
            )
            self.detail_labels[key].configure(
                text=(
                    f"{int(period.get('responses', 0)):,}次 · "
                    f"均次{format_average_money(float(period.get('average_cost', 0.0)))}"
                )
            )
            self.cache_labels[key].configure(
                text=f"命中率 {format_percent(period.get('cache_hit_rate', 0.0))}"
            )
            self.period_share_labels[key].configure(
                text=(
                    f"高峰 {format_percent(period.get('peak_cost_share', 0.0))} · "
                    f"低峰 {format_percent(period.get('off_peak_cost_share', 0.0))}"
                )
            )

        seven_days = periods.get("seven_days", {})
        compressed = int(snapshot.get("compressed_sessions_skipped", 0))
        suffix = f" · 跳过 {compressed} 个压缩文件" if compressed else ""
        config_warnings = snapshot.get("configuration_warnings", [])
        if config_warnings:
            suffix += f" · {config_warnings[0]}"
        today = periods.get("today", {})
        unpriced_tokens = max(
            int(periods.get(key, {}).get("unpriced_tokens", 0))
            for key in ("today", "week", "month")
        )
        if unpriced_tokens:
            suffix += f" · 未计价 {format_tokens(unpriced_tokens)}"
        self.ball_canvas.itemconfigure(
            self.ball_cost_text,
            text=format_compact_money(float(today.get("cost", 0.0))),
        )
        self.seven_day_label.configure(
            text=f"近7天 {format_money(float(seven_days.get('cost', 0.0)))}"
        )
        footer_text = (
            f"配置告警：{config_warnings[0]}"
            if config_warnings
            else f"Codex 本地记录 · 3秒刷新 · 峰谷按费用{suffix}"
        )
        self.footer_label.configure(
            text=footer_text,
            fg="#F8C64B" if config_warnings else "#667283",
        )
        try:
            generated = parse_iso(snapshot["generated_at"]).astimezone()
            self.root.title(f"{APP_NAME} · {generated:%H:%M:%S}")
        except Exception:
            pass
        status_color = "#F8C64B" if config_warnings else "#21D69B"
        self.status_dot.itemconfigure(1, fill=status_color)
        self.ball_canvas.itemconfigure(self.ball_status, fill=status_color)
        if self.details_window is not None and self.details_window.winfo_exists():
            self._render_details()

    def _open_details(self) -> None:
        if self.details_window is not None and self.details_window.winfo_exists():
            self.details_window.lift()
            return

        window = tk.Toplevel(self.root)
        window.title("DeepSeek 用量明细")
        window.configure(bg="#0E1218")
        window.geometry("960x540")
        window.minsize(800, 420)
        window.attributes("-topmost", True)
        self.details_window = window

        shell = tk.Frame(window, bg="#0E1218", padx=18, pady=16)
        shell.pack(fill="both", expand=True)

        heading = tk.Frame(shell, bg="#0E1218")
        heading.pack(fill="x")
        tk.Label(
            heading,
            text="DeepSeek 用量明细",
            fg="#F5F8FC",
            bg="#0E1218",
            font=tkfont.Font(family="Microsoft YaHei UI", size=15, weight="bold"),
        ).pack(side="left")
        close = tk.Label(
            heading,
            text="×",
            fg="#8D99A8",
            bg="#0E1218",
            font=tkfont.Font(family="Microsoft YaHei UI", size=16),
            cursor="hand2",
            padx=6,
        )
        close.pack(side="right")
        close.bind("<Button-1>", lambda _event: window.destroy())

        tabs = tk.Frame(shell, bg="#0E1218")
        tabs.pack(fill="x", pady=(14, 10))
        self.details_daily_button = tk.Label(
            tabs,
            text="最近每日",
            fg="#0E1218",
            bg="#69D9B2",
            padx=14,
            pady=6,
            cursor="hand2",
            font=tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold"),
        )
        self.details_daily_button.pack(side="left")
        self.details_monthly_button = tk.Label(
            tabs,
            text="每月汇总",
            fg="#9BA7B6",
            bg="#1A212B",
            padx=14,
            pady=6,
            cursor="hand2",
            font=tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold"),
        )
        self.details_monthly_button.pack(side="left", padx=(8, 0))
        self.details_daily_button.bind(
            "<Button-1>", lambda _event: self._set_details_mode("daily")
        )
        self.details_monthly_button.bind(
            "<Button-1>", lambda _event: self._set_details_mode("monthly")
        )

        self.details_title = tk.Label(
            shell,
            text="最近 14 天",
            fg="#8D99A8",
            bg="#0E1218",
            font=self.font_small,
            anchor="w",
        )
        self.details_title.pack(fill="x", pady=(0, 6))

        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure(
            "Usage.Treeview",
            background="#151B24",
            fieldbackground="#151B24",
            foreground="#E8EDF4",
            rowheight=31,
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Usage.Treeview.Heading",
            background="#202936",
            foreground="#AAB5C3",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "Usage.Treeview",
            background=[("selected", "#245D50")],
            foreground=[("selected", "#FFFFFF")],
        )

        table_frame = tk.Frame(shell, bg="#151B24")
        table_frame.pack(fill="both", expand=True)
        columns = (
            "period",
            "cost",
            "tokens",
            "input",
            "output",
            "cache",
            "peak",
            "off_peak",
            "requests",
        )
        tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            style="Usage.Treeview",
            selectmode="browse",
        )
        tree.heading("period", text="日期")
        tree.heading("cost", text="消费")
        tree.heading("tokens", text="总")
        tree.heading("input", text="入")
        tree.heading("output", text="出")
        tree.heading("cache", text="命中率")
        tree.heading("peak", text="高峰")
        tree.heading("off_peak", text="低峰")
        tree.heading("requests", text="请求次数")
        tree.column("period", width=135, anchor="w")
        tree.column("cost", width=95, anchor="e")
        tree.column("tokens", width=105, anchor="e")
        tree.column("input", width=110, anchor="e")
        tree.column("output", width=95, anchor="e")
        tree.column("cache", width=80, anchor="e")
        tree.column("peak", width=65, anchor="e")
        tree.column("off_peak", width=65, anchor="e")
        tree.column("requests", width=80, anchor="e")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.details_tree = tree
        self._render_details()

    def _set_details_mode(self, mode: str) -> None:
        self.details_mode = mode
        self._render_details()

    def _render_details(self) -> None:
        if self.details_tree is None or not self.details_tree.winfo_exists():
            return
        scroll_fraction = self.details_tree.yview()[0]
        selected = self.details_tree.selection()
        selected_id = selected[0] if selected else None
        for item in self.details_tree.get_children():
            self.details_tree.delete(item)
        if self.last_snapshot is None:
            return

        if self.details_daily_button and self.details_monthly_button:
            if self.details_mode == "daily":
                self.details_daily_button.configure(fg="#0E1218", bg="#69D9B2")
                self.details_monthly_button.configure(fg="#9BA7B6", bg="#1A212B")
                if self.details_title:
                    self.details_title.configure(text="最近 14 天 · 按北京时间自然日")
            else:
                self.details_daily_button.configure(fg="#9BA7B6", bg="#1A212B")
                self.details_monthly_button.configure(fg="#0E1218", bg="#69D9B2")
                if self.details_title:
                    self.details_title.configure(text="最近 6 个月 · 按北京时间自然月")

        source_key = "daily" if self.details_mode == "daily" else "monthly"
        rows = self.last_snapshot.get(source_key, [])[:14]
        first_item_id: str | None = None
        for period in rows:
            item_id = self.details_tree.insert(
                "",
                "end",
                iid=str(period.get("name", "")),
                values=(
                    period.get("name", ""),
                    format_money(float(period.get("cost", 0.0))),
                    format_tokens(int(period.get("total_tokens", 0))),
                    format_tokens(int(period.get("input_tokens", 0))),
                    format_tokens(int(period.get("output_tokens", 0))),
                    format_percent(period.get("cache_hit_rate", 0.0)),
                    format_percent(period.get("peak_cost_share", 0.0)),
                    format_percent(period.get("off_peak_cost_share", 0.0)),
                    f"{int(period.get('responses', 0)):,}",
                ),
            )
            if first_item_id is None:
                first_item_id = item_id
        self.details_tree.yview_moveto(scroll_fraction)
        if selected_id and self.details_tree.exists(selected_id):
            self.details_tree.selection_set(selected_id)
        elif first_item_id:
            self.details_tree.selection_set(first_item_id)

    def _check_stop_flag(self) -> None:
        if STOP_PATH.exists():
            self.close()
            return
        self.root.after(500, self._check_stop_flag)

    def close(self) -> None:
        self.stop_event.set()
        ensure_app_dir()
        try:
            INTENTIONAL_STOP_PATH.write_text("user-close", encoding="ascii")
        except OSError:
            logging.exception("Could not write %s", INTENTIONAL_STOP_PATH)
        try:
            PID_PATH.unlink(missing_ok=True)
        except OSError:
            logging.exception("Could not remove %s", PID_PATH)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def acquire_single_instance() -> Any:
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Global\\DeepSeekUsageMonitorWidget")
    if kernel32.GetLastError() == 183:
        return False
    return handle


def run_snapshot() -> int:
    pricing = Pricing(load_json(PRICING_PATH))
    files_scanned, compressed_skipped = ingest(pricing)
    snapshot = query_snapshot(pricing, files_scanned, compressed_skipped)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


def run_widget() -> int:
    setup_logging()
    mutex = acquire_single_instance()
    if mutex is False:
        return 0

    try:
        PID_PATH.write_text(str(os.getpid()), encoding="ascii")
        STOP_PATH.unlink(missing_ok=True)
        INTENTIONAL_STOP_PATH.unlink(missing_ok=True)
        widget = UsageWidget()
        widget.run()
        return 0
    except Exception:
        logging.exception("Widget terminated unexpectedly")
        return 1
    finally:
        try:
            PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--snapshot", action="store_true", help="Index and print one JSON snapshot")
    args = parser.parse_args()
    if args.snapshot:
        return run_snapshot()
    return run_widget()


if __name__ == "__main__":
    sys.exit(main())
