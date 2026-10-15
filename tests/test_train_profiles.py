import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from storage.database import Database
from storage.trains import TrainRepository, TrainStateError, train_title
from discord_bot.commands.trains import (
    TrainController, TrainPanelView, TrainSettingsView, TrainAdminCreateView,
    TrainBoardSelect, build_passenger_manage_view,
)


@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "trains.db")
    db.initialize()
    return TrainRepository(db)


@pytest.mark.parametrize("kind", ["illusion", "predation", "waterway"])
@pytest.mark.parametrize("capacity", [3, 4])
def test_each_profile_persists_and_enforces_its_own_limit(repo, kind, capacity):
    repo.start(10, 1, 100, train_type=kind, capacity=capacity)
    for user in range(101, 100 + capacity):
        repo.board(10, 1, user)
    with pytest.raises(TrainStateError, match="full"):
        repo.add_passenger(10, 1, 100, 200)
    state = TrainRepository(Database(repo.db.path)).snapshot(10)[1]
    assert (state["train_type"], state["capacity"]) == (kind, capacity)
    assert len(state["passenger_ids"]) == capacity - 1
    assert f"({capacity}인)" in train_title(state)
    with repo.db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="train_full"):
        conn.execute("INSERT INTO train_members VALUES (10, 1, 999, 'passenger')")


def test_upgrade_keeps_legacy_seats_and_schedules_and_replaces_old_trigger(repo):
    repo.start(10, 1, 100, [101, 102], ends_at=1800000000)
    with repo.db.connect() as conn:
        conn.executescript("""
            DROP TABLE train_profiles;
            DROP TRIGGER trg_train_capacity;
            CREATE TRIGGER trg_train_capacity BEFORE INSERT ON train_members
            WHEN (SELECT COUNT(*) FROM train_members WHERE guild_id = NEW.guild_id AND car_no = NEW.car_no) >= 3
            BEGIN SELECT RAISE(ABORT, 'train_full'); END;
        """)
    repo.db.initialize()
    state = repo.snapshot(10)[1]
    assert (state["train_type"], state["capacity"]) == ("illusion", 3)
    assert state["ends_at"] == 1800000000
    repo.set_profile(10, 1, 100, "predation", 4)
    repo.board(10, 1, 103)
    repo.db.initialize()
    assert repo.snapshot(10)[1]["passenger_ids"] == [101, 102, 103]
    assert repo.snapshot(10)[1]["capacity"] == 4


def test_shrink_preserves_everyone_until_a_passenger_leaves(repo):
    repo.start(10, 1, 100, [101, 102, 103], capacity=4)
    before = repo.snapshot(10)
    with pytest.raises(TrainStateError, match="capacity_below_occupancy"):
        repo.set_profile(10, 1, 100, "waterway", 3)
    assert repo.snapshot(10) == before
    with repo.db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="train_full"):
        conn.execute("UPDATE train_profiles SET capacity = 3 WHERE guild_id = 10 AND car_no = 1")
    repo.leave(10, 103)
    repo.set_profile(10, 1, 100, "waterway", 3)
    assert train_title(repo.snapshot(10)[1]) == "물길열차 (3인)"
    repo.end(10, 1, 100)
    repo.start(10, 1, 200)
    assert train_title(repo.snapshot(10)[1]) == "허상열차 (3인)"


def test_profile_change_checks_ownership_and_valid_values(repo):
    repo.start(10, 1, 100)
    for args, code in [((101, "predation", 4), "not_conductor"),
                       ((100, "unknown", 4), "invalid_train_type"),
                       ((100, "illusion", 5), "invalid_capacity")]:
        with pytest.raises(TrainStateError, match=code):
            repo.set_profile(10, 1, *args)
    repo.set_profile(10, 1, 999, "predation", 4, force=True)
    repo.set_composition(10, 1, 100, [101, 102, 103])
    assert train_title(repo.snapshot(10)[1]) == "포식열차 (4인)"
    repo.set_composition(10, 1, 100, [101], train_type="waterway", capacity=3)
    assert train_title(repo.snapshot(10)[1]) == "물길열차 (3인)"


def test_two_simultaneous_boardings_cannot_overbook_fourth_seat(repo):
    repo.start(10, 1, 100, [101, 102], capacity=4)
    def board(user):
        try:
            repo.board(10, 1, user)
            return "ok"
        except TrainStateError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(board, [103, 104])) == ["full", "ok"]


