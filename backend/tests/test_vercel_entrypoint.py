from pathlib import Path
from runpy import run_path

from movie_agent.main import app as service_app


def test_vercel_entrypoint_exports_service_app() -> None:
    entrypoint = Path(__file__).parents[1] / "main.py"
    assert run_path(str(entrypoint))["app"] is service_app
