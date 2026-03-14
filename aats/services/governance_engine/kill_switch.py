from __future__ import annotations


class KillSwitch:
    def __init__(self) -> None:
        self._halted = False
        self._reason: str | None = None

    def halt(self, reason: str = "manual_halt") -> None:
        self._halted = True
        self._reason = reason

    def resume(self) -> None:
        self._halted = False
        self._reason = None

    def status(self) -> dict[str, str | bool | None]:
        return {"halted": self._halted, "reason": self._reason}

    @property
    def halted(self) -> bool:
        return self._halted

