# API README

This service exposes HTTP APIs for status, outages, metrics, trends, and Home Assistant integration.

Base URL examples:
- Local: `http://localhost:18000`
- Docker host LAN: `http://<host-ip>:18000`

## Quick Endpoints

### `GET /api/status`
Current monitor state and last outage.

Example:
```bash
curl -s "http://localhost:18000/api/status?service=default"
```

### `GET /api/outages`
List outages (newest first).

Example:
```bash
curl -s "http://localhost:18000/api/outages?service=default"
```

### `GET /api/metrics`
Latency/packet-loss/jitter metrics with samples.

Query params:
- `range`: `5m` | `1h` | `24h` | `all`
- `service`: service name
- `limit`: max samples when `range=all`

Example:
```bash
curl -s "http://localhost:18000/api/metrics?range=5m&service=default"
```

### `GET /api/trends`
Daily trend aggregation (latency, outages, speedtest).

Query params:
- `days`: 7..180
- `service`: service name

Example:
```bash
curl -s "http://localhost:18000/api/trends?days=30&service=default"
```

## Home Assistant Integration API

### `GET /api/integration/home-assistant`
Flattened payload designed for Home Assistant REST sensors.

Query params:
- `service` (optional, default `default`)

Example:
```bash
curl -s "http://localhost:18000/api/integration/home-assistant?service=default"
```

Example response:
```json
{
  "service": "default",
  "status": "online",
  "last_ok": true,
  "last_latency_ms": 22.4,
  "checks": 24819,
  "consecutive_failures": 0,
  "consecutive_successes": 24819,
  "packet_loss_pct_5m": 0.0,
  "avg_latency_ms_5m": 21.8,
  "jitter_avg_abs_ms_5m": 1.9,
  "samples_5m": 300,
  "outages_24h": 0,
  "downtime_seconds_24h": 0.0,
  "outages_30d": 3,
  "packet_loss_pct_30d": 0.14,
  "speedtest_runs_30d": 92,
  "last_speedtest_ts": "2026-02-18T10:10:01.123456+00:00",
  "last_speedtest_download_mbps": 486.2,
  "last_speedtest_upload_mbps": 51.3,
  "last_speedtest_ping_ms": 7.8,
  "last_speedtest_server_name": "Provider Node A",
  "updated_at_utc": "2026-02-18T10:11:00.000000+00:00"
}
```

## Home Assistant YAML Example

Add this to `configuration.yaml` (or split packages):

```yaml
sensor:
  - platform: rest
    name: Internet Tracker
    resource: http://YOUR_HOST_IP:18000/api/integration/home-assistant?service=default
    method: GET
    value_template: "{{ value_json.status }}"
    scan_interval: 60
    json_attributes:
      - last_ok
      - last_latency_ms
      - packet_loss_pct_5m
      - avg_latency_ms_5m
      - jitter_avg_abs_ms_5m
      - outages_24h
      - downtime_seconds_24h
      - outages_30d
      - packet_loss_pct_30d
      - speedtest_runs_30d
      - last_speedtest_download_mbps
      - last_speedtest_upload_mbps
      - last_speedtest_ping_ms
      - updated_at_utc
```

Optional template sensors for dedicated entities:

```yaml
template:
  - sensor:
      - name: Internet Latency (5m avg)
        unit_of_measurement: "ms"
        state: "{{ state_attr('sensor.internet_tracker', 'avg_latency_ms_5m') }}"
      - name: Internet Packet Loss (5m)
        unit_of_measurement: "%"
        state: "{{ state_attr('sensor.internet_tracker', 'packet_loss_pct_5m') }}"
      - name: Internet Speedtest Download
        unit_of_measurement: "Mbps"
        state: "{{ state_attr('sensor.internet_tracker', 'last_speedtest_download_mbps') }}"
      - name: Internet Speedtest Upload
        unit_of_measurement: "Mbps"
        state: "{{ state_attr('sensor.internet_tracker', 'last_speedtest_upload_mbps') }}"
```

## Notes

- All timestamps in this API are UTC ISO 8601 unless explicitly labeled local in other endpoints.
- If you use multi-service mode (`MULTI_SERVICES`), pass `?service=<name>`.
- For Home Assistant polling, `scan_interval: 60` is usually enough.
