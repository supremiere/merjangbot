import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord_bot.commands.trains import (
    TrainPanelView,
    format_train_end_time,
    parse_train_end_time,
)
from storage.trains import TrainStateError


class FakeRepository:
    def __init__(self, *, leave_result=2, leave_error=None):
        self.leave_result = leave_result
        self.leave_error = leave_error
        self.leave_calls = []

    def leave(self, guild_id, user_id):
        self.leave_calls.append((guild_id, user_id))
        if self.leave_error is not None:
            raise self.leave_error
        return self.leave_result


class FakeController:
    def __init__(self, repository, *, panel_ok=True):
        self.repository = repository
        self.panel_ok = panel_ok
        self.ensure_panel = AsyncMock(return_value=panel_ok)

    @asynccontextmanager
    async def lock_for(self, guild_id):
        yield

    @staticmethod
    def panel_suffix(panel_ok):
        return "" if panel_ok else "\n⚠️ 좌석은 저장됐지만 현황판을 갱신하지 못했습니다."

    async def error_text(self, guild, error, requester_id=None):
        if error.code == "not_boarded":
            return "현재 탑승 중인 열차가 없습니다."
        if error.code == "conductor_cannot_leave":
            return "기장은 좌석도의 '내 열차 종료' 버튼을 이용해 주세요."
        return error.code


class FakeResponse:
    def __init__(self):
        self.defer = AsyncMock()
        self.send_message = AsyncMock()

    def is_done(self):
        return self.defer.await_count > 0 or self.send_message.await_count > 0


class FakeInteraction:
    def __init__(self):
        self.guild = SimpleNamespace(id=10)
        self.user = SimpleNamespace(id=100)
        self.response = FakeResponse()
        self.edit_original_response = AsyncMock()
        self.followup = SimpleNamespace(send=AsyncMock())


def leave_button(view):
    return next(
        item
        for item in view.children
        if getattr(item, "custom_id", None) == "train:panel:leave"
    )


def test_panel_view_is_persistent_and_has_expected_public_controls():
    view = TrainPanelView(FakeController(FakeRepository()))
    assert view.timeout is None

    custom_ids = {
        getattr(item, "custom_id", None)
        for item in view.children
    }
    assert {
        "train:panel:create",
        "train:panel:board",
        "train:panel:leave",
        "train:panel:passengers",
        "train:panel:end",
        "train:panel:admin",
    }.issubset(custom_ids)

    admin = next(
        item
        for item in view.children
        if getattr(item, "custom_id", None) == "train:panel:admin"
    )
    assert admin.row == 1


def test_public_leave_button_acknowledges_ephemerally_before_work():
    controller = FakeController(FakeRepository(leave_result=2))
    interaction = FakeInteraction()
    view = TrainPanelView(controller)

    asyncio.run(leave_button(view).callback(interaction))

    interaction.response.defer.assert_awaited_once_with(
        ephemeral=True,
        thinking=True,
    )
    assert controller.repository.leave_calls == [(10, 100)]
    controller.ensure_panel.assert_awaited_once_with(interaction.guild)
    interaction.edit_original_response.assert_awaited_once_with(
        content="👋 2호차에서 하차했습니다."
    )
    interaction.followup.send.assert_not_awaited()


def test_public_leave_button_returns_private_not_boarded_message():
    controller = FakeController(
        FakeRepository(
            leave_error=TrainStateError("not_boarded", user_id=100)
        )
    )
    interaction = FakeInteraction()
    view = TrainPanelView(controller)

    asyncio.run(leave_button(view).callback(interaction))

    interaction.response.defer.assert_awaited_once_with(
        ephemeral=True,
        thinking=True,
    )
    interaction.edit_original_response.assert_awaited_once_with(
        content="현재 탑승 중인 열차가 없습니다."
    )


def test_public_leave_button_guides_conductor_to_end_button():
    controller = FakeController(
        FakeRepository(
            leave_error=TrainStateError(
                "conductor_cannot_leave",
                car_no=2,
                user_id=100,
            )
        )
    )
    interaction = FakeInteraction()
    view = TrainPanelView(controller)

    asyncio.run(leave_button(view).callback(interaction))

    interaction.response.defer.assert_awaited_once_with(
        ephemeral=True,
        thinking=True,
    )
    interaction.edit_original_response.assert_awaited_once_with(
        content="기장은 좌석도의 '내 열차 종료' 버튼을 이용해 주세요."
    )


def test_public_leave_button_reports_panel_refresh_failure_without_losing_leave():
    controller = FakeController(
        FakeRepository(leave_result=4),
        panel_ok=False,
    )
    interaction = FakeInteraction()
    view = TrainPanelView(controller)

    asyncio.run(leave_button(view).callback(interaction))

    interaction.edit_original_response.assert_awaited_once_with(
        content=(
            "👋 4호차에서 하차했습니다."
            "\n⚠️ 좌석은 저장됐지만 현황판을 갱신하지 못했습니다."
        )
    )


def test_end_time_uses_today_when_clock_time_is_still_ahead():
    kst = timezone(timedelta(hours=9))
    now = datetime(2026, 9, 27, 15, 23, tzinfo=kst)

    ends_at = parse_train_end_time("23:30", now=now)

    assert datetime.fromtimestamp(ends_at, kst) == datetime(
        2026, 9, 27, 23, 30, tzinfo=kst
    )
    assert format_train_end_time(ends_at, now=now) == "오늘 23:30"


def test_end_time_rolls_to_next_day_when_clock_time_has_passed():
    kst = timezone(timedelta(hours=9))
    now = datetime(2026, 9, 27, 23, 40, tzinfo=kst)

    ends_at = parse_train_end_time("01:30", now=now)

    assert datetime.fromtimestamp(ends_at, kst) == datetime(
        2026, 9, 28, 1, 30, tzinfo=kst
    )
    assert format_train_end_time(ends_at, now=now) == "내일 01:30"


def test_end_time_rejects_invalid_clock_values():
    for value in ("24:00", "12:60", "abc", "12"):
        try:
            parse_train_end_time(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{value!r} should be rejected")
