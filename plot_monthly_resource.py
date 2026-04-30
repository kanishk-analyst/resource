from __future__ import annotations

import argparse
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from resource_planner.planner import (
    build_monthly_occurrences,
    build_time_load,
    get_default_db_path,
    list_tasks,
    occurrences_to_frame,
    summarize_month,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot monthly resource requirements from local task database.")
    parser.add_argument("--month", required=True, help="Month in format YYYY-MM")
    parser.add_argument("--team-fte", type=float, default=5.0, help="Available team FTE capacity")
    parser.add_argument("--interval", type=int, default=15, help="Overlap interval in minutes")
    parser.add_argument("--db", default=str(get_default_db_path()), help="Path to SQLite DB file")
    parser.add_argument("--output", default="monthly_resource_plot.html", help="Output HTML plot file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    year, month = [int(x) for x in args.month.split("-", maxsplit=1)]

    db_path = Path(args.db)
    tasks = list_tasks(db_path)
    occurrences = build_monthly_occurrences(tasks, year, month)
    occ_df = occurrences_to_frame(occurrences)
    load_df = build_time_load(occurrences, year, month, interval_minutes=args.interval)

    summary = summarize_month(occurrences, load_df, year, month, team_fte=args.team_fte)

    fig = make_subplots(
        rows=2,
        cols=1,
        subplot_titles=("Daily Required Resource Hours", "Concurrent Required FTE"),
        vertical_spacing=0.12,
    )

    if not occ_df.empty:
        daily_df = occ_df.groupby("date", as_index=False)["resource_hours"].sum().sort_values("date")
        fig.add_trace(
            go.Bar(x=daily_df["date"], y=daily_df["resource_hours"], name="Required Hours"),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=daily_df["date"],
                y=[args.team_fte * 8] * len(daily_df),
                mode="lines",
                name="Daily Capacity",
            ),
            row=1,
            col=1,
        )

    if not load_df.empty:
        fig.add_trace(
            go.Scatter(x=load_df["timestamp"], y=load_df["required_fte"], mode="lines", name="Required FTE"),
            row=2,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=load_df["timestamp"],
                y=[args.team_fte] * len(load_df),
                mode="lines",
                name="Available FTE",
            ),
            row=2,
            col=1,
        )

    fig.update_layout(
        height=800,
        title=(
            f"Monthly Resource Plan {summary['month']} | "
            f"Required: {summary['total_required_hours']}h | "
            f"Available: {summary['available_hours']}h | "
            f"Peak FTE: {summary['peak_required_fte']}"
        ),
    )

    fig.write_html(args.output)
    print(f"Saved plot to: {args.output}")
    print(summary)


if __name__ == "__main__":
    main()
