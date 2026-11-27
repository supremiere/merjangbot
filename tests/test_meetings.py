from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from storage.database import Database
from storage.meetings import MeetingError, MeetingStore
from storage.trains import TrainRepository


GUILD, OTHER_GUILD = 10, 11
LEADER, PLAYER, WATCHER, ADMIN = 100, 101, 102, 200
DAY = 86_400


class Clock:
    def __init__(self):
        self.now = 1_800_000_000

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    database = Database(tmp_path / "merjangbot.db")
    database.initialize()
    meetings = MeetingStore(database, clock=clock)
    for guild in (GUILD, OTHER_GUILD):
        meetings.configure(guild, ADMIN, enabled=True, category_id=500, panel_channel_id=501,
                           recruit_channel_id=502, promo_channel_id=503, max_created=10)
    return meetings


def party(store, guild=GUILD, leader=LEADER, **fields):
    fields.setdefault("destination", "토요일 물길")
    fields.setdefault("capacity", 3)
    pid = store.create(guild, leader, **fields)["id"]
    store.refs(guild, pid, phase="ready", room_id=600 + pid)
    return pid


def act(store, pid, actor, action, **fields):
    return store.mutate(GUILD, pid, actor, actor == ADMIN, action, **fields)


@pytest.mark.parametrize("days", [None, 0, 7, 30])
def test_any_schedule_survives_months_restart_and_reuse(store, clock, tmp_path, days):
    departure = None if days is None else clock.now + days * DAY
    pid = party(store, departure=departure)
    act(store, pid, PLAYER, "join")
    act(store, pid, PLAYER, "roster", target=PLAYER, character="일곱", note="메모")
    act(store, pid, LEADER, "strategy", text="두 번째 기믹에 모이기")
    before = store.get(GUILD, pid)
    clock.now += 400 * DAY
    database = Database(tmp_path / "merjangbot.db")
    database.initialize()
    reopened = MeetingStore(database, clock=clock)
    current = reopened.get(GUILD, pid)
    for key in ("members", "roster", "strategy", "room_id", "departure", "phase", "activity", "closed"):
        assert current[key] == before[key]
    assert reopened.page(GUILD, "inactive")[1] == 1
    act(reopened, pid, LEADER, "start")
    ended = act(reopened, pid, ADMIN, "end")
    assert ended["phase"] == "ready" and ended["activity"] == "ended"
    assert (ended["roster"], ended["strategy"], ended["room_id"]) == (
        before["roster"], before["strategy"], before["room_id"])
    with pytest.raises(MeetingError):
        act(reopened, pid, WATCHER, "join")
    act(reopened, pid, LEADER, "resume")
    act(reopened, pid, ADMIN, "edit", departure=clock.now + DAY)
    assert act(reopened, pid, LEADER, "edit", departure=None)["departure"] is None
    assert reopened.get(GUILD, pid)["members"] == [LEADER, PLAYER]


def test_watchers_default_open_in_full_party_and_never_occupy_seats(store):
    pid = party(store, capacity=1)
    snapshot = act(store, pid, WATCHER, "watch")
    assert snapshot["spectating"]
    assert snapshot["members"] == [LEADER] and snapshot["watchers"] == [WATCHER]
    assert store.stats(GUILD)["members"] == 1
    assert store.page(GUILD, "mine", user_id=WATCHER)[1] == 1
    assert store.page(GUILD, "open")[1] == 0
    with pytest.raises(MeetingError):
        act(store, pid, WATCHER, "roster", target=WATCHER, character="관전자")
    for action, fields in [("edit", {"capacity": 4}), ("strategy", {"text": "관리자 공지"}), ("close_watch", {})]:
        with pytest.raises(MeetingError):
            act(store, pid, WATCHER, action, **fields)
    act(store, pid, ADMIN, "edit", capacity=2)
    joined = act(store, pid, WATCHER, "join")
    assert joined["members"] == [LEADER, WATCHER] and WATCHER not in joined["watchers"]
    left = act(store, pid, WATCHER, "leave")
    assert WATCHER not in left["members"] and WATCHER not in left["watchers"]


def test_bulk_delete_resolves_only_matching_channel_and_guild_resources(store):
    first, second = party(store), party(store)
    other = party(store, guild=OTHER_GUILD)
    store.refs(GUILD, first, recruit_channel_id=502, recruit_id=900)
    store.refs(GUILD, second, ads=[{"channel_id": 502, "message_id": 901}])
    store.refs(OTHER_GUILD, other, recruit_channel_id=502, recruit_id=902)
    assert set(store.for_messages(GUILD, 502, range(890, 920))) == {first, second}
    assert store.for_messages(GUILD, 503, range(890, 920)) == []


