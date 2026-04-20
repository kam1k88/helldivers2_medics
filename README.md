# medicdivers_service

Скрипт при запуске:
1. Запрашивает текущую статистику планет Helldivers 2.
2. Ждёт окно наблюдения (`SAMPLE_SECONDS`).
3. Запрашивает срез ещё раз.
4. Считает динамику смертности и выдаёт приоритетные планеты для отправки саппортов/медиков.
5. Создаёт интерактивный Plotly-дашборд `dashboard.html` с MedCrit-метриками и классификацией планет.
6. Автоматически открывает дашборд в браузере (можно отключить).

## Установка

```bash
cd /Users/franceballin/PycharmProjects/medicdivers_service
.venv/bin/pip install -r requirements.txt
```

## Запуск

```bash
cd /Users/franceballin/PycharmProjects/medicdivers_service
.venv/bin/python main.py
```

## REST API Сервис (3 потока + анимации)

Сервис поднимает 3 фоновых воркера:
- `300` секунд (5 минут)
- `900` секунд (15 минут)
- `1800` секунд (30 минут)

Каждый воркер:
- отдельно опрашивает официальный API,
- считает MedCrit на своём окне,
- хранит результаты в SQLite,
- использует отдельный state-файл EMA (`.medcrit_state_300s.json`, `.medcrit_state_900s.json`, `.medcrit_state_1800s.json`),
- обновляет HTML-анимацию дашборда из 6 графиков в `animations/medcrit_<window>s.html`.
- набор графиков и компоненты heatmap в анимации соответствуют обычному дашборду `main.py`.

Запуск:

```bash
cd /Users/franceballin/PycharmProjects/medicdivers_service
.venv/bin/uvicorn rest_api:app --host 0.0.0.0 --port 8000
```

Полезные endpoint'ы:
- `GET /health`
- `GET /api/v1/windows`
- `GET /api/v1/latest?window=900`
- `GET /api/v1/top?window=900&n=10`
- `GET /api/v1/history?window=900&planet_name=HYDROBIUS`
- `GET /api/v1/animation?window=900`
- `POST /api/v1/rebuild-animation?window=900`

Анимации доступны как статические HTML:
- `http://localhost:8000/animations/medcrit_300s.html`
- `http://localhost:8000/animations/medcrit_900s.html`
- `http://localhost:8000/animations/medcrit_1800s.html`

## Деплой через GitHub Actions (бесплатно)

В проект добавлен workflow:
- `.github/workflows/publish_dashboard.yml`
- `generate_pages.py`

Он:
- запускается вручную (`workflow_dispatch`) и по расписанию (`каждые 5 минут`),
- работает как 3 виртуальных "воркера" по окнам `5/15/30` минут (`300/900/1800`),
- генерирует для каждого окна анимированный дашборд из 6 графиков только за текущий день,
- при смене дня архивирует вчерашние анимации и историю в `.pages_data/archive/<YYYY-MM-DD>/`,
- публикует итоговый сайт в GitHub Pages из директории `public/`.

Шаги настройки:
1. Запушь проект в GitHub-репозиторий.
2. В GitHub открой `Settings -> Pages`, выбери `Source: GitHub Actions`.
3. В `Settings -> Actions -> General -> Workflow permissions` включи `Read and write permissions` (workflow коммитит `.pages_data` обратно в репозиторий для сохранения состояния между запусками).
4. Открой `Actions -> Publish Dashboard` и нажми `Run workflow` для первого прогона.

После первого успешного деплоя дашборд будет доступен по URL:
- `https://<your-user>.github.io/<your-repo>/`

Примечания:
- GitHub Actions не является always-on сервером, это периодическая публикация статического HTML.
- Минимальный интервал schedule в GitHub Actions — 5 минут; в этом проекте выставлено 5 минут.

## Что показывает дашборд

- 6 графиков с компонентами MedCrit:
  - топ планет по `medcrit score` (0..1)
  - heatmap компонентов (`burn20`, `fail`, `vol`, `rel`, `trend`, `pressure`, `g_players`, `g_missions`)
  - топ планет по `trend` (ухудшение смертности/провалов)
  - scatter `deaths/min` vs `deaths/100 players/min` с цветом по тренду ухудшения
  - scatter `burn20` vs `fail-rate`
  - распределение классов планет: `resort (5)`, `control (4)`, `problematic (3)`, `tough (2)`, `slaughter (1)`
