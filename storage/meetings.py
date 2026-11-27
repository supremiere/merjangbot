"""Persistent, guild-scoped planned parties. Time never ends a party."""
import json
import math
import secrets
import sqlite3
import time
from contextlib import contextmanager


class MeetingError(ValueError):
    """Safe Korean text suitable for an ephemeral interaction response."""


DEFAULTS = {
    "enabled": False, "panel_channel_id": None, "recruit_channel_id": None,
    "promo_channel_id": None, "category_id": None, "log_channel_id": None,
    "panel_message_id": None, "cooldown": 60, "max_capacity": 90,
    "max_created": 0, "inactive_days": 30,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_settings (
    guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
    panel_channel_id INTEGER, recruit_channel_id INTEGER, promo_channel_id INTEGER,
    category_id INTEGER, log_channel_id INTEGER, panel_message_id INTEGER,
    cooldown INTEGER NOT NULL DEFAULT 60, max_capacity INTEGER NOT NULL DEFAULT 90,
    max_created INTEGER NOT NULL DEFAULT 0, inactive_days INTEGER NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS meeting_parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL, leader_id INTEGER, request_id TEXT,
    nonce TEXT NOT NULL UNIQUE, destination TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity>=1),
    departure INTEGER, conditions TEXT NOT NULL, description TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT '', activity TEXT NOT NULL DEFAULT 'planned'
        CHECK(activity IN ('planned','active','ended')),
    closed INTEGER NOT NULL DEFAULT 0, spectating INTEGER NOT NULL DEFAULT 1,
    phase TEXT NOT NULL DEFAULT 'creating' CHECK(phase IN ('creating','ready','disbanding','disbanded')),
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, activity_at INTEGER NOT NULL,
    last_promo REAL, promo_token TEXT, promo_channel_id INTEGER, promo_started_at REAL,
    room_id INTEGER, guide_id INTEGER, recruit_id INTEGER, recruit_channel_id INTEGER,
    dirty INTEGER NOT NULL DEFAULT 1, error TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(guild_id,request_id)
);
CREATE INDEX IF NOT EXISTS meeting_guild_list ON meeting_parties(guild_id,phase,id);
CREATE INDEX IF NOT EXISTS meeting_work_queue ON meeting_parties(guild_id,id)
    WHERE dirty=1 OR error!='' OR promo_token IS NOT NULL OR phase IN ('creating','disbanding');
CREATE INDEX IF NOT EXISTS meeting_create_limit ON meeting_parties(guild_id,creator_id,phase);
CREATE UNIQUE INDEX IF NOT EXISTS meeting_rooms ON meeting_parties(guild_id,room_id) WHERE room_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS meeting_recruit_refs ON meeting_parties(guild_id,recruit_channel_id,recruit_id)
    WHERE recruit_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS meeting_public_message ON meeting_parties(guild_id,recruit_channel_id,recruit_id);
