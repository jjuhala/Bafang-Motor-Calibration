"""Human-readable descriptions for the status byte returned by ``11 08``.

The controller reports its status as one byte whose *hexadecimal* digits are the
number printed on Bafang displays (``0x21`` is shown as "21"). The C961
calibration display prints the byte exactly like that ("Er 21"); it hides codes
below ``0x03`` while calibrating and below ``0x04`` while riding.

The same byte means different things in the two situations:

* while riding, it is an error code (tables in Bafang's display manuals);
* while the controller calibrates, it reports the calibration progress --
  Bafang's "Ultra's Controller Calibration (CR R10M)" instructions list
  40 -> 41 -> 43 -> 95 -> 96 -> 97 -> 98 -> 44 (finished), and 51, 53, 63, 73,
  84 or 85 when the calibration failed.
"""

from __future__ import annotations

import enum

__all__ = [
    "CALIBRATION_CODES",
    "CALIBRATION_DONE",
    "CALIBRATION_FAILURES",
    "CALIBRATION_STARTED",
    "STATUS_CODES",
    "CalibrationState",
    "calibration_state",
    "describe_calibration_status",
    "describe_status",
    "display_code",
]

STATUS_CODES: dict[int, str] = {
    0x01: "normal operation",
    0x03: "brake engaged",
    0x04: "throttle not in zero position",
    0x05: "throttle fault",
    0x06: "battery voltage low",
    0x07: "over-voltage protection",
    0x08: "motor hall / position sensor signal fault",
    0x09: "motor phase fault",
    0x10: "motor temperature reached protection limit",
    0x11: "motor temperature sensor fault",
    0x12: "current sensor fault",
    0x13: "battery temperature fault",
    0x14: "controller temperature reached protection limit",
    0x15: "controller temperature sensor fault",
    0x21: "speed sensor fault",
    0x22: "BMS communication fault",
    0x23: "headlight fault",
    0x24: "headlight sensor fault",
    0x25: "torque sensor signal fault",
    0x26: "torque sensor speed signal fault",
    0x27: "controller over-current",
    0x30: "communication fault",
    0x33: "brake signal fault",
    0x35: "15 V supply circuit fault",
    0x36: "keypad detection circuit fault",
    0x37: "watchdog circuit fault",
    0x41: "battery total voltage too high",
    0x42: "battery total voltage too low",
    0x43: "battery cell power too high",
    0x44: "battery single cell voltage too high",
    0x45: "battery temperature too high",
    0x46: "battery temperature too low",
    0x47: "battery state of charge too high",
    0x48: "battery state of charge too low",
    0x61: "gear-shift detection fault",
    0x62: "electronic derailleur cannot release",
    0x71: "electronic lock jammed",
    0x81: "Bluetooth module fault",
}

#: First code the controller reports after accepting the calibration command.
CALIBRATION_STARTED = 0x40
#: "Er 44 means the calibration is over."
CALIBRATION_DONE = 0x44
#: Codes after which Bafang says to calibrate again (and, if they persist,
#: that the controller is defective).
CALIBRATION_FAILURES = frozenset({0x51, 0x53, 0x63, 0x73, 0x84, 0x85})

CALIBRATION_CODES: dict[int, str] = {
    0x40: "calibration started",
    0x41: "calibrating",
    0x43: "calibrating",
    0x95: "calibrating",
    0x96: "calibrating",
    0x97: "calibrating",
    0x98: "calibrating",
    0x44: "calibration finished",
    **dict.fromkeys(sorted(CALIBRATION_FAILURES), "calibration failed"),
}


class CalibrationState(enum.Enum):
    """What a status byte says about a running calibration."""

    NOT_STARTED = "not started"
    IN_PROGRESS = "in progress"
    DONE = "done"
    FAILED = "failed"


def calibration_state(code: int) -> CalibrationState:
    """Classify a status byte received while calibrating."""
    if code == CALIBRATION_DONE:
        return CalibrationState.DONE
    if code in CALIBRATION_FAILURES:
        return CalibrationState.FAILED
    if code in CALIBRATION_CODES:
        return CalibrationState.IN_PROGRESS
    return CalibrationState.NOT_STARTED


def display_code(code: int) -> str:
    """Return the code the way a Bafang display prints it, e.g. ``0x21 -> "21"``."""
    return f"{code:02X}"


def describe_status(code: int) -> str:
    """Return ``"<display code>: <description>"`` for a status byte while riding."""
    return f"{display_code(code)}: {STATUS_CODES.get(code, 'unknown code')}"


def describe_calibration_status(code: int) -> str:
    """Like :func:`describe_status`, for a byte received while calibrating."""
    description = CALIBRATION_CODES.get(code) or STATUS_CODES.get(code, "unknown code")
    return f"{display_code(code)}: {description}"
