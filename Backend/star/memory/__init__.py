"""STAR 2.0 memory package (Phase 8) — layered memory over the stores Star already has.

Blueprint §9: *working / episodic / semantic / preferences / procedural patterns*, with the
instruction to **"inspect existing implementation before introducing another memory store"**.
Nothing here owns storage:

* :mod:`.layers` — five adapters: ``WorkingMemory`` (the brain's ``ContextBuilder`` scratch),
  ``EpisodicLayer`` (``agent.planning.memory.EpisodicMemory`` JSONL episode log),
  ``SemanticLayer`` and ``PreferenceLayer`` (``agent.memory.store.MemoryStore`` SQLite rows of
  kind ``fact`` / ``preference``), ``ProceduralLayer`` (the brain's ``PatternStore`` — read-only
  until Phase 9 validates candidate patterns).
* :mod:`.retrieval` — ``hybrid_retrieve``: parallel, timeout-bounded recall from every layer,
  blended by per-layer weight, deduplicated, per-layer capped, with an honest trace.
* :mod:`.manager` — ``MemoryManager``: the single object the brain (as a ``MemoryRetriever``),
  the gateway/console (``snapshot``) and the tools talk to; publishes ``memory.updated`` /
  ``memory.retrieved`` / ``memory.failed``.
* :mod:`.tools` — seven typed Phase 4 tools that add only what the legacy registry
  (``remember_fact`` / ``recall_memory``) does not already offer.

Exports are lazy (PEP 562) so ``import Backend.star.memory`` stays cheap.
"""

from __future__ import annotations

__all__ = [
    "EpisodicLayer",
    "LAYER_NAMES",
    "MemoryManager",
    "MemoryToolkit",
    "PreferenceLayer",
    "ProceduralLayer",
    "RetrievalTrace",
    "SemanticLayer",
    "WorkingMemory",
    "blend",
    "build_memory",
    "hybrid_retrieve",
    "layer_of_kind",
    "register_memory_tools",
    "token_overlap",
]

_LAZY = {
    "LAYER_NAMES": ("Backend.star.memory.layers", "LAYER_NAMES"),
    "EpisodicLayer": ("Backend.star.memory.layers", "EpisodicLayer"),
    "PreferenceLayer": ("Backend.star.memory.layers", "PreferenceLayer"),
    "ProceduralLayer": ("Backend.star.memory.layers", "ProceduralLayer"),
    "SemanticLayer": ("Backend.star.memory.layers", "SemanticLayer"),
    "WorkingMemory": ("Backend.star.memory.layers", "WorkingMemory"),
    "token_overlap": ("Backend.star.memory.layers", "token_overlap"),
    "RetrievalTrace": ("Backend.star.memory.retrieval", "RetrievalTrace"),
    "blend": ("Backend.star.memory.retrieval", "blend"),
    "hybrid_retrieve": ("Backend.star.memory.retrieval", "hybrid_retrieve"),
    "MemoryManager": ("Backend.star.memory.manager", "MemoryManager"),
    "build_memory": ("Backend.star.memory.manager", "build_memory"),
    "layer_of_kind": ("Backend.star.memory.manager", "layer_of_kind"),
    "MemoryToolkit": ("Backend.star.memory.tools", "MemoryToolkit"),
    "register_memory_tools": ("Backend.star.memory.tools", "register_memory_tools"),
}


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        module_name, attribute = _LAZY[name]
        return getattr(importlib.import_module(module_name), attribute)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
