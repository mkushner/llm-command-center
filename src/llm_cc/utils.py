"""Shared utilities: async subprocess runner."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


async def async_run(
    cmd: str | list[str],
    *,
    cwd: str | Path | None = None,
    capture: bool = False,
    check: bool = True,
    shell: bool = False,
    timeout: float | None = 60.0,
) -> RunResult:
    """Run a subprocess asynchronously. Returns RunResult."""
    stdout_pipe = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
    stderr_pipe = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL

    if shell:
        if isinstance(cmd, list):
            cmd = " ".join(cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=stdout_pipe,
            stderr=stderr_pipe,
        )
    else:
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            stdout=stdout_pipe,
            stderr=stderr_pipe,
        )

    try:
        raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    result = RunResult(
        returncode=proc.returncode or 0,
        stdout=(raw_out or b"").decode(errors="replace") if capture else "",
        stderr=(raw_err or b"").decode(errors="replace") if capture else "",
    )

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
            stderr=result.stderr,
        )

    return result
