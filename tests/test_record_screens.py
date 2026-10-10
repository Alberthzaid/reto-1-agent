"""record_screens against the mock server only (never the real desktop)."""

from pathlib import Path

import pytest
from PIL import Image

from dev_tools import record_screens
from dev_tools.mock_vnc_server import MockVncServer

PASSWORD = "hunter2-secret"


@pytest.mark.asyncio
async def test_record_saves_png_without_logging_password(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    async with MockVncServer(PASSWORD, width=64, height=48) as server:
        path = await record_screens.record(server.url, PASSWORD, tmp_path)
    assert path.parent == tmp_path and path.suffix == ".png"
    with Image.open(path) as img:
        assert img.size == (64, 48)
        assert img.getpixel((0, 0)) == (50, 100, 200)
    assert PASSWORD not in caplog.text


def test_main_without_env_returns_2_and_skips_connection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(record_screens, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(record_screens, "setup_logging", lambda *a, **k: None)
    monkeypatch.delenv("DESKTOP_URL", raising=False)
    monkeypatch.delenv("DESKTOP_PASSWORD", raising=False)
    assert record_screens.main() == 2
    assert "DESKTOP_URL" in caplog.text and "DESKTOP_PASSWORD" in caplog.text
