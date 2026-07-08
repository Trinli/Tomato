import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

MUTED_GRAY = "#898781"
BLUE = "#2a78d6"
AQUA = "#1baf7a"
PUMP_COLORS = {1: "#d6822a", 2: "#af1b6e"}


def add_session_columns(df):
    df["session_id"] = (df["seconds_since_start"].diff() < 0).cumsum()
    df["seconds_in_session"] = df.groupby("session_id")["seconds_since_start"].transform(
        lambda s: s - s.iloc[0]
    )
    df["days_since_start"] = df["seconds_in_session"] / 86400
    return df


def styled_figure(title):
    # Row 2 is a synced rug of pump watering events on the same per-session
    # time axis as row 1, so pump activity lines up with charge-time changes.
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.8, 0.2], vertical_spacing=0.04,
    )
    fig.update_layout(
        template="plotly_white",
        title=title,
        hovermode="x unified",
        legend=dict(traceorder="reversed"),
    )
    fig.update_yaxes(title_text="Charge time (µs)", row=1, col=1)
    fig.update_xaxes(title_text="Days since last restart", rangeslider_visible=True, row=2, col=1)
    fig.update_yaxes(
        title_text="Pump", tickvals=[1, 2], ticktext=["Pump 1", "Pump 2"], range=[0.5, 2.5], row=2, col=1,
    )
    return fig


def add_pump_traces(fig, pump_df):
    for pump_id, color in PUMP_COLORS.items():
        sub = pump_df[pump_df["pump"] == pump_id]
        fig.add_trace(
            go.Scatter(
                x=sub["days_since_start"], y=[pump_id] * len(sub),
                name=f"Pump {pump_id}", mode="markers",
                marker=dict(color=color, symbol="line-ns", size=10, line=dict(width=2, color=color)),
                hovertext=[f"pulse {p}" for p in sub["pulse_count"]],
                hoverinfo="text+x",
            ),
            row=2, col=1,
        )


def build_charts(log_file, sensor_label, plot_html, hourly_html, pump_df):
    df = pd.read_csv(log_file)
    reading_cols = [c for c in df.columns if c.startswith("reading_")]
    is_raw_format = len(reading_cols) > 0

    # The microcontroller's uptime clock resets to 0 on every reboot, so a drop
    # in seconds_since_start marks the start of a new session.
    df = add_session_columns(df)
    n_sessions = df["session_id"].nunique()

    # Only the current run matters day to day, so drop everything before the
    # most recent restart.
    last_session = df["session_id"].max()
    df = df[df["session_id"] == last_session].reset_index(drop=True)
    pump_df = pump_df[pump_df["session_id"] == pump_df["session_id"].max()]

    print(f"{log_file}: {n_sessions} session(s) (device restart(s)) total; showing only the latest")

    if not is_raw_format:
        # Firmware since 2026-07-08 logs one already-aggregated row per hour
        # (seconds_since_start,mean_us,min_us,max_us) instead of 20 raw
        # readings per 5-min sample, so no client-side resampling is needed.
        fig = styled_figure(f"{sensor_label} charge time — hourly mean and range (aggregated on-device)")
        fig.add_trace(go.Scatter(
            x=df["days_since_start"], y=df["max_us"],
            name="Max", mode="lines", line=dict(color=MUTED_GRAY, width=1, dash="dot"),
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df["days_since_start"], y=df["min_us"],
            name="Min", mode="lines", line=dict(color=MUTED_GRAY, width=1, dash="dash"),
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df["days_since_start"], y=df["mean_us"],
            name="Mean", mode="lines", line=dict(color=BLUE, width=3),
        ), row=1, col=1)
        add_pump_traces(fig, pump_df)
        fig.write_html(plot_html)
        fig.show()
        print(f"{log_file}: already hourly-aggregated on-device, skipping separate {hourly_html}")
        return

    df["median_us"] = df[reading_cols].median(axis=1)
    df["mean_us"] = df[reading_cols].mean(axis=1)
    df["max_us"] = df[reading_cols].max(axis=1)
    df["min_us"] = df[reading_cols].min(axis=1)

    # --- Per-sample chart: median/mean/min/max across the 20 readings at each timestamp ---
    fig = styled_figure(
        f"{sensor_label} charge time — median (robust), mean, and range across 20 readings per sample"
    )
    fig.add_trace(go.Scatter(
        x=df["days_since_start"], y=df["max_us"],
        name="Max", mode="lines", line=dict(color=MUTED_GRAY, width=1, dash="dot"),
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df["days_since_start"], y=df["min_us"],
        name="Min", mode="lines", line=dict(color=MUTED_GRAY, width=1, dash="dash"),
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df["days_since_start"], y=df["mean_us"],
        name="Mean", mode="lines", line=dict(color=AQUA, width=2),
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df["days_since_start"], y=df["median_us"],
        name="Median", mode="lines", line=dict(color=BLUE, width=3),
    ), row=1, col=1)
    add_pump_traces(fig, pump_df)
    fig.write_html(plot_html)
    fig.show()

    # --- Hourly chart: mean/max/min of the per-sample median, one bin per hour ---
    idx = pd.to_timedelta(df["seconds_in_session"], unit="s")
    hourly_df = df.set_index(idx)["median_us"].resample("1h").agg(["mean", "max", "min"])
    hourly_df["days_since_start"] = hourly_df.index.total_seconds() / 86400
    hourly_df = hourly_df.reset_index(drop=True)

    fig2 = styled_figure(f"Hourly aggregation of {sensor_label.lower()} median charge time — smoothing sample-to-sample noise")
    fig2.add_trace(go.Scatter(
        x=hourly_df["days_since_start"], y=hourly_df["max"],
        name="Hourly max", mode="lines", line=dict(color=MUTED_GRAY, width=1, dash="dot"),
    ), row=1, col=1)
    fig2.add_trace(go.Scatter(
        x=hourly_df["days_since_start"], y=hourly_df["min"],
        name="Hourly min", mode="lines", line=dict(color=MUTED_GRAY, width=1, dash="dash"),
    ), row=1, col=1)
    fig2.add_trace(go.Scatter(
        x=hourly_df["days_since_start"], y=hourly_df["mean"],
        name="Hourly mean", mode="lines", line=dict(color=BLUE, width=3),
    ), row=1, col=1)
    add_pump_traces(fig2, pump_df)
    fig2.write_html(hourly_html)
    fig2.show()


pump_df = pd.read_csv("pump_log.csv")
pump_df = add_session_columns(pump_df)

build_charts("cap_log.csv", "Sensor 1", "cap_log_plot.html", "cap_log_hourly.html", pump_df)
build_charts("cap_log_2.csv", "Sensor 2", "cap_log_2_plot.html", "cap_log_2_hourly.html", pump_df)
