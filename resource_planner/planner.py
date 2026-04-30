from __future__ import annotations

import calendar
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

FREQUENCIES = ("Weekly", "Monthly")
SHIFT_WINDOWS: dict[str, tuple[time, time]] = {
    "APAC": (time(5, 0), time(15, 0)),
    "EMEA": (time(14, 0), time(23, 0)),
    "NA": (time(22, 0), time(7, 0)),
}


@dataclass(frozen=True)
class Task:
    id: int
    shift: str
    task_name: str
    resource_required_hr: float
    frequency: str
    anchor_date: date
    weekly_days: tuple[str, ...]
    monthly_day_of_month: int | None
    start_time: time
    end_time: time
    created_by_email: str | None
    updated_by_email: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class User:
    id: int
    email: str
    is_active: bool
    is_admin: bool
    must_change_password: bool
    created_at: datetime | None
    last_login_at: datetime | None


@dataclass(frozen=True)
class Occurrence:
    task_id: int
    shift: str
    task_name: str
    start_dt: datetime
    end_dt: datetime
    resource_hours: float
    duration_hours: float
    fte_demand: float


def get_default_db_path() -> str:
    env_dsn = os.getenv("DATABASE_URL")
    if env_dsn:
        return env_dsn

    eve_path = Path(__file__).resolve().parent.parent / ".eve"
    if eve_path.exists():
        for raw_line in eve_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.upper().startswith("DATABASE_URL="):
                return line.split("=", maxsplit=1)[1].strip().strip('"').strip("'")
            return line

    raise ValueError("Database connection string not found. Set DATABASE_URL or create resource_planner_project/.eve")


DEFAULT_ADMIN_EMAIL = "admin@resource-planner.local"
DEFAULT_ADMIN_TEMP_PASSWORD = "Admin@123"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WEEKDAY_TO_INDEX = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}
_INDEX_TO_WEEKDAY = {v: k for k, v in _WEEKDAY_TO_INDEX.items()}


def _to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise ValueError("Please provide a valid email address.")
    return normalized


def _normalize_weekly_days(weekly_days: list[str] | None) -> tuple[str, ...]:
    if not weekly_days:
        return tuple()
    seen: set[str] = set()
    ordered: list[str] = []
    for value in weekly_days:
        day = value.strip().title()
        if day not in _WEEKDAY_TO_INDEX:
            raise ValueError(f"Unsupported weekday: {value}")
        if day not in seen:
            seen.add(day)
            ordered.append(day)
    return tuple(sorted(ordered, key=lambda d: _WEEKDAY_TO_INDEX[d]))


def _serialize_weekly_days(weekly_days: tuple[str, ...]) -> str | None:
    return ",".join(weekly_days) if weekly_days else None


def _parse_weekly_days(value: str | None) -> tuple[str, ...]:
    if not value:
        return tuple()
    return _normalize_weekly_days([token for token in value.split(",") if token.strip()])


def _normalize_monthly_day_of_month(day_of_month: int | None) -> int | None:
    if day_of_month is None:
        return None
    value = int(day_of_month)
    if value < 1 or value > 31:
        raise ValueError("Day of month must be between 1 and 31.")
    return value


def _hash_password(password: str, iterations: int = 200_000) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, iterations_str, salt_hex, digest_hex = password_hash.split("$", maxsplit=3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _hash_session_token(session_token: str) -> str:
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()


def _cleanup_legacy_default_admin(conn: psycopg.Connection) -> None:
    """Remove bootstrap admin account when another active admin already exists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(1)
            FROM users
            WHERE is_admin = TRUE AND is_active = TRUE AND email <> %s
            """,
            (DEFAULT_ADMIN_EMAIL,),
        )
        other_admins = int(cur.fetchone()[0])
        if other_admins <= 0:
            return

        cur.execute(
            "DELETE FROM users WHERE email = %s",
            (DEFAULT_ADMIN_EMAIL,),
        )


