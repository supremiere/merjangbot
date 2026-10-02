import sqlite3
from types import SimpleNamespace

from discord_bot.jobs.server_status import ServerStatusJobs
from storage.database import Database


def test_existing_reminders_migrate_without_duplicate_notifications(tmp_path):
    path = tmp_path / 'existing.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE maintenance_reminders (maintenance_start TEXT PRIMARY KEY, sent_at TEXT NOT NULL)')
        conn.executemany('INSERT INTO maintenance_reminders VALUES (?,?)', [
            ('2026-10-01T21:00:00Z', 'already-sent'),
            ('2026-10-02T05:30:00Z', 'also-sent'),
        ])
    database = Database(path)
    database.initialize()
    database.initialize()
    job = ServerStatusJobs(SimpleNamespace(database=database))
    assert job.reminder_sent('2026-10-01T21:00:00Z', '12h')
    assert job.reminder_sent('2026-10-02T05:30:00Z', '30m')
    assert not job.reminder_sent('2026-10-03T05:30:00Z', '30m')
    job.mark_reminder_sent('2026-10-03T05:30:00Z', '30m', 'now')
    job.mark_reminder_sent('2026-10-03T05:30:00Z', '30m', 'later')
    database.initialize()
    with database.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM maintenance_reminder_events').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(*) FROM maintenance_reminders').fetchone()[0] == 2
