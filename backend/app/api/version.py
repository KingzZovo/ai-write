"""Version / build info endpoint.

Reads primarily from env vars stamped by the Dockerfile at build time
(`GIT_SHA`, `GIT_TAG`, `BUILD_TIME`). Falls back to /build-info/git_sha if
available. In dev (with host volume overriding /app) env vars remain the
authoritative source.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["version"])

_BUILD_INFO_FILE = Path("/build-info/git_sha")

# Application semantic version. Bumped per release.
APP_VERSION = "1.2.0"


def _read_build_info_file() -> dict[str, str]:
    if not _BUILD_INFO_FILE.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in _BUILD_INFO_FILE.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    except OSError:
        return {}
    return out


@router.get("/version")
def get_version() -> dict[str, str]:
    file_info = _read_build_info_file()
    git_sha = os.environ.get("GIT_SHA") or file_info.get("git_sha") or "unknown"
    git_tag = os.environ.get("GIT_TAG") or file_info.get("git_tag") or f"v{APP_VERSION}"
    build_time = (
        os.environ.get("BUILD_TIME") or file_info.get("build_time") or "unknown"
    )
    return {
        "version": APP_VERSION,
        "git_sha": git_sha,
        "git_tag": git_tag,
        "build_time": build_time,
    }