def _to_time(time_str: str) -> time:
    return datetime.strptime(time_str, "%H:%M").time()


def get_shift_window(shift: str) -> tuple[time, time]:
    normalized = shift.strip().upper()
    if normalized not in SHIFT_WINDOWS:
        raise ValueError(f"Unsupported shift: {shift}")
    return SHIFT_WINDOWS[normalized]


def is_time_range_within_shift(shift: str, start_time: time, end_time: time) -> bool:
    shift_start, shift_end = get_shift_window(shift)

    def minutes_since_midnight(value: time) -> int:
        return value.hour * 60 + value.minute

    start_min = minutes_since_midnight(start_time)
    end_min = minutes_since_midnight(end_time)
    shift_start_min = minutes_since_midnight(shift_start)
    shift_end_min = minutes_since_midnight(shift_end)

    if end_min <= start_min:
        end_min += 24 * 60
    if shift_end_min <= shift_start_min:
        shift_end_min += 24 * 60
        if start_min < shift_start_min:
            start_min += 24 * 60
        if end_min < shift_start_min:
            end_min += 24 * 60

    return shift_start_min <= start_min and end_min <= shift_end_min


def init_db(db_path: Path | str) -> None:
    with psycopg.connect(str(db_path), sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                last_login_at TIMESTAMP
            )
            """
        )
            cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                shift TEXT NOT NULL,
                task_name TEXT NOT NULL,
                resource_required_hr DOUBLE PRECISION NOT NULL CHECK(resource_required_hr > 0),
                frequency TEXT NOT NULL CHECK(frequency IN ('Daily', 'Weekly', 'Monthly')),
                anchor_date DATE NOT NULL,
                weekly_days TEXT,
                monthly_day_of_month INTEGER,
                start_time TIME NOT NULL,
                end_time TIME NOT NULL,
                created_by_id INTEGER REFERENCES users(id),
                updated_by_id INTEGER REFERENCES users(id),
                updated_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
            cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMP
            )
            """
        )
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_by_id INTEGER")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS updated_by_id INTEGER")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS weekly_days TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS monthly_day_of_month INTEGER")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT TRUE")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP")
            cur.execute("DELETE FROM user_sessions WHERE expires_at <= NOW()")

        _cleanup_legacy_default_admin(conn)


def create_user(
    db_path: Path | str,
    email: str,
    temp_password: str,
    *,
    is_admin: bool = False,
) -> None:
    normalized_email = _normalize_email(email)
    password_hash = _hash_password(temp_password)

    with psycopg.connect(str(db_path), sslmode="require") as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, password_hash, is_active, is_admin, must_change_password)
                    VALUES (%s, %s, TRUE, %s, TRUE)
                    """,
                    (normalized_email, password_hash, bool(is_admin)),
                )
        except UniqueViolation as exc:
            raise ValueError("A user with this email already exists.") from exc


def list_users(db_path: Path | str) -> list[User]:
    with psycopg.connect(str(db_path), sslmode="require", row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
            """
            SELECT id, email, is_active, is_admin, must_change_password, created_at, last_login_at
            FROM users
            ORDER BY email
            """
            )
            rows = cur.fetchall()

    return [
        User(
            id=int(row["id"]),
            email=row["email"],
            is_active=bool(row["is_active"]),
            is_admin=bool(row["is_admin"]),
            must_change_password=bool(row["must_change_password"]),
            created_at=_to_datetime(row["created_at"]),
            last_login_at=_to_datetime(row["last_login_at"]),
        )
        for row in rows
    ]


def authenticate_user(db_path: Path | str, email: str, password: str) -> User | None:
    normalized_email = _normalize_email(email)

    with psycopg.connect(str(db_path), sslmode="require", row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
            """
            SELECT id, email, password_hash, is_active, is_admin, must_change_password, created_at, last_login_at
            FROM users
            WHERE email = %s
            """,
            (normalized_email,),
            )
            row = cur.fetchone()

        if row is None or not bool(row["is_active"]):
            return None
        if not _verify_password(password, row["password_hash"]):
            return None

        with conn.cursor() as cur:
            cur.execute("UPDATE users SET last_login_at = NOW() WHERE id = %s", (int(row["id"]),))

    return User(
        id=int(row["id"]),
        email=row["email"],
        is_active=bool(row["is_active"]),
        is_admin=bool(row["is_admin"]),
        must_change_password=bool(row["must_change_password"]),
        created_at=_to_datetime(row["created_at"]),
        last_login_at=_to_datetime(row["last_login_at"]),
    )


def create_user_session(db_path: Path | str, user_id: int, days_valid: int = 7) -> str:
    session_token = secrets.token_urlsafe(48)
    token_hash = _hash_session_token(session_token)

    with psycopg.connect(str(db_path), sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_sessions (user_id, token_hash, expires_at, last_seen_at)
                VALUES (%s, %s, NOW() + (%s * INTERVAL '1 day'), NOW())
                """,
                (int(user_id), token_hash, int(days_valid)),
            )
    return session_token


