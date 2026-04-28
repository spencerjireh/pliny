from collections.abc import Awaitable, Callable

from pliny.pipeline.context import StageContext

Handler = Callable[[StageContext], Awaitable[None]]


STAGE_VERSIONS: dict[str, int] = {
    "extract": 1,
    "summarize": 0,
    "chunk": 0,
    "embed": 0,
    "entities": 0,
    "graph_sync": 0,
    "snapshot": 0,
    "wayback_fallback": 0,
}

STAGE_POOLS: dict[str, str] = {
    "extract": "fast",
    "summarize": "fast",
    "chunk": "fast",
    "embed": "fast",
    "entities": "fast",
    "graph_sync": "fast",
    "snapshot": "slow",
    "wayback_fallback": "slow",
}


_HANDLERS: dict[str, Handler] = {}


class UnknownStageError(Exception):
    pass


class NoHandlerError(Exception):
    """Raised when an item type has no extraction handler. Short-circuits to failed."""


def register(stage: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[stage] = fn
        return fn

    return deco


def get_handler(stage: str) -> Handler:
    if stage not in _HANDLERS:
        raise UnknownStageError(stage)
    return _HANDLERS[stage]


def has_handler(stage: str) -> bool:
    return stage in _HANDLERS


def downstream_stages(item_type: str, finished_stage: str) -> list[str]:
    """Return downstream stages to enqueue after `finished_stage` completes.

    Step 4 only registers `extract`; downstream stages (summarize/chunk/embed/...)
    ship in later slices. Until then this returns []. Each later slice extends
    this map.
    """
    return []
