import asyncio
import signal

import pytest

from jimbo.core.runner import ESCALATE_WITHIN, make_sigint_handler


class FakeSession:
    def __init__(self, running):
        self.is_running, self.cancelled = running, 0

    def cancel(self):
        self.cancelled += 1


async def test_ctrl_c_with_nothing_running_quits():
    s = FakeSession(running=False)
    handler = make_sigint_handler(s, asyncio.get_running_loop())
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)
    await asyncio.sleep(0)
    assert s.cancelled == 0


async def test_ctrl_c_cancels_a_running_run():
    s = FakeSession(running=True)
    handler = make_sigint_handler(s, asyncio.get_running_loop())
    handler(signal.SIGINT, None)  # does not raise
    await asyncio.sleep(0)
    assert s.cancelled == 1


async def test_second_ctrl_c_right_after_a_cancel_quits_but_a_later_one_cancels_again():
    now = [100.0]
    s = FakeSession(running=True)  # the cancel did not stop the run
    handler = make_sigint_handler(s, asyncio.get_running_loop(), clock=lambda: now[0])
    handler(signal.SIGINT, None)
    now[0] += ESCALATE_WITHIN / 2
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)
    now[0] += ESCALATE_WITHIN * 2
    handler(signal.SIGINT, None)
    await asyncio.sleep(0)
    assert s.cancelled == 2
