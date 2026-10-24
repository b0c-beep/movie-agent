import pytest
from pydantic import ValidationError

from movie_agent.models.intent import MovieIntent, StringCollectionPatch


def test_intent_defaults_and_limits() -> None:
    intent = MovieIntent()
    assert intent.requested_count == 5
    assert intent.country == "RO"

    with pytest.raises(ValidationError):
        MovieIntent(requested_count=11)


def test_intent_rejects_reversed_years() -> None:
    with pytest.raises(ValidationError):
        MovieIntent(release_year_min=2000, release_year_max=1990)


def test_collection_patch_rejects_ambiguous_operations() -> None:
    with pytest.raises(ValidationError):
        StringCollectionPatch(set=["comedy"], add=["romance"])
