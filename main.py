#!/usr/bin/env python3
"""medicdivers_service: Helldivers 2 medical-support priority reporter.

On startup the script:
1. Pulls current planet stats from api.helldivers2.dev
2. Waits for a sampling window
3. Pulls stats again and computes mortality dynamics
4. Ranks planets where support/medics could have the highest impact
5. Builds an extended Plotly dashboard with many mortality-related views
"""

from __future__ import annotations

import json
import math
import http.client
import os
import socket
import ssl
import sys
import textwrap
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    PLOTLY_AVAILABLE = True
except ModuleNotFoundError:
    PLOTLY_AVAILABLE = False


API_URL = os.getenv("HD2_API_URL", "https://api.helldivers2.dev/api/v1/planets")
CLIENT_HEADER = os.getenv("HD2_CLIENT_HEADER", "medicdivers_service")
CONTACT_HEADER = os.getenv("HD2_CONTACT_HEADER", "mailto:medicdivers@example.com")
SAMPLE_SECONDS = int(os.getenv("SAMPLE_SECONDS", "30"))
TOP_N = int(os.getenv("TOP_N", "8"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "45"))
REQUEST_RETRIES = int(os.getenv("REQUEST_RETRIES", "4"))
RETRY_BACKOFF_SECONDS = float(os.getenv("RETRY_BACKOFF_SECONDS", "2"))
DASHBOARD_OUTPUT = os.getenv("DASHBOARD_OUTPUT", "dashboard.html")
DASHBOARD_TOP = int(os.getenv("DASHBOARD_TOP", "12"))
AUTO_OPEN_DASHBOARD = os.getenv("AUTO_OPEN_DASHBOARD", "1").strip().lower() not in ("0", "false", "no", "off")
FRONTLINE_ONLY = os.getenv("FRONTLINE_ONLY", "1").strip().lower() not in ("0", "false", "no", "off")
MIN_ACTIVE_PLAYERS = int(os.getenv("MIN_ACTIVE_PLAYERS", "50"))
NORMALIZATION_PLAYER_FLOOR = float(os.getenv("NORMALIZATION_PLAYER_FLOOR", "150"))

# MedCrit score model (0..1)
MEDCRIT_DPM_SCALE = float(os.getenv("MEDCRIT_DPM_SCALE", "120"))
MEDCRIT_NDPM_SCALE = float(os.getenv("MEDCRIT_NDPM_SCALE", "18"))
MEDCRIT_EMA_ALPHA = float(os.getenv("MEDCRIT_EMA_ALPHA", "0.35"))
MEDCRIT_PLAYER_GATE_CENTER = float(os.getenv("MEDCRIT_PLAYER_GATE_CENTER", "200"))
MEDCRIT_PLAYER_GATE_SCALE = float(os.getenv("MEDCRIT_PLAYER_GATE_SCALE", "40"))
MEDCRIT_MISSION_GATE_SCALE = float(os.getenv("MEDCRIT_MISSION_GATE_SCALE", "12"))
MEDCRIT_STATE_FILE = os.getenv("MEDCRIT_STATE_FILE", ".medcrit_state.json")

# Trend stabilization to avoid false 1.00 spikes
MEDCRIT_TREND_MIN_SAMPLES = int(os.getenv("MEDCRIT_TREND_MIN_SAMPLES", "2"))
MEDCRIT_TREND_MIN_MISSIONS = int(os.getenv("MEDCRIT_TREND_MIN_MISSIONS", "3"))
MEDCRIT_TREND_MIN_DEATHS = int(os.getenv("MEDCRIT_TREND_MIN_DEATHS", "15"))
MEDCRIT_TREND_DPM_BASELINE = float(os.getenv("MEDCRIT_TREND_DPM_BASELINE", "8"))
MEDCRIT_FAIL_BASELINE = float(os.getenv("MEDCRIT_FAIL_BASELINE", "0.05"))
MEDCRIT_TREND_GROWTH_SCALE = float(os.getenv("MEDCRIT_TREND_GROWTH_SCALE", "1.6"))
MEDCRIT_VOLUME_MIX = float(os.getenv("MEDCRIT_VOLUME_MIX", "0.15"))
MEDCRIT_STRESS_BURN_COEF = float(os.getenv("MEDCRIT_STRESS_BURN_COEF", "1.45"))
MEDCRIT_STRESS_FAIL_COEF = float(os.getenv("MEDCRIT_STRESS_FAIL_COEF", "1.11"))
MEDCRIT_STRESS_RELATIVE_COEF = float(os.getenv("MEDCRIT_STRESS_RELATIVE_COEF", "0.16"))
MEDCRIT_STRESS_TREND_COEF = float(os.getenv("MEDCRIT_STRESS_TREND_COEF", "0.03"))
MEDCRIT_STRESS_SCALE = float(os.getenv("MEDCRIT_STRESS_SCALE", "0.90"))
MEDCRIT_NONLINEAR_GAMMA = float(os.getenv("MEDCRIT_NONLINEAR_GAMMA", "1.02"))
MEDCRIT_REFERENCE_WINDOW_SECONDS = float(os.getenv("MEDCRIT_REFERENCE_WINDOW_SECONDS", "30"))