def test_close_watch_kick_and_reopen_require_explicit_new_entry(store):
    pid = party(store)
    act(store, pid, PLAYER, "join")
    act(store, pid, WATCHER, "watch")
    act(store, pid, ADMIN, "watch")
    closed = act(store, pid, LEADER, "close_watch")
    assert not closed["spectating"] and not closed["watchers"]
    assert closed["members"] == [LEADER, PLAYER]
    with pytest.raises(MeetingError):
        act(store, pid, WATCHER, "watch")
    assert act(store, pid, ADMIN, "open_watch")["watchers"] == []
    act(store, pid, WATCHER, "watch")
    kicked = act(store, pid, LEADER, "kick", target=WATCHER)
    assert WATCHER not in kicked["watchers"] and WATCHER in kicked["banned"]
    for path in ("watch", "join"):
        with pytest.raises(MeetingError):
            act(store, pid, WATCHER, path)
    act(store, pid, ADMIN, "allow", target=WATCHER)
    act(store, pid, WATCHER, "watch")
    assert act(store, pid, WATCHER, "unwatch")["watchers"] == []


def test_manual_recruitment_and_membership_are_independent_of_other_parties(store):
    first = party(store, capacity=2)
    second = party(store, leader=PLAYER)
    act(store, first, PLAYER, "join")
    act(store, second, LEADER, "join")
    assert store.page(GUILD, "mine", user_id=PLAYER)[1] == 2
    with pytest.raises(MeetingError):
        act(store, first, ADMIN, "edit", capacity=1)
    act(store, first, LEADER, "close")
    act(store, first, PLAYER, "leave")
    assert store.get(GUILD, first)["closed"] and store.get(GUILD, second)["members"] == [PLAYER, LEADER]
    with pytest.raises(MeetingError):
        act(store, first, PLAYER, "join")
    act(store, first, LEADER, "open")
    assert act(store, first, PLAYER, "join")["members"] == [LEADER, PLAYER]
    assert store.page(GUILD, "open")[1] == 1


def test_legacy_roster_fields_survive_character_and_note_edits(store):
    pid = party(store)
    with store.db.connect() as conn:
        conn.execute("UPDATE meeting_members SET job='힐러', role='보조' WHERE party_id=? AND user_id=?",
                     (pid, LEADER))
    assert LEADER not in store.get(GUILD, pid)["roster"]
    updated = act(store, pid, LEADER, "roster", character="일곱", note="메모")
    assert updated["roster"][LEADER] == {"character": "일곱", "note": "메모"}
    with store.db.connect() as conn:
        row = conn.execute("SELECT job, role FROM meeting_members WHERE party_id=? AND user_id=?",
                           (pid, LEADER)).fetchone()
    assert tuple(row) == ("힐러", "보조")


def test_roster_transfer_departure_orphan_and_guild_isolation(store):
    pid = party(store)
    act(store, pid, PLAYER, "join")
    act(store, pid, WATCHER, "watch")
    for victim in (LEADER, WATCHER):
        with pytest.raises(MeetingError):
            act(store, pid, PLAYER, "roster", target=victim, character="누구")
    for actor, action, fields in ((LEADER, "transfer", {"target": WATCHER}), (LEADER, "leave", {}),
                                  (PLAYER, "transfer", {"target": PLAYER})):
        with pytest.raises(MeetingError):
            act(store, pid, actor, action, **fields)
    act(store, pid, ADMIN, "roster", target=PLAYER, character="일곱")
    assert act(store, pid, ADMIN, "transfer", target=PLAYER)["leader_id"] == PLAYER
    assert ADMIN not in store.get(GUILD, pid)["members"]
    with pytest.raises(MeetingError):
        store.mutate(OTHER_GUILD, pid, ADMIN, True, "disband")
    with pytest.raises(MeetingError):
        store.get(OTHER_GUILD, pid)
    assert store.page(OTHER_GUILD, "all")[1] == 0
    store.depart(GUILD, pid, PLAYER)
    assert store.get(GUILD, pid)["leader_id"] == LEADER
    assert PLAYER not in store.get(GUILD, pid)["roster"]
    store.depart(GUILD, pid, LEADER)
    orphan = store.get(GUILD, pid)
    assert orphan["leader_id"] is None and orphan["phase"] == "ready"
    assert act(store, pid, ADMIN, "disband")["phase"] == "disbanding"
    with pytest.raises(MeetingError):
        act(store, pid, WATCHER, "join")


