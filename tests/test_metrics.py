from app.metrics_utils import compute_latency_metrics

def test_empty_samples():
    m = compute_latency_metrics([])
    assert m['count'] == 0
    assert m['packet_loss_pct'] == 0
    assert m['avg_latency_ms'] is None


def test_all_success_latency():
    samples = [
        {"success": 1, "latency_ms": 10},
        {"success": 1, "latency_ms": 20},
        {"success": 1, "latency_ms": 30},
    ]
    m = compute_latency_metrics(samples)
    assert m['count'] == 3
    assert m['failures'] == 0
    assert m['avg_latency_ms'] == 20
    assert m['min_latency_ms'] == 10
    assert m['max_latency_ms'] == 30
    assert m['packet_loss_pct'] == 0
    assert m['jitter_avg_abs_ms'] == ((10+10)/2)  # diffs 10,10


def test_mixed_success_failure():
    samples = [
        {"success": 1, "latency_ms": 50},
        {"success": 0, "latency_ms": None},
        {"success": 1, "latency_ms": 70},
        {"success": 0, "latency_ms": None},
    ]
    m = compute_latency_metrics(samples)
    assert m['count'] == 4
    assert m['successes'] == 2
    assert m['failures'] == 2
    assert round(m['packet_loss_pct'],2) == 50.00
    assert m['min_latency_ms'] == 50
    assert m['max_latency_ms'] == 70
    # jitter = abs(70-50) = 20 (only one diff)
    assert m['jitter_avg_abs_ms'] == 20


def test_single_sample_jitter_none():
    samples = [{"success": 1, "latency_ms": 42}]
    m = compute_latency_metrics(samples)
    assert m['jitter_avg_abs_ms'] is None


def test_no_latency_for_failures():
    samples = [
        {"success": 0, "latency_ms": None},
        {"success": 0, "latency_ms": None},
        {"success": 0, "latency_ms": None},
    ]
    m = compute_latency_metrics(samples)
    assert m['count'] == 3
    assert m['successes'] == 0
    assert m['failures'] == 3
    assert m['avg_latency_ms'] is None
    assert m['packet_loss_pct'] == 100
