"""main.py (the launcher) records startup failures in data/bot.log."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("app_source", "exit_code", "expected"),
    [
        (
            'raise SystemExit("Configuration error. Set MAX_BOT_TOKEN")',
            1,
            "CRITICAL main: Startup failed: Configuration error. Set MAX_BOT_TOKEN",
        ),
        (
            'async def main():\n    raise ValueError("GEMINI_API_KEY is not set")',
            1,
            "ValueError: GEMINI_API_KEY is not set",
        ),
        ("async def main():\n    pass", 0, "INFO     main: Starting, Python"),
    ],
    ids=["config-error", "crash", "clean-exit"],
)
def test_startup_is_logged_to_data_folder(tmp_path, app_source, exit_code, expected):
    """Runs the real main.py against a stand-in app.py in an empty folder."""
    shutil.copy(ROOT / "main.py", tmp_path / "main.py")
    (tmp_path / "app.py").write_text(app_source, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "main.py"], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == exit_code, result.stderr
    assert expected in (tmp_path / "data" / "bot.log").read_text(encoding="utf-8")
