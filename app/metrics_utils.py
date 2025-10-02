from typing import List, Dict, Any, Optional

def compute_latency_metrics(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    latencies = [s["latency_ms"] for s in samples if s.get("success") and s.get("latency_ms") is not None]
    total = len(samples)
    successes = sum(1 for s in samples if s.get("success"))
    failures = total - successes
    packet_loss_pct = (failures / total * 100) if total else 0.0
    avg = sum(latencies) / len(latencies) if latencies else None
    mn = min(latencies) if latencies else None
    mx = max(latencies) if latencies else None
    jitter = None
    if len(latencies) > 1:
        diffs = [abs(latencies[i] - latencies[i-1]) for i in range(1, len(latencies))]
        jitter = sum(diffs) / len(diffs) if diffs else None
    return {
        "count": total,
        "successes": successes,
        "failures": failures,
        "packet_loss_pct": packet_loss_pct,
        "avg_latency_ms": avg,
        "min_latency_ms": mn,
        "max_latency_ms": mx,
        "jitter_avg_abs_ms": jitter,
    }