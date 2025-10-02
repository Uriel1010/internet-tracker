<p align="center">
  <img src="app/static/favicon.svg" alt="Internet Tracker Logo" width="150">
</p>

<h1 align="center">Internet Connectivity Tracker</h1>

<p align="center">
  <strong>A lightweight, self-hosted web application to monitor your internet connectivity, track outages, and analyze network performance.</strong>
</p>

<p align="center">
  <img src="example_pages/STATUS_TAB_EXAMPLE.png" alt="Status Tab Screenshot">
</p>

<p align="center">
  <em>Monitor. Detect. Analyze. Export. All in one lightweight container.</em>
</p>

## ✨ Features

- **Real‑time Monitoring:** Sub‑second customizable interval (default 1s) for continuous reachability + latency sampling.
- **Smart Outage Detection:** Threshold-based failure / recovery logic with precise duration calculation.
- **Deep Metrics:** Live latency, min/max/avg, jitter (mean absolute delta), success/failure counts, packet loss %.
- **High‑Performance UI:** Web Worker offloads aggregation + downsampling (LTTB) for smooth charts even with tens of thousands of points.
- **Interactive Analytics:** Time range controls (5m / 1h / 24h / All) with dynamic decimation slider and pause/resume.
- **Resilient Streaming:** Server-Sent Events (SSE) with auto-reconnect and state reseeding after backend restarts.
- **Data Export:** One-click CSV (metrics) and TXT (outages) exports with both UTC and localized timestamps.
- **Timezone Aware:** Honors `TZ` environment variable; UI labels adapt automatically.
- **Lightweight Footprint:** Single small container (FastAPI + SQLite) — no external services required.
- **Stateless Frontend:** All computation reproducible from server history; worker can be reseeded at any time.
- **Easy Extensibility:** Clean endpoints + modular code allow adding alerts, webhooks, Prometheus, etc.

## 🧭 Architecture at a Glance

```
 ┌──────────────────────────────────────────────────────────┐
 │                          Browser                        │
 │  UI (Chart.js)  ◄── Worker (buffer, metrics, LTTB)      │
 │        ▲                       ▲                        │
 │        │updates (postMessage)  │SSE samples             │
 └────────┼───────────────────────┼────────────────────────┘
     │                       │
     ▼                       ▼
   FastAPI Backend  ── async monitor loop ── ping/http checks
     │
     ▼
  SQLite DB (latency_samples, outages)
```

Key performance choices:
- Circular buffer + downsampling in Worker keeps UI fast.
- SSE sends only new samples; page can recover fully after outage using reseed fetch.
- All times stored UTC; localized on output.

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- An internet connection to install dependencies

### Local Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/your-username/internet-tracker.git
    cd internet-tracker
    ```

2.  **Create and activate a virtual environment:**
    ```bash
    python -m venv .venv
    source .venv/bin/activate
    ```

3.  **Install the dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Run the application:**
    ```bash
    uvicorn app.main:app --reload
    ```

5.  **Open your browser** and navigate to `http://localhost:8000`.

### Docker Deployment (Recommended)

1.  **Build the Docker image:**
    ```bash
    docker build -t internet-tracker .
    ```

2.  **Run the Docker container:**
    ```bash
    docker run --name internet-tracker -p 8000:8000 internet-tracker
    ```
    
    You can also use the provided `docker-compose.yml` file:
    ```bash
    docker-compose up -d
    ```

## ⚙️ Configuration

The application can be configured using environment variables. You can create a `.env` file in the project root directory to store your configuration.

| Variable            | Description                                                 | Default     |
| ------------------- | ----------------------------------------------------------- | ----------- |
| `CHECK_INTERVAL`    | The interval in seconds between connectivity checks.        | `1`         |
| `TARGET_HOST`       | The host to ping for connectivity checks.                   | `8.8.8.8`   |
| `CHECK_METHOD`      | The method to use for connectivity checks (`ping` or `http`). | `ping`      |
| `FAIL_THRESHOLD`    | The number of consecutive failures to trigger an outage.    | `2`         |
| `RECOVER_THRESHOLD` | The number of consecutive successes to end an outage.       | `2`         |
| `DB_PATH`           | The path to the SQLite database file.                       | `/data/data.sqlite3` |
| `TZ`                | The timezone to use for displaying dates and times.         | `UTC`       |

