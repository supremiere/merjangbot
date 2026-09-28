import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord_bot.commands.trains import (
    TrainAdminPickerView,
    TrainPanelView,
    TrainPassengerManageView,
    build_admin_picker_view,
    format_train_end_time,
    parse_discord_user_id,
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

    manage = next(
        item
        for item in view.children
        if getattr(item, "custom_id", None) == "train:panel:passengers"
    )
    assert manage.label == "열차 관리"

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


class FakeAdminRepository:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self, guild_id):
        return self._snapshot


class FakeAdminController:
    def __init__(self, snapshot):
        self.repository = FakeAdminRepository(snapshot)


def test_admin_picker_keeps_create_button_when_no_trains_exist():
    view = TrainAdminPickerView(
        FakeAdminController({}),
        requester_id=100,
        snapshot={},
    )

    custom_ids = {
        getattr(item, "custom_id", None)
        for item in view.children
    }
    assert "train:admin:create" in custom_ids
    assert "train:admin:car-select" not in custom_ids


def test_empty_admin_picker_returns_create_capable_view():
    guild = SimpleNamespace(id=10)
    controller = FakeAdminController({})

    content, view = asyncio.run(
        build_admin_picker_view(controller, guild, requester_id=100)
    )

    assert "현재 운행 중인 열차가 없습니다." in content
    assert isinstance(view, TrainAdminPickerView)
    assert any(
        getattr(item, "custom_id", None) == "train:admin:create"
        for item in view.children
    )


def test_conductor_train_management_combines_passengers_and_end_time():
    view = TrainPassengerManageView(
        FakeController(FakeRepository()),
        requester_id=100,
        car_no=1,
        passenger_options=[],
        can_add=True,
    )

    labels = {
        getattr(item, "label", None)
        for item in view.children
        if getattr(item, "label", None)
    }
    assert "운행시간 수정" in labels
    assert any(
        getattr(item, "custom_id", "").startswith("train:manage:add:")
        for item in view.children
    )


def test_parse_discord_user_id_accepts_raw_id_and_mentions():
    user_id = 123456789012345678
    assert parse_discord_user_id(str(user_id)) == user_id
    assert parse_discord_user_id(f"<@{user_id}>") == user_id
    assert parse_discord_user_id(f"<@!{user_id}>") == user_id


def test_parse_discord_user_id_rejects_names_and_short_numbers():
    for value in ("nickname", "@nickname", "12345", "<@12345>"):
        try:
            parse_discord_user_id(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{value!r} should be rejected")


def test_conductor_train_management_has_offline_direct_add_button():
    view = TrainPassengerManageView(
        FakeController(FakeRepository()),
        requester_id=100,
        car_no=1,
        passenger_options=[],
        can_add=True,
    )

    labels = {
        getattr(item, "label", None)
        for item in view.children
        if getattr(item, "label", None)
    }
    assert "ID/멘션으로 추가" in labels
