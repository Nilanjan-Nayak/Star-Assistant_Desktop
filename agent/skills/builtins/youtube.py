from __future__ import annotations

import asyncio
import subprocess

from pydantic import BaseModel, Field, StrictStr
from typing_extensions import Annotated

from agent.core.enums import ElementRole, OSFamily
from agent.core.errors import PerceptionError
from agent.core.ids import parse_skill_name
from agent.geometry.monitor import CURRENT_OS
from agent.motor.spec import hotkey, press, type_text
from agent.perception.query import PerceptionQuery
from agent.skills.base import Skill
from agent.skills.context import SkillContext
from agent.skills.registry import register_skill
from agent.skills.result import SkillResult
from agent.motor.spec import click as click_at


class YouTubeParams(BaseModel):
    model_config = {"extra": "forbid"}
    query: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    prefer_playwright: bool = True


@register_skill
class YouTubeSkill(Skill[YouTubeParams]):
    name = parse_skill_name("youtube")
    description = "Search and play a YouTube video"
    params_model = YouTubeParams

    async def run(self, ctx: SkillContext, params: YouTubeParams) -> SkillResult:
        try:
            from playwright.async_api import async_playwright  # noqa: F401

            has_pw = True
        except ImportError:
            has_pw = False

        if ctx.governor.config.dry_run:
            return SkillResult(
                ok=True, skill=self.name, data={"query": params.query, "dry_run": True}
            )
        if params.prefer_playwright and has_pw:
            return await self._via_playwright(params.query)
        return await self._via_screen(ctx, params.query)

    async def _via_playwright(self, query: str) -> SkillResult:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=False)
            try:
                page = await browser.new_page()
                await page.goto("https://www.youtube.com", wait_until="domcontentloaded")
                await page.wait_for_selector("input#search", timeout=15_000)
                await page.fill("input#search", query)
                await page.press("input#search", "Enter")
                await page.wait_for_selector("ytd-video-renderer", timeout=15_000)
                first = page.locator("ytd-video-renderer").first
                title = await first.locator("#video-title").inner_text()
                await first.click()
                await asyncio.sleep(2)
                return SkillResult(
                    ok=True, skill=self.name, data={"query": query, "title": title}
                )
            finally:
                await browser.close()

    async def _via_screen(self, ctx: SkillContext, query: str) -> SkillResult:
        if CURRENT_OS is OSFamily.WINDOWS:
            await ctx.act(hotkey("win"))
            await asyncio.sleep(0.5)
            await ctx.act(type_text("chrome"))
            await ctx.act(press("enter"))
        elif CURRENT_OS is OSFamily.MACOS:
            await ctx.act(hotkey("command", "space"))
            await asyncio.sleep(0.4)
            await ctx.act(type_text("chrome"))
            await ctx.act(press("enter"))
        else:
            subprocess.Popen(["xdg-open", "https://youtube.com"])
        await asyncio.sleep(2.5)

        await ctx.act(hotkey("ctrl", "l"))
        await asyncio.sleep(0.3)
        await ctx.act(type_text("youtube.com"))
        await ctx.act(press("enter"))
        await asyncio.sleep(2.0)

        snap = ctx.world.observe()
        try:
            els = await ctx.perception.find(
                snap.raster,
                PerceptionQuery(text="Search", role_hint=ElementRole.TEXTBOX),
                screen_hash=snap.content_hash,
            )
            await ctx.act(click_at(els[0].center.x, els[0].center.y))
        except PerceptionError:
            await ctx.act(press("/"))

        await ctx.act(type_text(query))
        await ctx.act(press("enter"))
        await asyncio.sleep(2.0)

        snap = ctx.world.observe()
        try:
            els = await ctx.perception.find(
                snap.raster,
                PerceptionQuery(
                    text="first video thumbnail",
                    role_hint=ElementRole.IMAGE,
                    min_confidence=0.4,
                ),
                screen_hash=snap.content_hash,
            )
            await ctx.act(click_at(els[0].center.x, els[0].center.y))
            return SkillResult(ok=True, skill=self.name, data={"query": query})
        except PerceptionError as exc:
            return SkillResult(
                ok=False,
                skill=self.name,
                error="could not find first video",
                data=exc.to_dict(),
            )
