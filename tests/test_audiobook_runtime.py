import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from app.audiobook_runtime import SynthRuntime

SCRIPT = """
import json,sys,os,time
print(json.dumps({"ready":True}), flush=True)
for line in sys.stdin:
    job=json.loads(line)
    if job['voice']=='crash': sys.exit(1)
    if job['voice']=='hang': time.sleep(30)
    print(json.dumps({'ok':True,'pid':os.getpid()}), flush=True)
"""


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        script = Path(self.temp.name) / "synth.py"
        script.write_text(SCRIPT)
        self.runtime = SynthRuntime([sys.executable, "-u", str(script)], timeout=1, max_jobs=2)

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temp.cleanup()

    async def test_reuses_model_and_recycles_after_limit(self):
        first = await self.runtime.render("s", "d", "ok")
        second = await self.runtime.render("s", "d", "ok")
        third = await self.runtime.render("s", "d", "ok")
        self.assertEqual(first["pid"], second["pid"])
        self.assertNotEqual(second["pid"], third["pid"])

    async def test_timeout_kills_child_then_next_job_recovers(self):
        with self.assertRaises(TimeoutError):
            await self.runtime.render("s", "d", "hang")
        self.assertIsNone(self.runtime.proc)
        self.assertTrue((await self.runtime.render("s", "d", "ok"))["ok"])

    async def test_crash_and_cancel_do_not_poison_next_job(self):
        with self.assertRaises(json.JSONDecodeError):
            await self.runtime.render("s", "d", "crash")
        task = asyncio.create_task(self.runtime.render("s", "d", "hang"))
        await asyncio.sleep(0.1)
        proc = self.runtime.proc
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(proc.returncode)
        self.assertTrue((await self.runtime.render("s", "d", "ok"))["ok"])

    async def test_idle_releases_model(self):
        await self.runtime.render("s", "d", "ok")
        proc = self.runtime.proc
        self.runtime.last_used = 0
        await self.runtime.release_if_idle()
        self.assertIsNotNone(proc.returncode)
        self.assertIsNone(self.runtime.proc)
