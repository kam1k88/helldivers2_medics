#!/usr/bin/env python3
"""Generate GitHub Pages dashboards/animations for 5m/15m/30m windows."""

from __future__ import annotations

import json
import math
import os
import shutil
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from plotly.subplots import make_subplots

import main as core


WINDOWS = [300, 900, 1800]
TOP_N = int(os.getenv("PAGES_TOP_N", "12"))
TZ_NAME = os.getenv("PAGES_TZ", "Europe/Moscow")
ARCHIVE_DAYS_TO_PUBLISH = int(os.getenv("PAGES_ARCHIVE_DAYS_TO_PUBLISH", "30"))

DATA_DIR = Path(".pages_data")
PUBLIC_DIR = Path("public")

SNAPSHOTS_FILE = DATA_DIR / "snapshots.jsonl"
META_FILE = DATA_DIR / "meta.json"
ARCHIVE_DIR = DATA_DIR / "archive"

CLASS_COLOR = {
    "resort": "#2A9D8F",
    "control": "#8AB17D",
    "problematic": "#E9C46A",
    "tough": "#F4A261",
    "slaughter": "#E63946",
}
COMPARE_COLORSCALE = [[0.0, "#2F6BFF"], [0.5, "#8FAEFF"], [1.0, "#D7263D"]]


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso_now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_div(n: float, d: float) -> float:
    return 0.0 if d == 0 else n / d


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            continue
    return rows


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _snapshot_to_payload(snapshots: Dict[int, core.PlanetSnapshot]) -> List[Dict[str, Any]]:
    return [asdict(s) for s in snapshots.values()]


def _snapshot_from_payload(payload: List[Dict[str, Any]]) -> Dict[int, core.PlanetSnapshot]:
    out: Dict[int, core.PlanetSnapshot] = {}
    for row in payload:
        try:
            snap = core.PlanetSnapshot(**row)
            out[int(snap.index)] = snap
        except Exception:
            continue
    return out


def _history_file(window_seconds: int) -> Path:
    return DATA_DIR / f"runs_{window_seconds}.jsonl"


def _state_file(window_seconds: int) -> Path:
    return DATA_DIR / f"state_{window_seconds}.json"