CREATE TABLE IF NOT EXISTS meeting_members (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, party_id INTEGER NOT NULL REFERENCES meeting_parties(id),
    user_id INTEGER NOT NULL, character TEXT NOT NULL DEFAULT '', job TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', UNIQUE(party_id,user_id)
);
CREATE INDEX IF NOT EXISTS meeting_my_parties ON meeting_members(user_id,party_id);
CREATE TABLE IF NOT EXISTS meeting_watchers (
    party_id INTEGER NOT NULL REFERENCES meeting_parties(id), user_id INTEGER NOT NULL,
    PRIMARY KEY(party_id,user_id)
);
CREATE INDEX IF NOT EXISTS meeting_watched ON meeting_watchers(user_id,party_id);
CREATE TABLE IF NOT EXISTS meeting_bans (
    party_id INTEGER NOT NULL REFERENCES meeting_parties(id), user_id INTEGER NOT NULL,
    PRIMARY KEY(party_id,user_id)
);
CREATE TABLE IF NOT EXISTS meeting_ads (
    party_id INTEGER NOT NULL REFERENCES meeting_parties(id), channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL, PRIMARY KEY(party_id,message_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS meeting_ad_message ON meeting_ads(channel_id,message_id);
CREATE INDEX IF NOT EXISTS meeting_ad_refs ON meeting_ads(channel_id,message_id);
CREATE TABLE IF NOT EXISTS meeting_audit (
    id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, party_id INTEGER,
    actor_id INTEGER NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL,
    success INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS meeting_audit_guild ON meeting_audit(guild_id,id);
CREATE TABLE IF NOT EXISTS meeting_discord_sends (
    marker TEXT PRIMARY KEY, channel_id INTEGER NOT NULL, attempted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meeting_member_checks (
    party_id INTEGER PRIMARY KEY REFERENCES meeting_parties(id), failures INTEGER NOT NULL,
    retry_at INTEGER, error TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS meeting_capacity BEFORE INSERT ON meeting_members
WHEN (SELECT COUNT(*) FROM meeting_members WHERE party_id=NEW.party_id)
    >= (SELECT capacity FROM meeting_parties WHERE id=NEW.party_id)
BEGIN SELECT RAISE(ABORT,'meeting capacity exceeded'); END;
"""


def initialize_meetings(conn):
    conn.executescript(SCHEMA)


def _integer(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MeetingError(f"{label} 값을 확인해 주세요.")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise MeetingError(f"{label}에 정수를 입력해 주세요.") from None
    if not low <= result <= high:
        raise MeetingError(f"{label}은(는) {low}~{high} 범위로 입력해 주세요.")
    return result


def _snowflake(value):
    return _integer(value, "Discord ID", 1, 2**63 - 1)


def _text(value, label, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise MeetingError(f"{label}은(는) {maximum}자 이내로 입력해 주세요.")
    value = value.strip()
    if required and not value:
        raise MeetingError(f"{label}을(를) 입력해 주세요.")
    return value


def _bool(value, label):
    if value not in (True, False) or not isinstance(value, (int, bool)):
        raise MeetingError(f"{label} 설정값을 확인해 주세요.")
    return bool(value)


class MeetingStore:
    def __init__(self, database, clock=time.time):
        self.db = database
        self.clock = clock

    @contextmanager
    def _connection(self, write=False):
        with self.db.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn

    @staticmethod
    def _settings(conn, gid):
        row = conn.execute("SELECT * FROM meeting_settings WHERE guild_id=?", (gid,)).fetchone()
        result = dict(DEFAULTS, guild_id=gid) if row is None else dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def settings(self, gid):
        with self._connection() as conn:
            return self._settings(conn, gid)

    def _audit(self, conn, gid, pid, actor, action, detail="", success=True):
        conn.execute("""INSERT INTO meeting_audit(guild_id,party_id,actor_id,action,detail,success,created_at)
                        VALUES (?,?,?,?,?,?,?)""",
                     (gid, pid, actor, action, str(detail)[:1000], bool(success), int(self.clock())))

    def audit(self, gid, pid, actor, action, detail="", success=True):
        with self._connection(True) as conn:
            self._audit(conn, gid, pid, actor, action, detail, success)

    def configure(self, gid, actor, **fields):
        options = {}
        limits = {"cooldown": (0, 86400), "max_capacity": (1, 10000),
                  "max_created": (0, 10000), "inactive_days": (1, 36500)}
        for key, value in fields.items():
            if key not in DEFAULTS:
                raise MeetingError("알 수 없는 시스템 설정입니다.")
            if key == "enabled":
                value = _bool(value, "기능 활성화")
            elif key in limits:
                value = _integer(value, "설정", *limits[key])
            else:
                value = None if value is None else _snowflake(value)
            options[key] = value
        with self._connection(True) as conn:
            conn.execute("INSERT OR IGNORE INTO meeting_settings(guild_id) VALUES (?)", (gid,))
            before = self._settings(conn, gid)
            if "panel_channel_id" in options and options["panel_channel_id"] != before["panel_channel_id"]:
                options.setdefault("panel_message_id", None)
            if options:
                sets = ",".join(f"{k}=?" for k in options)
                conn.execute(f"UPDATE meeting_settings SET {sets} WHERE guild_id=?", (*options.values(), gid))
            if actor:
                self._audit(conn, gid, None, actor, "configure",
                            json.dumps({k: [before[k], v] for k, v in options.items()}, ensure_ascii=False))
            return self._settings(conn, gid)

    @staticmethod
    def _snapshots(conn, rows):
        by_id = {}
        for row in rows:
            party = dict(row)
            for key in ("closed", "spectating", "dirty"):
                party[key] = bool(party[key])
            party.update(members=[], watchers=[], banned=[], roster={}, ads=[], member_check=None)
            by_id[party["id"]] = party
        if not by_id:
            return []
        ids = list(by_id)
        slots = ",".join("?" for _ in ids)
        for pending in conn.execute(f"SELECT * FROM meeting_member_checks WHERE party_id IN ({slots})", ids):
            party = by_id[pending["party_id"]]
            party["member_check"] = dict(pending)
            if pending["error"]:
                party["error"] = "\n".join(filter(None, (party["error"], pending["error"])))
        members = conn.execute(f"SELECT * FROM meeting_members WHERE party_id IN ({slots}) ORDER BY seq", ids)
        for row in members:
            party = by_id[row["party_id"]]
            user = row["user_id"]
            party["members"].append(user)
            entry = {k: row[k] for k in ("character", "job", "role", "note")}
            if any(entry.values()):
                party["roster"][user] = entry
        for table, key in (("meeting_bans", "banned"), ("meeting_watchers", "watchers")):
            for pid, user in conn.execute(f"SELECT party_id,user_id FROM {table} WHERE party_id IN ({slots}) ORDER BY user_id", ids):
                by_id[pid][key].append(user)
        for pid, channel, message in conn.execute(
            f"SELECT party_id,channel_id,message_id FROM meeting_ads WHERE party_id IN ({slots}) ORDER BY message_id", ids
        ):
            by_id[pid]["ads"].append({"channel_id": channel, "message_id": message})
        return list(by_id.values())

    def _get(self, conn, gid, pid):
        pid = _integer(pid, "파티 번호", 1, 2**63 - 1)
        row = conn.execute("SELECT * FROM meeting_parties WHERE guild_id=? AND id=?", (gid, pid)).fetchone()
        if row is None:
            raise MeetingError("이 서버에서 해당 파티를 찾을 수 없어요.")
        return self._snapshots(conn, [row])[0]

    def get(self, gid, pid):
        with self._connection() as conn:
            return self._get(conn, gid, pid)

    @staticmethod
    def _profile(fields, max_capacity):
        values = {}
        for key, value in fields.items():
            if key == "capacity":
                values[key] = _integer(value, "모집 정원", 1, max_capacity)
            elif key == "departure":
                values[key] = None if value is None else _integer(value, "출발 예정 시각", 0, 253402268399)
            elif key in ("destination", "conditions", "description"):
                name, limit = {"destination": ("행선지", 100), "conditions": ("모집 조건", 1000),
                               "description": ("추가 설명", 1000)}[key]
                values[key] = _text(value, name, limit, required=(key == "destination"))
            else:
                raise MeetingError("알 수 없는 파티 설정입니다.")
        return values

    def create(self, gid, actor, **fields):
        actor = _snowflake(actor)
        request = fields.pop("request_id", None)
        request = None if request is None else _text(str(request), "요청 ID", 100, True)
        spectating = _bool(fields.pop("spectating", True), "관전")
        with self._connection(True) as conn:
            if request is not None:
                duplicate = conn.execute("SELECT id,creator_id FROM meeting_parties WHERE guild_id=? AND request_id=?",
                                         (gid, request)).fetchone()
                if duplicate:
                    if actor != duplicate["creator_id"]:
                        raise MeetingError("다른 사용자의 생성 요청입니다.")
                    return self._get(conn, gid, duplicate["id"])
            settings = self._settings(conn, gid)
            if not settings["enabled"]:
                raise MeetingError("이 서버의 계획된 만남 기능이 꺼져 있어요.")
            if settings["max_created"]:
                count = conn.execute("""SELECT COUNT(*) FROM meeting_parties WHERE guild_id=?
                                        AND creator_id=? AND phase!='disbanded'""", (gid, actor)).fetchone()[0]
                if count >= settings["max_created"]:
                    raise MeetingError("사용자별 파티 생성 한도에 도달했어요.")
            if not {"destination", "capacity"} <= fields.keys():
                raise MeetingError("행선지와 파티장 포함 정원을 입력해 주세요.")
            profile = self._profile(fields, settings["max_capacity"])
            now = int(self.clock())
            cursor = conn.execute("""
                INSERT INTO meeting_parties(guild_id,creator_id,leader_id,request_id,nonce,destination,capacity,
                    departure,conditions,description,spectating,created_at,updated_at,activity_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gid, actor, actor, request, secrets.token_hex(16), profile["destination"], profile["capacity"],
                 profile.get("departure"), profile.get("conditions", ""), profile.get("description", ""),
                 spectating, now, now, now))
            pid = cursor.lastrowid
            conn.execute("INSERT INTO meeting_members(party_id,user_id) VALUES (?,?)", (pid, actor))
            self._audit(conn, gid, pid, actor, "create", f"정원 {profile['capacity']}")
            return self._get(conn, gid, pid)

    @staticmethod
    def _set(conn, pid, values):
        # Keys only originate from explicit field allowlists or internal constants.
        if values:
            columns = ", ".join(f"{key}=?" for key in values)
            conn.execute(f"UPDATE meeting_parties SET {columns} WHERE id=?", (*values.values(), pid))

    @staticmethod
    def _remove_user(conn, pid, user):
        conn.execute("DELETE FROM meeting_members WHERE party_id=? AND user_id=?", (pid, user))
        conn.execute("DELETE FROM meeting_watchers WHERE party_id=? AND user_id=?", (pid, user))

    def _admission(self, conn, p, actor, action):
        pid = p["id"]
        if actor in p["banned"]:
            raise MeetingError("이 파티에서 제외되었어요. 파티장에게 재입장 허용을 요청해 주세요.")
        if actor in p["members"]:
            raise MeetingError("이미 참가 중인 파티예요.")
        if action == "watch":
            if not p["spectating"]:
                raise MeetingError("파티장이 관전을 닫았어요.")
            if actor in p["watchers"]:
                raise MeetingError("이미 관전 중이에요.")
            conn.execute("INSERT INTO meeting_watchers(party_id,user_id) VALUES (?,?)", (pid, actor))
            return
        if p["activity"] == "ended" or p["closed"]:
            raise MeetingError("현재 신규 참가를 받지 않는 파티예요.")
        if len(p["members"]) >= p["capacity"]:
            raise MeetingError("이미 정원이 찼어요.")
        conn.execute("INSERT INTO meeting_members(party_id,user_id) VALUES (?,?)", (pid, actor))
        conn.execute("DELETE FROM meeting_watchers WHERE party_id=? AND user_id=?", (pid, actor))

    def _people(self, conn, p, actor, action, fields, manager):
        pid = p["id"]
        if action == "unwatch":
            if actor not in p["watchers"]:
                raise MeetingError("관전 중인 파티가 아니에요.")
            conn.execute("DELETE FROM meeting_watchers WHERE party_id=? AND user_id=?", (pid, actor))
            return {}
        if action == "leave":
            if actor == p["leader_id"]:
                raise MeetingError("파티장은 먼저 다른 참가자에게 위임하거나 파티를 해산해 주세요.")
            if actor not in p["members"]:
                raise MeetingError("참가 중인 파티가 아니에요.")
            self._remove_user(conn, pid, actor)
            return {}
        target = _snowflake(fields.get("target", actor) if action == "roster" else fields.get("target"))
        if action == "kick":
            if target == p["leader_id"]:
                raise MeetingError("파티장을 제외하려면 먼저 파티장을 변경해 주세요.")
            if target not in p["members"] and target not in p["watchers"]:
                raise MeetingError("파티원 또는 관전자를 선택해 주세요.")
            self._remove_user(conn, pid, target)
            conn.execute("INSERT OR IGNORE INTO meeting_bans VALUES (?,?)", (pid, target))
        elif action == "allow":
            if target not in p["banned"]:
                raise MeetingError("재입장이 제한된 사용자가 아니에요.")
            conn.execute("DELETE FROM meeting_bans WHERE party_id=? AND user_id=?", (pid, target))
        elif action == "transfer":
            if target not in p["members"]:
                raise MeetingError("현재 참가 중인 파티원에게만 위임할 수 있어요.")
            return {"leader_id": target}
        elif action == "roster":
            if not manager and target != actor:
                raise MeetingError("본인의 로스터만 수정할 수 있어요.")
            if target not in p["members"]:
                raise MeetingError("로스터는 참가 중인 파티원만 작성할 수 있어요.")
            values = {key: _text(fields[key], "로스터", limit)
                      for key, limit in (("character", 100), ("job", 100), ("role", 100), ("note", 300))
                      if key in fields}
            if values:
                columns = ", ".join(f"{key}=?" for key in values)
                conn.execute(f"UPDATE meeting_members SET {columns} WHERE party_id=? AND user_id=?",
                             (*values.values(), pid, target))
        return {}

    def _reserve_promo(self, p, settings):
        if p["promo_token"]:
            raise MeetingError("이미 홍보를 처리 중이에요. 잠시 뒤 확인해 주세요.")
        if not settings["promo_channel_id"]:
            raise MeetingError("관리자가 자유-채팅 홍보 채널을 먼저 설정해야 해요.")
        if p["activity"] == "ended" or p["closed"] or len(p["members"]) >= p["capacity"]:
            raise MeetingError("빈자리가 있는 모집 중 파티만 홍보할 수 있어요.")
        now = self.clock()
        if p["last_promo"] is not None:
            remaining = math.ceil(p["last_promo"] + settings["cooldown"] - now)
            if remaining > 0:
                raise MeetingError(f"{remaining}초 뒤에 다시 홍보할 수 있어요.")
        return {"promo_token": secrets.token_hex(12), "promo_channel_id": settings["promo_channel_id"],
                "promo_started_at": now}

    def mutate(self, gid, pid, actor, admin, action, **fields):
        """Re-evaluate membership, capacity and party state within one write transaction."""
        actor = _snowflake(actor)
        with self._connection(True) as conn:
            p = self._get(conn, gid, pid)
            settings = self._settings(conn, gid)
            manager = admin is True or p["leader_id"] == actor
            personal = {"join", "leave", "watch", "unwatch", "roster"}
            if action not in personal and not manager:
                raise MeetingError("파티장 또는 관리자만 사용할 수 있어요.")
            if p["phase"] != "ready" and not (action == "disband" and p["phase"] == "creating"):
                raise MeetingError("생성 또는 해산 처리 중이거나 이미 해산된 파티예요.")
            if action in ("join", "watch", "promote") and not settings["enabled"]:
                raise MeetingError("이 서버의 계획된 만남 기능이 꺼져 있어요.")
            changes = {}
            if action in ("join", "watch"):
                self._admission(conn, p, actor, action)
            elif action in ("leave", "unwatch", "kick", "allow", "transfer", "roster"):
                changes = self._people(conn, p, actor, action, fields, manager)
            elif action == "edit":
                changes = self._profile(fields, settings["max_capacity"])
                if changes.get("capacity", p["capacity"]) < len(p["members"]):
                    raise MeetingError("모집 정원을 현재 참가 인원보다 작게 줄일 수 없어요.")
            elif action in ("open_watch", "close_watch"):
                changes["spectating"] = action == "open_watch"
                if not changes["spectating"]:
                    conn.execute("DELETE FROM meeting_watchers WHERE party_id=?", (pid,))
            elif action in ("open", "close"):
                changes["closed"] = action == "close"
            elif action in ("start", "end", "resume"):
                transitions = {"start": (("planned",), "active"), "end": (("planned", "active"), "ended"),
                               "resume": (("ended",), "active")}
                allowed, target = transitions[action]
                if p["activity"] not in allowed:
                    raise MeetingError("현재 활동 상태에서는 실행할 수 없어요.")
                changes["activity"] = target
            elif action == "strategy":
                changes["strategy"] = _text(fields.get("text"), "작전 및 공지", 3500)
            elif action == "promote":
                changes = self._reserve_promo(p, settings)
            elif action == "disband":
                changes["phase"] = "disbanding"
            else:
                raise MeetingError("알 수 없는 파티 동작입니다.")
            # Record field changes and target IDs without duplicating private text.
            detail = {k: [p.get(k), v] for k, v in changes.items()
                      if k not in ("strategy", "promo_token")}
            if "target" in fields:
                detail["target"] = fields["target"]
            now = int(self.clock())
            changes.update(dirty=1, updated_at=now, activity_at=now, retry_count=0, next_retry_at=0)
            self._set(conn, pid, changes)
            self._audit(conn, gid, pid, actor, action, json.dumps(detail, ensure_ascii=False))
            return self._get(conn, gid, pid)

    def undo_admit(self, gid, pid, user, action, previously_watching=False):
        if action not in ("join", "watch"):
            raise ValueError("Unknown admission")
        with self._connection(True) as conn:
            p = self._get(conn, gid, pid)
            if user == p["leader_id"]:
                raise MeetingError("파티장의 참가를 취소할 수 없어요.")
            if action == "join":
                conn.execute("DELETE FROM meeting_members WHERE party_id=? AND user_id=?", (pid, user))
                if previously_watching and p["spectating"] and user not in p["banned"] and p["phase"] == "ready":
                    conn.execute("INSERT OR IGNORE INTO meeting_watchers VALUES (?,?)", (pid, user))
            else:
                conn.execute("DELETE FROM meeting_watchers WHERE party_id=? AND user_id=?", (pid, user))
            self._set(conn, pid, {"dirty": 1})
            self._audit(conn, gid, pid, user, "undo_" + action, "채널 접근 권한 적용 실패", False)
            return self._get(conn, gid, pid)

    def depart(self, gid, pid, user):
        with self._connection(True) as conn:
            p = self._get(conn, gid, pid)
            if p["phase"] in ("disbanding", "disbanded") or user not in p["members"] + p["watchers"]:
                return p
            self._remove_user(conn, pid, user)
            now = int(self.clock())
            updates = {"dirty": 1, "updated_at": now, "activity_at": now, "retry_count": 0, "next_retry_at": 0}
            if user == p["leader_id"]:
                updates["leader_id"] = next((uid for uid in p["members"] if uid != user), None)
                if updates["leader_id"] is None:
                    updates["closed"] = True
            self._set(conn, pid, updates)
            self._audit(conn, gid, pid, user, "depart", "서버 퇴장")
            if "leader_id" in updates:
                self._audit(conn, gid, pid, 0, "transfer", f"{user} → {updates['leader_id']}")
            return self._get(conn, gid, pid)

    def refs(self, gid, pid, **fields):
        allowed = {"phase", "room_id", "guide_id", "recruit_channel_id", "recruit_id",
                   "dirty", "error", "retry_count", "next_retry_at", "ads"}
        if fields.keys() - allowed:
            raise ValueError("Unknown meeting reference field")
        ads = fields.pop("ads", None)
        with self._connection(True) as conn:
            p = self._get(conn, gid, pid)
            allowed_phases = {"creating": {"ready", "disbanding"}, "ready": {"disbanding"},
                              "disbanding": {"disbanded"}, "disbanded": set()}
            phase = fields.get("phase", p["phase"])
            if phase != p["phase"] and phase not in allowed_phases[p["phase"]]:
                raise MeetingError("이미 변경된 파티 상태입니다.")
            if phase == "disbanded":
                conn.execute("DELETE FROM meeting_member_checks WHERE party_id=?", (pid,))
            if ads is not None:
                conn.execute("DELETE FROM meeting_ads WHERE party_id=?", (pid,))
                conn.executemany("INSERT OR IGNORE INTO meeting_ads VALUES (?,?,?)",
                                 ((pid, _snowflake(ad["channel_id"]), _snowflake(ad["message_id"])) for ad in ads))
            if fields.get("error"):
                fields["error"] = str(fields["error"])[:600]
                if fields["error"] != p["error"]:
                    self._audit(conn, gid, pid, 0, "sync", fields["error"], False)
            self._set(conn, pid, fields)
            return self._get(conn, gid, pid)

    def save_member_check(self, gid, pid, failures=0, retry_at=0, error=""):
        with self._connection(True) as conn:
            conn.execute("""
                INSERT INTO meeting_member_checks(party_id,failures,retry_at,error)
                SELECT id,?,?,? FROM meeting_parties WHERE guild_id=? AND id=? AND phase IN ('creating','ready')
                ON CONFLICT(party_id) DO UPDATE SET failures=excluded.failures,
                    retry_at=excluded.retry_at, error=excluded.error""",
                         (failures, retry_at, error, gid, pid))

    def complete_member_check(self, gid, pid):
        with self._connection(True) as conn:
            conn.execute("""DELETE FROM meeting_member_checks WHERE party_id IN
                            (SELECT id FROM meeting_parties WHERE guild_id=? AND id=?)""", (gid, pid))

    def finish_promo(self, gid, pid, token, *, channel_id=None, message_id=None, posted_at=None):
        if (channel_id is None) != (message_id is None):
            raise ValueError("Both channel and message are required")
        with self._connection(True) as conn:
            p = self._get(conn, gid, pid)
            if not token or p["promo_token"] != token:
                return p  # A previous acknowledgement must not settle a more recent send.
            changes = {"promo_token": None, "promo_channel_id": None, "promo_started_at": None}
            if message_id is not None:
                if p["promo_channel_id"] != _snowflake(channel_id):
                    raise MeetingError("홍보 요청의 채널이 일치하지 않아요.")
                conn.execute("INSERT OR IGNORE INTO meeting_ads VALUES (?,?,?)",
                             (pid, channel_id, _snowflake(message_id)))
                now = self.clock()
                # A recovered acknowledgement can arrive long after Discord posted the ad.
                # Keep the confirmed timestamp inside the durable request's lifetime.
                when = now if posted_at is None else min(float(posted_at), now)
                changes["last_promo"] = max(float(p["promo_started_at"]), when)
            self._set(conn, pid, changes)
            self._audit(conn, gid, pid, 0, "promote_result", str(message_id or "게시 실패"), message_id is not None)
            return self._get(conn, gid, pid)

    def _filter(self, conn, gid, kind, user_id):
        clauses = ["p.guild_id=?"]
        params = [gid]
        if kind not in ("recovery", "due"):
            clauses.append("p.phase!='disbanded'")
        if kind in ("open", "watch"):
            clauses.append("p.phase='ready'")
        if kind == "open":
            clauses.extend(["p.closed=0", "p.activity!='ended'",
                            "(SELECT COUNT(*) FROM meeting_members m WHERE m.party_id=p.id)<p.capacity"])
        elif kind == "watch":
            clauses.append("p.spectating=1")
        elif kind == "mine":
            clauses.append("""(EXISTS (SELECT 1 FROM meeting_members m WHERE m.party_id=p.id AND m.user_id=?)
                            OR EXISTS (SELECT 1 FROM meeting_watchers w WHERE w.party_id=p.id AND w.user_id=?))""")
            params.extend([user_id, user_id])
        elif kind in ("recovery", "due"):
            verification = "SELECT party_id FROM meeting_member_checks"
            if kind == "due":
                verification += " WHERE retry_at<=?"
                params.append(int(self.clock()))
            clauses.append(f"""(p.dirty=1 OR p.error!='' OR p.promo_token IS NOT NULL
                             OR p.phase IN ('creating','disbanding') OR p.id IN ({verification}))""")
        elif kind == "errors":
            clauses.append("(p.error!='' OR p.id IN (SELECT party_id FROM meeting_member_checks WHERE error!=''))")
        elif kind == "inactive":
            clauses.append("p.activity_at<=?")
            params.append(int(self.clock()) - self._settings(conn, gid)["inactive_days"] * 86400)
        elif kind != "all":
            raise MeetingError("알 수 없는 목록입니다.")
        return " AND ".join(clauses), params

    def page(self, gid, kind="all", *, user_id=None, query="", offset=0, limit=10):
        offset = _integer(offset, "목록 위치", 0, 2**31 - 1)
        limit = _integer(limit, "목록 크기", 1, 100)
        query = _text(query, "검색어", 100)
        with self._connection() as conn:
            where, args = self._filter(conn, gid, kind, user_id)
            if query:
                where += " AND (instr(lower(p.destination),lower(?))>0 OR CAST(p.id AS TEXT)=? OR CAST(p.leader_id AS TEXT)=?)"
                args.extend((query, query.lstrip("#"), query))
            total = conn.execute(f"SELECT COUNT(*) FROM meeting_parties p WHERE {where}", args).fetchone()[0]
            order = "p.id DESC"
            if kind == "open":
                order = "p.departure IS NULL,p.departure,p.id DESC"
            rows = conn.execute(f"SELECT p.* FROM meeting_parties p WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                                (*args, limit, offset))
            return self._snapshots(conn, rows), total

    def scan(self, gid, after=0, limit=10, dirty_only=False):
        limit = _integer(limit, "목록 크기", 1, 100)
        with self._connection() as conn:
            if dirty_only:
                where, args = self._filter(conn, gid, "due", None)
            else:
                where = "p.guild_id=? AND (p.phase!='disbanded' OR p.dirty OR p.promo_token IS NOT NULL)"
                args = [gid]
            rows = conn.execute(f"SELECT p.* FROM meeting_parties p WHERE {where} AND p.id>? ORDER BY p.id LIMIT ?",
                                (*args, after, limit))
            return self._snapshots(conn, rows)

    def stats(self, gid):
        with self._connection() as conn:
            cutoff = int(self.clock()) - self._settings(conn, gid)["inactive_days"] * 86400
            row = conn.execute("""
                WITH counted AS (
                    SELECT p.*, (SELECT COUNT(*) FROM meeting_members m WHERE m.party_id=p.id) AS seats
                    FROM meeting_parties p WHERE guild_id=? AND phase!='disbanded'
                ) SELECT COUNT(*) total,
                    SUM(phase='ready' AND NOT closed AND activity!='ended' AND seats<capacity) AS open,
                    SUM(phase='ready' AND NOT closed AND activity!='ended' AND seats>=capacity) AS full,
                    SUM(phase='ready' AND closed) AS closed,
                    SUM(activity='active') AS active, SUM(activity='ended') AS ended,
                    SUM(seats) AS members, SUM(activity_at<=?) AS inactive,
                    SUM(error!='' OR id IN (SELECT party_id FROM meeting_member_checks WHERE error!='')) AS errors
                    FROM counted""", (gid, cutoff)).fetchone()
            return {k: int(row[k] or 0) for k in row.keys()}

    def associated(self, gid, user):
        with self._connection() as conn:
            where, args = self._filter(conn, gid, "mine", user)
            return [row[0] for row in conn.execute(f"SELECT p.id FROM meeting_parties p WHERE {where} ORDER BY p.id", args)]

    def guild_ids(self):
        with self._connection() as conn:
            return [row[0] for row in conn.execute("SELECT guild_id FROM meeting_settings ORDER BY guild_id")]

    def for_channel(self, gid, channel_id, msg_id=None):
        if msg_id is not None:
            return next(iter(self.for_messages(gid, channel_id, (msg_id,))), None)
        with self._connection() as conn:
            row = conn.execute("SELECT id FROM meeting_parties WHERE guild_id=? AND room_id=? AND phase!='disbanded'",
                               (gid, channel_id)).fetchone()
            return row[0] if row else None

    def for_messages(self, gid, channel_id, message_ids):
        """Resolve one Discord message-delete event (up to 100 IDs) in one read."""
        ids = tuple(message_ids)
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        args = (gid, channel_id, *ids)
        with self._connection() as conn:
            rows = conn.execute(f"""
                SELECT id FROM meeting_parties WHERE guild_id=? AND room_id=? AND guide_id IN ({marks})
                    AND phase!='disbanded'
                UNION
                SELECT id FROM meeting_parties WHERE guild_id=? AND recruit_channel_id=? AND recruit_id IN ({marks})
                    AND phase!='disbanded'
                UNION
                SELECT p.id FROM meeting_ads a JOIN meeting_parties p ON p.id=a.party_id
                    WHERE p.guild_id=? AND a.channel_id=? AND a.message_id IN ({marks}) AND p.phase!='disbanded'
            """, args * 3)
            return [row[0] for row in rows]

    def touch(self, gid, room_id, at=None):
        now = int(self.clock() if at is None else at)
        with self._connection(True) as conn:
            conn.execute("""UPDATE meeting_parties SET activity_at=? WHERE guild_id=? AND room_id=?
                            AND activity_at<=? AND phase='ready'""", (now, gid, room_id, now - 60))

    def recent_logs(self, gid, limit=10):
        limit = _integer(limit, "목록 크기", 1, 100)
        with self._connection() as conn:
            rows = conn.execute("""SELECT actor_id AS actor,party_id,action,created_at AS at,success,detail
                                FROM meeting_audit WHERE guild_id=? ORDER BY id DESC LIMIT ?""", (gid, limit))
            return [dict(row, success=bool(row["success"])) for row in rows]
