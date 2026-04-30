# Resource Planner Project

Streamlit-based resource planning application with:
- Secure login and user administration
- Task scheduling (Weekly and Monthly)
- Task ownership audit (created/updated by)
- Capacity and demand analytics charts
- Neon Postgres persistence
- SMTP-based temporary password emails

## Features

- Authentication and account management
  - Login with email/password
  - Change password
  - Admin create/reset/deactivate users
  - Session persistence across refresh (cookie + DB session)

- Task planning
  - Weekly tasks: select one or more weekdays (Monday-Sunday)
  - Monthly tasks: select day of month (1-31)
  - Shift-aware validation for task time windows

- Analytics and charts
  - Weekly view: Monday-Sunday pattern
  - Monthly view: calendar-style chart for selected month
  - Shift/global required vs available FTE trends

## Tech Stack

- Python 3.14+
- Streamlit
- Plotly
- Pandas
- Psycopg (Postgres driver)
- Neon Postgres

## Project Structure

- `resource_planner_app.py` - Main Streamlit application
- `resource_planner/planner.py` - Data model, DB operations, recurrence logic
- `plot_monthly_resource.py` - CLI chart generator for a month
- `requirements.txt` - Python dependencies
- `.eve` - Local environment/config values (ignored in git)

## Prerequisites

- Python installed
- Access to a Neon Postgres database
- SMTP credentials for email notifications (optional but recommended)

## Configuration

Create/update `.eve` in project root:

```env
DATABASE_URL="postgresql://<user>:<password>@<host>/<db>?sslmode=require&channel_binding=require"

SMTP_SERVER="smtp.azurecomm.net"
SMTP_PORT="587"
SMTP_SENDER_EMAIL="<sender-identity>"
FROM_EMAIL="<sender-identity>"
TO_EMAIL="<notification-recipient>"
SMTP_USERNAME="mailrelay-kofax"
SMTP_PASSWORD="<smtp-password>"
```

Notes:
- `DATABASE_URL` is required.
- App reads values from environment variables first, then `.eve`.

## Setup

From project root:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run the App

```bash
streamlit run resource_planner_app.py
```

## Run Monthly CLI Chart

```bash
python plot_monthly_resource.py --month 2026-04
```

Optional args:
- `--team-fte`
- `--interval`
- `--db`
- `--output`

## Database Notes

The app initializes/migrates required tables automatically on startup:
- `users`
- `tasks`
- `user_sessions`

## Security Notes

- Do not commit `.eve` to source control.
- Rotate database and SMTP credentials if they were exposed.
- Session tokens are stored hashed in DB.

## Troubleshooting

- Login lost on refresh:
  - Ensure browser cookies are enabled.
  - Ensure `user_sessions` table exists and DB is reachable.

- SMTP emails not sent:
  - Verify SMTP credentials and sender format.
  - Check firewall/proxy for outbound SMTP port 587.

- DB connection issues:
  - Verify `DATABASE_URL` in `.eve`.
  - Ensure Neon host allows your network.
