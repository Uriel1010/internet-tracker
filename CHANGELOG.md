# Changelog

All notable changes to this project will be documented here.

## [Unreleased]
- Add Prometheus metrics endpoint
- Add alerting/webhook integration
- Dark mode UI

## 0.2.0 - 2025-10-03
### Added
- Multi-service monitoring via `MULTI_SERVICES` JSON array (concurrent per-target loops)
- Webhook delivery system (start/end) with configurable `ALERT_WEBHOOK_SEND_START` (default false) & `ALERT_WEBHOOK_SEND_END` (default true)
- Example outage trigger endpoint `/api/webhook/example-outage` + Settings UI button
- End-only webhook mode (sends a single `outage.end` payload per outage) by default
- UI Settings tab with live webhook status & example outage button

### Changed
- README expanded (multi-service, webhook docs, compose usage)
- Docker Compose example file added (`docker-compose.example.yml`)
- Environment defaults updated to favor end-only alerts

### Fixed
- Various outage unwrapping / state selection regressions during multi-service refactor
- Resolved schema migration edge cases (service column backfill)

## 0.1.1 - 2025-10-02
### Changed
- Outages export now CSV with local timezone only (removed UTC + TXT format)

## 0.1.0 - 2025-10-02
### Added
- Initial public release
- Real-time connectivity monitoring (ping/http)
- Outage detection & recording
- Latency, jitter, packet loss metrics
- SSE streaming with auto-reconnect & reseed
- Time range analytics + decimation slider
- Timezone support via `TZ` env
- Data export (TXT outages, CSV metrics with UTC+local timestamps)
- Resilient worker-based chart rendering
- Comprehensive README, security, contributing docs
