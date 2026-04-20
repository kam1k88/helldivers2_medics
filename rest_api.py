#!/usr/bin/env python3
"""REST API service for MedicDivers with multi-window workers and animations."""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

import main as core

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    PLOTLY_AVAILABLE = True
except ModuleNotFoundError:
    PLOTLY_AVAILABLE = False


DB_PATH = Path(os.getenv("MEDICDIVERS_DB_PATH", "medicdivers.db")).expanduser()
ANIMATIONS_DIR = Path(os.getenv("MEDICDIVERS_ANIMATIONS_DIR", "animations")).expanduser()
WINDOWS = tuple(
    int(x.strip())
    for x in os.getenv("MEDICDIVERS_WINDOWS", "300,900,1800").split(",")
    if x.strip()
)
ANIMATION_TOP_N = int(os.getenv("MEDICDIVERS_ANIMATION_TOP_N", "12"))
ANIMATION_HISTORY_RUNS = int(os.getenv("MEDICDIVERS_ANIMATION_HISTORY_RUNS", "120"))
ANIMATION_REBUILD_EVERY = int(os.getenv("MEDICDIVERS_ANIMATION_REBUILD_EVERY", "1"))

CLASS_COLOR = {
    "resort": "#2A9D8F",
    "control": "#8AB17D",
    "problematic": "#E9C46A",
    "tough": "#F4A261",
    "slaughter": "#E63946",
}


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_storage() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANIMATIONS_DIR.mkdir(parents=True, exist_ok=True)
    with _db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              window_seconds INTEGER NOT NULL,
              requested_at TEXT NOT NULL,
              elapsed_seconds REAL NOT NULL,
              planets_count INTEGER NOT NULL,
              total_deaths_per_min REAL NOT NULL,
              avg_medcrit REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS planet_metrics (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL,
              planet_index INTEGER NOT NULL,
              planet_name TEXT NOT NULL,
              sector TEXT NOT NULL,
              owner TEXT NOT NULL,
              players_now INTEGER NOT NULL,
              delta_deaths INTEGER NOT NULL,
              delta_missions_total INTEGER NOT NULL,
              deaths_per_min REAL NOT NULL,
              deaths_per_100_players_min REAL NOT NULL,
              deaths_per_mission REAL NOT NULL,
              mission_fail_rate REAL NOT NULL,
              burn20 REAL NOT NULL,
              component_volume REAL NOT NULL DEFAULT 0,
              component_relative REAL NOT NULL DEFAULT 0,
              component_trend REAL NOT NULL,
              component_pressure REAL NOT NULL DEFAULT 0,
              medcrit_score REAL NOT NULL,
              medcrit_label TEXT NOT NULL,
              medcrit_rank INTEGER NOT NULL,
              gate_players REAL NOT NULL,
              gate_missions REAL NOT NULL,
              is_frontline INTEGER NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_runs_window_id
              ON runs(window_seconds, id DESC);
            CREATE INDEX IF NOT EXISTS idx_metrics_run
              ON planet_metrics(run_id);
            CREATE INDEX IF NOT EXISTS idx_metrics_planet
              ON planet_metrics(planet_index, run_id DESC);
            """
        )
        table_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(planet_metrics)").fetchall()}
        alter_columns = (
            "component_volume REAL NOT NULL DEFAULT 0",
            "component_relative REAL NOT NULL DEFAULT 0",
            "component_pressure REAL NOT NULL DEFAULT 0",
        )
        for col_def in alter_columns:
            col_name = col_def.split()[0]
            if col_name not in table_cols:
                conn.execute(f"ALTER TABLE planet_metrics ADD COLUMN {col_def}")


def _safe_div(n: float, d: float) -> float:
    return 0.0 if d == 0 else n / d


def persist_run(window_seconds: int, requested_at: datetime, elapsed_seconds: float, priorities: List[core.PlanetPriority]) -> int:
    total_dpm = sum(p.deaths_per_min for p in priorities)
    avg_medcrit = _safe_div(sum(p.medcrit_score for p in priorities), len(priorities))

    with _db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs(window_seconds, requested_at, elapsed_seconds, planets_count, total_deaths_per_min, avg_medcrit)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                int(window_seconds),
                requested_at.isoformat(),
                float(elapsed_seconds),
                int(len(priorities)),
                float(total_dpm),
                float(avg_medcrit),
            ),
        )
        run_id = int(cur.lastrowid)

        if priorities:
            conn.executemany(
                """
                INSERT INTO planet_metrics(
                    run_id, planet_index, planet_name, sector, owner, players_now,
                    delta_deaths, delta_missions_total, deaths_per_min, deaths_per_100_players_min,
                    deaths_per_mission, mission_fail_rate, burn20, component_volume, component_relative, component_trend, component_pressure,
                    medcrit_score, medcrit_label, medcrit_rank, gate_players, gate_missions, is_frontline
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        p.index,
                        p.name,
                        p.sector,
                        p.owner,
                        p.players_now,
                        p.delta_deaths,
                        p.delta_missions_total,
                        p.deaths_per_min,
                        p.deaths_per_100_players_min,
                        p.deaths_per_mission,
                        p.mission_fail_rate,
                        p.burn20,
                        p.component_volume,
                        p.component_relative,
                        p.component_trend,
                        p.component_pressure,
                        p.medcrit_score,
                        p.medcrit_label,
                        p.medcrit_rank,
                        p.gate_players,
                        p.gate_missions,
                        1 if p.is_frontline else 0,
                    )
                    for p in priorities
                ],
            )
    return run_id


