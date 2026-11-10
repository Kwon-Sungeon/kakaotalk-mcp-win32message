"""Argument checks for every public operation.

Each check returns the accepted value or raises ValueError with a message that
names the argument, so an agent can correct its call without guessing.
"""

import os
import re

ROOM_NAME_MAX = 512
SNAPSHOT_PATTERN = re.compile(r"[0-9a-f]{24}")
OPERATION_PATTERN = re.compile(r"[\w.:-]{1,128}", re.ASCII)


def room(value, field="window_title"):
    if isinstance(value, str) and value.strip() and len(value) <= ROOM_NAME_MAX:
        return value
    raise ValueError(f"{field}: give the exact room name (1-{ROOM_NAME_MAX} characters)")


def rooms(values, most):
    """A list of distinct exact room names."""
    if not isinstance(values, list) or not values or len(values) > most:
        raise ValueError(f"window_titles: give 1-{most} exact room names")
    for value in values:
        room(value, "window_titles[]")
    if len(set(values)) < len(values):
        raise ValueError("window_titles: each room may appear only once")
    return values


def number(value, field, lowest, highest):
    # type() rather than isinstance(): True/False are ints in Python but not valid counts here.
    if type(value) is int and lowest <= value <= highest:
        return value
    raise ValueError(f"{field}: expected a whole number from {lowest} to {highest}")


def flag(value, field):
    if value is True or value is False:
        return value
    raise ValueError(f"{field}: expected true or false")


def seconds(value, field="wait_seconds", highest=20):
    if type(value) in (int, float) and 0 <= value <= highest:
        return float(value)
    raise ValueError(f"{field}: expected a number of seconds from 0 to {highest}")


def text(value, field, most, allow_empty=False):
    if isinstance(value, str) and len(value) <= most and (allow_empty or value.strip()):
        return value
    kind = "a string" if allow_empty else "a non-empty string"
    raise ValueError(f"{field}: expected {kind} of at most {most} characters")


def snapshot(value, offset):
    """snapshot_id is empty on a first page and required (as returned) for any later page."""
    if not isinstance(value, str) or (value and not SNAPSHOT_PATTERN.fullmatch(value)):
        raise ValueError("snapshot_id: pass the snapshotId returned by the previous read")
    if offset > 0 and not value:
        raise ValueError("snapshot_id: required when offset > 0 (read the first page again to get one)")
    return value


def operation_id(value):
    if isinstance(value, str) and OPERATION_PATTERN.fullmatch(value):
        return value
    raise ValueError("operation_id: use 1-128 characters from A-Z, a-z, 0-9 and . _ : -")


def new_file(value, field="output_path"):
    """None (a temporary file is used) or an absolute path that does not exist yet."""
    if value is None:
        return None
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError(f"{field}: expected an absolute file path")
    if os.path.lexists(value):
        raise ValueError(f"{field}: {value} already exists; choose a new file name")
    return value
