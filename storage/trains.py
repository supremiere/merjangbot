# 우만열차 좌석 상태와 현황판 메시지 위치를 SQLite에 저장합니다.
from dataclasses import dataclass


TRAIN_CARS = (1, 2, 3)
TRAIN_CAPACITY = 3


class TrainStateError(ValueError):
    def __init__(self, code, *, car_no=None, user_id=None, existing_car=None):
        super().__init__(code)
        self.code = code
        self.car_no = car_no
        self.user_id = user_id
        self.existing_car = existing_car


@dataclass(frozen=True)
class TrainSeat:
    car_no: int
    user_id: int
    role: str


class TrainRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _check_car(car_no):
        car_no = int(car_no)
        if car_no not in TRAIN_CARS:
            raise TrainStateError("invalid_car", car_no=car_no)
        return car_no

    def snapshot(self, guild_id):
        result = {
            car_no: {"conductor_id": None, "passenger_ids": []}
            for car_no in TRAIN_CARS
        }
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT car_no, user_id, role
                FROM train_members
                WHERE guild_id = ?
                ORDER BY car_no,
                         CASE role WHEN 'conductor' THEN 0 ELSE 1 END,
                         user_id
                """,
                (int(guild_id),),
            ).fetchall()
        for car_no, user_id, role in rows:
            if car_no not in result:
                continue
            if role == "conductor":
                result[car_no]["conductor_id"] = int(user_id)
            else:
                result[car_no]["passenger_ids"].append(int(user_id))
        return result

    def find_user(self, guild_id, user_id):
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT car_no, role
                FROM train_members
                WHERE guild_id = ? AND user_id = ?
                """,
                (int(guild_id), int(user_id)),
            ).fetchone()
        if row is None:
            return None
        return TrainSeat(car_no=int(row[0]), user_id=int(user_id), role=str(row[1]))

    def start(self, guild_id, car_no, conductor_id, passenger_ids=()):
        car_no = self._check_car(car_no)
        participants = [int(conductor_id), *(int(value) for value in passenger_ids)]
        if len(participants) > TRAIN_CAPACITY:
            raise TrainStateError("full", car_no=car_no)
        if len(set(participants)) != len(participants):
            raise TrainStateError("duplicate_participants", car_no=car_no)

        with self.db.connect() as conn:
            active = conn.execute(
                "SELECT 1 FROM train_members WHERE guild_id = ? AND car_no = ? LIMIT 1",
                (int(guild_id), car_no),
            ).fetchone()
            if active is not None:
                raise TrainStateError("car_active", car_no=car_no)

            for user_id in participants:
                row = conn.execute(
                    """
                    SELECT car_no
                    FROM train_members
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (int(guild_id), user_id),
                ).fetchone()
                if row is not None:
                    raise TrainStateError(
                        "already_boarded",
                        user_id=user_id,
                        existing_car=int(row[0]),
                    )

            conn.execute(
                """
                INSERT INTO train_members (guild_id, car_no, user_id, role)
                VALUES (?, ?, ?, 'conductor')
                """,
                (int(guild_id), car_no, int(conductor_id)),
            )
            for user_id in passenger_ids:
                conn.execute(
                    """
                    INSERT INTO train_members (guild_id, car_no, user_id, role)
                    VALUES (?, ?, ?, 'passenger')
                    """,
                    (int(guild_id), car_no, int(user_id)),
                )

    def board(self, guild_id, car_no, user_id):
        car_no = self._check_car(car_no)
        guild_id = int(guild_id)
        user_id = int(user_id)

        with self.db.connect() as conn:
            existing = conn.execute(
                """
                SELECT car_no
                FROM train_members
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
            if existing is not None:
                raise TrainStateError(
                    "already_boarded",
                    user_id=user_id,
                    existing_car=int(existing[0]),
                )

            conductor = conn.execute(
                """
                SELECT 1
                FROM train_members
                WHERE guild_id = ? AND car_no = ? AND role = 'conductor'
                """,
                (guild_id, car_no),
            ).fetchone()
            if conductor is None:
                raise TrainStateError("car_inactive", car_no=car_no)

            count = conn.execute(
                """
                SELECT COUNT(*)
                FROM train_members
                WHERE guild_id = ? AND car_no = ?
                """,
                (guild_id, car_no),
            ).fetchone()[0]
            if int(count) >= TRAIN_CAPACITY:
                raise TrainStateError("full", car_no=car_no)

            conn.execute(
                """
                INSERT INTO train_members (guild_id, car_no, user_id, role)
                VALUES (?, ?, ?, 'passenger')
                """,
                (guild_id, car_no, user_id),
            )

    def leave(self, guild_id, user_id):
        guild_id = int(guild_id)
        user_id = int(user_id)
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT car_no, role
                FROM train_members
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
            if row is None:
                raise TrainStateError("not_boarded", user_id=user_id)
            car_no, role = int(row[0]), str(row[1])
            if role == "conductor":
                raise TrainStateError("conductor_cannot_leave", car_no=car_no, user_id=user_id)
            conn.execute(
                "DELETE FROM train_members WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        return car_no

    def end(self, guild_id, car_no, requester_id=None, *, force=False):
        car_no = self._check_car(car_no)
        guild_id = int(guild_id)
        with self.db.connect() as conn:
            conductor = conn.execute(
                """
                SELECT user_id
                FROM train_members
                WHERE guild_id = ? AND car_no = ? AND role = 'conductor'
                """,
                (guild_id, car_no),
            ).fetchone()
            if conductor is None:
                raise TrainStateError("car_inactive", car_no=car_no)
            if not force and int(conductor[0]) != int(requester_id):
                raise TrainStateError("not_conductor", car_no=car_no)
            conn.execute(
                "DELETE FROM train_members WHERE guild_id = ? AND car_no = ?",
                (guild_id, car_no),
            )

    def panel_location(self, guild_id):
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT channel_id, message_id
                FROM train_panels
                WHERE guild_id = ?
                """,
                (int(guild_id),),
            ).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[1])

    def save_panel_location(self, guild_id, channel_id, message_id):
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO train_panels (guild_id, channel_id, message_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id
                """,
                (int(guild_id), int(channel_id), int(message_id)),
            )
