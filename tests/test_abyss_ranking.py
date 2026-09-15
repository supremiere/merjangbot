# 순위 계산·동명이인·원자적 갱신·캐시 복원·명령어 등록을 검증합니다.
import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from bootstrap import create_bot
from config import Settings
from discord_bot.presenters.abyss_ranking import build_abyss_ranking_embed
from discord_bot.commands.abyss_ranking import CharacterPicker
from sites.moblife.abyss_ranking import AbyssRankingService, latest_season, parse_table, season_day
from storage.abyss_ranking import AbyssRankingRepository
from storage.database import Database


def entry(name="Roxxy", server="01", klass="26", score=75025, rank=31, cid="test-id"):
    return dict(character_name=name, server=server, klass=klass, score=score,
                rank=rank, character_id=cid, rank_delta=-2, score_delta=0, is_new=False)


def table(rows=None, day="2026-09-15"):
    rows = rows if rows is not None else [entry()]
    return dict(season_start="2026-08-12T21:00:00Z", snapshot_date=day,
                previous_snapshot_date="2026-09-14", entries=rows, total=len(rows))


def snapshot(rows=None):
    return dict(version=1, season_start="2026-08-12T21:00:00Z", season_end="2026-09-16T21:00:00Z",
                snapshot_date="2026-09-15", fetched_at=datetime.now(timezone.utc).isoformat(),
                previous_dates={"01:26": "2026-09-14"}, entries=rows or [entry()])