def _fetch_recent_frames(window_seconds: int, top_n: int, max_runs: int) -> List[Dict[str, Any]]:
    with _db_connect() as conn:
        run_rows = conn.execute(
            """
            SELECT id, requested_at, elapsed_seconds
            FROM runs
            WHERE window_seconds = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (window_seconds, max_runs),
        ).fetchall()

        if not run_rows:
            return []

        run_ids = [int(row["id"]) for row in run_rows]
        rows = conn.execute(
            """
            SELECT
              m.run_id,
              m.planet_name,
              m.sector,
              m.medcrit_score,
              m.medcrit_label,
              m.medcrit_rank,
              m.component_volume,
              m.component_relative,
              m.component_trend,
              m.component_pressure,
              m.deaths_per_min,
              m.deaths_per_100_players_min,
              m.burn20,
              m.mission_fail_rate,
              m.players_now,
              m.gate_players,
              m.gate_missions
            FROM planet_metrics m
            WHERE m.run_id IN ({})
            ORDER BY m.run_id ASC, m.medcrit_score DESC
            """.format(",".join("?" for _ in run_ids)),
            run_ids,
        ).fetchall()

    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rid = int(row["run_id"])
        grouped[rid].append(
            {
                "planet_name": str(row["planet_name"]),
                "sector": str(row["sector"]),
                "medcrit_score": float(row["medcrit_score"]),
                "medcrit_label": str(row["medcrit_label"]),
                "medcrit_rank": int(row["medcrit_rank"]),
                "component_volume": float(row["component_volume"] or 0.0),
                "component_relative": float(row["component_relative"] or 0.0),
                "component_trend": float(row["component_trend"]),
                "component_pressure": float(row["component_pressure"] or 0.0),
                "deaths_per_min": float(row["deaths_per_min"]),
                "deaths_per_100_players_min": float(row["deaths_per_100_players_min"]),
                "burn20": float(row["burn20"]),
                "mission_fail_rate": float(row["mission_fail_rate"]),
                "players_now": int(row["players_now"]),
                "gate_players": float(row["gate_players"]),
                "gate_missions": float(row["gate_missions"]),
            }
        )

    frames: List[Dict[str, Any]] = []
    for rr in sorted(run_rows, key=lambda r: int(r["id"])):
        rid = int(rr["id"])
        ts = str(rr["requested_at"])
        frames.append(
            {
                "run_id": rid,
                "timestamp": ts,
                "elapsed_seconds": float(rr["elapsed_seconds"]),
                "items": grouped.get(rid, []),
            }
        )
    return frames


def _format_slider_time(timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        return timestamp[11:19] if len(timestamp) >= 19 else timestamp


def _build_frame_payload(frame: Dict[str, Any], top_n: int) -> Dict[str, Any]:
    items = list(frame.get("items", []))
    active = [x for x in items if int(x.get("players_now", 0)) > 0]
    by_medcrit = sorted(active, key=lambda x: x["medcrit_score"], reverse=True)[:top_n]
    by_trend = sorted(active, key=lambda x: x["component_trend"], reverse=True)[:top_n]

    medcrit_x = [x["medcrit_score"] for x in by_medcrit]
    medcrit_y = [f"{x['planet_name']} [{x['sector']}] | {x['medcrit_label']} ({x['medcrit_rank']})" for x in by_medcrit]
    medcrit_colors = [CLASS_COLOR.get(x["medcrit_label"], "#888888") for x in by_medcrit]

    trend_x = [x["component_trend"] for x in by_trend]
    trend_y = [f"{x['planet_name']} [{x['sector']}]" for x in by_trend]

    comp_labels = ["burn20", "fail", "vol", "rel", "trend", "pressure", "g_players", "g_missions"]
    comp_rows = by_medcrit[: min(10, len(by_medcrit))]
    if comp_rows:
        comp_y = [f"{x['planet_name']} [{x['sector']}]" for x in comp_rows]
        comp_z = [
            [
                x["burn20"],
                x["mission_fail_rate"],
                x["component_volume"],
                x["component_relative"],
                x["component_trend"],
                x["component_pressure"],
                x["gate_players"],
                x["gate_missions"],
            ]
            for x in comp_rows
        ]
        comp_text = [[f"{v:.3f}" for v in row] for row in comp_z]
    else:
        comp_y = ["no data"]
        comp_z = [[0.0 for _ in comp_labels]]
        comp_text = [["0.000" for _ in comp_labels]]

    scatter_dpm_x = [x["deaths_per_min"] for x in active]
    scatter_dpm_y = [x["deaths_per_100_players_min"] for x in active]
    scatter_dpm_color = [x["component_trend"] for x in active]
    scatter_dpm_size = [max(8, min(26, 7 + math.sqrt(max(1, x["players_now"]) / 25.0))) for x in active]
    scatter_dpm_text = [f"{x['planet_name']} | {x['medcrit_label']} ({x['medcrit_rank']})" for x in active]
    scatter_dpm_custom = [[x["medcrit_score"], x["burn20"], x["mission_fail_rate"]] for x in active]

    scatter_burn_x = [x["burn20"] for x in active]
    scatter_burn_y = [x["mission_fail_rate"] for x in active]
    scatter_burn_color = [x["medcrit_score"] for x in active]
    scatter_burn_size = [max(8, min(24, 8 + 10 * x["gate_missions"])) for x in active]
    scatter_burn_text = [f"{x['planet_name']} | {x['medcrit_label']} ({x['medcrit_rank']})" for x in active]
    scatter_burn_custom = [[x["component_trend"], x["deaths_per_min"], x["deaths_per_100_players_min"]] for x in active]

    class_order = [("resort", 5), ("control", 4), ("problematic", 3), ("tough", 2), ("slaughter", 1)]
    class_x = [f"{name} ({rank})" for name, rank in class_order]
    class_y = [sum(1 for x in active if x["medcrit_label"] == name) for name, _ in class_order]
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
        "scatter_dpm_x": scatter_dpm_x,
        "scatter_dpm_y": scatter_dpm_y,
        "scatter_dpm_color": scatter_dpm_color,
        "scatter_dpm_size": scatter_dpm_size,
        "scatter_dpm_text": scatter_dpm_text,
        "scatter_dpm_custom": scatter_dpm_custom,
        "scatter_burn_x": scatter_burn_x,
        "scatter_burn_y": scatter_burn_y,
        "scatter_burn_color": scatter_burn_color,
        "scatter_burn_size": scatter_burn_size,
        "scatter_burn_text": scatter_burn_text,
        "scatter_burn_custom": scatter_burn_custom,
        "class_x": class_x,
        "class_y": class_y,
        "class_colors": class_colors,
    }


def render_animation(window_seconds: int) -> Path | None:
    if not PLOTLY_AVAILABLE:
        return None

    frames_data = _fetch_recent_frames(window_seconds, ANIMATION_TOP_N, ANIMATION_HISTORY_RUNS)
    if not frames_data:
        return None

    first_payload = _build_frame_payload(frames_data[0], ANIMATION_TOP_N)
    colorscale = [[0.0, "#2F6BFF"], [0.5, "#8FAEFF"], [1.0, "#D7263D"]]

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

    fig.add_trace(
        go.Bar(
            x=first_payload["medcrit_x"],
            y=first_payload["medcrit_y"],
            orientation="h",
            marker=dict(color=first_payload["medcrit_colors"]),
            hovertemplate="%{y}<br>medcrit=%{x:.3f}<extra></extra>",
            name="medcrit",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Heatmap(
            x=first_payload["comp_labels"],
            y=first_payload["comp_y"],
            z=first_payload["comp_z"],
            text=first_payload["comp_text"],
            colorscale=colorscale,
            zmin=0.0,
            zmax=1.0,
            showscale=False,
            hovertemplate="%{y}<br>%{x}: %{text}<extra></extra>",
        ),
        row=1,
        col=2,
    )
    fig.add_trace(
        go.Bar(
            x=first_payload["trend_x"],
            y=first_payload["trend_y"],
            orientation="h",
            marker=dict(color="#642020"),
            hovertemplate="%{y}<br>trend=%{x:.3f}<extra></extra>",
            name="trend",
        ),
        row=1,
        col=3,
    )
    fig.add_trace(
        go.Scatter(
            x=first_payload["scatter_dpm_x"],
            y=first_payload["scatter_dpm_y"],
            mode="markers",
            marker=dict(
                size=first_payload["scatter_dpm_size"],
                color=first_payload["scatter_dpm_color"],
                colorscale=colorscale,
                cmin=0.0,
                cmax=1.0,
                showscale=True,
                colorbar=dict(title="trend", x=1.02, y=0.25, len=0.35),
                line=dict(color="rgba(255,255,255,0.45)", width=1),
                opacity=0.88,
            ),
            text=first_payload["scatter_dpm_text"],
            customdata=first_payload["scatter_dpm_custom"],
            hovertemplate=(
                "%{text}<br>d/min=%{x:.2f}<br>d/100p/min=%{y:.3f}<br>"
                "medcrit=%{customdata[0]:.3f}<br>burn20=%{customdata[1]:.3f}<br>fail=%{customdata[2]:.3f}<extra></extra>"
            ),
            name="dpm_vs_ndpm",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=first_payload["scatter_burn_x"],
            y=first_payload["scatter_burn_y"],
            mode="markers",
            marker=dict(
                size=first_payload["scatter_burn_size"],
                color=first_payload["scatter_burn_color"],
                colorscale=colorscale,
                cmin=0.0,
                cmax=1.0,
                line=dict(color="rgba(255,255,255,0.45)", width=1),
                opacity=0.88,
            ),
            text=first_payload["scatter_burn_text"],
            customdata=first_payload["scatter_burn_custom"],
            hovertemplate=(
                "%{text}<br>burn20=%{x:.3f}<br>fail-rate=%{y:.3f}<br>"
                "trend=%{customdata[0]:.3f}<br>d/min=%{customdata[1]:.2f}<br>d/100p/min=%{customdata[2]:.3f}<extra></extra>"
            ),
            name="burn_vs_fail",
        ),
        row=2,
        col=2,
    )
    fig.add_trace(
        go.Bar(
            x=first_payload["class_x"],
            y=first_payload["class_y"],
            marker=dict(color=first_payload["class_colors"]),
            hovertemplate="%{x}<br>planets=%{y}<extra></extra>",
            name="class_dist",
        ),
        row=2,
        col=3,
    )

    minutes = window_seconds / 60.0
    fig.update_layout(
        template="plotly_dark",
        showlegend=False,
        height=1250,
        width=2100,
        paper_bgcolor="rgb(14, 16, 18)",
        plot_bgcolor="rgb(21, 24, 27)",
        margin=dict(l=90, r=150, t=130, b=100),
        title=(
            "MedicDivers Animated Dashboard (6 charts)"
            f"<br><sup>Window: {minutes:.0f} min | Frames: {len(frames_data)} | "
            f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</sup>"
        ),
        updatemenus=[
            {
                "type": "buttons",
                "direction": "left",
                "x": 0.0,
                "y": 1.18,
                "showactive": True,
                "buttons": [
                    {
                        "label": "Play",
                        "method": "animate",
                        "args": [None, {"frame": {"duration": 700, "redraw": True}, "fromcurrent": True}],
                    },
                    {
                        "label": "Pause",
                        "method": "animate",
                        "args": [[None], {"frame": {"duration": 0, "redraw": False}, "mode": "immediate"}],
                    },
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
                        "label": _format_slider_time(frame["timestamp"]),
                        "method": "animate",
                        "args": [[frame["timestamp"]], {"mode": "immediate", "frame": {"duration": 0, "redraw": True}}],
                    }
                    for frame in frames_data
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

    fig.frames = [
        go.Frame(
            name=frame["timestamp"],
            data=[
                go.Bar(
                    x=payload["medcrit_x"],
                    y=payload["medcrit_y"],
                    orientation="h",
                    marker=dict(color=payload["medcrit_colors"]),
                ),
                go.Heatmap(
                    x=payload["comp_labels"],
                    y=payload["comp_y"],
                    z=payload["comp_z"],
                    text=payload["comp_text"],
                    colorscale=colorscale,
                    zmin=0.0,
                    zmax=1.0,
                    showscale=False,
                ),
                go.Bar(
                    x=payload["trend_x"],
                    y=payload["trend_y"],
                    orientation="h",
                    marker=dict(color="#642020"),
                ),
                go.Scatter(
                    x=payload["scatter_dpm_x"],
                    y=payload["scatter_dpm_y"],
                    mode="markers",
                    marker=dict(
                        size=payload["scatter_dpm_size"],
                        color=payload["scatter_dpm_color"],
                        colorscale=colorscale,
                        cmin=0.0,
                        cmax=1.0,
                        line=dict(color="rgba(255,255,255,0.45)", width=1),
                        opacity=0.88,
                    ),
                    text=payload["scatter_dpm_text"],
                    customdata=payload["scatter_dpm_custom"],
                ),
                go.Scatter(
                    x=payload["scatter_burn_x"],
                    y=payload["scatter_burn_y"],
                    mode="markers",
                    marker=dict(
                        size=payload["scatter_burn_size"],
                        color=payload["scatter_burn_color"],
                        colorscale=colorscale,
                        cmin=0.0,
                        cmax=1.0,
                        line=dict(color="rgba(255,255,255,0.45)", width=1),
                        opacity=0.88,
                    ),
                    text=payload["scatter_burn_text"],
                    customdata=payload["scatter_burn_custom"],
                ),
                go.Bar(
                    x=payload["class_x"],
                    y=payload["class_y"],
                    marker=dict(color=payload["class_colors"]),
                ),
            ],
            traces=[0, 1, 2, 3, 4, 5],
        )
        for frame in frames_data
        for payload in [_build_frame_payload(frame, ANIMATION_TOP_N)]
    ]

    output = ANIMATIONS_DIR / f"medcrit_{window_seconds}s.html"
    fig.write_html(str(output), include_plotlyjs="cdn", full_html=True)
    return output


class WindowWorker:
    def __init__(self, window_seconds: int) -> None:
        self.window_seconds = int(window_seconds)
        self.state_file = f".medcrit_state_{self.window_seconds}s.json"
        self.prev_snapshot: Dict[int, core.PlanetSnapshot] | None = None
        self.last_fetch_ts: float | None = None
        self.runs_completed = 0

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                self.prev_snapshot = await asyncio.to_thread(core.fetch_planet_snapshots)
                self.last_fetch_ts = time.time()
                print(f"[worker:{self.window_seconds}s] initial snapshot: {len(self.prev_snapshot)} planets")
                break
            except Exception as exc:
                print(f"[worker:{self.window_seconds}s] initial fetch failed: {exc}")
                await asyncio.sleep(min(20, max(5, self.window_seconds // 2)))

        if self.prev_snapshot is None:
            return

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.window_seconds)
                break
            except asyncio.TimeoutError:
                pass

            try:
                current = await asyncio.to_thread(core.fetch_planet_snapshots)
                now_ts = time.time()
                elapsed = max(1.0, now_ts - (self.last_fetch_ts or now_ts))
                priorities = await asyncio.to_thread(
                    core.build_priorities,
                    self.prev_snapshot or {},
                    current,
                    elapsed,
                    self.state_file,
                )

                requested_at = datetime.now(timezone.utc)
                await asyncio.to_thread(persist_run, self.window_seconds, requested_at, elapsed, priorities)
                self.runs_completed += 1

                if ANIMATION_REBUILD_EVERY > 0 and self.runs_completed % ANIMATION_REBUILD_EVERY == 0:
                    await asyncio.to_thread(render_animation, self.window_seconds)

                self.prev_snapshot = current
                self.last_fetch_ts = now_ts
                print(
                    f"[worker:{self.window_seconds}s] run={self.runs_completed} "
                    f"planets={len(priorities)} elapsed={elapsed:.1f}s"
                )
            except Exception as exc:
                print(f"[worker:{self.window_seconds}s] run failed: {exc}")


class Runtime:
    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.tasks: List[asyncio.Task[Any]] = []
        self.workers: Dict[int, WindowWorker] = {}

    async def start(self) -> None:
        init_storage()
        for w in WINDOWS:
            worker = WindowWorker(w)
            self.workers[w] = worker
            self.tasks.append(asyncio.create_task(worker.run(self.stop_event), name=f"worker-{w}s"))
        print(f"Started workers: {', '.join(f'{w}s' for w in WINDOWS)}")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        print("All workers stopped")


runtime = Runtime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    yield
    await runtime.stop()


app = FastAPI(title="MedicDivers REST API", version="1.0.0", lifespan=lifespan)
ANIMATIONS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/animations", StaticFiles(directory=str(ANIMATIONS_DIR)), name="animations")


def _validate_window(window: int) -> int:
    if window not in WINDOWS:
        raise HTTPException(status_code=400, detail=f"window must be one of {list(WINDOWS)}")
    return int(window)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "windows": list(WINDOWS),
        "db_path": str(DB_PATH.resolve()),
        "animations_dir": str(ANIMATIONS_DIR.resolve()),
        "plotly_available": PLOTLY_AVAILABLE,
    }


@app.get("/api/v1/windows")
def windows_overview() -> Dict[str, Any]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT window_seconds, COUNT(*) AS runs, MAX(requested_at) AS last_requested_at
            FROM runs
            GROUP BY window_seconds
            ORDER BY window_seconds
            """
        ).fetchall()

    stats = {int(r["window_seconds"]): dict(r) for r in rows}
    result = []
    for w in WINDOWS:
        entry = stats.get(w, {})
        result.append(
            {
                "window_seconds": w,
                "runs": int(entry.get("runs", 0) or 0),
                "last_requested_at": entry.get("last_requested_at"),
                "state_file": f".medcrit_state_{w}s.json",
                "animation_url": f"/animations/medcrit_{w}s.html",
            }
        )
    return {"windows": result}


