from types import SimpleNamespace

import pytest
from pydantic_ai import UsageLimits
from pydantic_ai.models.test import TestModel

import movie_agent.agent.intent as intent_agent
from movie_agent.agent.intent import extract_intent_patch
from movie_agent.models.intent import MovieIntent, MovieIntentPatch, TurnKind


@pytest.mark.asyncio
async def test_intent_agent_returns_typed_patch_without_unrequested_filters() -> None:
    model = TestModel(
        custom_output_args={
            "turn_kind": "refine",
            "requested_count": 2,
            "similar_to_title": None,
            "similar_to_year": None,
            "genres": None,
            "moods": None,
            "release_year_min": None,
            "release_year_max": None,
            "original_language": None,
            "runtime_max_minutes": None,
            "tmdb_rating_min": 7.5,
            "people": None,
            "country": None,
            "clarification": None,
        }
    )

    patch = await extract_intent_patch(
        "Make them newer, only give me two, and keep the rating at least 7.5.",
        MovieIntent(genres=["comedy"], country="RO"),
        model=model,
    )

    assert isinstance(patch, MovieIntentPatch)
    assert patch.turn_kind is TurnKind.REFINE
    assert patch.requested_count == 2
    assert patch.tmdb_rating_min == 7.5
    assert patch.genres is None
    assert patch.country is None
    assert patch.similar_to_title is None


def test_openrouter_intent_routing_preserves_other_model_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings: list[object] = []

    def capture_agent(_model: object, **kwargs: object) -> object:
        settings.append(kwargs["model_settings"])
        return object()

    monkeypatch.setattr(intent_agent, "Agent", capture_agent)

    intent_agent.build_intent_agent("openrouter:openai/gpt-oss-120b")
    intent_agent.build_intent_agent("openrouter:openai/gpt-oss-20b")
    intent_agent.build_intent_agent("groq:openai/gpt-oss-20b")
    intent_agent.build_intent_agent(TestModel())

    assert settings == [
        {
            "extra_body": {
                "provider": {
                    "order": ["deepinfra"],
                    "ignore": ["groq", "coreweave"],
                    "require_parameters": True,
                    "allow_fallbacks": True,
                }
            }
        },
        None,
        None,
        None,
    ]


@pytest.mark.asyncio
async def test_intent_agent_rejects_empty_or_oversized_messages() -> None:
    prior = MovieIntent()
    with pytest.raises(ValueError, match="1-1200"):
        await extract_intent_patch("  ", prior, model=TestModel())
    with pytest.raises(ValueError, match="1-1200"):
        await extract_intent_patch("x" * 1201, prior, model=TestModel())


@pytest.mark.asyncio
async def test_intent_extraction_uses_bounded_request_and_output_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received_limits: list[UsageLimits] = []

    class CapturingAgent:
        async def run(self, _prompt: str, *, usage_limits: UsageLimits) -> SimpleNamespace:
            received_limits.append(usage_limits)
            return SimpleNamespace(output=MovieIntentPatch(turn_kind=TurnKind.REFINE))

    monkeypatch.setattr(intent_agent, "build_intent_agent", lambda _model=None: CapturingAgent())

    await extract_intent_patch("Make those movies newer.", MovieIntent())

    assert received_limits[0].request_limit == 6
    assert received_limits[0].output_tokens_limit == 10_000


@pytest.mark.asyncio
async def test_intent_agent_supports_an_out_of_scope_turn_kind() -> None:
    patch = await extract_intent_patch(
        "What is the weather tomorrow?",
        MovieIntent(),
        model=TestModel(custom_output_args={"turn_kind": "out_of_scope"}),
    )

    assert patch.turn_kind is TurnKind.OUT_OF_SCOPE
    assert patch.people is None
    assert patch.genres is None
