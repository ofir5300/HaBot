from abc import ABC, abstractmethod
from dataclasses import dataclass
import importlib
import pkgutil


@dataclass
class StockResult:
    in_stock: bool
    price: float | None = None
    name: str | None = None
    url: str | None = None


class Checker(ABC):
    """Base class for availability checkers."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable name for this source (e.g. 'KSP')."""

    @abstractmethod
    def check(self, item_id: str) -> StockResult:
        """Check availability for the given item ID."""


# Registry: source_name -> Checker instance
_registry: dict[str, Checker] = {}


def register(checker: Checker):
    _registry[checker.source_name] = checker


def get_checker(source: str) -> Checker | None:
    return _registry.get(source)


def all_checkers() -> dict[str, Checker]:
    return dict(_registry)


def discover():
    """Import all modules in the checkers package to trigger registration."""
    package_path = __path__
    for _, module_name, _ in pkgutil.iter_modules(package_path):
        importlib.import_module(f"{__name__}.{module_name}")
