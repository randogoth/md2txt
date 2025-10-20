from __future__ import annotations

from typing import Dict, Generic, List, TypeVar


T = TypeVar("T")


class PluginRegistry(Generic[T]):
    def __init__(self) -> None:
        self._factories: Dict[str, T] = {}

    def register(self, name: str, factory: T) -> None:
        if name in self._factories:
            raise ValueError(f"Plugin '{name}' is already registered.")
        self._factories[name] = factory

    def get(self, name: str) -> T:
        try:
            return self._factories[name]
        except KeyError as exc:
            raise KeyError(f"Plugin '{name}' is not registered.") from exc

    def names(self) -> List[str]:
        return sorted(self._factories.keys())
