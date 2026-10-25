import pytest
from pydantic_ai.models.test import TestModel

from movie_agent.agent.intent import extract_intent_patch
from movie_agent.models.intent import MovieIntent, MovieIntentPatch, TurnKind


@pytest.mark.asyncio
async def test_intent_agent_returns_typed_patch_without_unrequested_filters() -> None:
    model = TestModel(
        custom_output_args={
            "turn_kind": "refine",
            "requested_count": 2,
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


@pytest.mark.asyncio
async def test_intent_agent_rejects_empty_or_oversized_messages() -> None:
    prior = MovieIntent()
    with pytest.raises(ValueError, match="1-1200"):
        await extract_intent_patch("  ", prior, model=TestModel())
    with pytest.raises(ValueError, match="1-1200"):
        await extract_intent_patch("x" * 1201, prior, model=TestModel())