- В заголовке выводится дата/время обращения к API.
- Тёмная тема на палитре `rgb(100,32,32)`, `rgb(255,255,255)`, `rgb(17,100,55)`.
- Для сравнительных графиков используется шкала: маленькие значения = синий, большие = красный.

## Как считается MedCrit Score (0..1)

- Базовые метрики окна наблюдения:
  - `dpm = Δdeaths / Δt`
  - `ndpm = 100 * Δdeaths / (Δt * max(players, NORMALIZATION_PLAYER_FLOOR))`
  - `deaths_per_mission = Δdeaths / max(1, ΔmissionsWon + ΔmissionsLost)`
  - `fail-rate = ΔmissionsLost / max(1, ΔmissionsWon + ΔmissionsLost)`
- Игровой компонент выгорания жизни на миссию:
  - `burn20 = clip(deaths_per_mission / 20, 0, 1)`
- Нормализованные компоненты:
  - `vol = 1 - exp(-dpm / MEDCRIT_DPM_SCALE)`
  - `rel = 1 - exp(-ndpm / MEDCRIT_NDPM_SCALE)`
- Тренд ухудшения через EMA между запусками (состояние в `MEDCRIT_STATE_FILE`):
  - положительные производные `dpm` и `fail-rate` относительно EMA
  - включается только после прогрева истории и при достаточной активности окна (миссии/смерти), чтобы избежать ложных `trend=1.00`
  - пороги тренда и EMA автоматически масштабируются под фактическое окно наблюдения (`elapsed_seconds`)
    - если окно больше, требуем больше миссий/смертей за окно и меньше исторических сэмплов
    - если окно меньше, наоборот: больше исторических сэмплов и мягче пороги по событиям
- Нелинейное pressure-ядро:
  - `stress_input = k_burn*burn20 + k_fail*fail + k_rel*rel + k_trend*trend`
  - `stress = 1 - exp(-stress_input / MEDCRIT_STRESS_SCALE)`
  - `pressure = MEDCRIT_VOLUME_MIX*vol + (1-MEDCRIT_VOLUME_MIX)*stress`
  - это снижает влияние «голого объёма смертей» на планетах без реального миссионного коллапса.
- Anti-false-leader гейты:
  - фронт (`frontline`), онлайн (`g_players`), активность миссий (`g_missions`)
- Финал:
  - `raw = clip(frontline * g_players * g_missions * pressure, 0, 1)`
  - `medcrit = clip(raw^MEDCRIT_NONLINEAR_GAMMA, 0, 1)` (нелинейная калибровка, по умолчанию `1.02`)
  - классы: `resort (5)`, `control (4)`, `problematic (3)`, `tough (2)`, `slaughter (1)`.

## Какие поля API используются (mortality-related)

- `deaths`
- `friendlies`
- `revives`
- `missionSuccessRate`
- `missionsWon`
- `missionsLost`
- `playerCount`
- `missionTime`
- `timePlayed`
- `terminidKills`
- `automatonKills`
- `illuminateKills`

## Настройки (опционально)

