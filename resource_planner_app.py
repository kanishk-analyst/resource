from __future__ import annotations

import calendar
import os
import secrets
import smtplib
import ssl
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import extra_streamlit_components as stx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from resource_planner.planner import (
    FREQUENCIES,
    SHIFT_WINDOWS,
    admin_reset_password,
    add_task,
    authenticate_user,
    build_monthly_occurrences,
    build_time_load,
    create_user_session,
    create_user,
    delete_user_session,
    delete_tasks,
    get_user_by_session_token,
    get_default_db_path,
    init_db,
    list_tasks,
    list_users,
    occurrences_to_frame,
    set_user_active,
    update_password,
    update_task,
)


st.set_page_config(page_title="Data Management - Resource Planner", layout="wide")


def _load_eve_vars() -> dict[str, str]:
    eve_vars: dict[str, str] = {}
    eve_path = Path(__file__).resolve().parent / ".eve"
    if not eve_path.exists():
        return eve_vars

    for raw_line in eve_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        eve_vars[key.strip()] = value.strip().strip('"').strip("'")
    return eve_vars


def _setting(name: str, default: str = "") -> str:
    eve_vars = _load_eve_vars()
    return os.getenv(name) or eve_vars.get(name, default)


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DB_PATH = get_default_db_path()
init_db(DB_PATH)
SMTP_SERVER = _setting("SMTP_SERVER", "smtp.azurecomm.net")
SMTP_PORT = int(_setting("SMTP_PORT", "587"))
SENDER_EMAIL = _setting("FROM_EMAIL") or _setting("SMTP_SENDER_EMAIL", "notifications@example.com")
AUTH_COOKIE_NAME = "resource_planner_session"
AUTH_SESSION_DAYS = 7


def _init_state() -> None:
    today = date.today().replace(day=1)
    if "selected_month" not in st.session_state:
        st.session_state.selected_month = today
    if "interval_minutes" not in st.session_state:
        st.session_state.interval_minutes = 15
    if "shift_capacity" not in st.session_state:
        st.session_state.shift_capacity = {"APAC": 5.0, "EMEA": 5.0, "NA": 5.0}
    if "auth_user" not in st.session_state:
        st.session_state.auth_user = None


