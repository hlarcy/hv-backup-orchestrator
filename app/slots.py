"""Start-time slot allocation inside the backup window.

The window (default 18:00 to 00:00) is cut into fixed slots of `stagger_minutes`
(default 3). Each managed job owns one slot, so no two managed jobs start in the
same minute. Slots are sticky: once a job has a slot it keeps it, so adding a new
tenant never reshuffles everybody else's start time.
"""
from __future__ import annotations

from dataclasses import dataclass


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")[:2]
    return int(h) * 60 + int(m)


def _to_hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Window:
    start: str = "18:00"
    end: str = "00:00"
    stagger: int = 3

    @property
    def start_min(self) -> int:
        return _to_minutes(self.start)

    @property
    def length(self) -> int:
        s, e = _to_minutes(self.start), _to_minutes(self.end)
        if e <= s:
            e += 1440  # window crosses midnight (18:00 to 00:00 is 360 minutes)
        return e - s

    @property
    def capacity(self) -> int:
        return self.length // self.stagger

    def slot_time(self, slot: int) -> str:
        return _to_hhmm(self.start_min + slot * self.stagger)

    def slot_for_time(self, hhmm: str | None) -> int | None:
        """Slot index if `hhmm` falls exactly on a slot boundary inside the window."""
        if not hhmm:
            return None
        offset = (_to_minutes(hhmm) - self.start_min) % 1440
        if offset >= self.length or offset % self.stagger:
            return None
        return offset // self.stagger

    def slot_covering(self, hhmm: str | None) -> int | None:
        """Slot whose 3-minute span contains `hhmm` (used to reserve unmanaged job times)."""
        if not hhmm:
            return None
        offset = (_to_minutes(hhmm) - self.start_min) % 1440
        if offset >= self.length:
            return None
        return offset // self.stagger


class SlotAllocator:
    def __init__(self, window: Window, reserved: set[int] | None = None):
        self.window = window
        self.used: dict[int, str] = {}  # slot -> owner key
        self.reserved = set(reserved or ())

    def is_free(self, slot: int) -> bool:
        return 0 <= slot < self.window.capacity and slot not in self.used and slot not in self.reserved

    def claim(self, slot: int, key: str) -> bool:
        if self.is_free(slot):
            self.used[slot] = key
            return True
        return False

    def first_free(self, key: str) -> int | None:
        for s in range(self.window.capacity):
            if self.claim(s, key):
                return s
        return None

    @property
    def free_count(self) -> int:
        return self.window.capacity - len(self.used) - len(self.reserved - set(self.used))
