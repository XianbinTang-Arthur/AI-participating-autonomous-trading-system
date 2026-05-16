import pytest

from aats.data_platform.research_factory.status import (
    TERMINAL_RESEARCH_STATUSES,
    VALID_RESEARCH_STATUSES,
    is_terminal_status,
    require_valid_status,
)


def test_all_valid_statuses_are_accepted() -> None:
    for status in VALID_RESEARCH_STATUSES:
        assert require_valid_status(status) == status


def test_invalid_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid research status"):
        require_valid_status("applied")


def test_terminal_status_classification() -> None:
    for status in VALID_RESEARCH_STATUSES:
        assert is_terminal_status(status) is (status in TERMINAL_RESEARCH_STATUSES)


def test_terminal_status_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="invalid research status"):
        is_terminal_status("unknown")
