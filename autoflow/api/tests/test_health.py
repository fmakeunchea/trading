def test_health_engine_dir_present(client, var_dir):
    # Force db_ok=True via the SELECT 1 stub
    client.db_mock.execute.return_value = client.db_mock.execute.return_value
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["engine_var_dir_ok"] is True
    assert "api_uptime_s" in body


def test_health_engine_dir_missing(client, monkeypatch, tmp_path):
    from app import config as app_config
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(app_config.settings, "engine_var_dir", missing)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["engine_var_dir_ok"] is False
