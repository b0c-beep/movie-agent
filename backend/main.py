"""Vercel's standard FastAPI entrypoint for the portable application package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from movie_agent.main import app as service_app

app = service_app