# Requested palette
COLOR_PRIMARY = "rgb(100, 32, 32)"
COLOR_SECONDARY = "rgb(255, 255, 255)"
COLOR_ACCENT = "rgb(17, 100, 55)"
COLOR_PRIMARY_HEX = "#642020"
COLOR_SECONDARY_HEX = "#FFFFFF"
COLOR_ACCENT_HEX = "#116437"
COLOR_GRID = "rgba(140, 140, 140, 0.25)"
COLOR_ZERO = "rgba(160, 160, 160, 0.35)"
COLOR_BG = "rgb(14, 16, 18)"
COLOR_PANEL = "rgb(21, 24, 27)"

# Comparative palette: small=blue, large=red
COMPARE_LOW_HEX = "#2F6BFF"
COMPARE_MID_HEX = "#8FAEFF"
COMPARE_HIGH_HEX = "#D7263D"
COMPARE_COLORSCALE = [
    [0.0, COMPARE_LOW_HEX],
    [0.5, COMPARE_MID_HEX],
    [1.0, COMPARE_HIGH_HEX],
]

SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "900"))


@dataclass(frozen=True)
class PlanetSnapshot:
    index: int
    name: str
    sector: str
    current_owner: str
    deaths: int
    friendlies: int
    revives: int
    player_count: int
    mission_success_rate: float
    missions_won: int
    missions_lost: int
    mission_time: int
    time_played: int
    terminid_kills: int
    automaton_kills: int
    illuminate_kills: int
    health: int
    max_health: int
    attacking_count: int
    has_event: bool


@dataclass(frozen=True)
class PlanetPriority:
    index: int
    name: str
    sector: str
    owner: str
    players_now: int
    deaths_now: int
    delta_deaths: int
    delta_friendlies: int
    delta_revives: int
    delta_enemy_kills: int
    delta_missions_total: int
    delta_missions_lost: int
    deaths_per_min: float
    deaths_per_100_players_min: float
    deaths_per_mission: float
    friendlies_per_min: float
    revives_per_min: float
    friendly_share_percent: float
    mission_fail_rate: float
    burn20: float
    component_volume: float
    component_relative: float
    component_trend: float
    component_pressure: float
    gate_players: float
    gate_missions: float
    kill_to_death_ratio: float
    deaths_per_1000_enemy_kills: float
    deaths_per_1000_mission_hours: float
    deaths_per_1000_play_hours: float
    mission_success_rate: float
    casualty_pressure_index: float
    medcrit_score: float
    medcrit_label: str
    medcrit_rank: int
    priority_score: float
    is_frontline: bool


@dataclass(frozen=True)
class WindowScaledModel:
    elapsed_seconds: float
    reference_seconds: float
    window_scale: float
    ema_alpha: float
    trend_min_samples: int
    trend_min_missions: int
    trend_min_deaths: int
    mission_gate_scale: float


def safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def relu(value: float) -> float:
    return max(0.0, value)


def smooth_growth(ratio: float, scale: float) -> float:
    return 1.0 - math.exp(-max(0.0, ratio) / max(0.1, scale))


def sigmoid(value: float) -> float:
    if value >= 40.0:
        return 1.0
    if value <= -40.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def classify_medcrit(score: float) -> Tuple[str, int]:
    if score >= 0.8:
        return "slaughter", 1
    if score >= 0.6:
        return "tough", 2
    if score >= 0.4:
        return "problematic", 3
    if score >= 0.2:
        return "control", 4
    return "resort", 5


def get_window_scaled_model(elapsed_seconds: float) -> WindowScaledModel:
    elapsed = max(1.0, float(elapsed_seconds))
    reference = max(5.0, MEDCRIT_REFERENCE_WINDOW_SECONDS)
    scale = elapsed / reference

    # Keep EMA smoothing stable in real time when sample window changes.
    alpha_ref = clamp(MEDCRIT_EMA_ALPHA, 0.01, 0.99)
    ema_alpha = clamp(1.0 - ((1.0 - alpha_ref) ** scale), 0.01, 0.99)

    # Count-based thresholds scale with the actual observation window.
    trend_min_samples = max(1, int(math.ceil(MEDCRIT_TREND_MIN_SAMPLES / max(0.1, scale))))
    trend_min_missions = max(1, int(round(MEDCRIT_TREND_MIN_MISSIONS * scale)))
    trend_min_deaths = max(1, int(round(MEDCRIT_TREND_MIN_DEATHS * scale)))
    mission_gate_scale = max(1.0, MEDCRIT_MISSION_GATE_SCALE * scale)

    return WindowScaledModel(
        elapsed_seconds=elapsed,
        reference_seconds=reference,
        window_scale=scale,
        ema_alpha=ema_alpha,
        trend_min_samples=trend_min_samples,
        trend_min_missions=trend_min_missions,
        trend_min_deaths=trend_min_deaths,
        mission_gate_scale=mission_gate_scale,
    )


