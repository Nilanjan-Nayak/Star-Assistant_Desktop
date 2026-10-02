"""Phase 5 — browser layer tests: guardrails, extraction, tools, the agent, the registry.

Hermetic by design: every network test talks to a local ``http.server`` on an
ephemeral port with ``allow_private_hosts`` switched on for that test only. The
default configuration (public hosts only) is what the refusal tests assert.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from Backend.star.agents.base import AgentRegistry, AgentRunState, AgentStepPlan, StarAgent
from Backend.star.browser.agent import BrowserAgent, build_browser
from Backend.star.browser.extract import extract_page, fetch_url, read_page
from Backend.star.browser.guardrails import BrowserGuardrails, find_urls, is_private_name
from Backend.star.browser.tools import SEARCH_ENGINES, BrowserToolkit, register_browser_tools
from Backend.star.config.settings import BrowserSettings, PathsSettings, SecuritySettings, Settings
from Backend.star.observability.events import StarEventBus
from Backend.star.security.audit import AuditLog
from Backend.star.security.confirmations import ConfirmationStore
from Backend.star.tools.executor import ToolExecutor
from Backend.star.tools.permissions import PermissionEngine
from Backend.star.tools.registry import StarToolRegistry
from Backend.star.tools.spec import ToolSpec

PAGE = """<!doctype html>
<html lang="en">
  <head>
    <title>Star Test Page</title>
    <meta name="description" content="A tiny page used by the Phase 5 tests">
    <meta property="og:description" content="ignored because name=description won">
    <script>var hidden = "script text must never appear";</script>
    <style>.hidden { color: red } /* style text must never appear */</style>
  </head>
  <body>
    <h1>Hello Star</h1>
    <p>Bangla works too: স্টার অ্যাসিস্ট্যান্ট।</p>
    <h2>Section two</h2>
    <a href="/about">About</a>
    <a href="https://example.org/external">External</a>
    <a href="/about">About again</a>
    <a href="mailto:someone@example.com">Mail</a>
  </body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    """Serves a fixed page, a big page, a 404, a redirect and a nasty redirect."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        if self.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/big":
            self._send(200, b"x" * 400_000, "text/html; charset=utf-8")
        elif self.path == "/plain":
            self._send(200, "just text, no markup".encode("utf-8"), "text/plain; charset=utf-8")
        elif self.path == "/redirect":
            self._send(302, b"", "text/plain", extra={"Location": "/"})
        elif self.path == "/redirect-private":
            self._send(302, b"", "text/plain", extra={"Location": "http://169.254.169.254/latest/meta-data"})
        else:
            self._send(404, b"nope", "text/plain; charset=utf-8")

    def _send(self, code: int, body: bytes, content_type: str, *, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:      # keep pytest output readable
        return None


@pytest.fixture()
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="star-test-site")
    thread.start()
    port = int(server.server_address[1])
    try:
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _settings(tmp_path: Path, *, dry_run: bool = True, **browser: Any) -> Settings:
    return Settings(
        paths=PathsSettings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            workspace_root=tmp_path / "workspace",
            memory_db=tmp_path / "memory.db",
            patterns_path=tmp_path / "patterns.jsonl",
            episodes_path=tmp_path / "episodes.jsonl",
        ),
        security=SecuritySettings(dry_run=dry_run, audit_path=str(tmp_path / "audit.jsonl")),
        browser=BrowserSettings(**browser) if browser else BrowserSettings(),
    )


def _local_settings(tmp_path: Path, port: int, *, dry_run: bool = False, **browser: Any) -> Settings:
    browser.setdefault("allow_private_hosts", True)
    browser.setdefault("allowed_ports", (80, 443, port))
    return _settings(tmp_path, dry_run=dry_run, **browser)


def _stack(tmp_path: Path, settings: Settings, *, bus: StarEventBus | None = None):
    """Registry (browser tools only) + executor, wired exactly like main.py does."""
    bus = bus or StarEventBus()
    registry = StarToolRegistry(settings, bus=bus, import_legacy=False)
    toolkit = register_browser_tools(registry, settings, bus=bus)
    audit = AuditLog(settings.security.audit_path, bus=bus)
    confirmations = ConfirmationStore(settings, bus=bus)
    permissions = PermissionEngine(settings, bus=bus, confirmations=confirmations)
    executor = ToolExecutor(
        settings, registry=registry, permissions=permissions, audit=audit, confirmations=confirmations, bus=bus
    )
    agent = BrowserAgent(settings, bus=bus, executor=executor, registry=registry, toolkit=toolkit)
    return agent, executor, registry, toolkit, audit, bus


# ── guardrails ────────────────────────────────────────────────────────────────


def test_guardrails_allow_http_and_https_and_normalise_bare_hosts(tmp_path) -> None:
    guard = BrowserGuardrails(_settings(tmp_path))
    for url in ("https://example.com/a", "http://example.com", "example.com/page", "WWW.Example.COM"):
        verdict = guard.check_url(url)
        assert verdict.ok, url
        assert verdict.scheme in ("http", "https") and verdict.host.endswith("example.com")
    assert guard.check_url("example.com").url == "https://example.com"


def test_guardrails_refuse_dangerous_schemes(tmp_path) -> None:
    guard = BrowserGuardrails(_settings(tmp_path))
    for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,<b>x</b>", "ftp://host/x", "about:blank"):
        verdict = guard.check_url(url)
        assert verdict.ok is False and verdict.rule == "scheme", url
        assert "not allowed" in verdict.reason


def test_guardrails_refuse_embedded_credentials(tmp_path) -> None:
    verdict = BrowserGuardrails(_settings(tmp_path)).check_url("https://user:pass@example.com/")
    assert verdict.ok is False and verdict.rule == "userinfo"
    assert "credentials" in verdict.reason


def test_guardrails_refuse_private_loopback_and_metadata_addresses(tmp_path) -> None:
    guard = BrowserGuardrails(_settings(tmp_path))
    for url in ("http://127.0.0.1/x", "http://10.1.2.3/y", "http://192.168.0.5/", "http://169.254.169.254/latest"):
        verdict = guard.check_url(url)
        assert verdict.ok is False and verdict.rule == "private_host", url


def test_guardrails_refuse_private_hostnames_without_dns(tmp_path) -> None:
    guard = BrowserGuardrails(_settings(tmp_path))
    for url in ("http://localhost/x", "http://printer.local/", "https://metadata.google.internal/v1"):
        verdict = guard.check_url(url)
        assert verdict.ok is False and verdict.rule == "private_host", url
    assert is_private_name("localhost") and is_private_name("nas.lan") and not is_private_name("example.com")


def test_guardrails_allow_private_hosts_when_explicitly_enabled(tmp_path) -> None:
    settings = _settings(tmp_path, allow_private_hosts=True, allowed_ports=(80, 443, 8000))
    guard = BrowserGuardrails(settings)
    verdict = guard.check_url("http://127.0.0.1:8000/x")
    assert verdict.ok is True and verdict.risk == "medium", "an IP literal is a medium-risk target"
    assert guard.health()["status"] == "degraded", "allowing private hosts must show up in health"


def test_guardrails_dns_check_catches_a_public_name_pointing_at_loopback(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, resolve_hosts=True)
    guard = BrowserGuardrails(settings)

    def fake_getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    verdict = guard.check_url("https://innocent.example.com/")
    assert verdict.ok is False and verdict.rule == "dns"
    assert "127.0.0.1" in verdict.reason

    def broken_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", broken_getaddrinfo)
    assert guard.check_url("https://nowhere.example.com/").rule == "dns"


def test_guardrails_host_lists_and_ports(tmp_path) -> None:
    settings = _settings(tmp_path, host_denylist=("bad.example",), allowed_ports=(443,))
    guard = BrowserGuardrails(settings)
    assert guard.check_url("https://bad.example/").ok is False
    assert guard.check_url("https://sub.bad.example/").rule == "denylist"
    assert guard.check_url("https://good.example/").ok is True
    assert guard.check_url("https://good.example:8443/").rule == "port"

    strict = BrowserGuardrails(_settings(tmp_path, host_allowlist=("allowed.example",)))
    assert strict.check_url("https://allowed.example/x").ok is True
    assert strict.check_url("https://other.example/x").rule == "allowlist"


def test_guardrails_refuse_rubbish_and_huge_urls(tmp_path) -> None:
    guard = BrowserGuardrails(_settings(tmp_path))
    assert guard.check_url("").rule == "empty"
    assert guard.check_url("not a url at all").rule == "host"
    assert guard.check_url("https://example.com/" + "a" * 4000).rule == "length"
    assert guard.stats["checks"] == 3 and guard.stats["refused"] == 3


def test_guardrails_emit_safety_blocked_on_the_bus(tmp_path) -> None:
    bus = StarEventBus()
    guard = BrowserGuardrails(_settings(tmp_path), bus=bus)
    guard.check_url("https://example.com")
    guard.check_url("file:///etc/passwd")
    blocked = [event for event in bus.history(limit=20) if event.kind == "safety.blocked"]
    assert len(blocked) == 1
    assert blocked[0].payload["rule"] == "scheme" and blocked[0].payload["layer"] == "browser_guardrails"


def test_find_urls_handles_messy_voice_text() -> None:
    text = "open https://example.com/a, then www.foo.bar. also http://x.io/p?q=1 (twice https://example.com/a)"
    found = find_urls(text)
    assert found[0] == "https://example.com/a"
    assert "https://www.foo.bar" in found and "http://x.io/p?q=1" in found
    assert len(found) == len(set(found)), "duplicates must collapse"
    assert find_urls("no urls here, only words") == []


# ── extraction ────────────────────────────────────────────────────────────────


def test_extract_page_pulls_title_description_headings_text_and_links() -> None:
    page = extract_page(PAGE, "http://site.test/")
    assert page.title == "Star Test Page"
    assert page.description == "A tiny page used by the Phase 5 tests"
    assert page.headings[0] == "Hello Star" and "Section two" in page.headings
    assert "script text must never appear" not in page.text
    assert "style text must never appear" not in page.text
    assert "স্টার অ্যাসিস্ট্যান্ট" in page.text
    urls = [link.url for link in page.links]
    assert urls[0] == "http://site.test/about", "relative links must be absolutised"
    assert "https://example.org/external" in urls
    assert len(urls) == len(set(urls)), "duplicate hrefs collapse"
    assert all(not url.startswith("mailto:") for url in urls), "only http(s) links"
    assert page.word_count > 0 and page.error == ""


def test_extract_page_survives_broken_markup() -> None:
    page = extract_page("<html><body><p>unclosed <a href='/x'>link<title>T", "http://site.test/")
    assert page.error == "" and "unclosed" in page.text
    assert extract_page("", "http://site.test/").text == ""


def test_fetch_url_reads_a_local_page(site) -> None:
    base, _port = site
    result = fetch_url(f"{base}/", timeout_s=5.0, guard=lambda _url: True)
    assert result.ok is True and result.status == 200
    assert "Star Test Page" in result.html and result.bytes_read > 100
    assert result.truncated is False and result.elapsed_ms >= 0


def test_fetch_url_honours_the_byte_cap(site) -> None:
    base, _port = site
    result = fetch_url(f"{base}/big", timeout_s=5.0, max_bytes=4096, guard=lambda _url: True)
    assert result.ok is True and result.bytes_read <= 4096 and result.truncated is True


def test_fetch_url_reports_http_errors_without_raising(site) -> None:
    base, _port = site
    result = fetch_url(f"{base}/missing", timeout_s=5.0, guard=lambda _url: True)
    assert result.ok is False and result.status == 404 and "404" in result.error


def test_fetch_url_counts_redirects_and_re_checks_the_guard(site) -> None:
    base, _port = site
    followed = fetch_url(f"{base}/redirect", timeout_s=5.0, guard=lambda _url: True)
    assert followed.ok is True and followed.redirects == 1 and "Star Test Page" in followed.html

    blocked = fetch_url(f"{base}/redirect-private", timeout_s=5.0, guard=lambda url: "169.254" not in url)
    assert blocked.ok is False and "redirect" in blocked.error.lower()


def test_read_page_returns_a_text_observation(site) -> None:
    base, _port = site
    page = read_page(f"{base}/", timeout_s=5.0, max_chars=2000, guard=lambda _url: True)
    assert page.status == 200 and page.title == "Star Test Page"
    assert "Hello Star" in page.as_text(500)
    assert page.extras["content_type"].startswith("text/html")

    missing = read_page(f"{base}/missing", timeout_s=5.0, guard=lambda _url: True)
    assert missing.status == 404 and missing.error and missing.text == ""


# ── browser tools ─────────────────────────────────────────────────────────────


def test_register_browser_tools_adds_typed_specs(tmp_path) -> None:
    settings = _settings(tmp_path)
    bus = StarEventBus()
    registry = StarToolRegistry(settings, bus=bus, import_legacy=False)
    toolkit = register_browser_tools(registry, settings, bus=bus)

    assert len(registry) == 8
    names = set(registry.names())
    assert {"browser_open", "browser_read", "browser_links", "browser_search"} <= names
    for name in names:
        spec = registry.get(name)
        assert spec.agent.value == "browser" and spec.origin == "star2"
        assert spec.handler is not None and "phase5" in spec.tags
    assert registry.get("browser_click").risk == "high"
    assert registry.get("browser_click").dry_run_safe is False, "a click must never be faked"
    assert registry.get("browser_read").idempotent is True
    assert toolkit.stats["calls"] == 0
    registered = [event for event in bus.history(limit=50) if event.kind == "tool.registered"]
    assert len(registered) == 8, "one event per tool, emitted by the registry"


def test_browser_read_is_refused_by_the_guardrails_before_any_io(tmp_path) -> None:
    settings = _settings(tmp_path)
    registry = StarToolRegistry(settings, import_legacy=False)
    toolkit = register_browser_tools(registry, settings)

    out = registry.get("browser_read").handler("file:///etc/passwd")
    assert out["success"] is False and "guardrails" in out["error"]
    assert out["result"]["verdict"]["rule"] == "scheme"
    assert toolkit.stats["refused"] == 1


async def test_browser_read_really_reads_when_dry_run_is_off(tmp_path, site) -> None:
    base, port = site
    settings = _local_settings(tmp_path, port)
    _agent, executor, registry, _kit, audit, _bus = _stack(tmp_path, settings)
    await executor.startup()
    try:
        result = await executor.call("browser_read", {"url": f"{base}/"}, session_id="s1", call_id="c1")
        assert result.ok is True and result.decision == "executed"
        assert result.data["result"]["title"] == "Star Test Page"
        assert result.data["result"]["via"] == "star.extract"
        assert "Hello Star" in result.data["result"]["text"]
        entry = audit.tail(limit=1)[0]
        assert entry["tool"] == "browser_read" and entry["decision"] == "executed" and entry["persisted"] is True
    finally:
        await executor.aclose()


async def test_dry_run_never_touches_the_network(tmp_path, site, monkeypatch) -> None:
    base, port = site
    settings = _local_settings(tmp_path, port, dry_run=True)
    _agent, executor, _registry, _kit, _audit, _bus = _stack(tmp_path, settings)

    def explode(*_args, **_kwargs):
        raise AssertionError("dry-run must not perform network I/O")

    monkeypatch.setattr("Backend.star.browser.extract.fetch_url", explode)
    await executor.startup()
    try:
        result = await executor.call("browser_read", {"url": f"{base}/"}, session_id="s1")
        assert result.decision == "simulated" and result.ok is True
        assert result.data["simulated"] is True
    finally:
        await executor.aclose()


async def test_browser_click_needs_an_explicit_confirmation(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False)
    _agent, executor, _registry, _kit, _audit, bus = _stack(tmp_path, settings)
    await executor.startup()
    try:
        result = await executor.call("browser_click", {"selector": "Sign in"}, session_id="s1", call_id="c1")
        assert result.decision == "needs_confirmation" and result.ok is False
        assert result.confirmation_id
        pending = [event for event in bus.history(limit=50) if event.kind == "security.confirmation_requested"]
        assert pending and pending[0].payload["tool"] == "browser_click"
    finally:
        await executor.aclose()


def test_desktop_only_tools_report_honestly_when_the_legacy_tool_is_missing(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False)
    registry = StarToolRegistry(settings, import_legacy=False)
    toolkit = register_browser_tools(registry, settings)
    for tool, args in (("browser_click", {"selector": "x"}), ("browser_type", {"text": "hi"}), ("browser_snapshot", {}), ("browser_close_tab", {})):
        out = registry.get(tool).handler(**args)
        assert out["success"] is False and "not available here" in out["error"], tool
    assert toolkit.stats["unavailable"] == 4


def test_legacy_tools_are_reused_and_normalised_when_present(tmp_path) -> None:
    """The desktop implementation wins; its flat result shape is normalised."""
    settings = _settings(tmp_path, dry_run=False)
    registry = StarToolRegistry(settings, import_legacy=False)

    calls: list[tuple[Any, ...]] = []

    def legacy_read(url: str, max_chars: int = 1500) -> dict[str, Any]:
        calls.append((url, max_chars))
        return {
            "success": True,
            "url": url,
            "title": "Legacy Title",
            "content": "legacy body text",
            "total_length": 16,
            "message": "পেজ পড়া হয়েছে।",
        }

    registry.register(
        ToolSpec(name="web_read_page", description="legacy", risk="low", handler=legacy_read,
                 parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
    )
    toolkit = register_browser_tools(registry, settings)
    out = registry.get("browser_read").handler("https://example.com/x", 0)

    assert calls == [("https://example.com/x", settings.browser.max_chars)]
    assert out["success"] is True
    assert out["result"]["via"] == "web_read_page"
    assert out["result"]["text"] == "legacy body text", "legacy 'content' becomes 'text'"
    assert out["result"]["text_chars"] == 16
    assert out["message"] == "পেজ পড়া হয়েছে।"
    assert toolkit.stats["legacy"] == 1 and toolkit.stats["fallback"] == 0


def test_browser_search_builds_a_url_and_respects_auto_open(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, auto_open=False)
    registry = StarToolRegistry(settings, import_legacy=False)
    register_browser_tools(registry, settings)

    opened: list[str] = []
    monkeypatch.setattr("Backend.star.browser.tools.webbrowser.open", lambda url, new=0: opened.append(url) or True)

    out = registry.get("browser_search").handler("rust async book", "google", False)
    assert out["success"] is True and opened == []
    assert out["result"]["url"] == "https://www.google.com/search?q=rust+async+book"
    assert out["result"]["opened"] is False

    out2 = registry.get("browser_search").handler("কলকাতা", "wikipedia", True)
    assert out2["result"]["opened"] is True and len(opened) == 1
    assert registry.get("browser_search").handler("", "google", False)["success"] is False
    assert set(SEARCH_ENGINES) >= {"google", "duckduckgo", "wikipedia", "youtube"}


def test_browser_open_uses_webbrowser_when_no_desktop_tool_exists(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, dry_run=False)
    registry = StarToolRegistry(settings, import_legacy=False)
    register_browser_tools(registry, settings)
    opened: list[str] = []
    monkeypatch.setattr("Backend.star.browser.tools.webbrowser.open", lambda url, new=0: opened.append(url) or True)

    out = registry.get("browser_open").handler("example.com/hello")
    assert out["success"] is True and opened == ["https://example.com/hello"]
    assert out["result"]["via"] == "webbrowser" and out["result"]["host"] == "example.com"


# ── the agent: planning ───────────────────────────────────────────────────────


def test_agent_selection_scores_english_banglish_and_bengali(tmp_path) -> None:
    agent = BrowserAgent(_settings(tmp_path))
    assert agent.name.value == "browser"
    for goal in ("open example.com", "read the page", "search rust book", "ওয়েবসাইটটা খুলে পড়ো", "browse koro"):
        assert agent.can_handle(goal), goal
    assert agent.score_goal("what is 2+2") == 0.0
    assert agent.can_handle("lock the computer") is False


def test_agent_plans_open_then_read_for_a_url_goal(tmp_path) -> None:
    agent = BrowserAgent(_settings(tmp_path))
    steps = agent.plan("open https://example.com and tell me what it says")
    assert [step.action for step in steps] == ["open", "read"]
    assert steps[0].tool == "browser_open" and steps[1].tool == "browser_read"
    assert steps[0].optional is True, "a window is a nicety — reading is the point"
    assert steps[1].expect


def test_agent_plans_read_only_links_and_search_shapes(tmp_path) -> None:
    agent = BrowserAgent(_settings(tmp_path))
    assert [s.action for s in agent.plan("read https://en.wikipedia.org/wiki/Star")] == ["read"]
    assert [s.action for s in agent.plan("links on example.com")] == ["links"]
    assert [s.action for s in agent.plan("search rust async book")] == ["search"]
    assert agent.plan("wikipedia search কলকাতা")[0].arguments["engine"] == "wikipedia"
    assert agent.plan("what is 2+2") == (), "not a browser goal ⇒ no plan"
    assert agent.plan("") == ()


def test_agent_routes_a_forbidden_url_to_the_guardrails(tmp_path) -> None:
    agent = BrowserAgent(_settings(tmp_path))
    steps = agent.plan("open file:///etc/passwd please")
    assert len(steps) == 1 and steps[0].tool == "browser_open"
    assert steps[0].arguments["url"].startswith("file://"), "must be refused explicitly, never re-read as a search"


def test_agent_plan_is_honest_and_the_budget_is_applied_at_run_time(tmp_path) -> None:
    agent = BrowserAgent(_settings(tmp_path, max_steps=1))
    assert agent.max_steps == 1
    # the plan says what the goal really needs; run() is what enforces the budget
    assert [step.action for step in agent.plan("open https://example.com and read it and list links")] == [
        "open",
        "read",
        "links",
    ]


# ── the agent: running ────────────────────────────────────────────────────────


async def test_agent_run_is_simulated_and_honest_in_dry_run(tmp_path, site) -> None:
    base, port = site
    settings = _local_settings(tmp_path, port, dry_run=True)
    agent, executor, _registry, _kit, _audit, bus = _stack(tmp_path, settings)
    await executor.startup()
    try:
        run = await agent.run(f"read {base}/", session_id="s1")
        assert run.state == AgentRunState.DRY_RUN and run.dry_run is True
        assert all(step.decision == "simulated" for step in run.steps)
        assert "simulated" in run.steps[0].verification_note
        assert "dry-run" in run.summary
        kinds = [event.kind for event in bus.history(limit=50)]
        assert "agent.started" in kinds and "agent.step" in kinds and "agent.completed" in kinds
    finally:
        await executor.aclose()


async def test_agent_run_really_reads_a_page(tmp_path, site) -> None:
    base, port = site
    settings = _local_settings(tmp_path, port)
    agent, executor, _registry, _kit, _audit, _bus = _stack(tmp_path, settings)
    await executor.startup()
    try:
        run = await agent.run(f"open {base}/ and read it", session_id="s1")
        assert run.state == AgentRunState.DONE and run.succeeded is True
        assert run.steps_used == 2
        assert all(step.verified or step.recovered for step in run.steps), "every step either held or was recovered"
        assert run.result["title"] == "Star Test Page"
        assert "Hello Star" in run.result["text"]
        assert run.result["url"].startswith(base)
        assert "Star Test Page" in run.summary
        assert agent.last_run() is run and len(agent.runs) == 1
    finally:
        await executor.aclose()


async def test_agent_run_is_blocked_by_the_guardrails(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False)
    agent, executor, _registry, _kit, _audit, bus = _stack(tmp_path, settings)
    await executor.startup()
    try:
        run = await agent.run("open file:///etc/passwd", session_id="s1")
        assert run.state == AgentRunState.BLOCKED and run.succeeded is False
        assert "guardrails" in run.error and "scheme" in run.summary
        assert run.steps[0].observation["blocked_by_policy"] is True
        assert run.steps[0].observation["block_rule"] == "scheme"
        assert "agent.failed" in [event.kind for event in bus.history(limit=50)]
    finally:
        await executor.aclose()


async def test_agent_run_stops_when_the_emergency_stop_is_active(tmp_path) -> None:
    settings = _settings(tmp_path)
    agent, executor, _registry, _kit, _audit, _bus = _stack(tmp_path, settings)
    agent._stop_gate = lambda: True
    await executor.startup()
    try:
        run = await agent.run("read https://example.com")
        assert run.state == AgentRunState.BLOCKED and run.steps == []
        assert "emergency stop" in run.error
        assert agent.stats["blocked"] == 1
    finally:
        await executor.aclose()


async def test_agent_recovers_from_a_window_it_cannot_open(tmp_path, site, monkeypatch) -> None:
    base, port = site
    settings = _local_settings(tmp_path, port, auto_open=True)     # the open step is no longer optional
    agent, executor, registry, _kit, _audit, _bus = _stack(tmp_path, settings)
    monkeypatch.setattr("Backend.star.browser.tools.webbrowser.open", lambda *_a, **_k: False)
    await executor.startup()
    try:
        run = await agent.run(f"open {base}/ and read it", session_id="s1")
        open_step = run.steps[0]
        assert open_step.action == "open" and open_step.recovered is True
        assert "read" in open_step.recovery_note
        assert run.state in (AgentRunState.DONE, AgentRunState.DRY_RUN)
        assert agent.stats["recovered"] == 1
        assert registry.get("browser_read") is not None
    finally:
        await executor.aclose()


async def test_agent_reports_budget_exhaustion(tmp_path, site) -> None:
    base, port = site
    settings = _local_settings(tmp_path, port, max_steps=1)
    agent, executor, _registry, _kit, _audit, _bus = _stack(tmp_path, settings)
    await executor.startup()
    try:
        run = await agent.run(f"open {base}/ and read it and list the links")
        assert run.state == AgentRunState.BUDGET_EXHAUSTED
        assert run.steps_used == 1 and run.budget == 1
    finally:
        await executor.aclose()


async def test_agent_run_without_an_executor_says_so(tmp_path) -> None:
    agent = BrowserAgent(_settings(tmp_path))
    run = await agent.run("read https://example.com")
    assert run.state == AgentRunState.FAILED
    assert "no tool executor" in (run.error or "")
    assert agent.health()["status"] == "degraded"


def test_agent_describe_includes_guardrails_and_stats(tmp_path) -> None:
    settings = _settings(tmp_path)
    registry = StarToolRegistry(settings, import_legacy=False)
    toolkit = register_browser_tools(registry, settings)
    agent = BrowserAgent(settings, registry=registry, toolkit=toolkit)
    described = agent.describe()
    assert described["name"] == "browser" and len(described["tools"]) == 8
    assert described["guardrails"]["allowed_schemes"] == ["http", "https"]
    assert described["last_run"] is None
    assert agent.health()["status"] == "degraded", "no executor ⇒ the agent cannot act"


# ── agent spine + registry ────────────────────────────────────────────────────


class _EchoAgent(StarAgent):
    name = "conversation"  # type: ignore[assignment]
    description = "test double"
    keywords = ("echo",)

    def plan(self, goal):
        return [AgentStepPlan(action="echo", tool="demo_tool", arguments={"goal": goal}, expect="an echo")]

    def observe(self, step_plan, outcome, context):
        return {"echoed": step_plan.arguments.get("goal", ""), "ok": bool(outcome.get("ok"))}


def test_agent_registry_registers_dispatches_and_describes(tmp_path) -> None:
    from Backend.star.brain.schemas import AgentName

    settings = _settings(tmp_path)
    echo = _EchoAgent(settings)
    echo.name = AgentName.CONVERSATION
    browser = BrowserAgent(settings)
    registry = AgentRegistry([echo, browser])

    assert len(registry) == 2 and "browser" in registry and AgentName.BROWSER in registry
    assert registry.get("browser") is browser and registry.get(AgentName.BROWSER) is browser
    assert registry.dispatch("echo this back") is echo
    assert registry.dispatch("open example.com and read it") is browser
    assert registry.dispatch("what is 2+2") is None
    with pytest.raises(ValueError):
        registry.register(BrowserAgent(settings))
    assert registry.register(BrowserAgent(settings), replace=True) is not None
    described = registry.describe()
    assert {item["name"] for item in described} == {"conversation", "browser"}
    assert registry.health()["detail"]["agents"] == 2
    assert AgentRegistry().health()["status"] == "pending"


async def test_agent_registry_startup_and_close_are_safe(tmp_path) -> None:
    registry = AgentRegistry([BrowserAgent(_settings(tmp_path))])
    await registry.startup()
    await registry.aclose()
    assert registry.runs(limit=5) == []


# ── application wiring ────────────────────────────────────────────────────────


def test_build_browser_factory_wires_tools_and_guardrails(tmp_path) -> None:
    settings = _settings(tmp_path)
    registry = StarToolRegistry(settings, import_legacy=False)
    bus = StarEventBus()
    agent = build_browser(settings, bus=bus, registry=registry, executor=None, stop_gate=lambda: False)
    assert isinstance(agent, BrowserAgent) and agent.toolkit is not None
    assert len(registry) == 8
    assert agent.settings is settings

    disabled = build_browser(_settings(tmp_path, enabled=False), registry=StarToolRegistry(settings, import_legacy=False))
    assert disabled.toolkit is not None


async def test_application_exposes_the_browser_agent(tmp_path, site, monkeypatch) -> None:
    base, port = site
    monkeypatch.setenv("STAR_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("STAR_MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("STAR_EPISODES_PATH", str(tmp_path / "episodes.jsonl"))
    monkeypatch.setenv("STAR_PATTERNS_PATH", str(tmp_path / "patterns.jsonl"))
    monkeypatch.setenv("STAR_CANDIDATES_PATH", str(tmp_path / "learning" / "candidates.jsonl"))
    monkeypatch.setenv("STAR_FEEDBACK_PATH", str(tmp_path / "learning" / "feedback.jsonl"))
    monkeypatch.setenv("STAR_BROWSER_ALLOW_PRIVATE", "true")
    monkeypatch.setenv("STAR_BROWSER_PORTS", f"80,443,{port}")
    monkeypatch.setenv("STAR_BROWSER_MAX_STEPS", "3")

    from Backend.star.config.settings import Settings as RootSettings
    from Backend.star.main import build_application

    settings = RootSettings.from_env(load_env_file=False)
    app = build_application(settings)
    await app.startup()
    try:
        assert "browser" in app.capabilities() and "agents" in app.capabilities()
        assert app.health()["checks"]["browser"]["status"] == "ok"
        assert app.health()["status"] == "ok"
        state = app.browser_state()
        assert state["ok"] is True and state["enabled"] is True
        assert state["settings"]["allow_private_hosts"] is True
        # the worker registry only grows (Phase 6 added the computer agent)
        assert "browser" in {agent["name"] for agent in app.agents()}

        run = await app.run_browser_goal(f"read {base}/", session_id="app-test", dry_run=False)
        assert run["ok"] is True and run["state"] == "done"
        assert run["result"]["title"] == "Star Test Page"
        assert app.browser_state()["last_run"]["run_id"] == run["run_id"]

        refused = await app.run_browser_goal("open file:///etc/passwd", dry_run=False)
        assert refused["ok"] is False and refused["state"] == "blocked"
        assert (await app.run_browser_goal("   "))["error"] == "field 'goal' is required"
    finally:
        await app.aclose()
