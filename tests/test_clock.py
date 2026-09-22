from datetime import datetime

from app.core.clock import FakeClock


def test_fake_advance_moves_both():
    c = FakeClock()
    w0, m0 = c.wall(), c.mono()
    c.advance(90)
    assert c.wall() - w0 == 90
    assert c.mono() - m0 == 90


def test_fake_wall_jump_leaves_mono():
    c = FakeClock()
    m0 = c.mono()
    c.jump_wall(3600)
    assert c.mono() == m0
    assert c.wall() - (c.local_now().timestamp() - 0) == 0  # consistent


def test_fake_set_wall_monday_default():
    c = FakeClock()
    assert c.local_now().weekday() == 0  # Monday 2026-09-21
    c.set_wall(datetime(2026, 9, 22, 0, 30))
    assert c.local_now().day == 22