def _resolve_medcrit_state_path(state_file: str | None = None) -> Path:
    target = state_file if state_file else MEDCRIT_STATE_FILE
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def load_medcrit_state(state_file: str | None = None) -> Dict[str, Dict[str, float]]:
    path = _resolve_medcrit_state_path(state_file)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}

    clean: Dict[str, Dict[str, float]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        inferred_count = 1 if ("ema_dpm" in value or "ema_fail" in value or "last_dpm" in value) else 0
        clean[key] = {
            "ema_dpm": float(value.get("ema_dpm", 0.0) or 0.0),
            "ema_fail": float(value.get("ema_fail", 0.0) or 0.0),
            "last_dpm": float(value.get("last_dpm", 0.0) or 0.0),
            "last_fail": float(value.get("last_fail", 0.0) or 0.0),
            "sample_count": int(value.get("sample_count", inferred_count) or 0),
        }
    return clean


def save_medcrit_state(state: Dict[str, Dict[str, float]], state_file: str | None = None) -> None:
    path = _resolve_medcrit_state_path(state_file)
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"Не удалось сохранить состояние MedCrit: {exc}", file=sys.stderr)


def is_frontline_planet(p: PlanetSnapshot) -> bool:
    owner_front = p.current_owner != "Humans"
    under_attack = p.attacking_count > 0
    active_event = p.has_event
    contested_health = p.max_health > 0 and p.health < p.max_health
    return owner_front or under_attack or active_event or contested_health


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, http.client.RemoteDisconnected):
        return True

    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 425, 429, 500, 502, 503, 504)

    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout, ssl.SSLError)):
            return True
        text = str(reason).lower()
        retryable_markers = (
            "timed out",
            "handshake",
            "temporary failure",
            "name or service not known",
            "connection reset",
            "connection aborted",
        )
        return any(marker in text for marker in retryable_markers)

    return False


def _compute_retry_delay(attempt: int, exc: BaseException) -> float:
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        retry_after = exc.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                pass
    return RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _resolve_dashboard_path() -> Path:
    path = Path(DASHBOARD_OUTPUT).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _wrap_for_plotly(text: str, width: int = 120) -> str:
    wrapped = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        wrapped.extend(textwrap.wrap(line, width=width) or [""])
    return "<br>".join(wrapped)


def build_heuristic_summary(priorities: List[PlanetPriority], elapsed_seconds: float) -> str:
    if not priorities:
        return (
            "Сводка: после фильтров фронта и онлайна нет планет для рекомендации. "
            "Увеличьте SAMPLE_SECONDS или ослабьте фильтры."
        )

    top = priorities[:3]
    total_dpm = sum(p.deaths_per_min for p in priorities)
    avg_fail = safe_div(sum(p.mission_fail_rate for p in priorities), len(priorities))
    avg_burn = safe_div(sum(p.burn20 for p in priorities), len(priorities))

    top_text = ", ".join(
        f"{p.name} ({p.medcrit_label} {p.medcrit_rank}, medcrit={p.medcrit_score:.2f}, burn20={p.burn20:.2f}, fail={p.mission_fail_rate:.2f})"
        for p in top
    )
    return (
        f"Сводка за окно {elapsed_seconds:.1f} сек: суммарный deaths/min={total_dpm:.1f}, "
        f"средний fail-rate={avg_fail:.2f}, средний burn20={avg_burn:.2f}. "
        f"Рекомендуемые приоритеты медподдержки: {top_text}."
    )


def generate_summary(priorities: List[PlanetPriority], elapsed_seconds: float) -> str:
    return build_heuristic_summary(priorities, elapsed_seconds)


def fetch_planet_snapshots() -> Dict[int, PlanetSnapshot]:
    req = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/json",
            "X-Super-Client": CLIENT_HEADER,
            "X-Super-Contact": CONTACT_HEADER,
        },
        method="GET",
    )

    context = ssl.create_default_context()
    last_exc: BaseException | None = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=context) as resp:
                payload = json.load(resp)

            if not isinstance(payload, list):
                raise ValueError("API вернул неожиданный формат: ожидается список планет")

            snapshots: Dict[int, PlanetSnapshot] = {}
            for p in payload:
                stats = p.get("statistics") or {}
                snapshots[p["index"]] = PlanetSnapshot(
                    index=int(p["index"]),
                    name=str(p.get("name", f"PLANET-{p.get('index', '?')}")),
                    sector=str(p.get("sector", "Unknown")),
                    current_owner=str(p.get("currentOwner", "Unknown")),
                    deaths=int(stats.get("deaths", 0) or 0),
                    friendlies=int(stats.get("friendlies", 0) or 0),
                    revives=int(stats.get("revives", 0) or 0),
                    player_count=int(stats.get("playerCount", 0) or 0),
                    mission_success_rate=float(stats.get("missionSuccessRate", 0.0) or 0.0),
                    missions_won=int(stats.get("missionsWon", 0) or 0),
                    missions_lost=int(stats.get("missionsLost", 0) or 0),
                    mission_time=int(stats.get("missionTime", 0) or 0),
                    time_played=int(stats.get("timePlayed", 0) or 0),
                    terminid_kills=int(stats.get("terminidKills", 0) or 0),
                    automaton_kills=int(stats.get("automatonKills", 0) or 0),
                    illuminate_kills=int(stats.get("illuminateKills", 0) or 0),
                    health=int(p.get("health", 0) or 0),
                    max_health=int(p.get("maxHealth", 0) or 0),
                    attacking_count=len(p.get("attacking") or []),
                    has_event=(p.get("event") is not None),
                )
            return snapshots
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            ssl.SSLError,
            http.client.RemoteDisconnected,
        ) as exc:
            last_exc = exc
            if attempt >= REQUEST_RETRIES or not _is_retryable(exc):
                raise

            delay = _compute_retry_delay(attempt, exc)
            print(
                f"Сетевой сбой (попытка {attempt}/{REQUEST_RETRIES}): {exc}. "
                f"Повтор через {delay:.1f} сек...",
                file=sys.stderr,
            )
            time.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Неизвестная ошибка сети")


