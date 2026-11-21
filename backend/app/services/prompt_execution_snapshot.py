"""Persist registered prompt inputs without endpoint credentials or providers.

A snapshot freezes the logical route and system prompt for a stage's retry.
Endpoint configuration and provider-side routing remain runtime configuration.
"""
import math
from uuid import UUID


def validate_prompt_snapshot(value, task_type):
    fields = {"version", "task_type", "prompt_id", "endpoint_id", "model", "temperature",
              "max_tokens", "system_prompt", "mode", "preferred_tier"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("registered_prompt_snapshot_invalid")
    if type(value["version"]) is not int or value["version"] != 1 or value["task_type"] != task_type:
        raise ValueError("registered_prompt_snapshot_invalid")
    for key in ("prompt_id", "endpoint_id"):
        if not isinstance(value[key], str) or str(UUID(value[key])) != value[key]:
            raise ValueError("registered_prompt_snapshot_invalid")
    for key in ("model", "system_prompt", "mode"):
        if not isinstance(value[key], str):
            raise ValueError("registered_prompt_snapshot_invalid")
    if (not value["system_prompt"].strip() or value["mode"] not in {"text", "structured"}
            or type(value["max_tokens"]) is not int or value["max_tokens"] <= 0
            or type(value["temperature"]) not in (int, float)
            or not math.isfinite(value["temperature"])
            or value["preferred_tier"] is not None and not isinstance(value["preferred_tier"], str)):
        raise ValueError("registered_prompt_snapshot_invalid")
    return value


async def capture_prompt_snapshot(task_type, db):
    from app.services.prompt_registry import _resolve_route_and_tier_cached
    route, tier = await _resolve_route_and_tier_cached(task_type, db)
    value = dict(version=1, task_type=task_type, prompt_id=str(route.prompt_id),
                 endpoint_id=str(route.endpoint_id), model=route.model,
                 temperature=route.temperature, max_tokens=route.max_tokens,
                 system_prompt=route.system_prompt, mode=route.mode, preferred_tier=tier)
    return validate_prompt_snapshot(value, task_type)


def restore_prompt_snapshot(value, task_type):
    from app.services.prompt_registry import RouteSpec
    value = validate_prompt_snapshot(value, task_type)
    return RouteSpec(prompt_id=UUID(value["prompt_id"]), endpoint_id=UUID(value["endpoint_id"]),
                     model=value["model"], temperature=value["temperature"], max_tokens=value["max_tokens"],
                     system_prompt=value["system_prompt"], mode=value["mode"]), value["preferred_tier"]