def context(repo):
    guild = SimpleNamespace(id=10, owner_id=999, get_member=lambda uid: SimpleNamespace(display_name=f"멤버{uid}"))
    user = SimpleNamespace(id=100, guild=guild, roles=[], guild_permissions=SimpleNamespace(administrator=False))
    response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(),
                               defer=AsyncMock(), send_modal=AsyncMock())
    interaction = SimpleNamespace(guild=guild, user=user, response=response,
                                  edit_original_response=AsyncMock())
    controller = TrainController(SimpleNamespace(database=repo.db))
    controller.ensure_panel = AsyncMock(return_value=True)
    controller.announce_train_arrival = AsyncMock()
    return controller, interaction


async def select(view, field, value, interaction):
    item = next(x for x in view.children if getattr(x, "field", None) == field)
    item._values = [str(value)]
    await item.callback(interaction)


def test_public_create_choices_reach_database_and_title(repo):
    async def scenario():
        controller, interaction = context(repo)
        panel = TrainPanelView(controller)
        await panel.create_train.callback(interaction)
        view = interaction.response.send_message.call_args.kwargs["view"]
        assert isinstance(view, TrainSettingsView)
        await select(view, "train_type", "predation", interaction)
        await select(view, "capacity", 4, interaction)
        await view.save.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        modal.end_time._value = "23:59"
        await modal.on_submit(interaction)
        assert train_title(repo.snapshot(10)[1]) == "포식열차 (4인)"
        text = await controller.panel_text(interaction.guild)
        assert "포식열차 (4인)" in text and "1 / 4" in text
        content, manage = await build_passenger_manage_view(controller, interaction.guild, 100, 1)
        assert "포식열차 (4인)" in content and "1/4" in content
        assert any(getattr(x, "label", None) == "종류·정원 변경" for x in manage.children)
        options = TrainBoardSelect(controller, 200, repo.snapshot(10)).options
        assert "포식열차 (4인)" in options[0].label
        repo.board(10, 1, 101)
        repo.board(10, 1, 102)
        assert len(TrainBoardSelect(controller, 200, repo.snapshot(10)).options) == 1
    asyncio.run(scenario())


def test_admin_create_and_conductor_edit_use_selected_settings(repo):
    async def scenario():
        controller, interaction = context(repo)
        interaction.user.id = 999
        admin = TrainAdminCreateView(controller, 999)
        await select(admin, "train_type", "waterway", interaction)
        await select(admin, "capacity", 4, interaction)
        picker = next(x for x in admin.children if getattr(x, "custom_id", "") == "train:admin:create-conductor")
        picker._values = [SimpleNamespace(id=100, name="기장", display_name="기장", bot=False)]
        await picker.callback(interaction)
        assert train_title(repo.snapshot(10)[1]) == "물길열차 (4인)"
        interaction.user.id = 100
        edit = TrainSettingsView(controller, 100, car_no=1, state=repo.snapshot(10)[1])
        await select(edit, "train_type", "predation", interaction)
        await select(edit, "capacity", 3, interaction)
        await edit.save.callback(interaction)
        assert train_title(repo.snapshot(10)[1]) == "포식열차 (3인)"
        interaction.user.id = 200
        assert not await edit.interaction_check(interaction)
        stale = TrainSettingsView(controller, 100, car_no=1)
        interaction.user.id = 100
        repo.set_conductor(10, 1, 300)
        await stale.save.callback(interaction)
        assert "기장만" in interaction.edit_original_response.call_args.kwargs["content"]
    asyncio.run(scenario())


def test_settings_recheck_admin_role_and_report_over_capacity_without_dropping_members(repo):
    async def scenario():
        repo.start(10, 1, 100, [101, 102, 103], capacity=4)
        controller, interaction = context(repo)
        view = TrainSettingsView(controller, 100, car_no=1, state=repo.snapshot(10)[1])
        await select(view, "capacity", 3, interaction)
        await view.save.callback(interaction)
        assert "하차한 뒤" in interaction.edit_original_response.call_args.kwargs["content"]
        assert repo.snapshot(10)[1]["capacity"] == 4
        assert repo.snapshot(10)[1]["passenger_ids"] == [101, 102, 103]
        admin = TrainSettingsView(controller, 100, car_no=1, admin=True, state=repo.snapshot(10)[1])
        assert not await admin.interaction_check(interaction)
        await admin.save.callback(interaction)
        assert train_title(repo.snapshot(10)[1]) == "허상열차 (4인)"
    asyncio.run(scenario())
