from datetime import datetime, timezone


def test_status_returns_shape(client):
    client.db_mock.execute.return_value.mappings.return_value.first.return_value = {
        "running": True,
        "mode": "paper",
        "kill_switch_engaged": False,
        "heartbeat_at": datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc),
        "reconcile_ok": True,
        "broker_connected": True,
        "open_positions_count": 2,
        "incidents_today": 0,
        "diagnostics_today": 47,
    }
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True
    assert body["mode"] == "paper"
    assert body["open_positions_count"] == 2
    assert body["incidents_today"] == 0
    assert body["diagnostics_today"] == 47
    # heartbeat_fresh is computed live from filesystem; absent file => False
    assert body["heartbeat_fresh"] is False
