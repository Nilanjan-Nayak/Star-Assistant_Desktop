"""System volume skill."""

from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field
from typing_extensions import Annotated

from agent.core.enums import OSFamily
from agent.core.ids import SkillName, parse_skill_name
from agent.geometry.monitor import CURRENT_OS
from agent.skills.base import Skill
from agent.skills.context import SkillContext
from agent.skills.registry import register_skill
from agent.skills.result import SkillResult


class VolumeParams(BaseModel):
    model_config = {"extra": "forbid"}
    level: Annotated[int, Field(ge=0, le=100)]


@register_skill
class VolumeSkill(Skill[VolumeParams]):
    name = parse_skill_name("volume")
    description = "Set system volume (0-100)"
    params_model = VolumeParams

    async def run(self, ctx: SkillContext, params: VolumeParams) -> SkillResult:
        if ctx.governor.config.dry_run:
            return SkillResult(
                ok=True, skill=self.name, data={"volume": params.level, "dry_run": True}
            )
        try:
            if CURRENT_OS is OSFamily.WINDOWS:
                from pycaw.pycaw import AudioUtilities

                dev = AudioUtilities.GetSpeakers()
                if hasattr(dev, "EndpointVolume"):
                    vol = dev.EndpointVolume
                else:
                    from ctypes import POINTER, cast as ccast
                    from comtypes import CLSCTX_ALL
                    from pycaw.pycaw import IAudioEndpointVolume
                    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    vol = ccast(iface, POINTER(IAudioEndpointVolume))
                vol.SetMasterVolumeLevelScalar(params.level / 100, None)
            elif CURRENT_OS is OSFamily.LINUX:
                subprocess.run(
                    ["amixer", "-D", "pulse", "sset", "Master", f"{params.level}%"],
                    check=True,
                    capture_output=True,
                )
            elif CURRENT_OS is OSFamily.MACOS:
                subprocess.run(
                    ["osascript", "-e", f"set volume output volume {params.level}"],
                    check=True,
                    capture_output=True,
                )
            else:
                return SkillResult(ok=False, skill=self.name, error="unsupported OS")
            return SkillResult(ok=True, skill=self.name, data={"volume": params.level})
        except Exception as exc:
            return SkillResult(ok=False, skill=self.name, error=str(exc))