def build_priorities(
    old: Dict[int, PlanetSnapshot],
    new: Dict[int, PlanetSnapshot],
    elapsed_seconds: float,
    state_file: str | None = None,
) -> List[PlanetPriority]:
    if elapsed_seconds <= 0:
        elapsed_seconds = 1.0

    scaled = get_window_scaled_model(elapsed_seconds)
    previous_state = load_medcrit_state(state_file=state_file)
    next_state: Dict[str, Dict[str, float]] = dict(previous_state)

    volume_mix = clamp(MEDCRIT_VOLUME_MIX, 0.0, 1.0)
    stress_burn = max(0.0, MEDCRIT_STRESS_BURN_COEF)
    stress_fail = max(0.0, MEDCRIT_STRESS_FAIL_COEF)
    stress_rel = max(0.0, MEDCRIT_STRESS_RELATIVE_COEF)
    stress_trend = max(0.0, MEDCRIT_STRESS_TREND_COEF)
    stress_scale = max(0.05, MEDCRIT_STRESS_SCALE)
    medcrit_gamma = max(0.5, MEDCRIT_NONLINEAR_GAMMA)

    rows: List[PlanetPriority] = []
    for idx, curr in new.items():
        prev = old.get(idx)
        if prev is None:
            continue

        frontline = is_frontline_planet(curr)
        if FRONTLINE_ONLY and not frontline:
            continue
        if curr.player_count < MIN_ACTIVE_PLAYERS:
            continue

        delta_deaths = max(0, curr.deaths - prev.deaths)
        delta_friendlies = max(0, curr.friendlies - prev.friendlies)
        delta_revives = max(0, curr.revives - prev.revives)

        delta_missions_won = max(0, curr.missions_won - prev.missions_won)
        delta_missions_lost = max(0, curr.missions_lost - prev.missions_lost)
        mission_delta = delta_missions_won + delta_missions_lost

        prev_enemy_kills = prev.terminid_kills + prev.automaton_kills + prev.illuminate_kills
        curr_enemy_kills = curr.terminid_kills + curr.automaton_kills + curr.illuminate_kills
        delta_enemy_kills = max(0, curr_enemy_kills - prev_enemy_kills)

        minutes = elapsed_seconds / 60.0
        deaths_per_min = delta_deaths / minutes
        friendlies_per_min = delta_friendlies / minutes
        revives_per_min = delta_revives / minutes

        avg_players_raw = max(1.0, (prev.player_count + curr.player_count) / 2.0)
        effective_players = max(NORMALIZATION_PLAYER_FLOOR, avg_players_raw)
        deaths_per_100_players_min = deaths_per_min / effective_players * 100.0

        deaths_per_mission = safe_div(delta_deaths, mission_delta)
        mission_fail_rate = safe_div(delta_missions_lost, mission_delta)

        burn20 = clamp(deaths_per_mission / 20.0, 0.0, 1.0)
        component_volume = 1.0 - math.exp(-deaths_per_min / max(1.0, MEDCRIT_DPM_SCALE))
        component_relative = 1.0 - math.exp(-deaths_per_100_players_min / max(1.0, MEDCRIT_NDPM_SCALE))

        state_key = str(curr.index)
        state_item = previous_state.get(state_key, {})
        sample_count_prev = int(state_item.get("sample_count", 0) or 0)
        has_history = sample_count_prev > 0
        ema_dpm_prev = float(state_item.get("ema_dpm", deaths_per_min))
        ema_fail_prev = float(state_item.get("ema_fail", mission_fail_rate))

        trend_ready = (
            has_history
            and sample_count_prev >= scaled.trend_min_samples
            and mission_delta >= scaled.trend_min_missions
            and delta_deaths >= scaled.trend_min_deaths
        )

        if trend_ready:
            trend_dpm_ratio = relu(
                (deaths_per_min - ema_dpm_prev)
                / max(ema_dpm_prev, MEDCRIT_TREND_DPM_BASELINE)
            )
            trend_fail_ratio = relu(
                (mission_fail_rate - ema_fail_prev)
                / max(ema_fail_prev, MEDCRIT_FAIL_BASELINE)
            )
            trend_dpm = smooth_growth(trend_dpm_ratio, MEDCRIT_TREND_GROWTH_SCALE)
            trend_fail = smooth_growth(trend_fail_ratio, MEDCRIT_TREND_GROWTH_SCALE)
            component_trend = clamp(0.6 * trend_dpm + 0.4 * trend_fail, 0.0, 1.0)
        else:
            component_trend = 0.0

        ema_alpha = scaled.ema_alpha
        next_state[state_key] = {
            "ema_dpm": ema_alpha * deaths_per_min + (1.0 - ema_alpha) * ema_dpm_prev,
            "ema_fail": ema_alpha * mission_fail_rate + (1.0 - ema_alpha) * ema_fail_prev,
            "last_dpm": deaths_per_min,
            "last_fail": mission_fail_rate,
            "sample_count": sample_count_prev + 1,
        }

        stress_input = max(
            0.0,
            stress_burn * burn20
            + stress_fail * mission_fail_rate
            + stress_rel * component_relative
            + stress_trend * component_trend,
        )
        stress = 1.0 - math.exp(-stress_input / stress_scale)
        pressure = clamp(
            volume_mix * component_volume
            + (1.0 - volume_mix) * stress,
            0.0,
            1.0,
        )

        gate_players = sigmoid(
            (curr.player_count - MEDCRIT_PLAYER_GATE_CENTER)
            / max(1.0, MEDCRIT_PLAYER_GATE_SCALE)
        )
        activity_signal = mission_delta + min(20.0, delta_deaths / 12.0)
        gate_missions = 1.0 - math.exp(-activity_signal / scaled.mission_gate_scale)
        gate_frontline = 1.0 if frontline else 0.0

        medcrit_raw = clamp(gate_frontline * gate_players * gate_missions * pressure, 0.0, 1.0)
        medcrit = clamp(medcrit_raw**medcrit_gamma, 0.0, 1.0)
        medcrit_score = medcrit
        medcrit_label, medcrit_rank = classify_medcrit(medcrit_score)

        friendly_share_percent = safe_div(delta_friendlies * 100.0, delta_deaths)
        kill_to_death_ratio = safe_div(delta_enemy_kills, delta_deaths)
        deaths_per_1000_enemy_kills = safe_div(delta_deaths * 1000.0, delta_enemy_kills + 1.0)

        deaths_per_1000_mission_hours = safe_div(curr.deaths * 1000.0, curr.mission_time / 3600.0)
        deaths_per_1000_play_hours = safe_div(curr.deaths * 1000.0, curr.time_played / 3600.0)

        casualty_pressure_index = deaths_per_100_players_min * (1.0 + mission_fail_rate)

        rows.append(
            PlanetPriority(
                index=curr.index,
                name=curr.name,
                sector=curr.sector,
                owner=curr.current_owner,
                players_now=curr.player_count,
                deaths_now=curr.deaths,
                delta_deaths=delta_deaths,
                delta_friendlies=delta_friendlies,
                delta_revives=delta_revives,
                delta_enemy_kills=delta_enemy_kills,
                delta_missions_total=mission_delta,
                delta_missions_lost=delta_missions_lost,
                deaths_per_min=deaths_per_min,
                deaths_per_100_players_min=deaths_per_100_players_min,
                deaths_per_mission=deaths_per_mission,
                friendlies_per_min=friendlies_per_min,
                revives_per_min=revives_per_min,
                friendly_share_percent=friendly_share_percent,
                mission_fail_rate=mission_fail_rate,
                burn20=burn20,
                component_volume=component_volume,
                component_relative=component_relative,
                component_trend=component_trend,
                component_pressure=pressure,
                gate_players=gate_players,
                gate_missions=gate_missions,
                kill_to_death_ratio=kill_to_death_ratio,
                deaths_per_1000_enemy_kills=deaths_per_1000_enemy_kills,
                deaths_per_1000_mission_hours=deaths_per_1000_mission_hours,
                deaths_per_1000_play_hours=deaths_per_1000_play_hours,
                mission_success_rate=curr.mission_success_rate,
                casualty_pressure_index=casualty_pressure_index,
                medcrit_score=medcrit_score,
                medcrit_label=medcrit_label,
                medcrit_rank=medcrit_rank,
                priority_score=medcrit_score,
                is_frontline=frontline,
            )
        )

    save_medcrit_state(next_state, state_file=state_file)
    rows.sort(key=lambda r: (r.medcrit_score, r.component_pressure, r.deaths_per_min), reverse=True)
    return rows


