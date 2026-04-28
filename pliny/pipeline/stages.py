from collections.abc import Awaitable, Callable

from pliny.pipeline.context import StageContext

Handler = Callable[[StageContext], Awaitable[None]]


STAGE_VERSIONS: dict[str, int] = {
    "extract": 1,
    "summarize": 1,
    "chunk": 1,
    "embed": 1,
    "entities": 1,
    "graph_sync": 1,
    "snapshot": 1,
    "wayback_fallback": 1,
}

# Stages that can only enqueue once their prereqs' versions are at the
# current code constant. Read by the runner before INSERT.
STAGE_PREREQS: dict[str, list[str]] = {
    "embed": ["summarize", "chunk"],
    "entities": ["embed"],
    "graph_sync": ["entities"],
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

    For URL items the chain is `snapshot -> extract -> summarize/chunk -> embed
    -> entities -> graph_sync`. The `wayback_fallback` stage also feeds extract
    (recovery from a failed snapshot). embed is fan-in: it depends on both
    summarize and chunk. The runner re-checks STAGE_PREREQS before INSERT and
    the (item_id, stage) unique constraint absorbs concurrent enqueue races.
    """
    if finished_stage == "snapshot" and item_type in ("url", "audio", "video"):
        return ["extract"]
    if finished_stage == "wayback_fallback" and item_type == "url":
        return ["extract"]
    if finished_stage == "extract":
        return ["summarize", "chunk"]
    if finished_stage in {"summarize", "chunk"}:
        return ["embed"]
    if finished_stage == "embed":
        return ["entities"]
    if finished_stage == "entities":
        return ["graph_sync"]
    return []
