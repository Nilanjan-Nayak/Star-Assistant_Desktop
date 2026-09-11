from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.enums import MemoryKind
from agent.core.errors import BackendUnavailable
from agent.memory.extract import extract_from_goal
from agent.memory.store import MemoryStore
from agent.memory.sync import DriveSync, FolderSync
from agent.planning.planners.stub import StubPlanner
from agent.planning.memory import EpisodicMemory
from agent.world.model import WorldModel


def test_extract_volume() -> None:
    prefs = extract_from_goal("set volume to 40")
    assert prefs and prefs[0].key == "volume.level"
    assert prefs[0].value == "40"


def test_extract_ignores_bare_volume() -> None:
    assert extract_from_goal("set the volume") == []


def test_remember_and_recent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m.db", owner="Nilanjan")
    rec = store.remember("likes lo-fi at night", kind=MemoryKind.PREFERENCE)
    assert rec.memory_id
    recent = store.recall_recent(5)
    assert recent[0].text == "likes lo-fi at night"
    store.close()


def test_semantic_ranks_similar(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m.db")
    store.remember("Preferred volume is 20", kind=MemoryKind.PREFERENCE, key="volume.level", value="20")
    store.remember("Preferred YouTube query is lo-fi beats", kind=MemoryKind.PREFERENCE)
    store.remember("launched chrome yesterday", kind=MemoryKind.EPISODE)
    hits = store.recall_relevant("what volume does the user like")
    assert hits
    assert any("volume" in h.text.lower() for h in hits[:2])
    ctx = store.build_context_block("volume at night")
    assert "volume" in ctx.lower()
    store.close()


def test_latest_by_key_wins(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m.db")
    store.remember("Preferred volume is 70", kind=MemoryKind.PREFERENCE, key="volume.level", value="70")
    store.remember("Preferred volume is 30", kind=MemoryKind.PREFERENCE, key="volume.level", value="30")
    assert store.preferred_int("volume.level", 70) == 30
    store.close()


def test_dedup_same_key_value(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m.db")
    a = store.remember("Preferred volume is 30", kind=MemoryKind.PREFERENCE, key="volume.level", value="30")
    b = store.remember("Preferred volume is 30", kind=MemoryKind.PREFERENCE, key="volume.level", value="30")
    assert a.memory_id == b.memory_id
    assert store.count() == 1
    store.close()


def test_folder_sync(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    store = MemoryStore(db)
    store.remember("hello", kind=MemoryKind.FACT)
    store.close()
    backup = tmp_path / "backup"
    dest = FolderSync(backup).push(db)
    assert dest.exists()
    restored = tmp_path / "restored" / "m.db"
    FolderSync(backup).pull(restored)
    other = MemoryStore(restored)
    assert other.count() >= 1
    other.close()


def test_drive_sync_is_explicitly_unwired() -> None:
    sync = DriveSync("folder-id")
    with pytest.raises(BackendUnavailable):
        sync.push(Path("star_memory.db"))


@pytest.mark.asyncio
async def test_planner_uses_remembered_volume(
    fake_world: WorldModel, tmp_path: Path
) -> None:
    store = MemoryStore(tmp_path / "m.db")
    store.remember(
        "Preferred volume is 35",
        kind=MemoryKind.PREFERENCE,
        key="volume.level",
        value="35",
    )
    planner = StubPlanner(semantic=store)
    memory = EpisodicMemory(path=tmp_path / "ep.jsonl")
    step = await planner.next_step("set the volume", [], fake_world, memory)
    assert step is not None
    assert step.params["level"] == 35
    store.close()