def get_user_by_session_token(db_path: Path | str, session_token: str) -> User | None:
    token_hash = _hash_session_token(session_token)

    with psycopg.connect(str(db_path), sslmode="require", row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    users.id,
                    users.email,
                    users.is_active,
                    users.is_admin,
                    users.must_change_password,
                    users.created_at,
                    users.last_login_at,
                    user_sessions.id AS session_id
                FROM user_sessions
                JOIN users ON users.id = user_sessions.user_id
                WHERE user_sessions.token_hash = %s
                  AND user_sessions.expires_at > NOW()
                """,
                (token_hash,),
            )
            row = cur.fetchone()

            if row is None or not bool(row["is_active"]):
                return None

            cur.execute(
                "UPDATE user_sessions SET last_seen_at = NOW() WHERE id = %s",
                (int(row["session_id"]),),
            )

    return User(
        id=int(row["id"]),
        email=row["email"],
        is_active=bool(row["is_active"]),
        is_admin=bool(row["is_admin"]),
        must_change_password=bool(row["must_change_password"]),
        created_at=_to_datetime(row["created_at"]),
        last_login_at=_to_datetime(row["last_login_at"]),
    )


def delete_user_session(db_path: Path | str, session_token: str) -> None:
    token_hash = _hash_session_token(session_token)
    with psycopg.connect(str(db_path), sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_sessions WHERE token_hash = %s", (token_hash,))


def update_password(db_path: Path | str, user_id: int, current_password: str, new_password: str) -> None:
    with psycopg.connect(str(db_path), sslmode="require", row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
            "SELECT id, password_hash FROM users WHERE id = %s AND is_active = TRUE",
            (int(user_id),),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError("User not found or inactive.")
        if not _verify_password(current_password, row["password_hash"]):
            raise ValueError("Current password is incorrect.")

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET password_hash = %s, must_change_password = FALSE
                WHERE id = %s
                """,
                (_hash_password(new_password), int(user_id)),
            )


