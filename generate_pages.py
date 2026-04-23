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
TZ_NAME = os.getenv("PAGES_TZ", "UTC")
ARCHIVE_DAYS_TO_PUBLISH = int(os.getenv("PAGES_ARCHIVE_DAYS_TO_PUBLISH", "30"))
UPDATE_GRACE_SECONDS = int(os.getenv("PAGES_UPDATE_GRACE_SECONDS", "180"))

DATA_DIR = Path(".pages_data")
PUBLIC_DIR = Path("public")

SNAPSHOTS_FILE = DATA_DIR / "snapshots.jsonl"
META_FILE = DATA_DIR / "meta.json"
ARCHIVE_DIR = DATA_DIR / "archive"
ASSETS_DIR = Path("assets")

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


def _should_refresh_window(now_dt: datetime, window_seconds: int) -> bool:
    if window_seconds <= 300:
        return True
    phase = int(now_dt.timestamp()) % int(window_seconds)
    return phase <= UPDATE_GRACE_SECONDS


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


def _copy_brand_assets_to_public() -> None:
    branding = {
        "209b2ba2-5e77-4795-8962-51ee9fbd818d-removebg-preview.png": "logo_medicdivers.png",
        "b6899f8d-bd81-4588-8f5d-b73e634d70cc-removebg-preview.png": "logo_medcrit.png",
    }
    dst_dir = PUBLIC_DIR / "assets"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in branding.items():
        src = ASSETS_DIR / src_name
        if src.exists():
            shutil.copy2(src, dst_dir / dst_name)


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
            "Топ по MedCrit score (0..1)",
            "Компоненты MedCrit по топ-планетам",
            "Топ по тренду ухудшения",
            "Баланс абсолютной/относительной смертности",
            "Burn20 vs fail-rate",
            "Классификация планет (resort→slaughter)",
        ),
        vertical_spacing=0.15,
        horizontal_spacing=0.08,
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
        height=1250,
        width=2100,
        paper_bgcolor="rgb(14, 16, 18)",
        plot_bgcolor="rgb(21, 24, 27)",
        margin=dict(l=90, r=150, t=130, b=100),
        title=(
            f"MedicDivers Animated Dashboard {title_suffix}"
            f"<br><sup>Window: {window_seconds // 60} min | Frames: {len(run_rows)} | Last frame UTC: {str(run_rows[-1].get("timestamp", "n/a"))}</sup>"
        ),
        updatemenus=[
            {
                "type": "buttons",
                "direction": "left",
                "x": 0.0,
                "y": 1.18,
                "showactive": True,
                "buttons": [
                    {"label": "Play", "method": "animate", "args": [None, {"frame": {"duration": 700, "redraw": True}, "fromcurrent": True}]},
                    {"label": "Pause", "method": "animate", "args": [[None], {"frame": {"duration": 0, "redraw": False}, "mode": "immediate"}]},
                ],
            }
        ],
        sliders=[
            {
                "active": 0,
                "x": 0.08,
                "len": 0.9,
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
    fig.update_xaxes(range=[0, 1], title_text="medcrit score", row=1, col=1)
    fig.update_xaxes(range=[0, 1], title_text="trend", row=1, col=3)
    fig.update_xaxes(title_text="deaths/min", row=2, col=1)
    fig.update_yaxes(title_text="deaths/100 players/min", row=2, col=1)
    fig.update_xaxes(range=[0, 1], title_text="burn20", row=2, col=2)
    fig.update_yaxes(range=[0, 1], title_text="fail-rate", row=2, col=2)
    fig.update_yaxes(title_text="planets", row=2, col=3)
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
    fig.write_html(str(output_path), include_plotlyjs="cdn", full_html=True)


def _render_worker_today_pages(today_rows_by_window: Dict[int, List[Dict[str, Any]]], now_utc: str, day_local: str) -> None:
    _copy_brand_assets_to_public()

    for w, rows in today_rows_by_window.items():
        if not rows:
            continue
        _render_animation(rows, window_seconds=w, output_path=PUBLIC_DIR / f"animation_{w}_today.html", title_suffix=f"(today {day_local})")

        latest_items = rows[-1]["items"]
        latest_summary = core.generate_summary([core.PlanetPriority(**x) for x in latest_items], float(rows[-1]["elapsed_seconds"]))
        (PUBLIC_DIR / f"summary_{w}.txt").write_text(latest_summary, encoding="utf-8")

    worker_cards = []
    for w in WINDOWS:
        file_name = f"animation_{w}_today.html"
        rows = today_rows_by_window.get(w, [])
        badge = f"{w // 60} MIN"
        badge_cls = f"w{w // 60}"
        if (PUBLIC_DIR / file_name).exists() and rows:
            last_ts = str(rows[-1].get("timestamp", "n/a"))
            worker_cards.append(
                f'<a class="worker-card" href="{file_name}">'
                f'<span class="window-badge {badge_cls}">{badge}</span>'
                f'<span class="worker-title">Worker {w // 60} min</span>'
                f'<span class="worker-meta">Last frame UTC: {last_ts}</span>'
                f'</a>'
            )
        elif (PUBLIC_DIR / file_name).exists():
            worker_cards.append(
                f'<a class="worker-card" href="{file_name}">'
                f'<span class="window-badge {badge_cls}">{badge}</span>'
                f'<span class="worker-title">Worker {w // 60} min</span>'
                f'<span class="worker-meta">Данные скоро появятся</span>'
                f'</a>'
            )
        else:
            worker_cards.append(
                f'<div class="worker-card muted">'
                f'<span class="window-badge {badge_cls}">{badge}</span>'
                f'<span class="worker-title">Worker {w // 60} min</span>'
                f'<span class="worker-meta">Пока нет данных</span>'
                f'</div>'
            )

    top_medcrit_cards = []
    top_15_rows = today_rows_by_window.get(900, [])
    if top_15_rows:
        latest_15_items = top_15_rows[-1].get("items", [])
        ranked_15 = sorted(latest_15_items, key=lambda x: float(x.get("medcrit_score", 0.0)), reverse=True)[:3]
        for idx, item in enumerate(ranked_15):
            score = float(item.get("medcrit_score", 0.0))
            med_class = str(item.get("medcrit_label", "control"))
            color = CLASS_COLOR.get(med_class, "#8FB0C4")
            size_cls = "leader" if idx == 0 else "runner"
            planet = f"{item.get('name', 'Unknown')} [{item.get('sector', '?')}]"
            top_medcrit_cards.append(
                f'<div class="top-card {size_cls}" style="--class-color:{color};">'
                f'<div class="top-head">#{idx + 1} · {med_class.upper()}</div>'
                f'<div class="top-score">{score:.3f}</div>'
                f'<div class="top-planet">{planet}</div>'
                f'</div>'
            )
    if not top_medcrit_cards:
        top_medcrit_cards.append('<div class="top-card empty-card">No data yet for 15-minute window.</div>')

    archive_badges = []
    archive_pub = PUBLIC_DIR / "archive"
    if archive_pub.exists():
        for day_dir in sorted([p for p in archive_pub.iterdir() if p.is_dir()], reverse=True):
            archive_badges.append(f'<a class="day-badge" href="archive/{day_dir.name}/index.html">{day_dir.name}</a>')

    index_html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>MedicDivers Pages Dashboard</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700&family=Rajdhani:wght@500;700&display=swap');
    :root {{
      --bg-1: #081118;
      --bg-2: #10222f;
      --panel: rgba(11, 20, 30, 0.82);
      --line: rgba(133, 185, 224, 0.24);
      --txt: #e8f3ff;
      --muted: #93a8bd;
      --accent: #6ed4ff;
      --green: #1dbb6f;
      --gold: #f2c94c;
      --orange: #ff9f43;
      --red: #ff4d4f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: 'Rajdhani', sans-serif;
      color: var(--txt);
      background:
        radial-gradient(1200px 600px at 10% -5%, rgba(30, 110, 150, 0.35), transparent 60%),
        radial-gradient(900px 500px at 90% 0%, rgba(22, 58, 94, 0.35), transparent 60%),
        linear-gradient(180deg, var(--bg-2), var(--bg-1));
      padding: 2rem 1rem 3rem;
    }}
    .shell {{ max-width: 1160px; margin: 0 auto; }}
    .hero {{
      border: 1px solid var(--line);
      border-radius: 20px;
      background: linear-gradient(145deg, rgba(10, 22, 33, 0.9), rgba(8, 15, 24, 0.95));
      padding: 1.2rem 1.2rem 1.6rem;
      box-shadow: 0 20px 50px rgba(0,0,0,0.35);
    }}
    .brand-row {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
      align-items: center;
      margin-bottom: 1rem;
    }}
    .brand {{
      border: 1px solid rgba(135, 180, 220, 0.2);
      border-radius: 14px;
      min-height: 120px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(6, 15, 24, 0.72);
      padding: 0.6rem;
    }}
    .brand img {{ max-width: 100%; max-height: 116px; object-fit: contain; }}
    .title {{
      font-family: 'Orbitron', sans-serif;
      letter-spacing: 0.06em;
      font-size: clamp(1.3rem, 2.2vw, 2rem);
      margin: 0.2rem 0 0.35rem;
      text-transform: uppercase;
    }}
    .meta {{ color: var(--muted); font-size: 1.05rem; }}
    .section {{ margin-top: 1.2rem; }}
    .section h2 {{
      margin: 0 0 0.65rem;
      font-family: 'Orbitron', sans-serif;
      letter-spacing: 0.04em;
      font-size: 1.1rem;
      text-transform: uppercase;
    }}
    .top-grid {{
      display: grid;
      grid-template-columns: 1.35fr 1fr 1fr;
      gap: 0.9rem;
    }}
    .top-card {{
      border: 1px solid rgba(135, 180, 220, 0.24);
      border-radius: 14px;
      background: var(--panel);
      padding: 0.9rem;
      min-height: 128px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 0.25rem;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.02);
    }}
    .top-card.leader {{
      min-height: 160px;
      background: linear-gradient(145deg, rgba(19, 37, 54, 0.9), rgba(11, 20, 30, 0.9));
      border-color: rgba(149, 214, 255, 0.55);
    }}
    .top-card.empty-card {{
      grid-column: 1 / -1;
      color: var(--muted);
      justify-content: center;
      align-items: center;
      text-align: center;
    }}
    .top-head {{
      font-size: 0.84rem;
      letter-spacing: 0.07em;
      color: var(--muted);
      text-transform: uppercase;
    }}
    .top-score {{
      font-family: 'Orbitron', sans-serif;
      color: var(--class-color);
      font-weight: 700;
      line-height: 1;
      font-size: 2rem;
      text-shadow: 0 0 16px color-mix(in srgb, var(--class-color) 35%, transparent);
    }}
    .top-card.leader .top-score {{ font-size: 3.25rem; }}
    .top-planet {{
      color: #d7e8f7;
      font-size: 1.02rem;
      font-weight: 700;
      letter-spacing: 0.01em;
    }}
    .workers {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.9rem; }}
    .worker-card {{
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      text-decoration: none;
      color: var(--txt);
      border: 1px solid rgba(135, 180, 220, 0.24);
      border-radius: 14px;
      padding: 0.85rem;
      background: var(--panel);
      transition: transform .2s ease, border-color .2s ease, box-shadow .2s ease;
    }}
    .worker-card:hover {{ transform: translateY(-3px); border-color: rgba(110, 212, 255, 0.65); box-shadow: 0 8px 20px rgba(0,0,0,.25); }}
    .worker-card.muted {{ opacity: 0.65; }}
    .worker-title {{ font-size: 1.2rem; font-weight: 700; }}
    .worker-meta {{ color: var(--muted); font-size: 0.98rem; }}
    .window-badge {{
      align-self: flex-start;
      font-family: 'Orbitron', sans-serif;
      font-size: 0.75rem;
      letter-spacing: 0.07em;
      border-radius: 999px;
      padding: 0.28rem 0.58rem;
      border: 1px solid transparent;
      text-transform: uppercase;
    }}
    .window-badge.w5 {{ background: rgba(29,187,111,.18); color: #6af0a8; border-color: rgba(29,187,111,.52); }}
    .window-badge.w15 {{ background: rgba(242,201,76,.16); color: #ffe295; border-color: rgba(242,201,76,.5); }}
    .window-badge.w30 {{ background: rgba(255,77,79,.16); color: #ff9fa1; border-color: rgba(255,77,79,.5); }}
    .days {{ display: flex; flex-wrap: wrap; gap: 0.55rem; }}
    .day-badge {{
      text-decoration: none;
      color: #cfe9ff;
      background: rgba(29, 68, 101, 0.36);
      border: 1px solid rgba(110, 212, 255, 0.36);
      border-radius: 999px;
      padding: 0.36rem 0.72rem;
      font-size: 0.95rem;
      transition: background .2s ease, border-color .2s ease;
    }}
    .day-badge:hover {{ background: rgba(49, 112, 165, 0.45); border-color: rgba(110, 212, 255, 0.72); }}
    .empty {{ color: var(--muted); }}
    @media (max-width: 900px) {{
      .brand-row {{ grid-template-columns: 1fr; }}
      .top-grid {{ grid-template-columns: 1fr; }}
      .workers {{ grid-template-columns: 1fr; }}
      .top-card.leader .top-score {{ font-size: 2.45rem; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="brand-row">
        <div class="brand"><img src="assets/logo_medicdivers.png" alt="MedicDivers Utilities" /></div>
        <div class="brand"><img src="assets/logo_medcrit.png" alt="MedCrit by Permacura" /></div>
      </div>
      <h1 class="title">MedicDivers Daily Animated Dashboards</h1>
      <p class="meta">Timezone: UTC (Greenwich) | Day (UTC): {day_local} | Updated UTC: {now_utc}</p>
    </section>

    <section class="section">
      <h2>Top 3 MedCrit · 15 Minute Window</h2>
      <div class="top-grid">
        {''.join(top_medcrit_cards)}
      </div>
    </section>

    <section class="section">
      <h2>Workers</h2>
      <div class="workers">
        {''.join(worker_cards)}
      </div>
    </section>

    <section class="section">
      <h2>Archive Days</h2>
      <div class="days">
        {''.join(archive_badges) if archive_badges else '<span class="empty">Пока пусто</span>'}
      </div>
    </section>
  </div>
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
            src_anim = day_dir / f"animation_{w}.html"
            if src_anim.exists():
                shutil.copy2(src_anim, dst / f"animation_{w}.html")
                links.append(f'<li><a href="animation_{w}.html">Worker {w//60} min</a></li>')
        idx = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Archive {day_dir.name}</title></head>
<body style="font-family: sans-serif; max-width:800px; margin:2rem auto;">
<h1>Archive {day_dir.name}</h1>
<ul>{''.join(links) if links else '<li>Нет файлов</li>'}</ul>
<p><a href="../..">Back</a></p>
</body></html>"""
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
    refreshed_windows: List[int] = []
    for w in WINDOWS:
        base = _select_base_snapshot(snapshots, now_utc_dt, w)
        history_rows = _read_jsonl(_history_file(w))
        history_rows = [r for r in history_rows if _local_day(str(r.get("timestamp", "")), tz) == current_day]

        if base is not None and _should_refresh_window(now_utc_dt, w):
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
            refreshed_windows.append(w)
        today_rows_by_window[w] = history_rows

    _publish_archive_to_public(tz)
    _render_worker_today_pages(today_rows_by_window, now_utc=now_utc, day_local=current_day)

    _write_json(META_FILE, {"current_day": current_day, "updated_at_utc": now_utc})
    print(f"Generated pages for day={current_day}, refreshed={refreshed_windows}, windows={WINDOWS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