def _style() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&family=IBM+Plex+Mono:wght@500&display=swap');

        .stApp {
            background: radial-gradient(circle at 20% 0%, #d7e9f4 0%, #f4f8fb 40%, #f8fafc 100%);
        }

        h1, h2, h3, h4 {
            font-family: 'Manrope', sans-serif;
            color: #1a2d3d;
            letter-spacing: 0.2px;
        }

        .brand-wrap {
            border: 1px solid #d0deea;
            background: linear-gradient(135deg, #f8fcff 0%, #eef5fb 100%);
            border-radius: 14px;
            padding: 16px 18px;
            margin-bottom: 16px;
        }

        .brand-title {
            font-family: 'Manrope', sans-serif;
            font-size: 1.35rem;
            font-weight: 800;
            color: #183247;
            margin: 0;
        }

        .brand-sub {
            margin: 4px 0 0 0;
            color: #4a6377;
            font-size: 0.92rem;
        }

        html[data-theme="dark"] .stApp {
            background: radial-gradient(circle at 20% 0%, #1a2a3a 0%, #121c27 40%, #0c131b 100%);
        }

        html[data-theme="dark"] h1,
        html[data-theme="dark"] h2,
        html[data-theme="dark"] h3,
        html[data-theme="dark"] h4,
        html[data-theme="dark"] p,
        html[data-theme="dark"] label,
        html[data-theme="dark"] .stMarkdown,
        html[data-theme="dark"] .stCaption,
        html[data-theme="dark"] .stText,
        html[data-theme="dark"] [data-testid="stMarkdownContainer"] {
            color: #e6edf5;
        }

        html[data-theme="dark"] .brand-wrap {
            border: 1px solid #2f4358;
            background: linear-gradient(135deg, #213247 0%, #182535 100%);
        }

        html[data-theme="dark"] .brand-title {
            color: #f3f7fc;
        }

        html[data-theme="dark"] .brand-sub {
            color: #d1ddeb;
        }

        html[data-theme="dark"] div[data-baseweb="input"] input,
        html[data-theme="dark"] div[data-baseweb="select"] input,
        html[data-theme="dark"] textarea,
        html[data-theme="dark"] .stDateInput input,
        html[data-theme="dark"] .stTimeInput input,
        html[data-theme="dark"] .stNumberInput input {
            color: #f2f7ff !important;
            background-color: #1b2a3a !important;
        }

        html[data-theme="dark"] div[data-baseweb="input"],
        html[data-theme="dark"] div[data-baseweb="select"] {
            background-color: #1b2a3a !important;
        }

        html[data-theme="dark"] .stDataFrame,
        html[data-theme="dark"] [data-testid="stDataFrame"] {
            background-color: #111b26;
            color: #e6edf5;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )


def _next_weekday(from_date: date, target_weekday: int) -> date:
    return from_date + timedelta(days=(target_weekday - from_date.weekday()) % 7)


def _first_monthly_anchor(from_date: date, day_of_month: int) -> date:
    year, month = from_date.year, from_date.month
    while True:
        _, month_days = calendar.monthrange(year, month)
        if day_of_month <= month_days:
            candidate = date(year, month, day_of_month)
            if candidate >= from_date:
                return candidate
        if month == 12:
            month = 1
            year += 1
        else:
            month += 1


def _time_in_shift(ts: pd.Timestamp, shift: str) -> bool:
    shift_start, shift_end = SHIFT_WINDOWS[shift]
    ts_time = ts.time()
    if shift_end > shift_start:
        return shift_start <= ts_time < shift_end
    return ts_time >= shift_start or ts_time < shift_end


def _shift_duration_hours(shift: str) -> float:
    shift_start, shift_end = SHIFT_WINDOWS[shift]
    start_min = shift_start.hour * 60 + shift_start.minute
    end_min = shift_end.hour * 60 + shift_end.minute
    if end_min <= start_min:
        end_min += 24 * 60
    return (end_min - start_min) / 60.0


def _period_capacity_hours(shift_capacity: dict[str, float], granularity: str) -> float:
    day_factor = {"Weekly": 7, "Monthly": 30}[granularity]
    daily_capacity = sum(shift_capacity[s] * _shift_duration_hours(s) for s in SHIFT_WINDOWS)
    return daily_capacity * day_factor


def _generate_temp_password(length: int = 12) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _send_temp_password_email(recipient_email: str, temp_password: str, purpose: str) -> tuple[bool, str]:
    smtp_username = _setting("SMTP_USERNAME")
    smtp_password = _setting("SMTP_PASSWORD")
    if not smtp_username or not smtp_password:
        return False, "SMTP_USERNAME/SMTP_PASSWORD not configured."

    message = EmailMessage()
    message["Subject"] = f"Resource Planner {purpose}"
    message["From"] = SENDER_EMAIL
    message["To"] = recipient_email
    message.set_content(
        (
            "Hello,\n\n"
            f"Your account email: {recipient_email}\n"
            f"Temporary password: {temp_password}\n\n"
            "Please sign in and change your password immediately.\n\n"
            "Regards,\n"
            "Resource Planner Admin"
        )
    )

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
            server.starttls(context=context)
            server.login(smtp_username, smtp_password)
            server.send_message(message)
        return True, "Email sent successfully."
    except Exception as exc:  # noqa: BLE001
        return False, f"SMTP send failed: {exc}"


def _set_auth_user(user_id: int, email: str, is_admin: bool, must_change_password: bool) -> None:
    st.session_state.auth_user = {
        "id": int(user_id),
        "email": email,
        "is_admin": bool(is_admin),
        "must_change_password": bool(must_change_password),
    }


def _current_user() -> dict | None:
    user = st.session_state.get("auth_user")
    return user if isinstance(user, dict) else None


def _cookie_manager() -> stx.CookieManager:
    if "cookie_manager" not in st.session_state:
        st.session_state.cookie_manager = stx.CookieManager()
    return st.session_state.cookie_manager


def _set_session_cookie(session_token: str) -> None:
    _cookie_manager().set(
        AUTH_COOKIE_NAME,
        session_token,
        expires_at=datetime.now(timezone.utc) + timedelta(days=AUTH_SESSION_DAYS),
    )


def _get_session_cookie() -> str | None:
    return _cookie_manager().get(AUTH_COOKIE_NAME)


def _clear_session_cookie() -> None:
    _cookie_manager().delete(AUTH_COOKIE_NAME)


def _restore_login_from_cookie() -> None:
    if _current_user() is not None:
        return

    session_token = _get_session_cookie()
    if not session_token:
        return

    user = get_user_by_session_token(DB_PATH, session_token)
    if user is None:
        _clear_session_cookie()
        return

    _set_auth_user(user.id, user.email, user.is_admin, user.must_change_password)


def _logout() -> None:
    session_token = _get_session_cookie()
    if session_token:
        delete_user_session(DB_PATH, session_token)
    _clear_session_cookie()
    st.session_state.auth_user = None


def _login_page() -> None:
    st.markdown("## Login")
    st.caption("Use your email and password to access the planner.")

    with st.form("login_form", clear_on_submit=False):
        email = st.text_input("Email", placeholder="name@company.com")
        password = st.text_input("Password", type="password")
        submit = st.form_submit_button("Sign in", type="primary")

    if submit:
        try:
            user = authenticate_user(DB_PATH, email, password)
        except ValueError as exc:
            st.error(str(exc))
            return

        if user is None:
            st.error("Invalid credentials.")
            return

        session_token = create_user_session(DB_PATH, user.id, days_valid=AUTH_SESSION_DAYS)
        _set_session_cookie(session_token)
        _set_auth_user(user.id, user.email, user.is_admin, user.must_change_password)
        st.success("Login successful.")
        st.rerun()


def _account_page(user: dict) -> None:
    st.subheader("My Account")
    st.caption(f"Signed in as: {user['email']}")

    if user["must_change_password"]:
        st.warning("You are using a temporary password. Please update it now.")

    with st.form("change_password_form", clear_on_submit=True):
        current_password = st.text_input("Current Password", type="password")
        new_password = st.text_input("New Password", type="password")
        confirm_password = st.text_input("Confirm New Password", type="password")
        submit = st.form_submit_button("Update Password", type="primary")

    if submit:
        if new_password != confirm_password:
            st.error("New password and confirmation do not match.")
            return
        try:
            update_password(DB_PATH, int(user["id"]), current_password, new_password)
            st.session_state.auth_user["must_change_password"] = False
            st.success("Password updated.")
        except ValueError as exc:
            st.error(str(exc))


def _user_admin_page(user: dict) -> None:
    st.subheader("User Administration")
    st.caption("Create users by email, issue temporary passwords, and reset credentials.")

    with st.form("create_user_form", clear_on_submit=True):
        new_email = st.text_input("New User Email", placeholder="new.user@company.com")
        is_admin = st.checkbox("Grant admin permissions", value=False)
        create_submit = st.form_submit_button("Create User", type="primary")

    if create_submit:
        temp_password = _generate_temp_password()
        normalized_email = new_email.strip().lower()
        try:
            create_user(DB_PATH, email=new_email, temp_password=temp_password, is_admin=is_admin)
            sent, note = _send_temp_password_email(normalized_email, temp_password, "Account Access")
            if sent:
                st.success(f"User created and email sent: {normalized_email}")
            else:
                st.warning(f"User created: {normalized_email}. {note}")
                st.markdown("Share this onboarding message manually:")
                st.code(
                    (
                        f"Email: {normalized_email}\\n"
                        f"Temporary Password: {temp_password}\\n"
                        "Please sign in and change your password immediately."
                    ),
                    language="text",
                )
        except ValueError as exc:
            st.error(str(exc))

    users = list_users(DB_PATH)
    if users:
        st.markdown("### User Status")
        selected_email = st.selectbox(
            "Select Account",
            options=[u.email for u in users],
            key="status_user_email",
        )
        selected_user = next(u for u in users if u.email == selected_email)
        status_cols = st.columns(2)
        with status_cols[0]:
            if st.button("Activate Account", key="activate_user_btn", disabled=selected_user.is_active):
                try:
                    set_user_active(DB_PATH, selected_email, True)
                    st.success(f"Activated account: {selected_email}")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        with status_cols[1]:
            disable_deactivate = (selected_email == user["email"]) or (not selected_user.is_active)
            if st.button("Deactivate Account", key="deactivate_user_btn", disabled=disable_deactivate):
                try:
                    set_user_active(DB_PATH, selected_email, False)
                    st.success(f"Deactivated account: {selected_email}")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        if selected_email == user["email"]:
            st.caption("You cannot deactivate your own signed-in account from this page.")

        st.markdown("### Reset User Password")
        selectable_emails = [u.email for u in users]
        reset_email = st.selectbox("Select User", options=selectable_emails, key="reset_user_email")
        if st.button("Generate New Temporary Password", key="reset_password_btn"):
            new_temp_password = _generate_temp_password()
            try:
                admin_reset_password(DB_PATH, email=reset_email, temp_password=new_temp_password)
                sent, note = _send_temp_password_email(reset_email, new_temp_password, "Password Reset")
                if sent:
                    st.success(f"Temporary password reset and emailed for {reset_email}")
                else:
                    st.warning(f"Temporary password reset for {reset_email}. {note}")
                    st.code(
                        (
                            f"Email: {reset_email}\\n"
                            f"Temporary Password: {new_temp_password}\\n"
                            "Please sign in and change your password immediately."
                        ),
                        language="text",
                    )
            except ValueError as exc:
                st.error(str(exc))

        users_df = pd.DataFrame(
            [
                {
                    "Email": user.email,
                    "Admin": user.is_admin,
                    "Active": user.is_active,
                    "Must Change Password": user.must_change_password,
                    "Last Login": user.last_login_at.isoformat(sep=" ", timespec="seconds") if user.last_login_at else "-",
                }
                for user in users
            ]
        )
        st.markdown("### Existing Users")
        st.dataframe(users_df, width="stretch")


def _resolve_recurrence_input(
    prefix: str,
    frequency: str,
    default_weekday: int,
    default_weekdays: list[str] | None = None,
    default_monthly_day: int | None = None,
) -> tuple[list[str], int | None]:
    selected_weekdays: list[str] = []
    monthly_day_of_month: int | None = None

    if frequency == "Weekly":
        default_values = default_weekdays or [WEEKDAY_NAMES[default_weekday]]
        selected_weekdays = st.multiselect(
            "Select Day(s)",
            options=WEEKDAY_NAMES,
            default=default_values,
            key=f"{prefix}_weekdays",
        )
        st.caption("Weekly task runs only on the selected day(s).")
    elif frequency == "Monthly":
        dom_default = default_monthly_day if default_monthly_day is not None else 1
        monthly_day_of_month = int(
            st.selectbox(
            "Day Of Month",
            options=list(range(1, 32)),
            index=dom_default - 1,
            key=f"{prefix}_dom",
            )
        )
        st.caption("Monthly task runs on this day every month (if that day exists in the month).")

    return selected_weekdays, monthly_day_of_month


def _task_view_page(user: dict) -> None:
    st.subheader("Task View/Add/Edit")
    tasks = [task for task in list_tasks(DB_PATH) if task.frequency in FREQUENCIES]
    task_by_id = {task.id: task for task in tasks}
    today = date.today()

    add_tab, edit_tab = st.tabs(["Add Task", "Edit Task"])

    with add_tab:
        add_shift = st.selectbox("Shift", list(SHIFT_WINDOWS.keys()), key="add_shift")
        add_name = st.text_input("Task Name", key="add_name")
        add_resource = st.number_input(
            "Resource Required (Hr)",
            min_value=0.25,
            value=1.0,
            step=0.25,
            key="add_resource",
        )
        add_frequency = st.selectbox("Frequency", FREQUENCIES, key="add_frequency")
        add_weekdays, add_monthly_dom = _resolve_recurrence_input("add", add_frequency, today.weekday())

        shift_start, shift_end = SHIFT_WINDOWS[add_shift]
        st.caption(f"Shift window: {shift_start.strftime('%H:%M')} - {shift_end.strftime('%H:%M')}")
        add_start_time = st.time_input("Task Start Time", value=shift_start, key="add_start_time")
        add_end_time = st.time_input("Task End Time", value=shift_end, key="add_end_time")

        if st.button("Save New Task", type="primary", key="add_task_btn"):
            if not add_name.strip():
                st.error("Task Name is required.")
            elif add_frequency == "Weekly" and not add_weekdays:
                st.error("Select at least one day for weekly tasks.")
            else:
                try:
                    add_task(
                        DB_PATH,
                        shift=add_shift,
                        task_name=add_name,
                        resource_required_hr=float(add_resource),
                        frequency=add_frequency,
                        anchor_date=today,
                        weekly_days=add_weekdays,
                        monthly_day_of_month=add_monthly_dom,
                        start_time=add_start_time,
                        end_time=add_end_time,
                        actor_user_id=int(user["id"]),
                    )
                    st.success("Task saved.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    with edit_tab:
        if not tasks:
            st.info("No tasks to edit.")
        else:
            edit_id = st.selectbox(
                "Choose Task",
                options=[task.id for task in tasks],
                format_func=lambda value: f"ID {value} - {task_by_id[value].task_name}",
                key="edit_task_id",
            )
            chosen = task_by_id[edit_id]

            edit_shift = st.selectbox(
                "Shift",
                options=list(SHIFT_WINDOWS.keys()),
                index=list(SHIFT_WINDOWS.keys()).index(chosen.shift),
                key=f"edit_shift_{edit_id}",
            )
            edit_name = st.text_input("Task Name", value=chosen.task_name, key=f"edit_name_{edit_id}")
            edit_resource = st.number_input(
                "Resource Required (Hr)",
                min_value=0.25,
                value=float(chosen.resource_required_hr),
                step=0.25,
                key=f"edit_resource_{edit_id}",
            )
            edit_frequency = st.selectbox(
                "Frequency",
                FREQUENCIES,
                index=FREQUENCIES.index(chosen.frequency),
                key=f"edit_frequency_{edit_id}",
            )
            default_edit_days = list(chosen.weekly_days) if chosen.weekly_days else [WEEKDAY_NAMES[chosen.anchor_date.weekday()]]
            edit_weekdays, edit_monthly_dom = _resolve_recurrence_input(
                f"edit_{edit_id}",
                edit_frequency,
                chosen.anchor_date.weekday(),
                default_weekdays=default_edit_days,
                default_monthly_day=chosen.monthly_day_of_month or chosen.anchor_date.day,
            )

            shift_start, shift_end = SHIFT_WINDOWS[edit_shift]
            st.caption(f"Shift window: {shift_start.strftime('%H:%M')} - {shift_end.strftime('%H:%M')}")
            edit_start_time = st.time_input("Task Start Time", value=chosen.start_time, key=f"edit_start_{edit_id}")
            edit_end_time = st.time_input("Task End Time", value=chosen.end_time, key=f"edit_end_{edit_id}")

            if st.button("Update Task", key=f"edit_submit_{edit_id}"):
                if not edit_name.strip():
                    st.error("Task Name is required.")
                elif edit_frequency == "Weekly" and not edit_weekdays:
                    st.error("Select at least one day for weekly tasks.")
                else:
                    try:
                        update_task(
                            DB_PATH,
                            task_id=edit_id,
                            shift=edit_shift,
                            task_name=edit_name,
                            resource_required_hr=float(edit_resource),
                            frequency=edit_frequency,
                            anchor_date=chosen.anchor_date,
                            weekly_days=edit_weekdays,
                            monthly_day_of_month=edit_monthly_dom,
                            start_time=edit_start_time,
                            end_time=edit_end_time,
                            actor_user_id=int(user["id"]),
                        )
                        st.success("Task updated.")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

    st.markdown("### Task Inventory")
    if not tasks:
        st.info("No tasks available.")
        return

    tasks_df = pd.DataFrame(
        [
            {
                "ID": task.id,
                "Shift": task.shift,
                "Task": task.task_name,
                "Resource Hr": task.resource_required_hr,
                "Frequency": task.frequency,
                "Weekly Days": ", ".join(task.weekly_days) if task.weekly_days else "-",
                "Day Of Month": str(task.monthly_day_of_month) if task.monthly_day_of_month else "-",
                "Start": task.start_time.strftime("%H:%M"),
                "End": task.end_time.strftime("%H:%M"),
                "Created By": task.created_by_email or "-",
                "Updated By": task.updated_by_email or "-",
                "Created At": task.created_at.isoformat(sep=" ", timespec="seconds") if task.created_at else "-",
                "Updated At": task.updated_at.isoformat(sep=" ", timespec="seconds") if task.updated_at else "-",
            }
            for task in tasks
        ]
    )
    st.dataframe(tasks_df, width="stretch")

    st.markdown("### Bulk Select And Delete")
    select_all = st.checkbox("Select all tasks", key="bulk_select_all")
    default_ids = [task.id for task in tasks] if select_all else []
    selected_ids = st.multiselect(
        "Select task IDs",
        options=[task.id for task in tasks],
        default=default_ids,
        format_func=lambda value: f"ID {value} - {task_by_id[value].task_name}",
        key="bulk_delete_ids",
    )

    if st.button("Delete Selected", type="secondary", disabled=not selected_ids, key="bulk_delete_btn"):
        deleted = delete_tasks(DB_PATH, selected_ids)
        st.success(f"Deleted {deleted} task(s).")
        st.rerun()


def _planning_settings_page() -> None:
    st.subheader("Planning Settings")
    st.markdown("Tune planning month, overlap interval, and available shift capacity.")

    col1, col2 = st.columns(2)
    with col1:
        month_value = st.date_input("Planning Month", value=st.session_state.selected_month, key="settings_month")
        interval_value = st.selectbox(
            "Overlap Interval (minutes)",
            options=[15, 30, 60],
            index=[15, 30, 60].index(st.session_state.interval_minutes),
            key="settings_interval",
        )
    with col2:
        apac = st.number_input(
            "APAC Capacity (FTE)",
            min_value=0.0,
            value=float(st.session_state.shift_capacity["APAC"]),
            step=0.5,
            key="settings_apac",
        )
        emea = st.number_input(
            "EMEA Capacity (FTE)",
            min_value=0.0,
            value=float(st.session_state.shift_capacity["EMEA"]),
            step=0.5,
            key="settings_emea",
        )
        na = st.number_input(
            "NA Capacity (FTE)",
            min_value=0.0,
            value=float(st.session_state.shift_capacity["NA"]),
            step=0.5,
            key="settings_na",
        )

    st.session_state.selected_month = month_value.replace(day=1)
    st.session_state.interval_minutes = int(interval_value)
    st.session_state.shift_capacity = {"APAC": float(apac), "EMEA": float(emea), "NA": float(na)}

    summary_df = pd.DataFrame(
        [
            {
                "Shift": shift,
                "Window": f"{SHIFT_WINDOWS[shift][0].strftime('%H:%M')} - {SHIFT_WINDOWS[shift][1].strftime('%H:%M')}",
                "Capacity (FTE)": st.session_state.shift_capacity[shift],
                "Shift Hours": round(_shift_duration_hours(shift), 2),
            }
            for shift in SHIFT_WINDOWS
        ]
    )
    st.dataframe(summary_df, width="stretch")


def _chart_view_page() -> None:
    st.subheader("Chart View")
    tasks = [task for task in list_tasks(DB_PATH) if task.frequency in FREQUENCIES]
    if not tasks:
        st.info("No tasks found. Add tasks in Task View/Add/Edit.")
        return

    selected_month = st.session_state.selected_month
    shift_capacity = st.session_state.shift_capacity
    interval_minutes = st.session_state.interval_minutes

    occurrences = build_monthly_occurrences(tasks, selected_month.year, selected_month.month)
    occ_df = occurrences_to_frame(occurrences)
    load_df = build_time_load(occurrences, selected_month.year, selected_month.month, interval_minutes=interval_minutes)

    if load_df.empty:
        st.info("No occurrences in selected month.")
        return

    load_df = load_df.copy()
    load_df["available_fte"] = load_df["timestamp"].apply(
        lambda ts: sum(shift_capacity[shift] for shift in SHIFT_WINDOWS if _time_in_shift(ts, shift))
    )
    load_df["shortage_fte"] = (load_df["required_fte"] - load_df["available_fte"]).clip(lower=0.0)

    total_required_hours = float(sum(o.resource_hours for o in occurrences))
    month_days = calendar.monthrange(selected_month.year, selected_month.month)[1]
    total_available_hours = sum(shift_capacity[s] * _shift_duration_hours(s) for s in SHIFT_WINDOWS) * month_days
    utilization = (total_required_hours / total_available_hours * 100.0) if total_available_hours else 0.0

    peak_idx = load_df["required_fte"].idxmax()
    peak_ts = load_df.loc[peak_idx, "timestamp"]
    peak_required = float(load_df.loc[peak_idx, "required_fte"])
    peak_available = float(load_df.loc[peak_idx, "available_fte"])
    peak_shortage = max(peak_required - peak_available, 0.0)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Required Hours", f"{total_required_hours:.1f}")
    m2.metric("Total Available Hours", f"{total_available_hours:.1f}")
    m3.metric("Utilization", f"{utilization:.1f}%")
    m4.metric("Peak Shortage (FTE)", f"{peak_shortage:.2f}")

    st.caption(
        f"Peak time: {peak_ts:%Y-%m-%d %H:%M} | Required: {peak_required:.2f} FTE | Available: {peak_available:.2f} FTE"
    )

    granularity = st.selectbox("View", options=["Weekly", "Monthly"], key="chart_granularity")
    daily_capacity_hours = sum(shift_capacity[s] * _shift_duration_hours(s) for s in SHIFT_WINDOWS)

    if granularity == "Weekly":
        weekday_order = WEEKDAY_NAMES
        trend_df = occ_df.copy()
        trend_df["period"] = pd.to_datetime(trend_df["date"]).dt.day_name()
        trend_df = trend_df.groupby("period", as_index=False)["resource_hours"].sum()
        trend_df["period"] = pd.Categorical(
            trend_df["period"],
            categories=weekday_order,
            ordered=True,
        )
        trend_df = trend_df.sort_values("period")
        trend_df["capacity"] = daily_capacity_hours
        chart_title = "Weekly Pattern (Monday-Sunday)"
        fig_trend = px.bar(trend_df, x="period", y="resource_hours", title=chart_title)
        fig_trend.add_scatter(x=trend_df["period"], y=trend_df["capacity"], mode="lines", name="Capacity")
        st.plotly_chart(fig_trend, width="stretch")
    else:
        month_start = date(selected_month.year, selected_month.month, 1)
        month_end = date(
            selected_month.year,
            selected_month.month,
            calendar.monthrange(selected_month.year, selected_month.month)[1],
        )

        daily_hours = (
            occ_df.groupby("date", as_index=False)["resource_hours"].sum()
            if not occ_df.empty
            else pd.DataFrame(columns=["date", "resource_hours"])
        )
        day_hours_map = {row["date"]: float(row["resource_hours"]) for _, row in daily_hours.iterrows()}

        month_calendar = calendar.Calendar(firstweekday=0).monthdatescalendar(selected_month.year, selected_month.month)
        z_values: list[list[float | None]] = []
        text_values: list[list[str]] = []
        hover_values: list[list[str]] = []
        week_labels: list[str] = []

        for idx, week in enumerate(month_calendar, start=1):
            week_labels.append(f"Week {idx}")
            row_z: list[float | None] = []
            row_text: list[str] = []
            row_hover: list[str] = []
            for day_value in week:
                if day_value.month != selected_month.month:
                    row_z.append(None)
                    row_text.append("")
                    row_hover.append("")
                    continue
                required_hours = day_hours_map.get(day_value, 0.0)
                row_z.append(required_hours)
                row_text.append(str(day_value.day))
                row_hover.append(f"{day_value:%Y-%m-%d}<br>Required Hrs: {required_hours:.2f}")
            z_values.append(row_z)
            text_values.append(row_text)
            hover_values.append(row_hover)

        fig_calendar = go.Figure(
            data=go.Heatmap(
                z=z_values,
                x=WEEKDAY_NAMES,
                y=week_labels,
                text=text_values,
                customdata=hover_values,
                texttemplate="%{text}",
                textfont={"size": 12},
                colorscale="Blues",
                colorbar={"title": "Hours"},
                hovertemplate="%{customdata}<extra></extra>",
            )
        )
        fig_calendar.update_layout(
            title="Monthly Calendar View",
            yaxis_autorange="reversed",
            xaxis_title="",
            yaxis_title="",
        )
        st.plotly_chart(fig_calendar, width="stretch")

    shift_frames: list[pd.DataFrame] = []
    for shift_name in SHIFT_WINDOWS:
        shift_occurrences = [occ for occ in occurrences if occ.shift == shift_name]
        shift_load = build_time_load(
            shift_occurrences,
            selected_month.year,
            selected_month.month,
            interval_minutes=interval_minutes,
        )
        if shift_load.empty:
            continue
        shift_load = shift_load.copy()
        shift_load["shift"] = shift_name
        shift_load["available_fte"] = shift_capacity[shift_name]
        shift_frames.append(shift_load)

    if shift_frames:
        shift_df = pd.concat(shift_frames, ignore_index=True)
        shift_df = shift_df.rename(columns={"required_fte": "required_fte_shift"})
        shift_long = pd.melt(
            shift_df,
            id_vars=["timestamp", "shift"],
            value_vars=["required_fte_shift", "available_fte"],
            var_name="series",
            value_name="fte",
        )
        shift_long["series"] = shift_long["series"].map(
            {"required_fte_shift": "Required FTE", "available_fte": "Available FTE"}
        )
        fig_shift = px.line(
            shift_long,
            x="timestamp",
            y="fte",
            color="series",
            facet_row="shift",
            title="Required vs Available FTE by Shift",
        )
        fig_shift.for_each_annotation(lambda ann: ann.update(text=ann.text.split("=")[-1]))
        st.plotly_chart(fig_shift, width="stretch")

    global_long = pd.melt(
        load_df,
        id_vars=["timestamp"],
        value_vars=["required_fte", "available_fte", "shortage_fte"],
        var_name="series",
        value_name="fte",
    )
    global_long["series"] = global_long["series"].map(
        {
            "required_fte": "Required FTE",
            "available_fte": "Available FTE",
            "shortage_fte": "Shortage FTE",
        }
    )
    fig_global = px.line(global_long, x="timestamp", y="fte", color="series", title="Global Capacity vs Demand")
    st.plotly_chart(fig_global, width="stretch")

    if not occ_df.empty:
        gantt_df = occ_df.copy()
        gantt_df["Task Label"] = gantt_df["task_name"] + " (" + gantt_df["shift"] + ")"
        fig_gantt = px.timeline(
            gantt_df,
            x_start="start_dt",
            x_end="end_dt",
            y="Task Label",
            color="shift",
            hover_data={"resource_hours": True, "fte_demand": ":.2f"},
        )
        fig_gantt.update_yaxes(autorange="reversed")
        st.plotly_chart(fig_gantt, width="stretch")


_init_state()
_style()
_restore_login_from_cookie()

user = _current_user()

if user is None:
    st.markdown(
        """
        <div class="brand-wrap">
            <p class="brand-title">Data Management - Resource Planner</p>
            <p class="brand-sub">Secure planning workspace with task scheduling, capacity controls, and demand analytics.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    _login_page()
    st.stop()

st.markdown(
    """
    <div class="brand-wrap">
        <p class="brand-title">Data Management - Resource Planner</p>
        <p class="brand-sub">Operational planning workspace for task scheduling, shift capacity configuration, and demand analytics.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

pages = ["Task View/Add/Edit", "Planning Settings", "Chart View", "My Account"]
if user["is_admin"]:
    pages.append("User Administration")

nav_col, user_col = st.columns([6, 2])
with nav_col:
    page = st.radio(
        "Page",
        options=pages,
        label_visibility="collapsed",
        horizontal=True,
        key="top_navigation",
    )
with user_col:
    st.caption(f"Signed in: {user['email']}")
    if st.button("Log Out", key="logout_btn"):
        _logout()
        st.rerun()

if page == "Task View/Add/Edit":
    _task_view_page(user)
elif page == "Planning Settings":
    _planning_settings_page()
elif page == "Chart View":
    _chart_view_page()
elif page == "My Account":
    _account_page(user)
else:
    _user_admin_page(user)
