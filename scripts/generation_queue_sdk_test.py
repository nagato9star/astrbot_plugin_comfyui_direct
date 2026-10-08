"""Run with the real AstrBot SDK: lifecycle, tool schemas and shared session lock."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))


async def run(root):
    import astrbot.api  # noqa: F401 - load the real SDK before the test fixtures
    import generation_queue_test as cases
    from astrbot.core.utils.session_lock import session_lock_manager

    queue, context, client, _ = cases.make_queue(root / "locked")
    client.finished.set()
    try:
        async with session_lock_manager.acquire_lock(cases.event().unified_msg_origin):
            await queue.enqueue({}, cases.event())
            await cases.until(lambda: queue.jobs["C000001"]["state"] == "completed")
            await asyncio.sleep(0.04)
            assert not context.sent and not context.requests
        await cases.until(lambda: queue.jobs["C000001"]["delivery_state"] == "sent")
    finally:
        await queue.close()

    spec = importlib.util.spec_from_file_location(
        "queue_sdk_plugin", PLUGIN / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class PluginContext(cases.Context):
        def add_llm_tools(self, *tools):
            self.tools = tools

        def register_web_api(self, *args):
            pass

    context = PluginContext()
    with (
        patch.object(module.StarTools, "get_data_dir", return_value=root / "plugin"),
        patch.object(module.ComfyUIClient, "start_events"),
        patch.object(module.ComfyUIClient, "start_warmup"),
    ):
        plugin = module.ComfyUIDirectPlugin(context, {"image_cache_auto_clean": False})
        try:
            await plugin.initialize()
            assert (
                plugin._draw_tool.shared["generation_queue"] is plugin._generation_queue
            )
            job_tool = next(
                tool for tool in context.tools if tool.name == "comfyui_job"
            )
            assert job_tool.active and "task_id" in job_tool.parameters["properties"]
        finally:
            await plugin.terminate()
        assert plugin._generation_queue.closed and not plugin._generation_queue.tasks
    print(
        "real AstrBot SDK tool schemas / plugin initialization / session lock / termination OK"
    )


if __name__ == "__main__":
    previous_directory = Path.cwd()
    with tempfile.TemporaryDirectory() as directory:
        try:
            os.chdir(directory)  # Keep SDK initialization files out of the checkout.
            asyncio.run(run(Path(directory)))
        finally:
            os.chdir(previous_directory)
