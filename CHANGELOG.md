# Changelog

All notable changes to this project will be documented here.

## [Unreleased]
- Add Prometheus metrics endpoint
- Add alerting/webhook integration
- Dark mode UI

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
