"""Provider abstraction. Each vendor implements `complete`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int


class Provider(ABC):
    name: str  # 'anthropic' | 'google'

    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
    ) -> ProviderResult: ...