### Timezone Behavior
All timestamps are stored internally in UTC. API responses include both UTC and localized forms where appropriate:

- Outages export: `Start (UTC)`, `Start (Local)`, `End (UTC)`, `End (Local)`
- Metrics CSV: `ts_utc`, `ts_local`
- Streaming events: `ts` (UTC) + `ts_local`

Change timezone by setting `TZ` (e.g. `Europe/Berlin`, `Asia/Jerusalem`). Invalid zones fall back to UTC.

### Adding a .env (optional)
Create a `.env` and reference it in `docker-compose.yml` or load manually:

```
CHECK_INTERVAL=1
TARGET_HOST=8.8.4.4
FAIL_THRESHOLD=3
RECOVER_THRESHOLD=2
TZ=Asia/Jerusalem
```


## 🖼️ Screenshots

| Status Tab                                       | Outages Tab                                        | Analytics Tab                                      |
| ------------------------------------------------ | -------------------------------------------------- | -------------------------------------------------- |
| <img src="example_pages/STATUS_TAB_EXAMPLE.png"> | <img src="example_pages/OUTAGRES_TAB_EXAMPLE.png"> | <img src="example_pages/ANALYTIC_TAB_EXAMPLE.png"> |

## 📡 API Endpoints

The application provides a RESTful API for accessing connectivity data.

| Method | Endpoint | Description | Notes |
| ------ | -------- | ----------- | ----- |
| GET | `/api/status` | Current monitor state + last outage | Includes `tz` |
| GET | `/api/outages` | List outages (latest first) | Local timestamp fields included |
| GET | `/api/outages/export` | TXT export (UTC + Local) | Attachment |
| GET | `/api/metrics?range=5m` | Metrics + samples (filtered) | Ranges: 5m,1h,24h,all |
| GET | `/api/metrics/export.csv` | Bulk CSV export | Includes both time forms |
| GET | `/api/stream/samples` | SSE with new samples | Auto-reconnect handled in UI |

Future ideas: `/api/health`, `/api/version`, webhook triggers.

### Sample SSE Event
```json
{
  "id": 12345,
  "ts": "2025-10-02T12:34:56.789012+00:00",
  "ts_local": "2025-10-02T15:34:56.789012+03:00",
  "success": 1,
  "latency_ms": 12.4
}
```

## 🛠️ Technology Stack

- **Backend:** [FastAPI](https://fastapi.tiangolo.com/), [Python 3](https://www.python.org/)
- **Database:** [SQLite](https://www.sqlite.org/index.html)
- **Frontend:** HTML, CSS, JavaScript
- **Containerization:** [Docker](https://www.docker.com/)

## 🧪 Development

Run locally with auto-reload:
```bash
uvicorn app.main:app --reload
```

Format / lint (suggested tools):
```bash
pip install black isort ruff
black . && isort . && ruff check .
```

Run inside Docker while binding local `data/` volume for persistence.

### Performance Tuning
- Increase `CHECK_INTERVAL` to reduce load.
- Keep `TARGET_HOST` geographically close for lower baseline latency.
- Use `http` method if ICMP is blocked in your environment.
- Adjust decimation slider in UI for large ranges.

### Scaling & Persistence
SQLite is fine for personal / single-host usage. For long-term high-frequency retention or multi-user: migrate to Postgres and enlarge historical retention / pruning logic.

## 🔐 Security Considerations
- No authentication built-in (intended for private network / homelab). Place behind reverse proxy or add auth middleware for public exposure.
- Limit exposure of `/api/stream/samples` if sensitive.
- All inputs are controlled server-side; no user-supplied SQL.

## 🗺️ Roadmap (Ideas)
- Alerting (email, webhook, Slack, Telegram) on outage start / recovery.
- Historical aggregation rollups (hour/day averages) to reduce long-range payload sizes.
- Prometheus metrics endpoint.
- Dark mode toggle.
- Export JSON for external processing.
- Optional WebSocket transport.

## 🤝 Contributing

Contributions are welcome! If you have any ideas, suggestions, or bug reports, please open an issue or submit a pull request.

### Pull Request Guidelines
1. Open an issue first for significant changes.
2. Keep PRs small & focused.
3. Include before/after rationale for UI changes (screenshots helpful).
4. Ensure new environment variables have sane defaults.

## 🧾 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

<p align="center"><sub>Built to make intermittent connectivity visible — fork it, extend it, share improvements.</sub></p>