def print_overview(title: str, snapshots: Dict[int, PlanetSnapshot]) -> None:
    total_players = sum(p.player_count for p in snapshots.values())
    total_deaths = sum(p.deaths for p in snapshots.values())
    total_friendlies = sum(p.friendlies for p in snapshots.values())
    total_revives = sum(p.revives for p in snapshots.values())
    frontline_planets = sum(1 for p in snapshots.values() if is_frontline_planet(p))
    hot_planets = sorted(
        snapshots.values(),
        key=lambda p: (p.player_count, p.deaths),
        reverse=True,
    )[:5]

    print(title)
    print(f"Всего планет в срезе: {len(snapshots)}")
    print(f"Игроков сейчас (суммарно): {total_players:,}")
    print(f"Смертей (суммарно): {total_deaths:,}")
    print(f"Friendly-fire (суммарно): {total_friendlies:,}")
    print(f"Revives (суммарно): {total_revives:,}")
    print(f"Планет на фронте (по эвристике): {frontline_planets}")
    print("Топ-5 по онлайну:")
    for p in hot_planets:
        print(
            f"  - {p.name} [{p.sector}] | players={p.player_count:,} "
            f"| deaths={p.deaths:,} | friendlies={p.friendlies:,} | revives={p.revives:,} "
            f"| success={p.mission_success_rate:.1f}%"
        )
    print()


