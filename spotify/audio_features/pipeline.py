"""
ProviderChain — tries providers in order, passing only unresolved IDs to
each successive provider.  Stops early once all IDs are resolved.
"""

from __future__ import annotations

import logging

from .base import AudioFeaturesProvider, AudioFeaturesResult

logger = logging.getLogger(__name__)


class ProviderChain:
    """Run a list of providers in sequence, collecting results greedily.

    Example::

        chain = ProviderChain([KaggleDatasetProvider(), SomeApiProvider()])
        results = chain.fetch(track_ids)  # dict[track_id, AudioFeaturesResult]
    """

    def __init__(self, providers: list[AudioFeaturesProvider]) -> None:
        if not providers:
            raise ValueError("ProviderChain requires at least one provider")
        self.providers = providers

    def fetch(self, track_ids: list[str]) -> dict[str, AudioFeaturesResult]:
        results: dict[str, AudioFeaturesResult] = {}
        remaining = list(track_ids)

        for provider in self.providers:
            if not remaining:
                break
            logger.info("[%s] querying %d track(s)…", provider.name, len(remaining))
            found = provider.fetch(remaining)
            results.update(found)
            remaining = [tid for tid in remaining if tid not in found]
            logger.info(
                "[%s] resolved %d, %d still pending",
                provider.name,
                len(found),
                len(remaining),
            )

        if remaining:
            logger.warning(
                "%d track(s) unresolved after all providers: %s…",
                len(remaining),
                remaining[:5],
            )

        return results
