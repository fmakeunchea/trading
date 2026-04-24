def test_start_bot_invokes_docker_start(client, stub_docker, var_dir):
    r = client.post("/start-bot")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    cmds = [c[0] for c in stub_docker]
    assert "start" in cmds


def test_start_bot_blocked_by_kill_switch(client, stub_docker, var_dir):
    (var_dir / "trading_bot.kill").touch()
    r = client.post("/start-bot")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "kill switch" in (body["message"] or "").lower()
    # docker must not have been invoked
    assert all(c[0] != "start" for c in stub_docker)


def test_stop_bot_invokes_docker_stop(client, stub_docker):
    r = client.post("/stop-bot")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert any(c[0] == "stop" for c in stub_docker)