def print_priority_report(priorities: List[PlanetPriority], elapsed_seconds: float) -> None:
    scaled = get_window_scaled_model(elapsed_seconds)
    print(f"Смертность за окно наблюдения: {elapsed_seconds:.1f} сек")
    print("Рейтинг 'куда отправить саппорт/медиков' (по MedCrit score 0..1):")
    print(
        f"Фильтры: FRONTLINE_ONLY={int(FRONTLINE_ONLY)}, "
        f"MIN_ACTIVE_PLAYERS={MIN_ACTIVE_PLAYERS}, "
        f"NORMALIZATION_PLAYER_FLOOR={int(NORMALIZATION_PLAYER_FLOOR)}"
    )
    print(
        f"Window scaling: ref={scaled.reference_seconds:.0f}s, factor={scaled.window_scale:.2f}, "
        f"ema_alpha={scaled.ema_alpha:.3f}, trend gates(samples/missions/deaths)="
        f"{scaled.trend_min_samples}/{scaled.trend_min_missions}/{scaled.trend_min_deaths}, "
        f"mission_gate_scale={scaled.mission_gate_scale:.2f}"
    )

    total_deaths_pm = sum(p.deaths_per_min for p in priorities)
    avg_fail = safe_div(sum(p.mission_fail_rate for p in priorities), len(priorities))
    avg_burn = safe_div(sum(p.burn20 for p in priorities), len(priorities))
    avg_trend = safe_div(sum(p.component_trend for p in priorities), len(priorities))
    avg_medcrit = safe_div(sum(p.medcrit_score for p in priorities), len(priorities))
    print(
        f"Итоги по отфильтрованным планетам: deaths/min={total_deaths_pm:,.2f}, "
        f"avg fail-rate={avg_fail:,.3f}, avg burn20={avg_burn:,.3f}, "
        f"avg trend={avg_trend:,.3f}, avg medcrit={avg_medcrit:,.3f}"
    )

    shown = 0
    for p in priorities:
        if p.players_now <= 0:
            continue
        shown += 1
        print(
            f"{shown:>2}. {p.name} [{p.sector}] owner={p.owner} | players={p.players_now:,} | "
            f"dΔ={p.delta_deaths:,} | missionsΔ={p.delta_missions_total:,} (lost={p.delta_missions_lost:,}) | "
            f"d/min={p.deaths_per_min:,.2f} | d/100p/min={p.deaths_per_100_players_min:,.3f} | "
            f"d/mission={p.deaths_per_mission:,.3f} | fail={p.mission_fail_rate:,.3f} | burn20={p.burn20:,.3f} | "
            f"trend={p.component_trend:,.3f} | medcrit={p.medcrit_score:,.3f} -> {p.medcrit_label} ({p.medcrit_rank})"
        )
        if shown >= TOP_N:
            break

    if shown == 0:
        print("Нет планет, прошедших фильтры фронта/онлайна для ранжирования.")

    if priorities and all(p.delta_deaths == 0 for p in priorities):
        print("\nПодсказка: за текущее окно deaths не изменились. Увеличьте SAMPLE_SECONDS (например, 60-120).")


def _top_by_metric(items: List[PlanetPriority], metric: str, limit: int) -> List[PlanetPriority]:
    return sorted(items, key=lambda p: getattr(p, metric), reverse=True)[:limit]


def _hierarchy_nodes(
    items: List[PlanetPriority],
    value_metric: str,
    color_metric: str,
) -> Tuple[List[str], List[str], List[str], List[float], List[float]]:
    owner_values: Dict[str, float] = {}
    owner_color_weighted: Dict[str, float] = {}
    sector_values: Dict[Tuple[str, str], float] = {}
    sector_color_weighted: Dict[Tuple[str, str], float] = {}

    for p in items:
        owner = p.owner or "Unknown"
        sector = p.sector or "Unknown"
        value = max(0.001, float(getattr(p, value_metric)))
        color_value = float(getattr(p, color_metric))

        owner_values[owner] = owner_values.get(owner, 0.0) + value
        owner_color_weighted[owner] = owner_color_weighted.get(owner, 0.0) + color_value * value

        key = (owner, sector)
        sector_values[key] = sector_values.get(key, 0.0) + value
        sector_color_weighted[key] = sector_color_weighted.get(key, 0.0) + color_value * value

    ids: List[str] = []
    labels: List[str] = []
    parents: List[str] = []
    values: List[float] = []
    colors: List[float] = []

    for owner, owner_val in sorted(owner_values.items(), key=lambda kv: kv[1], reverse=True):
        owner_id = f"owner::{owner}"
        owner_color = safe_div(owner_color_weighted[owner], owner_val)
        ids.append(owner_id)
        labels.append(owner)
        parents.append("")
        values.append(owner_val)
        colors.append(owner_color)

    for (owner, sector), sector_val in sorted(sector_values.items(), key=lambda kv: kv[1], reverse=True):
        sector_id = f"sector::{owner}::{sector}"
        owner_id = f"owner::{owner}"
        sector_color = safe_div(sector_color_weighted[(owner, sector)], sector_val)
        ids.append(sector_id)
        labels.append(sector)
        parents.append(owner_id)
        values.append(sector_val)
        colors.append(sector_color)

    for p in sorted(items, key=lambda x: getattr(x, value_metric), reverse=True):
        owner = p.owner or "Unknown"
        sector = p.sector or "Unknown"
        planet_id = f"planet::{p.index}"
        sector_id = f"sector::{owner}::{sector}"
        value = max(0.001, float(getattr(p, value_metric)))
        color_value = float(getattr(p, color_metric))
        ids.append(planet_id)
        labels.append(p.name)
        parents.append(sector_id)
        values.append(value)
        colors.append(color_value)

    return ids, labels, parents, values, colors


