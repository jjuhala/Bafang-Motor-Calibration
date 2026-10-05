from __future__ import annotations

import pytest

from bafang_cal.codes import (
    CALIBRATION_CODES,
    STATUS_CODES,
    CalibrationState,
    calibration_state,
    describe_calibration_status,
    describe_status,
    display_code,
)


@pytest.mark.parametrize(("code", "text"), [(0x01, "01"), (0x08, "08"), (0x21, "21"), (0x30, "30")])
def test_display_code_matches_display_digits(code: int, text: str) -> None:
    assert display_code(code) == text


def test_describe_known_and_unknown_codes() -> None:
    assert describe_status(0x21) == "21: speed sensor fault"
    assert describe_status(0x01) == "01: normal operation"
    assert describe_status(0xEE) == "EE: unknown code"


def test_table_codes_are_bytes() -> None:
    assert all(0 <= code <= 0xFF for code in STATUS_CODES)


@pytest.mark.parametrize(
    ("code", "state"),
    [
        (0x01, CalibrationState.NOT_STARTED),
        (0x08, CalibrationState.NOT_STARTED),
        (0x40, CalibrationState.IN_PROGRESS),
        (0x41, CalibrationState.IN_PROGRESS),
        (0x43, CalibrationState.IN_PROGRESS),
        (0x95, CalibrationState.IN_PROGRESS),
        (0x98, CalibrationState.IN_PROGRESS),
        (0x44, CalibrationState.DONE),
        (0x51, CalibrationState.FAILED),
        (0x53, CalibrationState.FAILED),
        (0x63, CalibrationState.FAILED),
        (0x73, CalibrationState.FAILED),
        (0x84, CalibrationState.FAILED),
        (0x85, CalibrationState.FAILED),
    ],
)
def test_calibration_states_from_bafang_instructions(code: int, state: CalibrationState) -> None:
    assert calibration_state(code) is state


def test_calibration_codes_mean_something_else_while_riding() -> None:
    # 0x44 is a battery fault on the riding screen but "finished" while calibrating.
    assert describe_status(0x44) == "44: battery single cell voltage too high"
    assert describe_calibration_status(0x44) == "44: calibration finished"
    assert describe_calibration_status(0x84) == "84: calibration failed"
    assert describe_calibration_status(0x08) == "08: motor hall / position sensor signal fault"
    assert describe_calibration_status(0xEE) == "EE: unknown code"
    assert set(CALIBRATION_CODES) >= {0x40, 0x44}
