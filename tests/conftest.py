from __future__ import annotations

import pytest

from bafang_cal.simulator import ManualClock, SimulatedController, SimulatorConfig


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def controller(clock: ManualClock) -> SimulatedController:
    return SimulatedController(clock, SimulatorConfig())