def build_plotly_dashboard(
    priorities: List[PlanetPriority],
    requested_at: datetime,
    elapsed_seconds: float,
    summary_text: str,
) -> Path | None:
    if not PLOTLY_AVAILABLE:
        print("Plotly не установлен: дашборд не создан. Установите: pip install plotly", file=sys.stderr)
        return None

    active = [p for p in priorities if p.players_now > 0]
    if not active:
        return None

    chart_limit = max(8, min(DASHBOARD_TOP, len(active)))
    scaled = get_window_scaled_model(elapsed_seconds)
    by_medcrit = _top_by_metric(active, "medcrit_score", chart_limit)
    by_trend = _top_by_metric(active, "component_trend", chart_limit)

    class_color = {
        "resort": "#2A9D8F",
        "control": "#8AB17D",
        "problematic": "#E9C46A",
        "tough": "#F4A261",
        "slaughter": "#E63946",
    }

    component_rows = by_medcrit[: min(10, len(by_medcrit))]
    component_labels = [
        "burn20",
        "fail",
        "vol",
        "rel",
        "trend",
        "pressure",
        "g_players",
        "g_missions",
    ]
    component_z = [
        [
            p.burn20,
            p.mission_fail_rate,
            p.component_volume,
            p.component_relative,
            p.component_trend,
            p.component_pressure,
            p.gate_players,
            p.gate_missions,
        ]
        for p in component_rows
    ]
    component_text = [[f"{v:.3f}" for v in row] for row in component_z]

    class_order = [("resort", 5), ("control", 4), ("problematic", 3), ("tough", 2), ("slaughter", 1)]
    class_names = [f"{name} ({rank})" for name, rank in class_order]
    class_counts = [sum(1 for p in active if p.medcrit_label == name) for name, _ in class_order]
    class_colors = [class_color[name] for name, _ in class_order]

    fig = make_subplots(
        rows=2,
        cols=3,
        subplot_titles=(
            "Топ по MedCrit score (0..1)",
            "Компоненты MedCrit по топ-планетам",
            "Топ по тренду ухудшения (trend component)",
            "Баланс абсолютной/относительной смертности",
            "Burn20 vs fail-rate",
            "Классификация планет (resort→slaughter)",
        ),
        vertical_spacing=0.16,
        horizontal_spacing=0.08,
    )

    fig.add_trace(
        go.Bar(
            x=[p.medcrit_score for p in by_medcrit],
            y=[f"{p.name} [{p.sector}] | {p.medcrit_label} ({p.medcrit_rank})" for p in by_medcrit],
            orientation="h",
            marker=dict(color=[class_color.get(p.medcrit_label, COLOR_ACCENT_HEX) for p in by_medcrit]),
            hovertemplate="%{y}<br>medcrit=%{x:.3f}<extra></extra>",
            name="medcrit score",
        ),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Heatmap(
            x=component_labels,
            y=[f"{p.name} [{p.sector}]" for p in component_rows],
            z=component_z,
            text=component_text,
            colorscale=COMPARE_COLORSCALE,
            zmin=0.0,
            zmax=1.0,
            hovertemplate="%{y}<br>%{x}: %{text}<extra></extra>",
            showscale=False,
        ),
        row=1,
        col=2,
    )

    fig.add_trace(
        go.Bar(
            x=[p.component_trend for p in by_trend],
            y=[f"{p.name} [{p.sector}]" for p in by_trend],
            orientation="h",
            marker=dict(color=COLOR_PRIMARY),
            hovertemplate="%{y}<br>trend=%{x:.3f}<extra></extra>",
            name="trend",
        ),
        row=1,
        col=3,
    )

    fig.add_trace(
        go.Scatter(
            x=[p.deaths_per_min for p in active],
            y=[p.deaths_per_100_players_min for p in active],
            mode="markers",
            marker=dict(
                size=[max(8, min(26, 7 + math.sqrt(max(1, p.players_now) / 25.0))) for p in active],
                color=[p.component_trend for p in active],
                colorscale=COMPARE_COLORSCALE,
                cmin=0.0,
                cmax=1.0,
                opacity=0.85,
                line=dict(color="rgba(255,255,255,0.45)", width=1),
                showscale=True,
                colorbar=dict(title="trend", x=1.01, y=0.25, len=0.35),
            ),
            text=[f"{p.name} | {p.medcrit_label} ({p.medcrit_rank})" for p in active],
            customdata=[[p.medcrit_score, p.burn20, p.mission_fail_rate] for p in active],
            hovertemplate=(
                "%{text}<br>d/min=%{x:.2f}<br>d/100p/min=%{y:.3f}<br>"
                "medcrit=%{customdata[0]:.3f}<br>burn20=%{customdata[1]:.3f}<br>fail=%{customdata[2]:.3f}<extra></extra>"
            ),
            name="dpm vs ndpm",
        ),
        row=2,
        col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=[p.burn20 for p in active],
            y=[p.mission_fail_rate for p in active],
            mode="markers",
            marker=dict(
                size=[max(8, min(24, 8 + 10 * p.gate_missions)) for p in active],
                color=[p.medcrit_score for p in active],
                colorscale=COMPARE_COLORSCALE,
                cmin=0.0,
                cmax=1.0,
                opacity=0.88,
                line=dict(color="rgba(255,255,255,0.45)", width=1),
            ),
            text=[f"{p.name} | {p.medcrit_label} ({p.medcrit_rank})" for p in active],
            customdata=[[p.component_trend, p.deaths_per_mission, p.delta_missions_total] for p in active],
            hovertemplate=(
                "%{text}<br>burn20=%{x:.3f}<br>fail-rate=%{y:.3f}<br>"
                "trend=%{customdata[0]:.3f}<br>d/mission=%{customdata[1]:.3f}<br>missionsΔ=%{customdata[2]}<extra></extra>"
            ),
            name="burn vs fail",
        ),
        row=2,
        col=2,
    )

    fig.add_trace(
        go.Bar(
            x=class_names,
            y=class_counts,
            marker=dict(color=class_colors),
            hovertemplate="%{x}<br>planets=%{y}<extra></extra>",
            name="class distribution",
        ),
        row=2,
        col=3,
    )

    requested_at_str = requested_at.strftime("%Y-%m-%d %H:%M:%S %Z")
    fig.update_layout(
        height=1300,
        width=2200,
        template="plotly_dark",
        showlegend=False,
        paper_bgcolor=COLOR_BG,
        plot_bgcolor=COLOR_PANEL,
        title={
            "text": (
                "MedicDivers: Helldivers 2 MedCrit Dashboard"
                f"<br><sup>Дата обращения: {requested_at_str} | Окно: {elapsed_seconds:.1f} сек | Источник: {API_URL} | "
                f"ref={scaled.reference_seconds:.0f}s, k={scaled.window_scale:.2f} | "
                f"model: burn20/fail/trend + anti-false-leader gates</sup>"
            ),
            "x": 0.5,
            "xanchor": "center",
        },
        font=dict(color=COLOR_SECONDARY),
        margin=dict(l=80, r=150, t=130, b=200),
    )

    fig.add_annotation(
        xref="paper",
        yref="paper",
        x=0,
        y=-0.18,
        xanchor="left",
        yanchor="bottom",
        align="left",
        showarrow=False,
        font={"size": 12, "color": "rgba(210,225,255,0.96)"},
        text=(
            "<b>Эвристическая сводка</b><br>"
            f"{_wrap_for_plotly(_clip_text(summary_text, SUMMARY_MAX_CHARS), width=150)}"
        ),
    )

    for row in range(1, 3):
        for col in range(1, 4):
            fig.update_xaxes(gridcolor=COLOR_GRID, zerolinecolor=COLOR_ZERO, row=row, col=col)
            fig.update_yaxes(gridcolor=COLOR_GRID, zerolinecolor=COLOR_ZERO, row=row, col=col)

    fig.update_yaxes(autorange="reversed", row=1, col=1)
    fig.update_yaxes(autorange="reversed", row=1, col=3)

    fig.update_xaxes(range=[0, 1], title_text="medcrit score", row=1, col=1)
    fig.update_xaxes(title_text="deaths/min", row=2, col=1)
    fig.update_yaxes(title_text="deaths/100 players/min", row=2, col=1)
    fig.update_xaxes(range=[0, 1], title_text="burn20", row=2, col=2)
    fig.update_yaxes(range=[0, 1], title_text="fail-rate", row=2, col=2)
    fig.update_yaxes(title_text="planets", row=2, col=3)

    out_path = _resolve_dashboard_path()
    fig.write_html(str(out_path), include_plotlyjs="cdn", full_html=True)
    return out_path


