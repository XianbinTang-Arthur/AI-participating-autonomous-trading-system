from __future__ import annotations


class KillSwitch:
    """Thread-safe halt/resume switch.

    Uses a single tuple assignment for atomicity — Python guarantees that
    binding a name to a new object is atomic at the bytecode level, so a
    concurrent reader of ``status()`` will always see a consistent
    (halted, reason) pair.
    """

    def __init__(self) -> None:
        self._state: tuple[bool, str | None] = (False, None)

    def halt(self, reason: str = "manual_halt") -> None:
        self._state = (True, reason)

    def resume(self) -> None:
        self._state = (False, None)

    def status(self) -> dict[str, str | bool | None]:
        halted, reason = self._state
        return {"halted": halted, "reason": reason}

    @property
    def halted(self) -> bool:
        return self._state[0]

