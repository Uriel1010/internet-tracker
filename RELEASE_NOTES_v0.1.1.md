# v0.1.1 – Outages CSV Export (Local TZ)

## Changed
- Outages export (`/api/outages/export`) now returns **CSV** with only local timezone timestamps.
  - Columns: `id,start_time_local,end_time_local,duration_seconds`
  - Removed prior TXT format and UTC columns for simpler downstream processing.

## Internal / Maintenance
- Pruned verbose frontend debug logging for production cleanliness.

## Reminder
If you previously parsed the legacy TXT outage export or depended on UTC fields, update any scripts to consume the new CSV or derive UTC directly from the database.

---
See the full history in [CHANGELOG.md](./CHANGELOG.md).