def maybe_open_dashboard(path: Path) -> None:
    if not AUTO_OPEN_DASHBOARD:
        return
    try:
        opened = webbrowser.open_new_tab(path.as_uri())
        if opened:
            print("Открыл дашборд в браузере.")
        else:
            print("Не удалось авто-открыть браузер. Откройте файл вручную:", path)
    except Exception as exc:
        print(f"Не удалось авто-открыть браузер: {exc}", file=sys.stderr)


def main() -> int:
    requested_at = datetime.now().astimezone()

    try:
        print("Запрос 1/2: получаю текущий срез планет...")
        first = fetch_planet_snapshots()
        print_overview("Текущая статистика:", first)

        t0 = time.time()
        print(f"Жду {SAMPLE_SECONDS} сек, чтобы посчитать динамику смертности...")
        time.sleep(SAMPLE_SECONDS)

        print("Запрос 2/2: обновляю срез планет...")
        second = fetch_planet_snapshots()
        elapsed = max(1.0, time.time() - t0)

        priorities = build_priorities(first, second, elapsed)
        print_priority_report(priorities, elapsed)
        summary_text = generate_summary(priorities, elapsed)
        print("\nИтоговая сводка (heuristic):")
        print(summary_text)

        dashboard_path = build_plotly_dashboard(
            priorities,
            requested_at,
            elapsed,
            summary_text=summary_text,
        )
        if dashboard_path is not None:
            print(f"\nPlotly-дашборд сохранён: {dashboard_path}")
            maybe_open_dashboard(dashboard_path)

        return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP ошибка API: {e.code} {e.reason}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, http.client.RemoteDisconnected) as e:
        print(
            f"Сетевая ошибка API: {e}\n"
            f"Попробуйте: REQUEST_TIMEOUT=90 REQUEST_RETRIES=6 .venv/bin/python main.py",
            file=sys.stderr,
        )
        return 3
    except KeyboardInterrupt:
        print("Остановлено пользователем.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
