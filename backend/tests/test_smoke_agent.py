import pytest
from pydantic_ai.models.test import TestModel

from movie_agent.agent.smoke import SmokeResult, build_smoke_agent


@pytest.mark.asyncio
async def test_tool_and_typed_output_contract() -> None:
    model = TestModel(
        call_tools=["lookup_fixture"],
        custom_output_args={
            "selected_title": "Groundhog Day",
            "reason": "It was returned by the fixture lookup.",
        },
    )
    result = await build_smoke_agent(model).run("Pick a comedy.")

    assert result.output == SmokeResult(
        selected_title="Groundhog Day",
        reason="It was returned by the fixture lookup.",
    )
