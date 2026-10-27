from __future__ import annotations

import asyncio


class _CompatEventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    """Keep legacy synchronous tests working after asyncio.run() closes a loop."""

    def get_event_loop(self):
        try:
            return super().get_event_loop()
        except RuntimeError:
            loop = self.new_event_loop()
            self.set_event_loop(loop)
            return loop


asyncio.set_event_loop_policy(_CompatEventLoopPolicy())
