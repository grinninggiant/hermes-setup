from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from test_native_platform import adapter_mod


class TaskDumpTests(unittest.TestCase):
    def test_dump_only_on_request_and_shows_stuck_task(self):
        dump = adapter_mod._dump_asyncio_tasks_on_request

        async def stuck_here():
            await asyncio.Event().wait()

        async def main(db):
            task = asyncio.create_task(stuck_here(), name="stuck-final-send")
            await asyncio.sleep(0)
            self.assertIsNone(dump(db))
            Path(db).parent.joinpath("linear-task-dump.request").touch()
            out = dump(db)
            task.cancel()
            return out

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ledger.sqlite3")
            text = Path(asyncio.run(main(db))).read_text()
            self.assertIn("stuck-final-send", text)
            self.assertIn("stuck_here", text)
            self.assertFalse(Path(tmp, "linear-task-dump.request").exists())


if __name__ == "__main__":
    unittest.main()