def admin_reset_password(db_path: Path | str, email: str, temp_password: str) -> None:
    normalized_email = _normalize_email(email)
    with psycopg.connect(str(db_path), sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET password_hash = %s, must_change_password = TRUE
                WHERE email = %s
                """,
                (_hash_password(temp_password), normalized_email),
            )
            rowcount = int(cur.rowcount)
        if rowcount == 0:
            raise ValueError("User email not found.")


def set_user_active(db_path: Path | str, email: str, is_active: bool) -> None:
    normalized_email = _normalize_email(email)
    with psycopg.connect(str(db_path), sslmode="require", row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, is_admin, is_active FROM users WHERE email = %s",
                (normalized_email,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("User email not found.")

            current_active = bool(row["is_active"])
            if current_active == bool(is_active):
                return

            # Never allow deactivating the last active admin.
            if not bool(is_active) and bool(row["is_admin"]):
                cur.execute(
                    """
                    SELECT COUNT(1)
                    FROM users
                    WHERE is_admin = TRUE AND is_active = TRUE AND email <> %s
                    """,
                    (normalized_email,),
                )
                active_other_admins = int(cur.fetchone()[0])
                if active_other_admins <= 0:
                    raise ValueError("Cannot deactivate the last active admin account.")

            cur.execute(
                "UPDATE users SET is_active = %s WHERE email = %s",
                (bool(is_active), normalized_email),
            )
            if not bool(is_active):
                cur.execute("DELETE FROM user_sessions WHERE user_id = %s", (int(row["id"]),))


def add_task(
    db_path: Path | str,
    shift: str,
    task_name: str,
    resource_required_hr: float,
    frequency: str,
    anchor_date: date,
    weekly_days: list[str] | None,
    monthly_day_of_month: int | None,
    start_time: time,
    end_time: time,
    actor_user_id: int | None = None,
) -> None:
    if frequency not in FREQUENCIES:
        raise ValueError(f"Unsupported frequency: {frequency}")
    normalized_weekly_days = _normalize_weekly_days(weekly_days)
    normalized_monthly_day = _normalize_monthly_day_of_month(monthly_day_of_month)
    if frequency == "Weekly" and not normalized_weekly_days:
        raise ValueError("Select at least one day for weekly tasks.")
    if frequency == "Monthly" and normalized_monthly_day is None:
        raise ValueError("Select a day of month for monthly tasks.")
    if frequency != "Weekly":
        normalized_weekly_days = tuple()
    if frequency != "Monthly":
        normalized_monthly_day = None
    if not is_time_range_within_shift(shift, start_time, end_time):
        shift_start, shift_end = get_shift_window(shift)
        raise ValueError(
            "Task time is outside shift window: "
            f"{shift.upper()} ({shift_start.strftime('%H:%M')} - {shift_end.strftime('%H:%M')})"
        )

    with psycopg.connect(str(db_path), sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute(
            """
            INSERT INTO tasks (
                shift, task_name, resource_required_hr, frequency,
                anchor_date, weekly_days, monthly_day_of_month, start_time, end_time, created_by_id, updated_by_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                shift.strip(),
                task_name.strip(),
                float(resource_required_hr),
                frequency,
                anchor_date,
                _serialize_weekly_days(normalized_weekly_days),
                normalized_monthly_day,
                start_time,
                end_time,
                int(actor_user_id) if actor_user_id is not None else None,
                int(actor_user_id) if actor_user_id is not None else None,
            ),
        )


def delete_task(db_path: Path | str, task_id: int) -> None:
    with psycopg.connect(str(db_path), sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))


def delete_tasks(db_path: Path | str, task_ids: Iterable[int]) -> int:
    ids = [int(task_id) for task_id in task_ids]
    if not ids:
        return 0

    placeholders = ", ".join(["%s"] * len(ids))
    query = f"DELETE FROM tasks WHERE id IN ({placeholders})"
    with psycopg.connect(str(db_path), sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(ids))
            return int(cur.rowcount)


