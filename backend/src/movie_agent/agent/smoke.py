from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"


class SmokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_title: str
    reason: str = Field(min_length=1, max_length=200)


def build_smoke_agent(model: Model | str | None = None) -> Agent[object, SmokeResult]:
    resolved_model = model or f"groq:{os.getenv('GROQ_MODEL', DEFAULT_GROQ_MODEL)}"
    agent = Agent(
        resolved_model,
        output_type=SmokeResult,
        system_prompt=(
            "Use the lookup_fixture tool, then select one observed title. "
            "Never invent a title that the tool did not return."
        ),
    )

    @agent.tool
    async def lookup_fixture(_ctx: RunContext[object], /, genre: str) -> list[str]:
        """Return deterministic fixture titles for a genre."""
        return ["Groundhog Day", "The Truman Show"] if genre else []

    return agent


async def run_live_smoke() -> SmokeResult:
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY is required for the opt-in live smoke test")
    result = await build_smoke_agent().run("Pick one comedy from the tool results.")
    return result.output
