"""The image route's settings.  Each is an environment variable with a default; all carry the prefix ESP_IMAGE_ROUTE_
but the inference profile, which keeps the name the stack already gives it (ESP_INFERENCE_PROFILE_ID).

    ESP_INFERENCE_PROFILE_ID         the profile called; extraction/bedrock.py's MODEL_ID.  Must start "us."
    ESP_IMAGE_ROUTE_SKILL_DIR        the transcribe-design-scan skill's folder; this repo's copy
    ESP_IMAGE_ROUTE_EFFORT           thinking effort; extraction/bedrock.py's EFFORT
    ESP_IMAGE_ROUTE_MAX_TOKENS       output limit of one call, thinking included; 32000
    ESP_IMAGE_ROUTE_CONCURRENCY      pages read at once; 4
    ESP_IMAGE_ROUTE_RETRIES          retries of one call after a throttle or a transient error; 5
    ESP_IMAGE_ROUTE_MAX_CROPS        crops one page may ask for, at most 19; 12
    ESP_IMAGE_ROUTE_DEADLINE_S       seconds from the document's start after which no call starts; none
    ESP_IMAGE_ROUTE_CACHE            1 for a cache point after the system prompt and after the page image; 0
    ESP_IMAGE_ROUTE_STREAM           1 to call converse_stream, 0 to call converse; 0
    ESP_IMAGE_ROUTE_READ_TIMEOUT_S   socket read timeout of one call; 900
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from extraction.bedrock import EFFORT, MODEL_ID

from .render import MAX_IMAGES

PREFIX = "ESP_IMAGE_ROUTE_"
PROFILE_ENV = "ESP_INFERENCE_PROFILE_ID"
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "transcribe-design-scan"


class SettingsError(ValueError):
    """A setting the route cannot run with; the message says which and why."""


def flag(value: str) -> bool:
    if value.strip().lower() not in ("0", "1", "true", "false", ""):
        raise ValueError(f"{value!r} is not 0 or 1")
    return value.strip().lower() in ("1", "true")


@dataclass(frozen=True)
class Settings:
    model_id: str = MODEL_ID
    skill_dir: Path = SKILL_DIR
    effort: str = EFFORT
    max_tokens: int = 32_000
    concurrency: int = 4
    retries: int = 5
    max_crops: int = 12
    deadline_s: float | None = None
    cache: bool = False
    stream: bool = False
    read_timeout_s: float = 900.0

    def __post_init__(self):
        if not self.model_id.startswith("us."):
            # The global profile is denied by organization policy; only a US profile is called.
            raise SettingsError(f"the inference profile {self.model_id!r} does not start 'us.'; only a US profile is "
                                f"called ({PROFILE_ENV})")
        for name in ("max_tokens", "concurrency", "max_crops"):
            if getattr(self, name) < 1:
                raise SettingsError(f"{name} must be at least 1, not {getattr(self, name)}")
        if self.max_crops >= MAX_IMAGES:
            raise SettingsError(f"max_crops must stay under {MAX_IMAGES}, the images one request may carry with the "
                                f"page's own, not {self.max_crops}")
        if self.retries < 0:
            raise SettingsError(f"retries cannot be negative, not {self.retries}")


# field -> (variable, how its text is read)
VARIABLES = {
    "model_id": (PROFILE_ENV, str),
    "skill_dir": (PREFIX + "SKILL_DIR", Path),
    "effort": (PREFIX + "EFFORT", str),
    "max_tokens": (PREFIX + "MAX_TOKENS", int),
    "concurrency": (PREFIX + "CONCURRENCY", int),
    "retries": (PREFIX + "RETRIES", int),
    "max_crops": (PREFIX + "MAX_CROPS", int),
    "deadline_s": (PREFIX + "DEADLINE_S", float),
    "cache": (PREFIX + "CACHE", flag),
    "stream": (PREFIX + "STREAM", flag),
    "read_timeout_s": (PREFIX + "READ_TIMEOUT_S", float),
}


def load(env=None) -> Settings:
    """The settings the environment gives; a variable unset or empty keeps its default."""
    env = os.environ if env is None else env
    given = {}
    for field, (variable, read) in VARIABLES.items():
        text = env.get(variable, "").strip()
        if text:
            try:
                given[field] = read(text)
            except ValueError as err:
                raise SettingsError(f"{variable}={text!r} cannot be read: {err}") from err
    return Settings(**given)