- `SAMPLE_SECONDS` (по умолчанию `30`) — длина окна наблюдения для расчёта смертности.
- `TOP_N` (по умолчанию `8`) — сколько планет показывать в текстовом приоритетном списке.
- `DASHBOARD_TOP` (по умолчанию `12`) — сколько планет включать в топы/графики.
- `DASHBOARD_OUTPUT` (по умолчанию `dashboard.html`) — путь к HTML-дашборду.
- `AUTO_OPEN_DASHBOARD` (по умолчанию `1`) — авто-открывать HTML в браузере (`0` чтобы отключить).
- `SUMMARY_MAX_CHARS` (по умолчанию `900`) — ограничение длины эвристической сводки в подписи дашборда.
- `HD2_API_URL` (по умолчанию `https://api.helldivers2.dev/api/v1/planets`) — URL API.
- `HD2_CLIENT_HEADER` (по умолчанию `medicdivers_service`) — значение заголовка `X-Super-Client`.
- `HD2_CONTACT_HEADER` (по умолчанию `mailto:medicdivers@example.com`) — значение заголовка `X-Super-Contact`.
- `REQUEST_TIMEOUT` (по умолчанию `45`) — таймаут одного HTTP-запроса в секундах.
- `REQUEST_RETRIES` (по умолчанию `4`) — число попыток при временных сетевых сбоях.
- `RETRY_BACKOFF_SECONDS` (по умолчанию `2`) — базовая задержка между ретраями (экспоненциально).
- `FRONTLINE_ONLY` (по умолчанию `1`) — учитывать только фронтовые планеты.
- `MIN_ACTIVE_PLAYERS` (по умолчанию `50`) — минимальный онлайн планеты для ранжирования.
- `NORMALIZATION_PLAYER_FLOOR` (по умолчанию `150`) — нижний порог игроков в формуле `deaths/100 players/min`, чтобы убрать выбросы на планетах с 1-5 игроками.
- `MEDCRIT_DPM_SCALE` (по умолчанию `120`) — шкала насыщения для `dpm`.
- `MEDCRIT_NDPM_SCALE` (по умолчанию `18`) — шкала насыщения для `ndpm`.
- `MEDCRIT_EMA_ALPHA` (по умолчанию `0.35`) — коэффициент EMA для тренда.
- `MEDCRIT_PLAYER_GATE_CENTER` (по умолчанию `200`) — центр sigmoid-гейта по онлайну.
- `MEDCRIT_PLAYER_GATE_SCALE` (по умолчанию `40`) — крутизна sigmoid-гейта по онлайну.
- `MEDCRIT_MISSION_GATE_SCALE` (по умолчанию `12`) — масштаб гейта по активности миссий.
- `MEDCRIT_STATE_FILE` (по умолчанию `.medcrit_state.json`) — файл с EMA-состоянием между запусками.
- `MEDCRIT_TREND_MIN_SAMPLES` (по умолчанию `2`) — минимум исторических сэмплов до включения trend.
- `MEDCRIT_TREND_MIN_MISSIONS` (по умолчанию `3`) — минимум завершённых миссий за окно для trend.
- `MEDCRIT_TREND_MIN_DEATHS` (по умолчанию `15`) — минимум смертей за окно для trend.
- `MEDCRIT_TREND_DPM_BASELINE` (по умолчанию `8`) — нижняя база для сравнения dpm с EMA.
- `MEDCRIT_FAIL_BASELINE` (по умолчанию `0.05`) — нижняя база для fail-rate в trend.
- `MEDCRIT_TREND_GROWTH_SCALE` (по умолчанию `1.6`) — плавность насыщения trend-компонента.
- `MEDCRIT_VOLUME_MIX` (по умолчанию `0.15`) — доля `vol` в итоговом `pressure`.
- `MEDCRIT_STRESS_BURN_COEF` (по умолчанию `1.45`) — вклад `burn20` в `stress_input`.
- `MEDCRIT_STRESS_FAIL_COEF` (по умолчанию `1.11`) — вклад `fail-rate` в `stress_input`.
- `MEDCRIT_STRESS_RELATIVE_COEF` (по умолчанию `0.16`) — вклад `rel` в `stress_input`.
- `MEDCRIT_STRESS_TREND_COEF` (по умолчанию `0.03`) — вклад `trend` в `stress_input`.
- `MEDCRIT_STRESS_SCALE` (по умолчанию `0.90`) — масштаб насыщения нелинейного `stress`.
- `MEDCRIT_NONLINEAR_GAMMA` (по умолчанию `1.02`) — степень нелинейной калибровки итогового `raw`.
- `MEDCRIT_REFERENCE_WINDOW_SECONDS` (по умолчанию `30`) — референсное окно, относительно которого масштабируются `MEDCRIT_EMA_ALPHA`, `MEDCRIT_TREND_MIN_SAMPLES`, `MEDCRIT_TREND_MIN_MISSIONS`, `MEDCRIT_TREND_MIN_DEATHS` и `MEDCRIT_MISSION_GATE_SCALE`.

## Настройки REST-сервиса

- `MEDICDIVERS_DB_PATH` (по умолчанию `medicdivers.db`) — путь к SQLite базе.
- `MEDICDIVERS_ANIMATIONS_DIR` (по умолчанию `animations`) — директория HTML-анимаций.
- `MEDICDIVERS_WINDOWS` (по умолчанию `300,900,1800`) — окна фоновых воркеров в секундах.
- `MEDICDIVERS_ANIMATION_TOP_N` (по умолчанию `12`) — сколько планет показывать в каждом кадре.
- `MEDICDIVERS_ANIMATION_HISTORY_RUNS` (по умолчанию `120`) — сколько последних запусков использовать в анимации.
- `MEDICDIVERS_ANIMATION_REBUILD_EVERY` (по умолчанию `1`) — как часто пересобирать HTML-анимацию (каждый N-й run).

## Пример устойчивого запуска

```bash
REQUEST_TIMEOUT=90 REQUEST_RETRIES=6 SAMPLE_SECONDS=60 TOP_N=10 DASHBOARD_TOP=15 .venv/bin/python main.py
```
