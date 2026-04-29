def import_stages() -> None:
    """Side-effect import: registers every pipeline stage handler."""
    import pliny.pipeline.chunk  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.embed  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.entities  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.extract  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.graph_sync  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.snapshot  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.summarize  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.wayback_fallback  # noqa: F401  # pyright: ignore[reportUnusedImport]
