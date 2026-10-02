"""Phase 4 — security layer tests: audit trail, confirmations, security surface."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from Backend.star.config.settings import PathsSettings, SecuritySettings, Settings
from Backend.star.observability.events import StarEventBus
from Backend.star.security.audit import AuditLog, fingerprint
from Backend.star.security.confirmations import Confirmation, ConfirmationStore
from Backend.star.security.surface import SecuritySurface, build_security
from Backend.star.tools.executor import ToolExecutor
from Backend.star.tools.permissions import PermissionEngine
from Backend.star.tools.registry import StarToolRegistry
from Backend.star.tools.spec import ToolSpec


def _settings(tmp_path: Path, **security: Any) -> Settings:
    return Settings(
        paths=PathsSettings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            workspace_root=tmp_path / "workspace",
            memory_db=tmp_path / "memory.db",
            patterns_path=tmp_path / "patterns.jsonl",
            episodes_path=tmp_path / "episodes.jsonl",
        ),
        security=SecuritySettings(**security) if security else SecuritySettings(),
    )


def _kinds(bus: StarEventBus, limit: int = 200) -> list[str]:
    return [event.kind for event in bus.history(limit=limit)]


def _events(bus: StarEventBus, kind: str, limit: int = 200) -> list[Any]:
    return [event for event in bus.history(limit=limit) if event.kind == kind]


# ── fingerprints ──────────────────────────────────────────────────────────────


def test_fingerprint_is_stable_and_argument_sensitive() -> None:
    base = fingerprint("set_volume", {"level": 40}, "s1")
    assert base == fingerprint("set_volume", {"level": 40}, "s1")
    assert base != fingerprint("set_volume", {"level": 41}, "s1")
    assert base != fingerprint("set_volume", {"level": 40}, "s2")
    assert base != fingerprint("set_brightness", {"level": 40}, "s1")
    assert fingerprint("tool", {"a": 1, "b": 2}) == fingerprint("tool", {"b": 2, "a": 1}), "key order must not matter"
    assert len(base) == 32


# ── audit log ─────────────────────────────────────────────────────────────────


def test_audit_records_are_stamped_redacted_and_persisted(tmp_path) -> None:
    bus = StarEventBus()
    audit = AuditLog(tmp_path / "audit.jsonl", bus=bus)
    entry = audit.record(
        {
            "tool": "search_web",
            "decision": "executed",
            "ok": True,
            "risk": "low",
            "session_id": "s1",
            "request_id": "r1",
            "arguments": {"query": "star assistant", "api_key": "sk-ABCDEFGHIJKLMNOPQRSTUVWX", "token": "abc=secretvalue"},
            "duration_ms": 12.5,
            "error": None,
        }
    )
    assert entry["seq"] == 1 and entry["kind"] == "tool.decision" and entry["ts"]
    assert entry["persisted"] is True
    assert "error" not in entry, "None values are dropped"
    assert entry["arguments"]["api_key"] == "[REDACTED]"
    assert "secretvalue" not in json.dumps(entry)
    assert entry["arguments_fingerprint"] == fingerprint("search_web", entry["arguments"], "s1")

    written = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(written) == 1
    assert json.loads(written[0])["tool"] == "search_web"
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in written[0]

    event = _events(bus, "audit.recorded")[0]
    assert event.payload["entry_kind"] == "tool.decision" and event.payload["decision"] == "executed"
    assert audit.counts == {"executed": 1}


def test_audit_truncates_huge_arguments(tmp_path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    entry = audit.record({"tool": "write_file", "decision": "executed", "arguments": {"text": "x" * 5000, "n": 1}})
    assert entry["arguments"]["text"].endswith("...<5000 chars>")
    assert len(entry["arguments"]["text"]) < 500
    many = audit.record({"tool": "t", "decision": "executed", "arguments": {f"key{i}": i for i in range(40)}})
    assert many["arguments"]["..."] == "16 more"


def test_audit_tail_query_and_file_reading(tmp_path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl", buffer=3)
    for index in range(5):
        audit.record(
            {
                "tool": "set_volume" if index % 2 else "see_screen",
                "decision": "executed" if index < 4 else "denied",
                "ok": index < 4,
                "risk": "medium",
                "session_id": "s1" if index < 3 else "s2",
            }
        )
    assert len(audit.tail(limit=10)) == 3, "the ring buffer is capped"
    assert audit.tail(limit=1)[0]["seq"] == 5
    assert len(audit.query(tool="set_volume")) >= 1
    assert all(item["session_id"] == "s1" for item in audit.query(session_id="s1"))
    assert [item["seq"] for item in audit.query(decision="denied")] == [5]
    assert audit.query(since_seq=4)[0]["seq"] == 5
    assert len(audit.read_file(limit=10)) == 5, "the file keeps everything"
    assert audit.describe()["entries"] == 5 and audit.describe()["exists"] is True
    assert audit.health()["status"] == "ok"


def test_audit_tolerates_a_corrupt_or_missing_file(tmp_path) -> None:
    missing = AuditLog(tmp_path / "nope.jsonl")
    assert missing.read_file() == []
    assert missing.describe()["exists"] is False and missing.describe()["size_bytes"] == 0

    path = tmp_path / "audit.jsonl"
    path.write_text('{"tool": "a"}\ngarbage\n{"tool": "b"}\n', encoding="utf-8")
    audit = AuditLog(path)
    assert [item["tool"] for item in audit.read_file()] == ["a", "b"]


def test_audit_never_raises_when_it_cannot_write(tmp_path) -> None:
    blocked = AuditLog(tmp_path)          # a directory, not a file
    entry = blocked.record({"tool": "x", "decision": "denied"})
    assert entry["persisted"] is False
    assert blocked.tail(limit=1)[0]["tool"] == "x", "the in-memory trail still works"
    assert blocked.health()["status"] == "degraded"


def test_audit_custom_kind_and_rotation(tmp_path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    entry = audit.record({"tool": "*", "decision": "emergency_stop"}, kind="security.stop")
    assert entry["kind"] == "security.stop"
    assert audit.query(tool="*")[0]["decision"] == "emergency_stop"


# ── confirmations ─────────────────────────────────────────────────────────────


def test_confirmation_lifecycle(tmp_path) -> None:
    bus = StarEventBus()
    store = ConfirmationStore(_settings(tmp_path), bus=bus, ttl_s=60)
    confirmation = store.request(
        tool="lock_workstation",
        arguments={"delay": 0},
        risk="high",
        session_id="s1",
        request_id="r1",
        task_id="t1",
        call_id="c1",
    )
    assert confirmation.confirmation_id.startswith("conf_")
    assert confirmation.state == "pending" and confirmation.expires_at > confirmation.created_at
    public = confirmation.public()
    assert public["tool"] == "lock_workstation" and public["risk"] == "high"
    assert 0 < public["ttl_remaining_s"] <= 60
    assert public["result"] == {}
    assert store.pending() == [public]
    assert store.get(confirmation.confirmation_id) is confirmation
    assert store.get("conf_missing") is None
    assert store.approved_token("lock_workstation", {"delay": 0}, "s1") is None

    resolved = store.resolve(confirmation.confirmation_id, approve=True, note="user said yes")
    assert resolved["ok"] is True and resolved["state"] == "approved"
    assert resolved["confirmation"]["note"] == "user said yes"
    assert store.approved_token("lock_workstation", {"delay": 0}, "s1") == confirmation.confirmation_id
    assert store.pending() == []
    assert store.resolve(confirmation.confirmation_id, approve=False)["ok"] is False
    assert store.resolve("conf_missing", approve=True) == {"ok": False, "error": "unknown confirmation 'conf_missing'"}

    kinds = _kinds(bus)
    assert "security.confirmation_requested" in kinds and "security.confirmation_resolved" in kinds
    assert store.stats["requested"] == 1 and store.stats["approved"] == 1


def test_confirmation_reuse_and_distinct_calls(tmp_path) -> None:
    store = ConfirmationStore(ttl_s=60)
    first = store.request(tool="lock_workstation", arguments={}, session_id="s1")
    same = store.request(tool="lock_workstation", arguments={}, session_id="s1")
    assert same.confirmation_id == first.confirmation_id
    assert store.stats["reused"] == 1

    other_args = store.request(tool="lock_workstation", arguments={"delay": 5}, session_id="s1")
    assert other_args.confirmation_id != first.confirmation_id
    other_session = store.request(tool="lock_workstation", arguments={}, session_id="s2")
    assert other_session.confirmation_id != first.confirmation_id
    assert len(store.pending()) == 3


def test_confirmation_expiry(tmp_path) -> None:
    store = ConfirmationStore(ttl_s=0.01)
    confirmation = store.request(tool="shutdown_now", arguments={}, risk="critical")
    time.sleep(0.03)
    assert store.get(confirmation.confirmation_id).state == "expired"
    assert store.pending() == []
    assert store.stats["expired"] == 1
    assert store.approved_token("shutdown_now", {}) is None
    assert "security.confirmation_expired" in _kinds(store.bus)
    assert store.resolve(confirmation.confirmation_id, approve=True)["ok"] is False


def test_confirmation_denial_is_not_an_approval(tmp_path) -> None:
    store = ConfirmationStore(ttl_s=60)
    confirmation = store.request(tool="lock_workstation", arguments={})
    result = store.resolve(confirmation.confirmation_id, approve=False, note="not now")
    assert result["state"] == "denied"
    assert store.approved_token("lock_workstation", {}) is None
    assert store.stats["denied"] == 1


def test_mark_approved_does_not_fire_the_callback(tmp_path) -> None:
    fired: list[str] = []
    store = ConfirmationStore(ttl_s=60, on_resolve=lambda item, approve: fired.append(item.tool))
    confirmation = store.request(tool="lock_workstation", arguments={})
    assert store.mark_approved(confirmation.confirmation_id, note="via executor")["state"] == "approved"
    assert fired == [], "mark_approved must not recurse into the executor"
    assert store.mark_approved(confirmation.confirmation_id)["state"] == "approved"
    assert store.mark_approved("conf_missing")["ok"] is False
    resolved = store.resolve(confirmation.confirmation_id, approve=True)
    assert resolved["ok"] is False, "already approved"

    store2 = ConfirmationStore(ttl_s=60, on_resolve=lambda item, approve: {"ran": item.tool})
    second = store2.request(tool="reboot", arguments={})
    payload = store2.resolve(second.confirmation_id, approve=True)
    assert payload["execution"] == {"ran": "reboot"}
    denied = store2.request(tool="wipe", arguments={})
    assert "execution" not in store2.resolve(denied.confirmation_id, approve=False)


def test_confirmation_store_describe_and_custom_ids(tmp_path) -> None:
    store = ConfirmationStore(_settings(tmp_path), ttl_s=45, id_factory=lambda tool: f"conf-{tool}")
    confirmation = store.request(tool="lock_workstation", arguments={})
    assert confirmation.confirmation_id == "conf-lock_workstation"
    described = store.describe()
    assert described["ttl_s"] == 45 and described["pending"] == 1 and described["tracked"] == 1
    assert described["has_callback"] is False
    assert store.health()["status"] == "ok"
    assert isinstance(confirmation, Confirmation)


# ── security surface ──────────────────────────────────────────────────────────


def _surface(tmp_path: Path, *, dry_run: bool = False, **security: Any):
    bus = StarEventBus()
    settings = _settings(tmp_path, dry_run=dry_run, **security)
    registry = StarToolRegistry(settings, bus=bus, import_legacy=False)
    registry.register(
        ToolSpec(
            name="lock_workstation",
            description="Lock the PC",
            risk="high",
            handler=lambda **kwargs: {"success": True, "result": {"locked": True}},
        )
    )
    registry.register(
        ToolSpec(name="format_disk", description="Destroy everything", risk="critical", handler=lambda **kw: {"success": True})
    )
    audit = AuditLog(tmp_path / "audit.jsonl", bus=bus)
    confirmations = ConfirmationStore(settings, bus=bus)
    permissions = PermissionEngine(settings, bus=bus, confirmations=confirmations)
    executor = ToolExecutor(
        settings, registry=registry, permissions=permissions, audit=audit, confirmations=confirmations, bus=bus
    )
    surface = SecuritySurface(
        settings, bus=bus, audit=audit, confirmations=confirmations, permissions=permissions, executor=executor
    )
    return surface, executor, bus


async def test_surface_startup_and_mute(tmp_path) -> None:
    surface, executor, bus = _surface(tmp_path)
    await surface.startup()
    event = _events(bus, "security.ready")[0]
    assert event.payload["dry_run"] is False and event.payload["confirm_above_risk"] == "high"
    assert event.payload["audit_path"].endswith("audit.jsonl")

    assert surface.stopped is False
    stopped = surface.mute(reason="test")
    assert stopped["stopped"] is True and surface.stopped is True
    decision = surface.permissions.check(executor.registry.get("lock_workstation"), {})
    assert decision.decision.value == "deny" and "emergency stop" in decision.reason
    assert surface.health()["status"] == "degraded"
    assert "emergency stop active" in surface.health()["detail"]["problems"]

    resumed = surface.unmute()
    assert resumed["stopped"] is False and surface.stopped is False
    assert "emergency stop active" not in surface.health()["detail"]["problems"]
    assert surface.health()["status"] == "degraded", "this fixture runs with dry-run off"
    assert "dry-run disabled" in surface.health()["detail"]["problems"][0]
    assert [entry["kind"] for entry in surface.audit.query(tool="*")] == ["security.stop", "security.resume"]
    assert any(entry["decision"] == "emergency_stop" for entry in surface.audit.read_file(limit=20))
    await surface.aclose()


async def test_surface_resolve_runs_the_approved_action(tmp_path) -> None:
    surface, executor, bus = _surface(tmp_path)
    await surface.startup()

    waiting = await executor.call("lock_workstation", {}, session_id="s1", request_id="r1", task_id="t1")
    assert waiting.decision == "needs_confirmation"
    pending = surface.pending()
    assert len(pending) == 1 and pending[0]["tool"] == "lock_workstation"

    resolved = await surface.resolve(pending[0]["confirmation_id"], approve=True, note="go ahead")
    assert resolved["ok"] is True and resolved["state"] == "approved"
    execution = resolved["execution"]
    assert execution["success"] is True and execution["decision"] == "executed" and execution["dry_run"] is False
    assert execution["result"] == {"locked": True}
    assert surface.pending() == []
    assert "security.confirmation_resolved" in _kinds(bus)

    denied_call = await executor.call("lock_workstation", {"delay": 9}, session_id="s1")
    assert denied_call.decision == "needs_confirmation"
    refused = await surface.resolve(denied_call.confirmation_id, approve=False, note="nope")
    assert refused["ok"] is True and refused["state"] == "denied" and "execution" not in refused

    unknown = await surface.resolve("conf_nope", approve=True)
    assert unknown["ok"] is False and "unknown confirmation" in unknown["error"]


async def test_surface_blocks_critical_actions_outright(tmp_path) -> None:
    surface, executor, _ = _surface(tmp_path)
    result = await executor.call("format_disk", {}, session_id="s1")
    assert result.decision == "denied" and result.ok is False
    assert surface.pending() == [], "a denied action never becomes a confirmation"
    assert surface.audit_tail(limit=1)[0]["decision"] == "denied"


async def test_surface_audit_queries_and_description(tmp_path) -> None:
    surface, executor, _ = _surface(tmp_path)
    await executor.call("lock_workstation", {}, session_id="s1")
    await executor.call("lock_workstation", {}, session_id="s2")
    assert len(surface.audit_tail(limit=10)) == 2
    assert len(surface.audit_query(session_id="s1")) == 1
    assert len(surface.audit_query(decision="needs_confirmation")) == 2

    described = surface.describe()
    assert described["phase"] in (4, 11) and described["dry_run"] is False
    assert described["permissions"]["confirm_above_risk"] == "high"
    assert described["confirmations"]["pending"] == 2
    assert described["audit"]["entries"] == 2
    assert described["executor"] == "tool-executor"

    live = _surface(tmp_path / "live", dry_run=True)[0]
    assert live.health()["status"] == "ok"
    assert live.describe()["dry_run"] is True


async def test_surface_survives_a_broken_executor(tmp_path) -> None:
    surface, executor, _ = _surface(tmp_path)
    waiting = await executor.call("lock_workstation", {}, session_id="s1")

    async def broken(confirmation: Confirmation, approved: bool = True) -> dict[str, Any]:
        raise RuntimeError("executor down")

    executor.execute_confirmed = broken      # type: ignore[method-assign]
    resolved = await surface.resolve(waiting.confirmation_id, approve=True)
    assert resolved["ok"] is True and resolved["execution"]["ok"] is False
    assert "executor down" in resolved["execution"]["error"]


def test_build_security_factory(tmp_path) -> None:
    settings = _settings(tmp_path)
    surface = build_security(settings)
    assert isinstance(surface, SecuritySurface)
    assert isinstance(surface.audit, AuditLog) and isinstance(surface.confirmations, ConfirmationStore)
    assert isinstance(surface.permissions, PermissionEngine)
    assert surface.executor is None
    assert surface.pending() == []
    assert surface.describe()["phase"] in (4, 11)


async def test_surface_without_an_executor_still_resolves(tmp_path) -> None:
    settings = _settings(tmp_path)
    surface = build_security(settings)
    confirmation = surface.confirmations.request(tool="lock_workstation", arguments={}, risk="high")
    resolved = await surface.resolve(confirmation.confirmation_id, approve=True)
    assert resolved["ok"] is True and "execution" not in resolved


# ── Phase 11: Secrets, Identity, Budgets, Policy & Tools ──────────────────────


def test_secret_vault_and_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    from Backend.star.security import SecretVault

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890abcdefghijklmnopqrstuvwxyz")
    vault = SecretVault()
    assert vault.has_secret("OPENAI_API_KEY") is True
    assert vault.has_secret("NONEXISTENT_KEY") is False

    # In-memory registration
    vault.register_secret("CUSTOM_SECRET", "super-secret-val")
    assert vault.get_secret("CUSTOM_SECRET") == "super-secret-val"

    # Public dict must never reveal real values
    pub = vault.public_dict()
    assert pub["OPENAI_API_KEY"] == "<set>"
    assert pub["CUSTOM_SECRET"] == "<set>"
    assert "sk-test" not in str(pub)
    assert "super-secret" not in str(pub)


def test_secret_scanner_detection_and_redaction() -> None:
    from Backend.star.security import SecretScanner, contains_secrets, redact_secrets

    scanner = SecretScanner()
    text = "Here is my key: sk-abcdefghijklmnopqrstuvwxyz123456 and bearer token: Bearer my-secret-token-value-12345"
    matches = scanner.scan_text(text)
    assert len(matches) >= 2
    assert any(m[0] == "openai_key" for m in matches)
    assert any(m[0] == "bearer_token" for m in matches)

    redacted = scanner.redact_text(text)
    assert "sk-abcdef" not in redacted
    assert "[REDACTED:OPENAI_KEY]" in redacted
    assert "[REDACTED:BEARER_TOKEN]" in redacted

    # Nested dictionary scanning
    payload = {
        "user": "Alice",
        "nested": {
            "api_key": "raw-key-value",
            "notes": "safe text",
        },
        "tokens": ["sk-123456789012345678901234", "Bearer secret-token-abcdef123456"],
    }
    has_secret, kinds = scanner.scan_payload(payload)
    assert has_secret is True
    assert any("api_key" in k for k in kinds)

    clean = scanner.redact_payload(payload)
    assert clean["nested"]["api_key"] == "[REDACTED]"
    assert "raw-key-value" not in str(clean)

    assert contains_secrets(payload) is True
    assert contains_secrets({"safe": "clean text"}) is False


def test_operator_identity_roles_and_owner_privileges() -> None:
    from Backend.star.security import OperatorIdentity, Role

    settings = Settings(owner="Nilanjan")
    identity = OperatorIdentity(settings)

    # Owner detection
    assert identity.is_owner("Nilanjan") is True
    assert identity.is_owner("nilanjan") is True
    assert identity.is_owner("guest") is False
    assert identity.get_role("Nilanjan") is Role.OWNER
    assert identity.get_role("guest") is Role.GUEST

    # Default without identifier is owner on local desktop
    assert identity.is_owner("") is True
    assert identity.get_role("") is Role.OWNER

    # Token registration
    identity.register_token("tok_guest_123", Role.GUEST)
    identity.register_token("tok_admin_456", Role.OWNER)
    assert identity.get_role(token="tok_guest_123") is Role.GUEST
    assert identity.get_role(token="tok_admin_456") is Role.OWNER

    # Role capability tiers
    assert identity.can_execute_tier(Role.OWNER, "critical") is True
    assert identity.can_execute_tier(Role.OPERATOR, "high") is True
    assert identity.can_execute_tier(Role.GUEST, "medium") is False
    assert identity.can_execute_tier(Role.GUEST, "low") is True

    # Confirmation resolution permissions
    assert identity.can_resolve_confirmation(Role.OWNER, "high") is True
    assert identity.can_resolve_confirmation(Role.GUEST, "high") is False


def test_security_budget_tracking_and_rate_limits() -> None:
    from Backend.star.security import SecurityBudget

    bus = StarEventBus()
    settings = Settings(
        security=SecuritySettings(max_actions_per_session=5, max_actions_per_minute=3)
    )
    budget = SecurityBudget(settings, bus=bus)

    # Normal consumption
    for _ in range(3):
        allowed, _ = budget.consume("session_1")
        assert allowed is True

    # 4th action exceeds per-minute rate limit
    allowed, reason = budget.consume("session_1")
    assert allowed is False
    assert "rate limit" in reason

    # Reset
    budget.reset("session_1")
    usage = budget.usage("session_1")
    assert usage["session_actions"] == 0


def test_policy_engine_safety_level_strict_and_paranoid() -> None:
    from Backend.star.security import SecurityPolicyEngine
    from Backend.star.tools.spec import ToolSpec

    # Normal safety
    normal_settings = Settings(security=SecuritySettings(safety_level="normal"))
    policy_normal = SecurityPolicyEngine(normal_settings)

    spec_medium = ToolSpec(name="set_volume", risk="medium", handler=lambda: {})
    dec_normal = policy_normal.evaluate(spec_medium, {"level": 50}, dry_run=False)
    assert dec_normal.decision.value == "allow"

    # Strict safety level escalates medium to confirmation
    strict_settings = Settings(security=SecuritySettings(safety_level="strict"))
    policy_strict = SecurityPolicyEngine(strict_settings)
    dec_strict = policy_strict.evaluate(spec_medium, {"level": 50}, dry_run=False)
    assert dec_strict.decision.value == "needs_confirmation"

    # Paranoid safety level denies high/critical and forces dry-run
    paranoid_settings = Settings(security=SecuritySettings(safety_level="paranoid"))
    policy_paranoid = SecurityPolicyEngine(paranoid_settings)
    spec_high = ToolSpec(name="shutdown_system", risk="high", handler=lambda: {})
    dec_paranoid = policy_paranoid.evaluate(spec_high, {}, dry_run=False)
    assert dec_paranoid.decision.value == "deny"


def test_policy_engine_refuses_secret_leak_in_arguments() -> None:
    from Backend.star.security import SecurityPolicyEngine
    from Backend.star.tools.spec import ToolSpec

    bus = StarEventBus()
    settings = Settings(security=SecuritySettings(safety_level="normal"))
    policy = SecurityPolicyEngine(settings, bus=bus)

    external_tool = ToolSpec(name="browser_open", risk="low", handler=lambda **kw: {})
    leaky_args = {"url": "https://api.example.com", "token": "sk-12345678901234567890123456"}

    dec = policy.evaluate(external_tool, leaky_args)
    assert dec.decision.value == "deny"
    assert "credentials" in dec.reason

    events = bus.history(kinds=frozenset({"security.secret_detected"}))
    assert len(events) >= 1


@pytest.mark.asyncio
async def test_security_tools_registration_and_execution(tmp_path: Path) -> None:
    from Backend.star.security import register_security_tools

    settings = _settings(tmp_path)
    registry = StarToolRegistry(settings, import_legacy=False)
    surface = build_security(settings)
    specs = register_security_tools(registry, settings, security=surface)

    assert len(specs) == 5
    names = {s.name for s in specs}
    assert "security_status" in names
    assert "security_audit_query" in names
    assert "security_emergency_stop" in names
    assert "security_resume" in names
    assert "security_scan_secrets" in names

    # Run status tool
    status_tool = registry.get("security_status")
    status_res = status_tool.handler()
    assert status_res["ok"] is True
    assert status_res["name"] == "security"

    # Run emergency stop tool
    stop_tool = registry.get("security_emergency_stop")
    stop_res = stop_tool.handler(reason="unit_test")
    assert stop_res["ok"] is True
    assert stop_res["stopped"] is True
    assert surface.stopped is True

    # Run resume tool
    resume_tool = registry.get("security_resume")
    resume_res = resume_tool.handler()
    assert resume_res["ok"] is True
    assert resume_res["stopped"] is False
    assert surface.stopped is False

    # Run secrets scanner tool
    scan_tool = registry.get("security_scan_secrets")
    scan_res = scan_tool.handler(text="secret: sk-abcdefghijklmnopqrstuvwxyz12345")
    assert scan_res["ok"] is True
    assert scan_res["has_secrets"] is True
    assert "[REDACTED" in scan_res["redacted_text"]