def _select_base_snapshot(
    snapshots: List[Dict[str, Any]],
    now_dt: datetime,
    window_seconds: int,
) -> Dict[str, Any] | None:
    target = now_dt - timedelta(seconds=window_seconds)
    candidates = []
    for row in snapshots:
        ts = _parse_ts(str(row.get("timestamp", "")))
        if ts <= target:
            candidates.append((abs((target - ts).total_seconds()), row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _local_day(ts: str, tz: ZoneInfo) -> str:
    return _parse_ts(ts).astimezone(tz).date().isoformat()


def _archive_previous_day_if_needed(current_day: str, tz: ZoneInfo) -> None:
    meta = _read_json(META_FILE, {"current_day": current_day})
    prev_day = str(meta.get("current_day", current_day))
    if prev_day == current_day:
        return

    day_dir = ARCHIVE_DIR / prev_day
    day_dir.mkdir(parents=True, exist_ok=True)

    for w in WINDOWS:
        runs = _read_jsonl(_history_file(w))
        prev_runs = [r for r in runs if _local_day(str(r.get("timestamp", "")), tz) == prev_day]
        if prev_runs:
            _write_jsonl(day_dir / f"runs_{w}.jsonl", prev_runs)
            _render_animation(
                prev_runs,
                window_seconds=w,
                output_path=day_dir / f"animation_{w}.html",
                title_suffix=f"(archive {prev_day})",
            )
        current_runs = [r for r in runs if _local_day(str(r.get("timestamp", "")), tz) == current_day]
        _write_jsonl(_history_file(w), current_runs)

    _write_json(META_FILE, {"current_day": current_day, "updated_at_utc": _iso_now_utc()})


def _build_frame_payload(items: List[Dict[str, Any]], top_n: int) -> Dict[str, Any]:
    active = [x for x in items if int(x.get("players_now", 0)) > 0]
    by_medcrit = sorted(active, key=lambda x: float(x.get("medcrit_score", 0.0)), reverse=True)[:top_n]
    by_trend = sorted(active, key=lambda x: float(x.get("component_trend", 0.0)), reverse=True)[:top_n]

    medcrit_x = [float(x["medcrit_score"]) for x in by_medcrit]
    medcrit_y = [f"{x['name']} [{x['sector']}] | {x['medcrit_label']} ({x['medcrit_rank']})" for x in by_medcrit]
    medcrit_colors = [CLASS_COLOR.get(str(x["medcrit_label"]), "#888888") for x in by_medcrit]

    trend_x = [float(x["component_trend"]) for x in by_trend]
    trend_y = [f"{x['name']} [{x['sector']}]" for x in by_trend]

    comp_labels = ["burn20", "fail", "vol", "rel", "trend", "pressure", "g_players", "g_missions"]
    comp_rows = by_medcrit[: min(10, len(by_medcrit))]
    if comp_rows:
        comp_y = [f"{x['name']} [{x['sector']}]" for x in comp_rows]
        comp_z = [
            [
                float(x["burn20"]),
                float(x["mission_fail_rate"]),
                float(x["component_volume"]),
                float(x["component_relative"]),
                float(x["component_trend"]),
                float(x["component_pressure"]),
                float(x["gate_players"]),
                float(x["gate_missions"]),
            ]
            for x in comp_rows
        ]
        comp_text = [[f"{v:.3f}" for v in row] for row in comp_z]
    else:
        comp_y = ["no data"]
        comp_z = [[0.0 for _ in comp_labels]]
        comp_text = [["0.000" for _ in comp_labels]]

    dpm_x = [float(x["deaths_per_min"]) for x in active]
    dpm_y = [float(x["deaths_per_100_players_min"]) for x in active]
    dpm_color = [float(x["component_trend"]) for x in active]
    dpm_size = [max(8, min(26, 7 + math.sqrt(max(1, int(x["players_now"])) / 25.0))) for x in active]
    dpm_text = [f"{x['name']} | {x['medcrit_label']} ({x['medcrit_rank']})" for x in active]
    dpm_custom = [[float(x["medcrit_score"]), float(x["burn20"]), float(x["mission_fail_rate"])] for x in active]

    burn_x = [float(x["burn20"]) for x in active]
    burn_y = [float(x["mission_fail_rate"]) for x in active]
    burn_color = [float(x["medcrit_score"]) for x in active]
    burn_size = [max(8, min(24, 8 + 10 * float(x["gate_missions"]))) for x in active]
    burn_text = [f"{x['name']} | {x['medcrit_label']} ({x['medcrit_rank']})" for x in active]
    burn_custom = [[float(x["component_trend"]), float(x["deaths_per_mission"]), int(x["delta_missions_total"])] for x in active]

    class_order = [("resort", 5), ("control", 4), ("problematic", 3), ("tough", 2), ("slaughter", 1)]
    class_x = [f"{name} ({rank})" for name, rank in class_order]
    class_y = [sum(1 for x in active if str(x["medcrit_label"]) == name) for name, _ in class_order]
    class_colors = [CLASS_COLOR[name] for name, _ in class_order]

    return {
        "medcrit_x": medcrit_x or [0.0],
        "medcrit_y": medcrit_y or ["no data"],
        "medcrit_colors": medcrit_colors or ["#666666"],
        "trend_x": trend_x or [0.0],
        "trend_y": trend_y or ["no data"],
        "comp_labels": comp_labels,
        "comp_y": comp_y,
        "comp_z": comp_z,
        "comp_text": comp_text,
        "dpm_x": dpm_x,
        "dpm_y": dpm_y,
        "dpm_color": dpm_color,
        "dpm_size": dpm_size,
        "dpm_text": dpm_text,
        "dpm_custom": dpm_custom,
        "burn_x": burn_x,
        "burn_y": burn_y,
        "burn_color": burn_color,
        "burn_size": burn_size,
        "burn_text": burn_text,
        "burn_custom": burn_custom,
        "class_x": class_x,
        "class_y": class_y,
        "class_colors": class_colors,
    }


def _format_slider_time(ts: str) -> str:
    try:
        return _parse_ts(ts).strftime("%H:%M:%S")
    except Exception:
        return ts


def _themed_shell_css() -> str:
    return """
    :root {
      --bg-0: #070b12;
      --bg-1: #0d1420;
      --card: rgba(14, 24, 39, 0.86);
      --border: rgba(98, 127, 170, 0.34);
      --text: #f3f7ff;
      --muted: #9fb1c7;
      --accent-blue: #2f6bff;
      --accent-red: #d7263d;
      --accent-green: #1c773f;
      --glow: rgba(47, 107, 255, 0.25);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      font-family: "Rajdhani", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 12% 14%, rgba(47,107,255,0.12), transparent 34%),
        radial-gradient(circle at 90% 8%, rgba(28,119,63,0.10), transparent 28%),
        radial-gradient(circle at 84% 92%, rgba(215,38,61,0.12), transparent 30%),
        linear-gradient(145deg, var(--bg-0) 0%, var(--bg-1) 100%);
      overflow: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
      background-size: 42px 42px;
      mask-image: radial-gradient(circle at center, black 48%, transparent 100%);
      opacity: 0.4;
    }
    .page {
      position: relative;
      height: 100%;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 14px;
      padding: 16px 20px 14px;
      z-index: 1;
    }
    .masthead {
      border: 1px solid var(--border);
      background: linear-gradient(160deg, rgba(17,26,39,0.92), rgba(10,17,27,0.88));
      box-shadow: 0 14px 36px var(--glow), inset 0 0 0 1px rgba(255,255,255,0.03);
      border-radius: 16px;
      padding: 12px 16px 10px;
      backdrop-filter: blur(4px);
    }
    .masthead h1 {
      margin: 0;
      font-size: clamp(1.15rem, 1.75vw, 1.85rem);
      line-height: 1.2;
      letter-spacing: 0.03em;
      font-family: "Orbitron", "Rajdhani", sans-serif;
      text-transform: uppercase;
      color: #f9fcff;
    }
    .masthead p {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: clamp(0.93rem, 1.2vw, 1.02rem);
    }
    .chart-card {
      min-height: 0;
      border: 1px solid var(--border);
      border-radius: 18px;
      background: linear-gradient(160deg, rgba(12,20,30,0.92), rgba(8,12,20,0.94));
      box-shadow: 0 24px 70px rgba(0,0,0,0.50), inset 0 0 0 1px rgba(255,255,255,0.03);
      overflow: hidden;
      display: grid;
      grid-template-rows: 1fr;
    }
    .chart-wrap {
      width: 100%;
      height: 100%;
      min-height: 0;
      overflow: hidden;
      padding: 8px;
    }
    .chart-wrap > div {
      width: 100% !important;
      max-width: 100% !important;
      height: 100% !important;
    }
    .js-plotly-plot, .plotly, .plot-container, .svg-container {
      width: 100% !important;
      max-width: 100% !important;
      height: 100% !important;
    }
    .plotly .modebar {
      background: rgba(12, 18, 28, 0.74) !important;
      border: 1px solid rgba(98,127,170,0.36) !important;
      border-radius: 12px !important;
      padding: 2px !important;
      box-shadow: 0 8px 20px rgba(0,0,0,0.38);
      right: 14px !important;
      top: 14px !important;
    }
    .plotly .modebar-btn path {
      fill: #d9e6ff !important;
    }
    .footer-note {
      color: #8fa5c1;
      font-size: 0.90rem;
      text-align: right;
      margin: 0 4px;
    }
    a.inline-link {
      color: #9cc1ff;
      text-decoration: none;
    }
    a.inline-link:hover { text-decoration: underline; }
    """


def _render_themed_shell_page(
    *,
    title: str,
    heading: str,
    subheading: str,
    plot_fragment: str,
    footer_note: str,
    back_href: str | None = None,
) -> str:
    back_html = ""
    if back_href:
        back_html = f' · <a class="inline-link" href="{back_href}">Back</a>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700;800&family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet">
  <style>{_themed_shell_css()}</style>
</head>
<body>
  <main class="page">
    <section class="masthead">
      <h1>{heading}</h1>
      <p>{subheading}{back_html}</p>
    </section>
    <section class="chart-card">
      <div class="chart-wrap">
        {plot_fragment}
      </div>
    </section>
    <p class="footer-note">{footer_note}</p>
  </main>
</body>
</html>
"""


def _render_animation(
    run_rows: List[Dict[str, Any]],
    window_seconds: int,
    output_path: Path,
    title_suffix: str = "(today)",
) -> None:
    if not run_rows:
        return

    first_payload = _build_frame_payload(run_rows[0]["items"], TOP_N)
    fig = make_subplots(
        rows=2,
        cols=3,
        subplot_titles=(
            "Top MedCrit Score (0..1)",
            "MedCrit Components (Top Planets)",
            "Top Deterioration Trend",
            "Absolute vs Relative Mortality Balance",
            "Burn20 vs Mission Fail Rate",
            "Planet Classes (resort→slaughter)",
        ),
        vertical_spacing=0.14,
        horizontal_spacing=0.07,
    )

    fig.add_trace(go.Bar(x=first_payload["medcrit_x"], y=first_payload["medcrit_y"], orientation="h", marker=dict(color=first_payload["medcrit_colors"])), row=1, col=1)
    fig.add_trace(go.Heatmap(x=first_payload["comp_labels"], y=first_payload["comp_y"], z=first_payload["comp_z"], text=first_payload["comp_text"], colorscale=COMPARE_COLORSCALE, zmin=0, zmax=1, showscale=False), row=1, col=2)
    fig.add_trace(go.Bar(x=first_payload["trend_x"], y=first_payload["trend_y"], orientation="h", marker=dict(color="#642020")), row=1, col=3)
    fig.add_trace(
        go.Scatter(
            x=first_payload["dpm_x"],
            y=first_payload["dpm_y"],
            mode="markers",
            marker=dict(
                size=first_payload["dpm_size"],
                color=first_payload["dpm_color"],
                colorscale=COMPARE_COLORSCALE,
                cmin=0,
                cmax=1,
                line=dict(color="rgba(255,255,255,0.45)", width=1),
                opacity=0.88,
                showscale=True,
                colorbar=dict(title="trend", x=1.02, y=0.25, len=0.35),
            ),
            text=first_payload["dpm_text"],
            customdata=first_payload["dpm_custom"],
            hovertemplate=(
                "%{text}<br>d/min=%{x:.2f}<br>d/100p/min=%{y:.3f}<br>"
                "medcrit=%{customdata[0]:.3f}<br>burn20=%{customdata[1]:.3f}<br>fail=%{customdata[2]:.3f}<extra></extra>"
            ),
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=first_payload["burn_x"],
            y=first_payload["burn_y"],
            mode="markers",
            marker=dict(
                size=first_payload["burn_size"],
                color=first_payload["burn_color"],
                colorscale=COMPARE_COLORSCALE,
                cmin=0,
                cmax=1,
                line=dict(color="rgba(255,255,255,0.45)", width=1),
                opacity=0.88,
            ),
            text=first_payload["burn_text"],
            customdata=first_payload["burn_custom"],
            hovertemplate=(
                "%{text}<br>burn20=%{x:.3f}<br>fail-rate=%{y:.3f}<br>"
                "trend=%{customdata[0]:.3f}<br>d/mission=%{customdata[1]:.3f}<br>missionsΔ=%{customdata[2]}<extra></extra>"
            ),
        ),
        row=2,
        col=2,
    )
    fig.add_trace(go.Bar(x=first_payload["class_x"], y=first_payload["class_y"], marker=dict(color=first_payload["class_colors"])), row=2, col=3)

    fig.update_layout(
        template="plotly_dark",
        showlegend=False,
        autosize=True,
        height=920,
        paper_bgcolor="rgb(14, 16, 18)",
        plot_bgcolor="rgb(21, 24, 27)",
        margin=dict(l=88, r=130, t=128, b=110),
        title=(
            f"MedCrit Animated Dashboard {title_suffix}"
            f"<br><sup>Window: {window_seconds // 60} min | Frames: {len(run_rows)} | Updated UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}</sup>"
        ),
        title_font=dict(family="Orbitron, Rajdhani, sans-serif", size=30, color="#f5f9ff"),
        font=dict(family="Rajdhani, Segoe UI, sans-serif", size=14, color="#eaf0ff"),
        hoverlabel=dict(bgcolor="rgba(7,10,15,0.95)", bordercolor="#2f6bff", font=dict(color="#f4f7ff", size=13)),
        updatemenus=[
            {
                "type": "buttons",
                "direction": "left",
                "x": 0.0,
                "y": 1.17,
                "showactive": True,
                "pad": {"t": 6, "r": 8},
                "font": {"family": "Rajdhani, sans-serif", "size": 16, "color": "#f8fbff"},
                "bgcolor": "rgba(17,30,48,0.86)",
                "bordercolor": "#2f6bff",
                "borderwidth": 2,
                "buttons": [
                    {"label": "Play ▶", "method": "animate", "args": [None, {"frame": {"duration": 700, "redraw": True}, "fromcurrent": True}]},
                    {"label": "Pause ⏸", "method": "animate", "args": [[None], {"frame": {"duration": 0, "redraw": False}, "mode": "immediate"}]},
                ],
            }
        ],
        sliders=[
            {
                "active": 0,
                "x": 0.08,
                "len": 0.9,
                "y": -0.01,
                "pad": {"t": 58, "b": 0},
                "bgcolor": "rgba(17,30,48,0.65)",
                "activebgcolor": "#d7263d",
                "bordercolor": "#2f6bff",
                "borderwidth": 2,
                "ticklen": 8,
                "tickwidth": 2,
                "tickcolor": "#9fb7ff",
                "currentvalue": {
                    "visible": True,
                    "prefix": "Shown time (UTC): ",
                    "font": {
                        "family": "Orbitron, Rajdhani, sans-serif",
                        "size": 27,
                        "color": "#f8fcff",
                    },
                    "xanchor": "left",
                    "offset": 14,
                },
                "font": {
                    "family": "Rajdhani, sans-serif",
                    "size": 13,
                    "color": "#d6e3fb",
                },
                "steps": [
                    {
                        "label": _format_slider_time(str(fr["timestamp"])),
                        "method": "animate",
                        "args": [[str(fr["timestamp"])], {"mode": "immediate", "frame": {"duration": 0, "redraw": True}}],
                    }
                    for fr in run_rows
                ],
            }
        ],
    )
    fig.update_xaxes(range=[0, 1], title_text="MedCrit score", row=1, col=1)
    fig.update_xaxes(range=[0, 1], title_text="Trend component", row=1, col=3)
    fig.update_xaxes(title_text="deaths/min", row=2, col=1)
    fig.update_yaxes(title_text="deaths/100 players/min", row=2, col=1)
    fig.update_xaxes(range=[0, 1], title_text="burn20", row=2, col=2)
    fig.update_yaxes(range=[0, 1], title_text="mission fail rate", row=2, col=2)
    fig.update_yaxes(title_text="Planets", row=2, col=3)
    fig.update_yaxes(autorange="reversed", row=1, col=1)
    fig.update_yaxes(autorange="reversed", row=1, col=3)
    fig.update_xaxes(gridcolor="rgba(140,140,140,0.25)")
    fig.update_yaxes(gridcolor="rgba(140,140,140,0.25)")

    frames = []
    for fr in run_rows:
        payload = _build_frame_payload(fr["items"], TOP_N)
        frames.append(
            go.Frame(
                name=str(fr["timestamp"]),
                data=[
                    go.Bar(x=payload["medcrit_x"], y=payload["medcrit_y"], orientation="h", marker=dict(color=payload["medcrit_colors"])),
                    go.Heatmap(x=payload["comp_labels"], y=payload["comp_y"], z=payload["comp_z"], text=payload["comp_text"], colorscale=COMPARE_COLORSCALE, zmin=0, zmax=1, showscale=False),
                    go.Bar(x=payload["trend_x"], y=payload["trend_y"], orientation="h", marker=dict(color="#642020")),
                    go.Scatter(x=payload["dpm_x"], y=payload["dpm_y"], mode="markers", marker=dict(size=payload["dpm_size"], color=payload["dpm_color"], colorscale=COMPARE_COLORSCALE, cmin=0, cmax=1, line=dict(color="rgba(255,255,255,0.45)", width=1), opacity=0.88), text=payload["dpm_text"], customdata=payload["dpm_custom"]),
                    go.Scatter(x=payload["burn_x"], y=payload["burn_y"], mode="markers", marker=dict(size=payload["burn_size"], color=payload["burn_color"], colorscale=COMPARE_COLORSCALE, cmin=0, cmax=1, line=dict(color="rgba(255,255,255,0.45)", width=1), opacity=0.88), text=payload["burn_text"], customdata=payload["burn_custom"]),
                    go.Bar(x=payload["class_x"], y=payload["class_y"], marker=dict(color=payload["class_colors"])),
                ],
                traces=[0, 1, 2, 3, 4, 5],
            )
        )
    fig.frames = frames

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plot_id = f"medcrit-plot-{window_seconds}"
    plot_html = fig.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        div_id=plot_id,
        default_width="100%",
        default_height="100%",
        config={"responsive": True, "scrollZoom": False, "displaylogo": False},
        post_script="""
const _plot = document.getElementById('{plot_id}');
function _fitPlotHeight() {
  if (!_plot || typeof Plotly === 'undefined') return;
  const h = Math.max(560, window.innerHeight - 220);
  Plotly.relayout(_plot, {height: h});
}
_fitPlotHeight();
window.addEventListener('resize', _fitPlotHeight);
""",
    )
    page = _render_themed_shell_page(
        title=f"MedCrit Animation {window_seconds // 60} min",
        heading="MedCrit Animated Dashboard",
        subheading=(
            f"Window: {window_seconds // 60} min · Frames: {len(run_rows)} · "
            f"Updated (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
        ),
        plot_fragment=plot_html,
        footer_note="Timeline slider shows the active frame in UTC. Use Play/Pause for continuous replay.",
        back_href="index.html",
    )
    output_path.write_text(page, encoding="utf-8")


def _render_worker_today_pages(today_rows_by_window: Dict[int, List[Dict[str, Any]]], now_utc: str, day_local: str) -> None:
    for w, rows in today_rows_by_window.items():
        if not rows:
            continue
        _render_animation(rows, window_seconds=w, output_path=PUBLIC_DIR / f"animation_{w}_today.html", title_suffix=f"(today {day_local})")

        latest_items = rows[-1]["items"]
        latest_summary = core.generate_summary([core.PlanetPriority(**x) for x in latest_items], float(rows[-1]["elapsed_seconds"]))
        (PUBLIC_DIR / f"summary_{w}.txt").write_text(latest_summary, encoding="utf-8")

    links = []
    for w in WINDOWS:
        file_name = f"animation_{w}_today.html"
        if (PUBLIC_DIR / file_name).exists():
            links.append(f'<li><a href="{file_name}">Window {w//60} min (today)</a></li>')
        else:
            links.append(f"<li>Window {w//60} min (today): no data yet</li>")

    archive_links = []
    archive_pub = PUBLIC_DIR / "archive"
    if archive_pub.exists():
        for day_dir in sorted([p for p in archive_pub.iterdir() if p.is_dir()], reverse=True):
            archive_links.append(f'<li><a href="archive/{day_dir.name}/index.html">{day_dir.name}</a></li>')

    index_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>MedCrit Pages Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700;800&family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet">
  <style>
    {_themed_shell_css()}
    body {{ overflow:auto; min-height:100%; }}
    .page {{ height: auto; min-height:100vh; grid-template-rows: auto auto auto; }}
    .stack {{ display:grid; gap:14px; }}
    .box {{
      border: 1px solid var(--border);
      background: linear-gradient(160deg, rgba(17,26,39,0.92), rgba(10,17,27,0.88));
      box-shadow: 0 14px 36px var(--glow), inset 0 0 0 1px rgba(255,255,255,0.03);
      border-radius: 14px;
      padding: 14px 16px;
    }}
    h2 {{ margin:0 0 8px; font-family:"Orbitron","Rajdhani",sans-serif; letter-spacing:0.03em; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li {{ margin: 6px 0; color: #d9e5f8; }}
    a {{ color:#9cc1ff; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
  </style>
</head>
<body>
  <main class="page">
    <section class="masthead">
      <h1>MedCrit Daily Animated Dashboards</h1>
      <p>Timezone: {TZ_NAME} · Current local day: {day_local} · Updated (UTC): {now_utc}</p>
    </section>
    <section class="stack">
      <div class="box">
        <h2>Today</h2>
        <ul>{''.join(links)}</ul>
      </div>
      <div class="box">
        <h2>Archive</h2>
        <ul>{''.join(archive_links) if archive_links else '<li>No archive days yet</li>'}</ul>
      </div>
    </section>
    <p class="footer-note">Select a window to open the themed animated dashboard.</p>
  </main>
</body>
</html>
"""
    (PUBLIC_DIR / "index.html").write_text(index_html, encoding="utf-8")
    (PUBLIC_DIR / "generated_at_utc.txt").write_text(now_utc + "\n", encoding="utf-8")


def _publish_archive_to_public(tz: ZoneInfo) -> None:
    archive_pub = PUBLIC_DIR / "archive"
    if archive_pub.exists():
        shutil.rmtree(archive_pub)
    archive_pub.mkdir(parents=True, exist_ok=True)

    day_dirs = sorted([p for p in ARCHIVE_DIR.iterdir() if p.is_dir()], reverse=True)[:ARCHIVE_DAYS_TO_PUBLISH]
    for day_dir in day_dirs:
        dst = archive_pub / day_dir.name
        dst.mkdir(parents=True, exist_ok=True)
        links = []
        for w in WINDOWS:
            runs_file = day_dir / f"runs_{w}.jsonl"
            rows = _read_jsonl(runs_file)
            if rows:
                _render_animation(
                    rows,
                    window_seconds=w,
                    output_path=dst / f"animation_{w}.html",
                    title_suffix=f"(archive {day_dir.name})",
                )
                links.append(f'<li><a href="animation_{w}.html">Window {w//60} min</a></li>')
        idx = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Archive {day_dir.name}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700;800&family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet">
  <style>
    {_themed_shell_css()}
    body {{ overflow:auto; min-height:100%; }}
    .page {{ height:auto; min-height:100vh; grid-template-rows:auto auto auto; }}
    .box {{
      border: 1px solid var(--border);
      background: linear-gradient(160deg, rgba(17,26,39,0.92), rgba(10,17,27,0.88));
      box-shadow: 0 14px 36px var(--glow), inset 0 0 0 1px rgba(255,255,255,0.03);
      border-radius: 14px;
      padding: 14px 16px;
    }}
    h2 {{ margin:0 0 8px; font-family:"Orbitron","Rajdhani",sans-serif; letter-spacing:0.03em; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li {{ margin: 6px 0; color: #d9e5f8; }}
    a {{ color:#9cc1ff; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
  </style>
</head>
<body>
  <main class="page">
    <section class="masthead">
      <h1>Archive {day_dir.name}</h1>
      <p>Choose a time window to open the archived animation.</p>
    </section>
    <section class="box">
      <h2>Windows</h2>
      <ul>{''.join(links) if links else '<li>No files</li>'}</ul>
    </section>
    <p class="footer-note"><a class="inline-link" href="../..">Back to home</a></p>
  </main>
</body>
</html>"""
        (dst / "index.html").write_text(idx, encoding="utf-8")


def main() -> int:
    _ensure_dirs()
    tz = ZoneInfo(TZ_NAME)

    now_utc_dt = datetime.now(timezone.utc)
    now_utc = now_utc_dt.isoformat()
    current_day = now_utc_dt.astimezone(tz).date().isoformat()

    _archive_previous_day_if_needed(current_day, tz)

    snapshots = _read_jsonl(SNAPSHOTS_FILE)
    snapshots = [s for s in snapshots if (now_utc_dt - _parse_ts(str(s.get("timestamp", now_utc)))).total_seconds() <= 48 * 3600]

    curr_snap = core.fetch_planet_snapshots()
    snapshots.append({"timestamp": now_utc, "snapshots": _snapshot_to_payload(curr_snap)})
    _write_jsonl(SNAPSHOTS_FILE, snapshots)

    today_rows_by_window: Dict[int, List[Dict[str, Any]]] = {}
    for w in WINDOWS:
        base = _select_base_snapshot(snapshots, now_utc_dt, w)
        history_rows = _read_jsonl(_history_file(w))
        history_rows = [r for r in history_rows if _local_day(str(r.get("timestamp", "")), tz) == current_day]

        if base is not None:
            old_snap = _snapshot_from_payload(base["snapshots"])
            elapsed = max(1.0, (now_utc_dt - _parse_ts(str(base["timestamp"]))).total_seconds())
            priorities = core.build_priorities(old_snap, curr_snap, elapsed, state_file=str(_state_file(w)))
            history_rows.append(
                {
                    "timestamp": now_utc,
                    "elapsed_seconds": elapsed,
                    "items": [asdict(p) for p in priorities],
                }
            )
            history_rows = history_rows[-400:]
            _write_jsonl(_history_file(w), history_rows)
        today_rows_by_window[w] = history_rows

    _publish_archive_to_public(tz)
    _render_worker_today_pages(today_rows_by_window, now_utc=now_utc, day_local=current_day)

    _write_json(META_FILE, {"current_day": current_day, "updated_at_utc": now_utc})
    print(f"Generated pages for day={current_day}, windows={WINDOWS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
