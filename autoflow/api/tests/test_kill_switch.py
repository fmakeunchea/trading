def test_kill_switch_initially_off(client, var_dir):
    r = client.get("/kill-switch")
    assert r.status_code == 200
    assert r.json() == {"engaged": False}


def test_engage_creates_file(client, var_dir):
    r = client.post("/kill-switch", json={"engaged": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert (var_dir / "trading_bot.kill").exists()


def test_release_removes_file(client, var_dir):
    (var_dir / "trading_bot.kill").touch()
    r = client.post("/kill-switch", json={"engaged": False})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert not (var_dir / "trading_bot.kill").exists()


def test_release_when_already_off_is_idempotent(client, var_dir):
    r = client.post("/kill-switch", json={"engaged": False})
    assert r.status_code == 200
    assert r.json()["ok"] is True
