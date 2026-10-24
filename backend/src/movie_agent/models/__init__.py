"""Typed boundaries shared by the API, agent, and provider adapters."""

from movie_agent.models.events import TraceEvent
from movie_agent.models.intent import MovieIntent, MovieIntentPatch
from movie_agent.models.movies import Recommendation, RecommendationBatch

__all__ = [
    "MovieIntent",
    "MovieIntentPatch",
    "Recommendation",
    "RecommendationBatch",
    "TraceEvent",
]