class RankingTests(unittest.TestCase):
    def test_scope_ranks_ties_and_name_disambiguation(self):
        service = AbyssRankingService(None, None)
        service.cache = snapshot([
            entry(), entry("Other", score=76000, rank=1, cid="2"),
            entry("Roxxy", server="02", score=78000, rank=1, cid="3"),
            entry("Mage", klass="07", score=77000, rank=1, cid="4"),
            entry("Tie", klass="07", score=75025, rank=2, cid="5"),
        ])
        self.assertEqual(len(service.find(" ＲＯＸＸＹ ")), 2)
        self.assertEqual(service.find("Rox"), [])
        target = service.find("Roxxy", "01")[0]
        result = service.result(target)
        self.assertEqual((result.overall_rank, result.server_rank, result.class_rank), (4, 3, 3))
        self.assertEqual(result.entry["rank"], 31)  # 주 순위는 API 원본을 유지
        self.assertEqual((result.overall_count, result.server_count, result.class_count), (5, 4, 3))

    def test_filter_mismatch_and_malformed_payload_rejected(self):
        for change in ({"server": "02"}, {"klass": "07"}, {"score": True},
                       {"rank": 0}, {"character_id": ""}, {"score_delta": "0"}):
            bad = table()
            bad["entries"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_table(bad, "2026-08-12T21:00:00Z", "01", "26")
        for bad in (None, {**table(), "total": 0}, {**table(), "snapshot_date": None},
                    {**table(), "previous_snapshot_date": "2026-09-16"},
                    table([entry(), entry()]),
                    table([entry(score=10, rank=1), entry(score=20, rank=2, cid="2")])):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                parse_table(bad, "2026-08-12T21:00:00Z", "01", "26")

    def test_null_and_zero_deltas_are_distinct(self):
        payload = table()
        payload["entries"][0]["score_delta"] = None
        parsed = parse_table(payload, "2026-08-12T21:00:00Z", "01", "26")
        self.assertIsNone(parsed["entries"][0]["score_delta"])
        self.assertEqual(parsed["entries"][0]["rank_delta"], -2)

    def test_partial_table_is_not_a_complete_top_100_sample(self):
        incomplete = table()
        incomplete["total"] = 100
        with self.assertRaises(ValueError):
            parse_table(incomplete, "2026-08-12T21:00:00Z", "01", "26")

    def test_api_season_timestamp_kept_for_request_and_displayed_in_korea(self):
        payload = [{"season_start": "2026-08-12T21:00:00Z",
                    "season_end": "2026-09-16T21:00:00Z"}]
        season = latest_season(payload)
        self.assertEqual(season["season_start"], "2026-08-12T21:00:00Z")
        self.assertEqual(season_day(season["season_start"]), "2026-08-13")
        self.assertEqual(season_day(season["season_end"]), "2026-09-17")

    def test_presentation_prioritizes_class_rank_and_labels_sample(self):
        service = AbyssRankingService(None, None)
        service.cache = snapshot()
        result = service.result(service.cache["entries"][0])
        embed = build_abyss_ranking_embed(result)
        self.assertEqual(embed.title, "🏆 데이안 암흑술사 · 31위")
        self.assertIn("75,025", embed.description)
        self.assertIn("▼ 2위", embed.fields[0].value)
        self.assertIn("변동 없음", embed.fields[0].value)
        self.assertTrue(all("집계 랭커 중" in f.name for f in embed.fields[1:4]))
        self.assertIn("상위 100명", embed.footer.text)
        self.assertNotIn("모비라이프", str(embed.to_dict()))
        service.last_error = RuntimeError("offline")
        self.assertIn("갱신 지연", [f.name for f in build_abyss_ranking_embed(service.result(result.entry)).fields])

    def test_repository_round_trip_preserves_existing_subscribers(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "test.db")
            database.initialize()
            with database.connect() as conn:
                conn.execute("INSERT INTO abyss_subscribers VALUES (123, 'today')")
            repo = AbyssRankingRepository(database)
            self.assertIsNone(repo.load())
            data = snapshot()
            repo.save(data)
            self.assertEqual(repo.load(), data)
            database.initialize()
            with database.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM abyss_subscribers").fetchone()[0], 1)

    def test_latest_season_not_response_order(self):
        payload = [{"season_start": "2025-01-01T00:00:00Z", "season_end": "2025-02-01T00:00:00Z"},
                   {"season_start": "2026-08-12T21:00:00Z", "season_end": "2026-09-16T21:00:00Z"}]
        self.assertEqual(latest_season(payload)["season_start"], "2026-08-12T21:00:00Z")


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "cache.db")
        self.database.initialize()
        self.repository = AbyssRankingRepository(self.database)
        self.client = AsyncMock()
        self.service = AbyssRankingService(self.client, self.repository)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def fetch(self, path, *, params=None):
        if path.endswith("/seasons"):
            return [{"season_start": "2026-08-12T21:00:00Z", "season_end": "2026-09-16T21:00:00Z"}]
        return table([entry(server=params["server"], klass=params["klass"])])

    async def test_complete_refresh_and_restoration(self):
        self.client.get.side_effect = self.fetch
        with patch("sites.moblife.abyss_ranking.SERVERS", {"01": "데이안", "02": "아이라"}), \
             patch("sites.moblife.abyss_ranking.CLASSES", {"26": "암흑술사"}):
            await self.service.refresh()
        self.assertEqual(len(self.service.cache["entries"]), 2)
        restored = AbyssRankingService(self.client, self.repository)
        restored.load_cache()
        self.assertEqual(restored.cache, self.service.cache)

    async def test_different_dates_never_replace_saved_snapshot(self):
        old = snapshot()
        self.repository.save(old)
        self.service.load_cache()

        async def fetch(path, *, params=None):
            result = await self.fetch(path, params=params)
            if params and params["server"] == "02":
                result["snapshot_date"] = "2026-09-16"
            return result

        self.client.get.side_effect = fetch
        with patch("sites.moblife.abyss_ranking.SERVERS", {"01": "A", "02": "B"}), \
             patch("sites.moblife.abyss_ranking.CLASSES", {"26": "C"}), self.assertRaises(ValueError):
            await self.service.refresh()
        self.assertEqual(self.repository.load(), old)
        self.assertEqual(self.service.cache, old)

    async def test_save_failure_keeps_memory_cache(self):
        old = snapshot()
        self.service.cache = old
        self.client.get.side_effect = self.fetch
        with patch("sites.moblife.abyss_ranking.SERVERS", {"01": "A"}), \
             patch("sites.moblife.abyss_ranking.CLASSES", {"26": "C"}), \
             patch.object(self.repository, "save", side_effect=OSError("disk full")), \
             self.assertRaises(OSError):
            await self.service.refresh()
        self.assertIs(self.service.cache, old)

    async def test_requests_coalesce_and_failures_back_off(self):
        async def fail():
            await asyncio.sleep(0)
            raise RuntimeError("unavailable")
        self.service.refresh = AsyncMock(side_effect=fail)
        with self.assertLogs("sites.moblife.abyss_ranking", level="ERROR"):
            task = self.service.request_refresh()
            self.assertIs(task, self.service.request_refresh())
            await task
        self.assertIsNone(self.service.request_refresh())
        self.service.refresh.assert_awaited_once()

    async def test_new_command_registered_without_login(self):
        settings = Settings(token="test", notice_channel_id=1, abyss_channel_id=2,
                            server_status_channel_id=3, db_file=Path(self.temp.name) / "bot.db")
        bot = create_bot(settings)
        try:
            names = {c.name for c in bot.tree.get_commands()}
            self.assertTrue({"어비스랭킹", "룬통계", "어비스", "어구알림", "시세", "악보", "청소"} <= names)
            command = bot.tree.get_command("어비스랭킹")
            self.assertEqual([p.name for p in command.parameters], ["닉네임", "서버"])
            self.assertFalse(command.parameters[1].required)
        finally:
            await bot.close()

    async def test_character_picker_rejects_other_users_and_expires(self):
        self.service.cache = snapshot([entry(), entry(server="02")])
        view = CharacterPicker(self.service, self.service.cache["entries"], owner_id=1)
        interaction = SimpleNamespace(user=SimpleNamespace(id=2), response=AsyncMock())
        self.assertFalse(await view.interaction_check(interaction))
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
        view.message = AsyncMock()
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))
        view.message.edit.assert_awaited_once()
        view.stop()

    async def test_picker_keeps_one_snapshot_when_cache_refreshes(self):
        old = snapshot([entry(), entry(server="02")])
        self.service.cache = old
        view = CharacterPicker(self.service, old["entries"], owner_id=1)
        self.service.cache = snapshot([entry(score=80000, rank=1)])
        view.menu = SimpleNamespace(values=["0"])
        interaction = SimpleNamespace(response=AsyncMock())
        await view.select_character(interaction)
        embed = interaction.response.edit_message.call_args.kwargs["embed"]
        self.assertIn("31위", embed.title)
        self.assertIn("75,025", embed.description)
        self.assertIn("갱신 지연", [f.name for f in embed.fields])

    async def test_command_uses_korean_server_filter_without_network(self):
        settings = Settings(token="test", notice_channel_id=1, abyss_channel_id=2,
                            server_status_channel_id=3, db_file=Path(self.temp.name) / "bot.db")
        bot = create_bot(settings)
        bot.abyss_ranking.cache = snapshot([entry(), entry(server="02")])
        bot.abyss_ranking.ensure_cache = AsyncMock(return_value=bot.abyss_ranking.cache)
        interaction = SimpleNamespace(response=AsyncMock(), followup=AsyncMock(),
                                      user=SimpleNamespace(id=1))
        try:
            command = bot.tree.get_command("어비스랭킹")
            await command.callback(interaction, "Roxxy", "데이안")
            embed = interaction.followup.send.call_args.kwargs["embed"]
            self.assertEqual(embed.title, "🏆 데이안 암흑술사 · 31위")
            interaction.response.defer.assert_awaited_once()
            interaction.followup.send.assert_awaited_once()
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
