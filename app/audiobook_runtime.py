"""One reusable inference subprocess, bounded by timeout, age and memory."""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("audiobook-runtime")


class SynthRuntime:
    def __init__(self, command=None, timeout=None, max_jobs=None):
        self.command = command or [
            os.getenv("AUDIOBOOK_SYNTH_PYTHON", sys.executable),
            "-m",
            "app.audiobook_synthesize",
            "--serve",
        ]
        self.timeout = timeout or int(os.getenv("AUDIOBOOK_RENDER_TIMEOUT", "3600"))
        self.max_jobs = max_jobs or int(os.getenv("AUDIOBOOK_RECYCLE_CHAPTERS", "20"))
        self.proc = None
        self.jobs = 0
        self.last_used = 0.0
        self._lock = asyncio.Lock()

    async def close(self):
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        self.jobs = 0

    def memory_exceeded(self):
        if not self.proc:
            return False
        try:
            # Linux RSS, including ONNX allocations; leave room for encoding/API.
            pages = int(Path(f"/proc/{self.proc.pid}/statm").read_text().split()[1])
            limit = int(os.getenv("AUDIOBOOK_RECYCLE_RSS_MB", "1800")) * 1024 * 1024
            return pages * os.sysconf("SC_PAGE_SIZE") > limit
        except (OSError, ValueError, IndexError):
            return False

    async def release_if_idle(self):
        if self.proc and time.monotonic() - self.last_used > 600:
            async with self._lock:
                await self.close()

    async def render(self, source, destination, voice):
        async with self._lock:
            try:
                if self.jobs >= self.max_jobs or self.memory_exceeded():
                    await self.close()
                async with asyncio.timeout(self.timeout):
                    if self.proc is None or self.proc.returncode is not None:
                        await self.close()
                        self.proc = await asyncio.create_subprocess_exec(
                            *self.command,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            # Inherit stderr: no unbounded pipe/log buffer in parent.
                        )
                        ready = json.loads(await self.proc.stdout.readline())
                        if ready.get("ready") is not True:
                            raise RuntimeError("Inference process failed to initialize")
                        log.info("Model loaded in %.2fs", ready.get("loadSeconds", 0))
                    self.proc.stdin.write(
                        (
                            json.dumps(
                                {
                                    "source": str(source),
                                    "destination": str(destination),
                                    "voice": voice,
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    await self.proc.stdin.drain()
                    result = json.loads(await self.proc.stdout.readline())
                    if result.get("ok") is not True:
                        raise RuntimeError("Inference process failed")
                    self.jobs += 1
                    self.last_used = time.monotonic()
                    log.info(
                        "Chapter synthesized in %.2fs (warm session chapter %s)",
                        result.get("renderSeconds", 0),
                        self.jobs,
                    )
                    return result
            except BaseException:
                await self.close()
                raise
