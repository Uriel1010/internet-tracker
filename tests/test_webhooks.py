from app import webhooks


def setup_function():
    webhooks._recent_outage_events.clear()


def test_should_send_deduplicates_same_event_service_and_outage():
    payload = {"event": "outage.end", "service": "default", "outage_id": 42}
    assert webhooks._should_send(payload) is True
    assert webhooks._should_send(payload) is False


def test_should_send_allows_different_events_for_same_outage():
    start_payload = {"event": "outage.start", "service": "default", "outage_id": 42}
    end_payload = {"event": "outage.end", "service": "default", "outage_id": 42}
    assert webhooks._should_send(start_payload) is True
    assert webhooks._should_send(end_payload) is True


def test_should_send_allows_payload_without_outage_id():
    payload = {"event": "test", "service": "default"}
    assert webhooks._should_send(payload) is True
    assert webhooks._should_send(payload) is True
