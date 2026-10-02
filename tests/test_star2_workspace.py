"""Phase 7 — the invisible background workspace: sessions, jail, checkpoints, tools.

Hermetic: everything happens inside a temporary ``workspace_root``. No network, no
desktop, no display. The one process-spawn test starts ``sys.executable -c pass``
and waits for it, so nothing is orphaned.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from Backend.star.config.settings import (
    PathsSettings,
    SecuritySettings,
    Settings,
    WorkspaceSettings,
)
from Backend.star.observability.events import StarEventBus
from Backend.star.security.audit import AuditLog
from Backend.star.security.confirmations import ConfirmationStore
from Backend.star.tools.executor import ToolExecutor
from Backend.star.tools.permissions import PermissionEngine
from Backend.star.tools.registry import StarToolRegistry
from Backend.star.workspace.isolation import (
    NullIsolation,
    ProcessIsolation,
    VirtualDesktopIsolation,
    build_isolation,
)
from Backend.star.workspace.manager import WorkspaceError, WorkspaceManager, build_workspace
from Backend.star.workspace.session import Checkpoint, WorkspaceKind, WorkspaceSession, WorkspaceState
from Backend.star.workspace.tools import WorkspaceToolkit, register_workspace_tools


def _settings(tmp_path: Path, *, dry_run: bool = False, workspace: dict[str, Any] | None = None, **security: Any) -> Settings:
    return Settings(
        paths=PathsSettings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            workspace_root=tmp_path / "workspace",
            memory_db=tmp_path / "memory.db",
            patterns_path=tmp_path / "patterns.jsonl",
            episodes_path=tmp_path / "episodes.jsonl",
        ),
        security=SecuritySettings(dry_run=dry_run, audit_path=str(tmp_path / "audit.jsonl"), **security),
        workspace=WorkspaceSettings(**(workspace or {})),
    )


async def _manager(tmp_path: Path, settings: Settings | None = None, *, bus: StarEventBus | None = None, audit: Any = None):
    cfg = settings or _settings(tmp_path)
    bus = bus if bus is not None else StarEventBus()
    manager = build_workspace(cfg, bus=bus, audit=audit)
    await manager.startup()
    return manager, bus


def _kinds(event_bus: StarEventBus, prefix: str = "workspace") -> list[str]:
    return [event.kind for event in event_bus.history(limit=200) if str(event.kind).startswith(prefix)]


# ── session model ─────────────────────────────────────────────────────────────


def test_session_defaults_and_public_view(tmp_path) -> None:
    session = WorkspaceSession(root=tmp_path / "ws" / "ws_1", kind=WorkspaceKind.BROWSER, label="scrape")
    assert session.state is WorkspaceState.IDLE and session.alive and session.usable
    assert session.dry_run is True, "dry-run is the development default"
    public = session.public()
    json.dumps(public)                                   # must be JSON-safe
    assert public["kind"] == "browser" and public["state"] == "idle"
    assert public["checkpoints"] == 0 and public["checkpoint_ids"] == []
    assert public["name"] == "scrape" and public["path"].endswith("ws_1")


def test_session_expiry_touch_and_note_cap(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    session = WorkspaceSession(root=tmp_path / "a", expires_at=now - timedelta(seconds=1))
    assert session.is_expired(now) is True
    fresh = WorkspaceSession(root=tmp_path / "b", expires_at=now + timedelta(seconds=60))
    assert fresh.is_expired(now) is False
    closed = WorkspaceSession(root=tmp_path / "c", state=WorkspaceState.CLOSED, expires_at=now - timedelta(seconds=1))
    assert closed.is_expired(now) is False, "only alive sessions expire"
    assert closed.alive is False and closed.usable is False

    before = session.last_used_at
    session.touch(now + timedelta(seconds=5))
    assert session.last_used_at > before
    for index in range(40):
        session.note(f"note {index} " + "x" * 400)
    assert len(session.notes) == 24 and all(len(note) <= 200 for note in session.notes)


def test_checkpoint_model_and_kind_values() -> None:
    stamp = Checkpoint(checkpoint_id="ckpt_1", label="before", files=3, bytes=42, truncated=True)
    assert stamp.public()["truncated"] is True and stamp.public()["checkpoint_id"] == "ckpt_1"
    assert {kind.value for kind in WorkspaceKind} == {"generic", "browser", "computer", "files", "coding", "system"}
    assert WorkspaceState.DESTROYED.value == "destroyed"


# ── isolation backends ────────────────────────────────────────────────────────


def test_null_isolation_claims_nothing_it_cannot_do(tmp_path) -> None:
    settings = _settings(tmp_path)
    backend = NullIsolation(settings)
    assert backend.available() is True and backend.name == "null"
    described = backend.describe()
    assert described["capabilities"]["jail"] is True and described["capabilities"]["process"] is False
    assert "NOT hidden" in described["note"], "honest about the lack of OS-level isolation"
    session = WorkspaceSession(root=tmp_path / "s")
    backend.prepare(session)
    assert any("directory jail only" in note for note in session.notes)


def test_process_isolation_records_intent_in_dry_run(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=True, workspace={"isolation_backend": "process"})
    backend = ProcessIsolation(settings)
    session = WorkspaceSession(root=tmp_path / "s")
    out = backend.spawn(session, ["python", "-c", "pass"])
    assert out["ok"] is True and out["dry_run"] is True and out["pid"] is None
    assert "no process was started" in out["note"]
    assert backend.processes == [] and backend.describe()["spawns"] == 1


def test_process_isolation_refuses_spawning_unless_opted_in(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, workspace={"isolation_backend": "process", "allow_spawn": False})
    backend = ProcessIsolation(settings)
    session = WorkspaceSession(root=tmp_path / "s")
    out = backend.spawn(session, [sys.executable, "-c", "pass"])
    assert out["ok"] is False and out["blocked_by_policy"] is True
    assert "STAR_WORKSPACE_ALLOW_SPAWN" in out["error"]
    assert backend.processes == []
    assert backend.describe()["allow_spawn"] is False


def test_process_isolation_can_spawn_inside_the_jail_when_allowed(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, workspace={"isolation_backend": "process", "allow_spawn": True})
    backend = ProcessIsolation(settings)
    root = tmp_path / "s"
    root.mkdir(parents=True)
    session = WorkspaceSession(root=root)
    out = backend.spawn(session, [sys.executable, "-c", "pass"])
    try:
        assert out["ok"] is True and out["pid"] and session.pid == out["pid"]
        assert backend.describe()["allow_spawn"] is True
    finally:
        for process in backend.processes:      # never orphan a child in a test run
            process.wait(timeout=30)
    assert backend.describe()["running"] == 0
    assert backend.spawn(session, [])["error"] == "field 'argv' is required"


def test_virtual_desktop_backend_is_an_honest_placeholder(tmp_path) -> None:
    settings = _settings(tmp_path, workspace={"isolation_backend": "windows_virtual_desktop"})
    backend = VirtualDesktopIsolation(settings)
    assert backend.available() is False
    described = backend.describe()
    assert described["available"] is False and described["platform"] == sys.platform
    assert "no virtual-desktop API is wired yet" in described["note"]
    session = WorkspaceSession(root=tmp_path / "s")
    backend.prepare(session)
    assert any("unavailable" in note for note in session.notes)


def test_build_isolation_falls_back_and_reports(tmp_path) -> None:
    backend, note = build_isolation(_settings(tmp_path, workspace={"isolation_backend": "windows_virtual_desktop"}))
    assert isinstance(backend, ProcessIsolation) and "fell back to 'process'" in note

    backend, note = build_isolation(_settings(tmp_path, workspace={"isolation_backend": "nonsense"}))
    assert isinstance(backend, NullIsolation) and "unknown isolation backend" in note

    backend, note = build_isolation(_settings(tmp_path, workspace={"isolation_backend": "null"}))
    assert isinstance(backend, NullIsolation) and "is active" in note


# ── manager: sessions, jail, quotas ───────────────────────────────────────────


async def test_create_makes_a_jailed_directory_and_emits(tmp_path) -> None:
    manager, bus = await _manager(tmp_path)
    session = manager.create("browser", "scrape run", agent="browser")
    assert session.root.is_dir() and (session.root / "_checkpoints").is_dir()
    assert session.root.parent == Path(os.path.realpath(manager.root))
    assert session.state is WorkspaceState.IDLE and session.expires_at is not None
    assert session.kind is WorkspaceKind.BROWSER and session.agent == "browser"
    assert "workspace.created" in _kinds(bus)
    assert manager.stats["created"] == 1
    assert manager.describe()[0]["session_id"] == session.session_id


async def test_kind_is_coerced_from_strings_and_enums(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path)
    assert manager.create("computer").kind is WorkspaceKind.COMPUTER
    assert manager.create(WorkspaceKind.FILES).kind is WorkspaceKind.FILES
    assert manager.create("nonsense").kind is WorkspaceKind.GENERIC
    assert manager.acquire("coding").kind is WorkspaceKind.CODING


async def test_jail_refuses_every_escape_and_counts_it(tmp_path) -> None:
    manager, bus = await _manager(tmp_path)
    session = manager.create("files")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    for bad in ("../../outside.txt", str(outside), "notes/../../../outside.txt", "../workspace"):
        with pytest.raises(WorkspaceError) as info:
            manager.resolve(session, bad, write=True)
        assert info.value.code == "jail_escape" and info.value.blocked_by_policy is True

    link = session.root / "sneaky"
    link.symlink_to(tmp_path)
    with pytest.raises(WorkspaceError) as info:
        manager.resolve(session, "sneaky/outside.txt", write=True)
    assert info.value.code == "jail_escape", "a symlink hop is still an escape"

    assert manager.stats["escapes"] == 5
    assert "workspace.blocked" in _kinds(bus)
    assert manager.health()["status"] == "degraded"
    assert outside.read_text(encoding="utf-8") == "secret", "nothing outside the jail was touched"


async def test_jail_refuses_reserved_and_empty_paths(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path)
    session = manager.create("files")
    with pytest.raises(WorkspaceError) as info:
        manager.write_text(session, "_checkpoints/x.txt", "no")
    assert info.value.code == "reserved_path"
    with pytest.raises(WorkspaceError) as info:
        manager.resolve(session, "   ")
    assert info.value.code == "empty_path"
    with pytest.raises(WorkspaceError) as info:
        manager.resolve(session, "x" * 2000)
    assert info.value.code == "path_length"


async def test_write_read_and_list_stay_inside_the_session(tmp_path) -> None:
    manager, bus = await _manager(tmp_path)
    session = manager.create("files")
    written = manager.write_text(session, "notes/page.txt", "hello star")
    assert written["ok"] is True and written["dry_run"] is False and written["bytes"] == 10
    assert (session.root / "notes" / "page.txt").read_text(encoding="utf-8") == "hello star"
    assert manager.scan(session) == {"files": 1, "bytes": 10}

    read = manager.read_text(session, "notes/page.txt")
    assert read["text"] == "hello star" and read["truncated"] is False
    assert manager.read_text(session, "notes/page.txt", limit=5)["truncated"] is True

    files = manager.list_files(session)
    assert [item["path"] for item in files] == ["notes/page.txt"] and files[0]["bytes"] == 10
    assert "workspace.write" in _kinds(bus)

    with pytest.raises(WorkspaceError) as info:
        manager.read_text(session, "missing.txt")
    assert info.value.code == "not_found"


async def test_quotas_and_the_per_write_cap_are_enforced(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path, _settings(tmp_path, workspace={"max_write_bytes": 100, "max_files": 2, "max_bytes": 150}))
    session = manager.create("files")
    with pytest.raises(WorkspaceError) as info:
        manager.write_text(session, "big.txt", "x" * 101)
    assert info.value.code == "write_cap"

    manager.write_text(session, "a.txt", "aaaa")
    manager.write_text(session, "b.txt", "bbbb")
    with pytest.raises(WorkspaceError) as info:
        manager.write_text(session, "c.txt", "cccc")
    assert info.value.code == "quota_files"
    assert manager.stats["refused"] >= 2


async def test_dry_run_records_the_intent_and_writes_nothing(tmp_path) -> None:
    manager, bus = await _manager(tmp_path, _settings(tmp_path, dry_run=True))
    session = manager.create("files")
    assert session.dry_run is True
    out = manager.write_text(session, "notes/a.txt", "hello")
    assert out["ok"] is True and out["dry_run"] is True
    assert "was not written" in out["note"]
    assert not (session.root / "notes" / "a.txt").exists()
    assert manager.stats["writes"] == 0, "a dry-run write is not a write"
    assert any(event.payload.get("dry_run") is True for event in bus.history(limit=50) if event.kind == "workspace.write")


async def test_session_budget_and_ttl_expiry(tmp_path) -> None:
    manager, bus = await _manager(tmp_path, _settings(tmp_path, workspace={"max_sessions": 2, "session_ttl_s": 3600}))
    first = manager.create("files")
    manager.create("browser")
    with pytest.raises(WorkspaceError) as info:
        manager.create("coding")
    assert info.value.code == "budget" and "close one first" in info.value.message
    assert manager.health()["status"] == "degraded"

    expired = manager.expire(now=datetime.now(timezone.utc) + timedelta(hours=2))
    assert len(expired) == 2 and first.session_id in expired
    assert first.state is WorkspaceState.EXPIRED and "workspace.expired" in _kinds(bus)
    assert manager.create("coding").kind is WorkspaceKind.CODING, "expired sessions free a slot"


async def test_disabled_workspace_refuses_to_create(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path, _settings(tmp_path, workspace={"enabled": False}))
    with pytest.raises(WorkspaceError) as info:
        manager.create("files")
    assert info.value.code == "disabled" and info.value.blocked_by_policy is True
    assert manager.health()["status"] == "degraded"


async def test_acquire_reuses_a_matching_session(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path)
    first = manager.acquire("computer", agent="computer", label="one")
    second = manager.acquire("computer", agent="computer", label="two")
    assert first.session_id == second.session_id and manager.stats["reused"] == 1
    other = manager.acquire("browser", agent="browser")
    assert other.session_id != first.session_id and other.kind is WorkspaceKind.BROWSER
    assert manager.find("computer").session_id == first.session_id
    assert manager.find(WorkspaceKind.SYSTEM) is None


async def test_closed_sessions_cannot_be_written_to(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path)
    session = manager.create("files")
    manager.close(session.session_id)
    assert session.state is WorkspaceState.CLOSED
    with pytest.raises(WorkspaceError) as info:
        manager.write_text(session, "a.txt", "x")
    assert info.value.code == "not_usable"
    with pytest.raises(WorkspaceError) as info:
        manager.checkpoint(session.session_id)
    assert info.value.code == "not_usable"


# ── checkpoints ───────────────────────────────────────────────────────────────


async def test_checkpoint_snapshots_files_and_suspends_the_session(tmp_path) -> None:
    manager, bus = await _manager(tmp_path)
    session = manager.create("coding")
    manager.write_text(session, "src/main.py", "print('hi')")
    manager.write_text(session, "notes.md", "# plan")

    out = manager.checkpoint(session.session_id, "before refactor")
    snapshot = out["checkpoint"]
    assert snapshot["files"] == 2 and snapshot["bytes"] > 0 and snapshot["truncated"] is False
    assert session.state is WorkspaceState.SUSPENDED
    store = session.root / "_checkpoints" / snapshot["checkpoint_id"]
    manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_id"] == session.session_id and len(manifest["entries"]) == 2
    assert all(entry["sha256_16"] for entry in manifest["entries"])
    assert sorted(entry["path"] for entry in manifest["entries"]) == ["notes.md", "src/main.py"]
    assert "workspace.checkpoint" in _kinds(bus)


async def test_restore_brings_back_the_snapshotted_content(tmp_path) -> None:
    manager, bus = await _manager(tmp_path)
    session = manager.create("coding")
    manager.write_text(session, "notes.md", "original")
    stamp = manager.checkpoint(session.session_id, "v1")["checkpoint"]["checkpoint_id"]
    manager.write_text(session, "notes.md", "edited by the agent")
    assert manager.read_text(session, "notes.md")["text"] == "edited by the agent"

    out = manager.restore(session.session_id, stamp)
    assert out["ok"] is True and out["restored"] == 1 and out["skipped"] == []
    assert manager.read_text(session, "notes.md")["text"] == "original"
    assert session.state is WorkspaceState.ACTIVE and "workspace.restored" in _kinds(bus)
    assert manager.restore(session.session_id)["ok"] is True, "no id ⇒ the newest checkpoint"


async def test_checkpoint_budget_evicts_the_oldest_snapshot(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path, _settings(tmp_path, workspace={"max_checkpoints": 2}))
    session = manager.create("files")
    manager.write_text(session, "a.txt", "1")
    ids = [manager.checkpoint(session.session_id, f"v{index}")["checkpoint"]["checkpoint_id"] for index in range(4)]
    assert len(set(ids)) == 4, "ids stay unique even inside one second"
    assert session.checkpoint_ids() == ids[-2:]
    store = session.root / "_checkpoints"
    assert sorted(item.name for item in store.iterdir()) == sorted(ids[-2:])
    assert any("dropped the oldest checkpoint" in note for note in session.notes)


async def test_checkpoint_caps_are_reported_not_hidden(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path, _settings(tmp_path, workspace={"checkpoint_max_bytes": 20}))
    session = manager.create("files")
    manager.write_text(session, "a.txt", "x" * 15)
    manager.write_text(session, "b.txt", "y" * 15)
    out = manager.checkpoint(session.session_id, "too big")["checkpoint"]
    assert out["truncated"] is True and out["bytes"] <= 20 and out["files"] == 1
    assert "truncated by the checkpoint cap" in out["note"]


async def test_restore_refuses_unknown_or_broken_checkpoints(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path)
    session = manager.create("files")
    with pytest.raises(WorkspaceError) as info:
        manager.restore(session.session_id, "ckpt_nope")
    assert info.value.code == "unknown_checkpoint" and "none" in info.value.message

    stamp = manager.checkpoint(session.session_id)["checkpoint"]["checkpoint_id"]
    (session.root / "_checkpoints" / stamp / "manifest.json").unlink()
    with pytest.raises(WorkspaceError) as info:
        manager.restore(session.session_id, stamp)
    assert info.value.code == "missing_manifest"

    with pytest.raises(WorkspaceError) as info:
        manager.checkpoint("ws_missing")
    assert info.value.code == "unknown_session"


# ── close / destroy ───────────────────────────────────────────────────────────


async def test_close_keeps_the_files_for_inspection(tmp_path) -> None:
    manager, bus = await _manager(tmp_path)
    session = manager.create("files")
    manager.write_text(session, "keep.txt", "evidence")
    out = manager.close(session.session_id)
    assert out["ok"] is True and out["state"] == "closed" and "files kept" in out["note"]
    assert (session.root / "keep.txt").is_file()
    assert "workspace.closed" in _kinds(bus)


async def test_destroy_in_dry_run_only_closes_the_session(tmp_path) -> None:
    manager, bus = await _manager(tmp_path, _settings(tmp_path, dry_run=True))
    session = manager.create("files")
    manager.write_text(session, "keep.txt", "evidence", dry_run=False)
    out = manager.close(session.session_id, destroy=True)
    assert out["dry_run"] is True and "were NOT deleted" in out["note"]
    assert session.state is WorkspaceState.CLOSED and (session.root / "keep.txt").is_file()
    assert manager.stats["destroyed"] == 0


async def test_destroy_removes_the_jail_and_audits_it(tmp_path) -> None:
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    manager, bus = await _manager(tmp_path, _settings(tmp_path), audit=audit)
    session = manager.create("files")
    manager.write_text(session, "notes/a.txt", "temp")
    root = session.root
    out = manager.close(session.session_id, destroy=True)
    assert out["destroyed"] is True and out["removed_files"] == 1 and out["removed_bytes"] == 4
    assert session.state is WorkspaceState.DESTROYED and not root.exists()
    assert manager.stats["destroyed"] == 1
    assert "workspace.destroyed" in _kinds(bus)
    entries = [entry for entry in audit.tail(limit=20) if entry["kind"] == "workspace.decision"]
    assert any(entry["tool"] == "workspace_destroy" and entry["decision"] == "executed" for entry in entries)


async def test_destroy_refuses_anything_that_is_not_a_session_directory(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path)
    session = manager.create("files")
    session.root = manager.root                     # defence in depth: never delete the root itself
    with pytest.raises(WorkspaceError) as info:
        manager.close(session.session_id, destroy=True)
    assert info.value.code == "jail_escape" and info.value.blocked_by_policy is True
    assert manager.root.exists()

    other = manager.create("browser")
    other.root = manager.root / "_profiles"         # a reserved sibling directory
    with pytest.raises(WorkspaceError) as info:
        manager.close(other.session_id, destroy=True)
    assert info.value.code == "reserved_path"


async def test_aclose_closes_sessions_but_keeps_their_files(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path)
    session = manager.create("files")
    manager.write_text(session, "keep.txt", "evidence")
    await manager.aclose()
    assert session.state is WorkspaceState.CLOSED and (session.root / "keep.txt").is_file()
    await manager.aclose()                      # idempotent
    assert session.state is WorkspaceState.CLOSED


async def test_spawn_needs_a_capable_backend(tmp_path) -> None:
    manager, _bus = await _manager(tmp_path, _settings(tmp_path, workspace={"isolation_backend": "null"}))
    session = manager.create("files")
    out = manager.spawn(session.session_id, [sys.executable, "-c", "pass"])
    assert out["ok"] is False and out["blocked_by_policy"] is True
    assert "cannot start processes" in out["error"]
    with pytest.raises(WorkspaceError):
        manager.spawn("ws_missing", ["true"])


def test_browser_profile_lives_inside_the_session(tmp_path) -> None:
    manager = WorkspaceManager(_settings(tmp_path))
    session = WorkspaceSession(root=tmp_path / "ws" / "ws_p", kind=WorkspaceKind.BROWSER)
    profile = manager.browser_profile(session)
    assert profile == session.root / "profile" and profile.is_dir()


# ── tools ─────────────────────────────────────────────────────────────────────


def _stack(tmp_path: Path, settings: Settings | None = None):
    cfg = settings or _settings(tmp_path)
    bus = StarEventBus()
    registry = StarToolRegistry(cfg, bus=bus, import_legacy=False)
    manager = build_workspace(cfg, bus=bus)
    toolkit = register_workspace_tools(registry, cfg, bus=bus, manager=manager)
    audit = AuditLog(cfg.security.audit_path, bus=bus)
    confirmations = ConfirmationStore(cfg, bus=bus)
    permissions = PermissionEngine(cfg, bus=bus, confirmations=confirmations)
    executor = ToolExecutor(
        cfg, registry=registry, permissions=permissions, audit=audit, confirmations=confirmations, bus=bus
    )
    return manager, toolkit, registry, executor, bus, audit, confirmations


def test_register_workspace_tools_adds_nine_typed_specs(tmp_path) -> None:
    manager, toolkit, registry, _executor, bus, _audit, _conf = _stack(tmp_path)
    assert len(registry) == 9
    for name in registry.names():
        spec = registry.get(name)
        assert spec.agent.value == "filesystem" and spec.category.value == "files"
        assert spec.origin == "star2" and "phase7" in spec.tags and spec.dry_run_safe is True
    assert registry.get("workspace_close").risk == "medium"
    assert registry.get("workspace_create").risk == "medium"
    assert registry.get("workspace_read").risk == "low"
    assert isinstance(toolkit, WorkspaceToolkit) and toolkit.manager is manager
    assert len([event for event in bus.history(limit=60) if event.kind == "tool.registered"]) == 9


def test_tool_handlers_report_refusals_honestly(tmp_path) -> None:
    _manager_, toolkit, registry, _executor, _bus, _audit, _conf = _stack(tmp_path)
    missing = registry.get("workspace_read").handler(path="a.txt")
    assert missing["success"] is False and "no background workspace session is open" in missing["error"]
    assert missing["result"]["code"] == "no_session" and toolkit.stats["no_session"] == 1

    created = registry.get("workspace_create").handler(kind="files", label="demo")
    assert created["success"] is True and created["result"]["kind"] == "files"
    session_id = created["session_id"]

    escaped = registry.get("workspace_write").handler(path="../../evil.txt", text="x", session_id=session_id)
    assert escaped["success"] is False and escaped["result"]["code"] == "jail_escape"
    assert escaped["result"]["blocked_by_policy"] is True

    unknown = registry.get("workspace_files").handler(session_id="ws_nope")
    assert unknown["success"] is False and unknown["result"]["code"] == "unknown_session"

    written = registry.get("workspace_write").handler(path="notes/a.txt", text="hello", session_id=session_id)
    assert written["success"] is True and written["path"] == "notes/a.txt"
    assert registry.get("workspace_read").handler(path="notes/a.txt", session_id=session_id)["text"] == "hello"
    assert registry.get("workspace_files").handler(session_id=session_id)["result"]["count"] == 1
    state = registry.get("workspace_state").handler()
    assert state["success"] is True and state["result"]["sessions_alive"] == 1
    listed = registry.get("workspace_list").handler()
    assert listed["result"]["count"] == 1 and toolkit.stats["ok"] >= 5


async def test_executor_simulates_workspace_writes_in_dry_run(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=True)
    manager, toolkit, registry, executor, _bus, audit, _conf = _stack(tmp_path, settings)
    await executor.startup()
    try:
        session = manager.create("files")
        result = await executor.call("workspace_write", {"path": "a.txt", "text": "hi", "session_id": session.session_id},
                                     session_id="s1", call_id="c1")
        assert result.decision == "simulated" and result.ok is True
        assert not (session.root / "a.txt").exists(), "dry-run must not create the file"
        entry = audit.tail(limit=1)[0]
        assert entry["tool"] == "workspace_write" and entry["dry_run"] is True
        assert entry["arguments"]["text"] == "hi", "the intent is recorded faithfully"
        assert toolkit.stats["calls"] == 0, "the handler was never reached"
    finally:
        await executor.aclose()


async def test_executor_writes_and_audits_a_real_workspace_call(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False)
    manager, _toolkit, registry, executor, _bus, audit, _conf = _stack(tmp_path, settings)
    await executor.startup()
    try:
        session = manager.create("files")
        result = await executor.call("workspace_write", {"path": "a.txt", "text": "hi", "session_id": session.session_id},
                                     session_id="s1", call_id="c1")
        assert result.decision == "executed" and result.ok is True
        assert (session.root / "a.txt").read_text(encoding="utf-8") == "hi"
        entry = audit.tail(limit=1)[0]
        assert entry["decision"] == "executed" and entry["dry_run"] is False and entry["persisted"] is True
    finally:
        await executor.aclose()


async def test_removing_files_needs_an_explicit_confirmation(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False)
    manager, _toolkit, registry, executor, _bus, _audit, confirmations = _stack(tmp_path, settings)
    await executor.startup()
    try:
        session = manager.create("files")
        manager.write_text(session, "a.txt", "temp")

        plain = await executor.call("workspace_close", {"session_id": session.session_id}, session_id="s1", call_id="c1")
        assert plain.decision == "executed" and plain.risk == "medium", "closing keeps the files"

        second = manager.create("browser")
        manager.write_text(second, "b.txt", "temp")
        risky = await executor.call("workspace_close", {"session_id": second.session_id, "remove_files": True},
                                    session_id="s1", call_id="c2")
        assert risky.decision == "needs_confirmation" and risky.risk == "high"
        assert risky.confirmation_id and (second.root / "b.txt").is_file(), "nothing is deleted before approval"

        approved = await executor.execute_confirmed(confirmations.get(risky.confirmation_id), approved=True)
        assert approved["decision"] == "executed" and approved["destroyed"] is True
        assert not second.root.exists()

        denied = manager.create("coding")
        refused = await executor.call("workspace_close", {"session_id": denied.session_id, "remove_files": True},
                                      session_id="s1", call_id="c3")
        outcome = await executor.execute_confirmed(confirmations.get(refused.confirmation_id), approved=False)
        assert outcome["decision"] == "denied" and denied.root.exists()
    finally:
        await executor.aclose()


# ── agents and application wiring ─────────────────────────────────────────────


async def test_agents_run_inside_their_own_background_session(tmp_path) -> None:
    from Backend.star.computer.agent import build_computer

    settings = _settings(tmp_path, dry_run=True)
    manager, _toolkit, registry, executor, bus, _audit, _conf = _stack(tmp_path, settings)
    agent = build_computer(settings, bus=bus, executor=executor, registry=registry, workspace=manager)
    await executor.startup()
    await agent.startup()
    try:
        run = await agent.run("click at 480,320", session_id="s1")
        assert run.state == "dry_run"
        workspace = run.result["workspace"]
        assert workspace["kind"] == "computer" and workspace["dry_run"] is True
        assert Path(workspace["path"]).is_dir()
        session = manager.get(workspace["session_id"])
        assert session is not None and session.agent == "computer" and session.root.is_dir()

        second = await agent.run("take a screenshot", session_id="s1")
        assert second.result["workspace"]["session_id"] == workspace["session_id"], "the session is reused"
        assert manager.stats["reused"] >= 1
    finally:
        await agent.aclose()
        await executor.aclose()


async def test_an_unavailable_workspace_never_stops_the_run(tmp_path) -> None:
    from Backend.star.computer.agent import build_computer

    class Broken:
        def acquire(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("workspace budget: 4 session(s) alive")

    settings = _settings(tmp_path, dry_run=True)
    manager, _toolkit, registry, executor, bus, _audit, _conf = _stack(tmp_path, settings)
    agent = build_computer(settings, bus=bus, executor=executor, registry=registry, workspace=Broken())
    await executor.startup()
    await agent.startup()
    try:
        run = await agent.run("take a screenshot", session_id="s1")
        assert run.state == "dry_run", "the run still happens"
        assert "workspace budget" in run.result["workspace_error"]
        assert "workspace" not in run.result
    finally:
        await agent.aclose()
        await executor.aclose()


async def test_application_exposes_the_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STAR_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("STAR_MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("STAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STAR_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("STAR_PATTERNS_PATH", str(tmp_path / "patterns.jsonl"))
    monkeypatch.setenv("STAR_CANDIDATES_PATH", str(tmp_path / "learning" / "candidates.jsonl"))
    monkeypatch.setenv("STAR_FEEDBACK_PATH", str(tmp_path / "learning" / "feedback.jsonl"))
    monkeypatch.setenv("STAR_EPISODES_PATH", str(tmp_path / "episodes.jsonl"))
    monkeypatch.setenv("STAR_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("STAR_DRY_RUN", "false")
    monkeypatch.setenv("STAR_COMPUTER_BACKEND", "null")

    from Backend.star.main import build_application

    app = build_application(Settings.from_env(load_env_file=False))
    await app.startup()
    try:
        assert "workspace" in app.capabilities()
        assert len(app.tool_registry) in (74, 79, 80, 128, 129), "legacy + workspace + memory + learning + orchestrator + security tools"
        state = app.workspace_state()
        assert state["ok"] is True and state["enabled"] is True and state["sessions_max"] == 4
        assert state["isolation"]["backend"] == "null" and state["workspaces"] == []

        created = await app.create_workspace("files", "invoice run")
        assert created["ok"] is True and created["decision"] == "executed"
        session_id = created["data"]["session_id"]
        assert app.workspaces()[0]["session_id"] == session_id

        detail = app.workspace_session(session_id)
        assert detail["ok"] is True and detail["kind"] == "files" and detail["files_list"] == []
        assert app.workspace_session("ws_nope")["ok"] is False

        run = await app.run_computer_goal("click at 10,10")
        assert run["result"]["workspace"]["kind"] == "computer"

        stamp = await app.checkpoint_workspace(session_id, "console")
        assert stamp["ok"] is True and stamp["data"]["checkpoint"]["checkpoint_id"]
        restored = await app.restore_workspace(session_id)
        assert restored["ok"] is True

        closed = await app.close_workspace(session_id)
        assert closed["ok"] is True and closed["decision"] == "executed"
        risky = await app.close_workspace(session_id, destroy=True)
        assert risky["decision"] == "needs_confirmation" and risky["confirmation_id"]
        approved = await app.resolve_confirmation(risky["confirmation_id"], approve=True)
        assert approved["execution"]["decision"] == "executed"
        assert app.workspace_session(session_id)["state"] == "destroyed"

        await app.emergency_stop(reason="test")
        stopped = await app.create_workspace("files")
        assert stopped["ok"] is False and stopped["stopped"] is True
        assert app.health()["checks"]["workspace"]["status"] == "ok"
    finally:
        await app.aclose()
