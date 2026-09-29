import time

from fastapi.testclient import TestClient

from bot.api.routes import create_app
from bot.model_race import ModelRaceManager
from bot.models import model_releases


def race_request():
    return {
        "name": "Equal start test",
        "starting_bankroll": 100,
        "allocation_percent": 20,
        "minimum_edge_percent": 3,
        "entry_cutoff_seconds": 30,
        "thin_gap_allocation_percent": 25,
        "movement_disagreement_allocation_percent": 50,
    }


def test_model_race_starts_every_release_with_separate_logs(tmp_path):
    manager = ModelRaceManager(tmp_path)
    client = TestClient(create_app(tmp_path, model_races=manager))
    try:
        response = client.post("/api/v1/model-races", json=race_request())
        assert response.status_code == 201
        race = response.json()
        assert {row["model_version"] for row in race["models"]} == {
            release["id"] for release in model_releases()
        }
        assert {row["confidence"] for row in race["models"]} == {
            release["recommended_confidence"] for release in model_releases()
        }
        race_id = race["race_id"]
        for release in model_releases():
            folder = tmp_path / "model-races" / race_id / release["id"]
            assert (folder / f"race-{race_id}.jsonl").exists()
            assert (folder / f"race-{race_id}-status.json").exists()

        listing = client.get("/api/v1/model-races").json()
        assert listing["active"] is True
        assert listing["items"][0]["race_id"] == race_id
        assert client.post(f"/api/v1/model-races/{race_id}/stop").status_code == 200
        deadline = time.monotonic() + 2
        while manager.is_active() and time.monotonic() < deadline:
            time.sleep(.02)
        assert manager.is_active() is False
    finally:
        manager.shutdown()


def test_model_race_rejects_second_active_race(tmp_path):
    manager = ModelRaceManager(tmp_path)
    client = TestClient(create_app(tmp_path, model_races=manager))
    try:
        assert client.post("/api/v1/model-races", json=race_request()).status_code == 201
        response = client.post("/api/v1/model-races", json=race_request())
        assert response.status_code == 409
        assert "already active" in response.json()["detail"]
    finally:
        manager.shutdown()


def test_runtime_restart_resumes_clean_model_race_balances(tmp_path):
    manager = ModelRaceManager(tmp_path)
    race = manager.start({
        "name": "Persistent race", "starting_bankroll": 100,
        "allocation_fraction": .2, "maximum_trade_usd": None,
        "minimum_edge": .03, "entry_cutoff_seconds": 30,
        "thin_gap_allocation_fraction": .25,
        "movement_disagreement_allocation_fraction": .5,
    })
    race_id = race["race_id"]
    manager.shutdown()
    resumed = ModelRaceManager(tmp_path)
    try:
        assert resumed.is_active() is True
        detail = resumed.detail(race_id)
        assert detail["status"] == "RUNNING"
        assert all(row["status"]["available_bankroll"] == 100
                   for row in detail["models"])
        assert all(row["status"]["starting_bankroll"] == 100
                   for row in detail["models"])
    finally:
        resumed.shutdown()