def test_independent_connections_cannot_overbook_or_double_promote(store):
    pid = party(store, capacity=2)

    def race(actors, action):
        gate = Barrier(len(actors))

        def attempt(actor):
            gate.wait()
            try:
                return act(store, pid, actor, action)
            except MeetingError:
                return None

        with ThreadPoolExecutor(max_workers=len(actors)) as pool:
            return [result for result in pool.map(attempt, actors) if result]

    assert len(race((PLAYER, WATCHER), "join")) == 1
    joined = store.get(GUILD, pid)
    assert len(joined["members"]) == len(set(joined["members"])) == 2
    act(store, pid, LEADER, "edit", capacity=5)
    assert len(race((ADMIN, LEADER), "promote")) == 1
    assert store.get(GUILD, pid)["promo_token"]


def test_promotion_uses_durable_party_clock_and_charges_only_success(store, clock, tmp_path):
    pid = party(store)
    assert store.settings(GUILD)["cooldown"] == 60
    first = act(store, pid, LEADER, "promote")["promo_token"]
    store.finish_promo(GUILD, pid, first)
    assert store.get(GUILD, pid)["last_promo"] is None
    token = act(store, pid, ADMIN, "promote")["promo_token"]
    store.finish_promo(GUILD, pid, token, channel_id=503, message_id=1001)
    act(store, pid, PLAYER, "join")
    act(store, pid, LEADER, "transfer", target=PLAYER)
    act(store, pid, PLAYER, "edit", destination="다음 일정")
    reopened = MeetingStore(Database(tmp_path / "merjangbot.db"), clock=clock)
    clock.now += 59
    with pytest.raises(MeetingError):
        act(reopened, pid, PLAYER, "promote")
    clock.now += 1
    assert act(reopened, pid, ADMIN, "promote")["promo_token"]
    assert reopened.page(GUILD, "recovery")[1] >= 1
    assert reopened.get(GUILD, pid)["ads"] == [{"channel_id": 503, "message_id": 1001}]


def test_migration_preserves_existing_train_and_meeting_state(store, tmp_path):
    db = Database(tmp_path / "merjangbot.db")
    trains = TrainRepository(db)
    trains.start(GUILD, 1, LEADER, [PLAYER], ends_at=1_800_010_000)
    before = trains.snapshot(GUILD)
    pid = party(store)
    act(store, pid, WATCHER, "watch")
    # Model the earlier schema that did not track interrupted roster verification.
    with db.connect() as conn:
        conn.execute("DROP TABLE meeting_member_checks")
    db.initialize()
    db.initialize()
    assert TrainRepository(db).snapshot(GUILD) == before
    assert MeetingStore(db).get(GUILD, pid)["watchers"] == [WATCHER]


def test_interrupted_sql_transaction_rolls_back_seat_change_and_watcher_removal(store):
    pid = party(store)
    act(store, pid, WATCHER, "watch")
    with store.db.connect() as conn:
        conn.execute("""CREATE TRIGGER fail_join_audit BEFORE INSERT ON meeting_audit
                        WHEN NEW.action='join' BEGIN SELECT RAISE(ABORT,'simulated write failure'); END""")
    with pytest.raises(sqlite3.DatabaseError):
        act(store, pid, WATCHER, "join")
    assert store.get(GUILD, pid)["members"] == [LEADER]
    assert store.get(GUILD, pid)["watchers"] == [WATCHER]
    with store.db.connect() as conn:
        conn.execute("DROP TRIGGER fail_join_audit")
    assert act(store, pid, WATCHER, "join")["members"] == [LEADER, WATCHER]


def test_party_limit_disabled_feature_and_validation_preserve_existing_data(store):
    store.configure(GUILD, ADMIN, max_created=1, max_capacity=8)
    for fields in ({"destination": " "}, {"capacity": 0}, {"capacity": 9}, {"description": "x" * 2000}):
        with pytest.raises(MeetingError):
            party(store, **fields)
    pid = party(store)
    assert store.get(GUILD, pid)["members"] == [LEADER]
    with pytest.raises(MeetingError):
        party(store)
    store.configure(GUILD, ADMIN, enabled=False)
    for role in ("join", "watch", "promote"):
        with pytest.raises(MeetingError):
            act(store, pid, LEADER if role == "promote" else WATCHER, role)
    before = store.get(GUILD, pid)
    assert before["phase"] == "ready" and before["room_id"]
    store.configure(GUILD, ADMIN, enabled=True)
    assert act(store, pid, WATCHER, "watch")["watchers"] == [WATCHER]


def test_departure_must_be_representable_in_kst(store):
    maximum = 253402268399  # 9999-12-31 23:59:59 KST
    pid = party(store, departure=maximum)
    assert store.get(GUILD, pid)["departure"] == maximum
    with pytest.raises(MeetingError):
        act(store, pid, LEADER, "edit", departure=maximum + 1)