@app.get("/api/v1/latest")
def latest(window: int = Query(900)) -> Dict[str, Any]:
    window = _validate_window(window)
    with _db_connect() as conn:
        run = conn.execute(
            """
            SELECT id, requested_at, elapsed_seconds, planets_count, total_deaths_per_min, avg_medcrit
            FROM runs
            WHERE window_seconds = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (window,),
        ).fetchone()

        if run is None:
            raise HTTPException(status_code=404, detail="no data yet for this window")

        metrics = conn.execute(
            """
            SELECT
              planet_index, planet_name, sector, owner, players_now,
              delta_deaths, delta_missions_total, deaths_per_min,
              deaths_per_100_players_min, deaths_per_mission, mission_fail_rate,
              burn20, component_trend, medcrit_score, medcrit_label, medcrit_rank,
              gate_players, gate_missions, is_frontline
            FROM planet_metrics
            WHERE run_id = ?
            ORDER BY medcrit_score DESC
            """,
            (int(run["id"]),),
        ).fetchall()

    return {
        "run": dict(run),
        "items": [dict(x) for x in metrics],
    }


@app.get("/api/v1/top")
def top(window: int = Query(900), n: int = Query(10, ge=1, le=50)) -> Dict[str, Any]:
    window = _validate_window(window)
    with _db_connect() as conn:
        run = conn.execute(
            "SELECT id, requested_at FROM runs WHERE window_seconds = ? ORDER BY id DESC LIMIT 1",
            (window,),
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="no data yet for this window")

        rows = conn.execute(
            """
            SELECT planet_index, planet_name, sector, medcrit_score, medcrit_label, medcrit_rank,
                   deaths_per_min, deaths_per_100_players_min, mission_fail_rate, burn20, component_trend
            FROM planet_metrics
            WHERE run_id = ?
            ORDER BY medcrit_score DESC
            LIMIT ?
            """,
            (int(run["id"]), n),
        ).fetchall()

    return {
        "window_seconds": window,
        "requested_at": run["requested_at"],
        "top": [dict(r) for r in rows],
    }


@app.get("/api/v1/history")
def history(
    window: int = Query(900),
    planet_index: int | None = Query(default=None),
    planet_name: str | None = Query(default=None),
    limit: int = Query(default=200, ge=10, le=2000),
) -> Dict[str, Any]:
    window = _validate_window(window)
    if planet_index is None and not planet_name:
        raise HTTPException(status_code=400, detail="set planet_index or planet_name")

    clauses = ["r.window_seconds = ?"]
    params: List[Any] = [window]
    if planet_index is not None:
        clauses.append("m.planet_index = ?")
        params.append(planet_index)
    if planet_name:
        clauses.append("LOWER(m.planet_name) = LOWER(?)")
        params.append(planet_name.strip())

    params.append(limit)
    where = " AND ".join(clauses)
    with _db_connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
              r.requested_at, r.elapsed_seconds, m.planet_index, m.planet_name, m.sector,
              m.players_now, m.deaths_per_min, m.deaths_per_100_players_min,
              m.mission_fail_rate, m.burn20, m.component_trend,
              m.medcrit_score, m.medcrit_label, m.medcrit_rank
            FROM planet_metrics m
            JOIN runs r ON r.id = m.run_id
            WHERE {where}
            ORDER BY r.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return {
        "window_seconds": window,
        "items": [dict(r) for r in reversed(rows)],
    }


@app.get("/api/v1/animation")
def animation(window: int = Query(900)) -> Dict[str, Any]:
    window = _validate_window(window)
    path = ANIMATIONS_DIR / f"medcrit_{window}s.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="animation not built yet")
    return {
        "window_seconds": window,
        "animation_url": f"/animations/{path.name}",
        "file_path": str(path.resolve()),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "size_bytes": int(path.stat().st_size),
    }


@app.post("/api/v1/rebuild-animation")
async def rebuild_animation(window: int = Query(900)) -> Dict[str, Any]:
    window = _validate_window(window)
    path = await asyncio.to_thread(render_animation, window)
    if path is None:
        raise HTTPException(status_code=404, detail="no frame data yet or plotly unavailable")
    return {
        "window_seconds": window,
        "animation_url": f"/animations/{path.name}",
        "file_path": str(path.resolve()),
    }
