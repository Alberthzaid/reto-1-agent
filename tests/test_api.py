import pytest
from fastapi.testclient import TestClient

from agent.main import app

ENV_KEYS = ["ANTHROPIC_API_KEY", "TEAM_TOKEN", "DESKTOP_URL", "DESKTOP_PASSWORD"]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return TestClient(app)


def test_health_ok_without_env(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}


def test_health_does_not_leak_secrets(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_value = "redaction-fixture-value"
    monkeypatch.setenv("DESKTOP_PASSWORD", fixture_value)
    assert fixture_value not in client.get("/health").text
