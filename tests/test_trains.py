import sqlite3

import pytest

from storage.database import Database
from storage.trains import TRAIN_CAPACITY, TrainRepository, TrainStateError


@pytest.fixture()
def repository(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    return TrainRepository(database)


def assert_error(code, func, *args, **kwargs):
    with pytest.raises(TrainStateError) as captured:
        func(*args, **kwargs)
    assert captured.value.code == code
    return captured.value


def test_start_and_snapshot(repository):
    repository.start(10, 1, 100, [101])
    state = repository.snapshot(10)

    assert state[1]["conductor_id"] == 100
    assert state[1]["passenger_ids"] == [101]
    assert state[2]["conductor_id"] is None
    assert repository.find_user(10, 100).role == "conductor"
    assert repository.find_user(10, 101).role == "passenger"


def test_user_cannot_board_multiple_trains(repository):
    repository.start(10, 1, 100)
    repository.start(10, 2, 200)
    repository.board(10, 1, 101)

    error = assert_error("already_boarded", repository.board, 10, 2, 101)
    assert error.existing_car == 1


def test_capacity_is_three_including_conductor(repository):
    repository.start(10, 1, 100, [101, 102])

    assert_error("full", repository.board, 10, 1, 103)
    assert len(repository.snapshot(10)[1]["passenger_ids"]) == TRAIN_CAPACITY - 1


def test_database_trigger_also_blocks_fourth_seat(repository):
    repository.start(10, 1, 100, [101, 102])

    with pytest.raises(sqlite3.IntegrityError, match="train_full"):
        with repository.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO train_members (guild_id, car_no, user_id, role)
                VALUES (?, ?, ?, 'passenger')
                """,
                (10, 1, 999),
            )


def test_passenger_can_leave_but_conductor_cannot(repository):
    repository.start(10, 1, 100, [101])

    assert repository.leave(10, 101) == 1
    assert repository.find_user(10, 101) is None
    assert_error("conductor_cannot_leave", repository.leave, 10, 100)


def test_only_conductor_can_end_train(repository):
    repository.start(10, 1, 100, [101])

    assert_error("not_conductor", repository.end, 10, 1, 101)
    repository.end(10, 1, 100)

    state = repository.snapshot(10)[1]
    assert state["conductor_id"] is None
    assert state["passenger_ids"] == []


def test_force_end_clears_train(repository):
    repository.start(10, 1, 100, [101, 102])

    repository.end(10, 1, force=True)

    state = repository.snapshot(10)[1]
    assert state["conductor_id"] is None
    assert state["passenger_ids"] == []


def test_conductor_can_add_passenger(repository):
    repository.start(10, 1, 100)

    repository.add_passenger(10, 1, 100, 101)

    assert repository.find_user(10, 101).role == "passenger"
    assert repository.snapshot(10)[1]["passenger_ids"] == [101]


def test_non_conductor_cannot_add_passenger(repository):
    repository.start(10, 1, 100, [101])

    assert_error("not_conductor", repository.add_passenger, 10, 1, 101, 102)
    assert repository.find_user(10, 102) is None


def test_conductor_add_respects_full_and_cross_train_membership(repository):
    repository.start(10, 1, 100, [101])
    repository.start(10, 2, 200, [201])

    error = assert_error(
        "already_boarded",
        repository.add_passenger,
        10,
        1,
        100,
        201,
    )
    assert error.existing_car == 2

    repository.add_passenger(10, 1, 100, 102)
    assert_error("full", repository.add_passenger, 10, 1, 100, 103)


def test_conductor_can_remove_passenger(repository):
    repository.start(10, 1, 100, [101, 102])

    repository.remove_passenger(10, 1, 100, 101)

    assert repository.find_user(10, 101) is None
    assert repository.snapshot(10)[1]["passenger_ids"] == [102]


def test_non_conductor_cannot_remove_passenger(repository):
    repository.start(10, 1, 100, [101, 102])

    assert_error("not_conductor", repository.remove_passenger, 10, 1, 101, 102)
    assert repository.find_user(10, 102) is not None


def test_conductor_cannot_remove_self_as_passenger(repository):
    repository.start(10, 1, 100, [101])

    assert_error("not_passenger", repository.remove_passenger, 10, 1, 100, 100)
    assert repository.find_user(10, 100).role == "conductor"


def test_conductor_cannot_remove_member_from_another_train(repository):
    repository.start(10, 1, 100, [101])
    repository.start(10, 2, 200, [201])

    assert_error("not_passenger", repository.remove_passenger, 10, 1, 100, 201)
    assert repository.find_user(10, 201).car_no == 2


def test_panel_location_persists(repository):
    repository.save_panel_location(10, 1234, 5678)
    assert repository.panel_location(10) == (1234, 5678)

    repository.save_panel_location(10, 9999, 8888)
    assert repository.panel_location(10) == (9999, 8888)
