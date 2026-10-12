"""Provider packages (flights, accommodation) and a small generic registry they share."""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T", bound=type)


class ProviderRegistry(Generic[T]):
    """Maps provider ``key`` -> provider class, in registration order."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._classes: dict[str, T] = {}

    def register(self, cls: T) -> T:
        """Register ``cls`` under ``cls.key``; usable as a class decorator."""
        key = getattr(cls, "key", None)
        if not isinstance(key, str) or not key:
            raise ValueError(f"{cls.__name__} must define a non-empty 'key'")
        existing = self._classes.get(key)
        if existing is not None and existing.__qualname__ != cls.__qualname__:
            raise ValueError(f"{self.kind} provider key {key!r} already registered")
        self._classes[key] = cls
        return cls

    def get(self, key: str) -> T:
        """Class registered under ``key`` (``KeyError`` if unknown)."""
        try:
            return self._classes[key]
        except KeyError:
            raise KeyError(f"unknown {self.kind} provider {key!r}") from None

    def all(self) -> list[T]:
        """All registered classes in registration order."""
        return list(self._classes.values())