def update_task(
    db_path: Path | str,
    task_id: int,
    shift: str,
    task_name: str,
    resource_required_hr: float,
    frequency: str,
    anchor_date: date,
    weekly_days: list[str] | None,
    monthly_day_of_month: int | None,
    start_time: time,
    end_time: time,
    actor_user_id: int | None = None,
) -> None:
    if frequency not in FREQUENCIES:
        raise ValueError(f"Unsupported frequency: {frequency}")
    normalized_weekly_days = _normalize_weekly_days(weekly_days)
    normalized_monthly_day = _normalize_monthly_day_of_month(monthly_day_of_month)
    if frequency == "Weekly" and not normalized_weekly_days:
        raise ValueError("Select at least one day for weekly tasks.")
    if frequency == "Monthly" and normalized_monthly_day is None:
        raise ValueError("Select a day of month for monthly tasks.")
    if frequency != "Weekly":
        normalized_weekly_days = tuple()
    if frequency != "Monthly":
        normalized_monthly_day = None
    if not is_time_range_within_shift(shift, start_time, end_time):
        shift_start, shift_end = get_shift_window(shift)
        raise ValueError(
            "Task time is outside shift window: "
            f"{shift.upper()} ({shift_start.strftime('%H:%M')} - {shift_end.strftime('%H:%M')})"
        )

    with psycopg.connect(str(db_path), sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute(
            """
            UPDATE tasks
            SET
                shift = %s,
                task_name = %s,
                resource_required_hr = %s,
                frequency = %s,
                anchor_date = %s,
                weekly_days = %s,
                monthly_day_of_month = %s,
                start_time = %s,
                end_time = %s,
                updated_by_id = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                shift.strip(),
                task_name.strip(),
                float(resource_required_hr),
                frequency,
                anchor_date,
                _serialize_weekly_days(normalized_weekly_days),
                normalized_monthly_day,
                start_time,
                end_time,
                int(actor_user_id) if actor_user_id is not None else None,
                int(task_id),
            ),
        )


def list_tasks(db_path: Path | str) -> list[Task]:
    with psycopg.connect(str(db_path), sslmode="require", row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
            """
            SELECT
                tasks.id,
                tasks.shift,
                tasks.task_name,
                tasks.resource_required_hr,
                tasks.frequency,
                tasks.anchor_date,
                tasks.weekly_days,
                tasks.monthly_day_of_month,
                tasks.start_time,
                tasks.end_time,
                cu.email AS created_by_email,
                uu.email AS updated_by_email,
                tasks.created_at,
                tasks.updated_at
            FROM tasks
            LEFT JOIN users cu ON cu.id = tasks.created_by_id
            LEFT JOIN users uu ON uu.id = tasks.updated_by_id
            ORDER BY tasks.shift, tasks.task_name, tasks.id
            """
            )
            rows = cur.fetchall()

    tasks: list[Task] = []
    for row in rows:
        tasks.append(
            Task(
                id=int(row["id"]),
                shift=row["shift"],
                task_name=row["task_name"],
                resource_required_hr=float(row["resource_required_hr"]),
                frequency=row["frequency"],
                anchor_date=row["anchor_date"],
                weekly_days=(
                    _parse_weekly_days(row["weekly_days"])
                    if row["frequency"] == "Weekly"
                    else tuple()
                ),
                monthly_day_of_month=(
                    int(row["monthly_day_of_month"])
                    if row["frequency"] == "Monthly" and row["monthly_day_of_month"] is not None
                    else None
                ),
                start_time=row["start_time"],
                end_time=row["end_time"],
                created_by_email=row["created_by_email"],
                updated_by_email=row["updated_by_email"],
                created_at=_to_datetime(row["created_at"]),
                updated_at=_to_datetime(row["updated_at"]),
            )
        )
    return tasks


def month_bounds(year: int, month: int) -> tuple[date, date]:
    month_start = date(year, month, 1)
    _, month_days = calendar.monthrange(year, month)
    month_end = date(year, month, month_days)
    return month_start, month_end


def _iter_occurrence_days(task: Task, month_start: date, month_end: date):
    if task.frequency == "Daily":
        current = month_start
        while current <= month_end:
            yield current
            current += timedelta(days=1)
        return

    if task.frequency == "Weekly":
        weekdays = task.weekly_days or (_INDEX_TO_WEEKDAY[task.anchor_date.weekday()],)
        weekday_indexes = {_WEEKDAY_TO_INDEX[d] for d in weekdays}
        current = month_start
        while current <= month_end:
            if current.weekday() in weekday_indexes:
                yield current
            current += timedelta(days=1)
        return

    if task.frequency == "Monthly":
        day = task.monthly_day_of_month or task.anchor_date.day
        if month_end.day >= day:
            current = date(month_start.year, month_start.month, day)
            yield current
        return


def build_monthly_occurrences(tasks: list[Task], year: int, month: int) -> list[Occurrence]:
    month_start, month_end = month_bounds(year, month)
    occurrences: list[Occurrence] = []

    for task in tasks:
        for occ_day in _iter_occurrence_days(task, month_start, month_end):
            start_dt = datetime.combine(occ_day, task.start_time)
            end_dt = datetime.combine(occ_day, task.end_time)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)

            duration_hours = (end_dt - start_dt).total_seconds() / 3600.0
            if duration_hours <= 0:
                continue

            fte_demand = task.resource_required_hr / duration_hours
            occurrences.append(
                Occurrence(
                    task_id=task.id,
                    shift=task.shift,
                    task_name=task.task_name,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    resource_hours=task.resource_required_hr,
                    duration_hours=duration_hours,
                    fte_demand=fte_demand,
                )
            )

    return sorted(occurrences, key=lambda x: x.start_dt)


def occurrences_to_frame(occurrences: list[Occurrence]) -> pd.DataFrame:
    if not occurrences:
        return pd.DataFrame(
            columns=[
                "task_id",
                "shift",
                "task_name",
                "start_dt",
                "end_dt",
                "resource_hours",
                "duration_hours",
                "fte_demand",
                "date",
            ]
        )

    df = pd.DataFrame(
        [
            {
                "task_id": o.task_id,
                "shift": o.shift,
                "task_name": o.task_name,
                "start_dt": o.start_dt,
                "end_dt": o.end_dt,
                "resource_hours": o.resource_hours,
                "duration_hours": o.duration_hours,
                "fte_demand": o.fte_demand,
                "date": o.start_dt.date(),
            }
            for o in occurrences
        ]
    )
    return df


def build_time_load(
    occurrences: list[Occurrence],
    year: int,
    month: int,
    interval_minutes: int = 15,
) -> pd.DataFrame:
    month_start, month_end = month_bounds(year, month)
    start_dt = datetime.combine(month_start, time.min)
    end_dt = datetime.combine(month_end + timedelta(days=1), time.min)

    timeline = pd.date_range(start=start_dt, end=end_dt, freq=f"{interval_minutes}min", inclusive="left")
    if len(timeline) == 0:
        return pd.DataFrame(columns=["timestamp", "required_fte", "date", "time"])

    load = pd.Series(0.0, index=timeline)

    for occ in occurrences:
        mask = (timeline >= occ.start_dt) & (timeline < occ.end_dt)
        load.loc[mask] += occ.fte_demand

    return pd.DataFrame(
        {
            "timestamp": load.index,
            "required_fte": load.values,
            "date": load.index.date,
            "time": load.index.time,
        }
    )


def summarize_month(
    occurrences: list[Occurrence],
    load_df: pd.DataFrame,
    year: int,
    month: int,
    team_fte: float,
) -> dict[str, Any]:
    month_start, month_end = month_bounds(year, month)
    business_days = len(pd.bdate_range(month_start, month_end))

    total_required_hours = float(sum(o.resource_hours for o in occurrences))
    available_hours = float(team_fte * 8 * business_days)

    peak_required_fte = 0.0
    peak_timestamp: datetime | None = None
    if not load_df.empty:
        peak_idx = load_df["required_fte"].idxmax()
        peak_required_fte = float(load_df.loc[peak_idx, "required_fte"])
        peak_timestamp = load_df.loc[peak_idx, "timestamp"]

    return {
        "month": f"{year:04d}-{month:02d}",
        "business_days": business_days,
        "total_required_hours": round(total_required_hours, 2),
        "available_hours": round(available_hours, 2),
        "utilization_pct": round((total_required_hours / available_hours) * 100, 2) if available_hours else 0.0,
        "peak_required_fte": round(peak_required_fte, 2),
        "peak_timestamp": peak_timestamp,
        "peak_over_capacity": round(max(peak_required_fte - team_fte, 0.0), 2),
    }
