# /머장봇테스트로 운영 데이터를 건드리지 않고 활성 기능을 한 번에 점검합니다.
import logging
from datetime import datetime, timezone

import discord

from discord_bot.presenters.abyss import build_abyss_alert, build_abyss_status
from discord_bot.presenters.market import build_market_pages
from discord_bot.presenters.notices import build_notice_embed
from discord_bot.presenters.rune_stats import build_rune_stats_embed
from discord_bot.presenters.server_status import build_status_embed
from sites.erinndata.models import RUNE_CLASSES
from sites.moblife.market import filter_market_items

logger = logging.getLogger(__name__)


def _line(ok, name, detail=""):
    icon = "✅" if ok else "❌"
    suffix = f" — {detail}" if detail else ""
    return f"{icon} **{name}**{suffix}"


def register(bot):
    @bot.tree.command(
        name="머장봇테스트",
        description="현재 활성화된 머장봇 기능을 안전하게 한 번에 점검합니다.",
    )
    @discord.app_commands.default_permissions(manage_guild=True)
    async def merjang_test_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        results = []
        previews = []
        now_utc = datetime.now(timezone.utc)

        # DB / 구독 저장소: 읽기만 한다.
        try:
            with bot.database.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            results.append(_line(True, "DB", "연결 정상"))
        except Exception as e:
            results.append(_line(False, "DB", type(e).__name__))

        try:
            abyss_subs = len(bot.subscriptions.list_ids())
            open_subs = len(bot.open_subscriptions.list_ids())
            results.append(
                _line(
                    True,
                    "알림 구독 저장소",
                    f"어구 {abyss_subs}명 / 오픈 {open_subs}명",
                )
            )
        except Exception as e:
            results.append(_line(False, "알림 구독 저장소", type(e).__name__))

        # 실제 설정된 채널과 권한을 확인한다. 메시지를 지우거나 채널명을 바꾸지는 않는다.
        channel_specs = (
            ("공지 채널", bot.settings.notice_channel_id, False),
            ("어비스 채널", bot.settings.abyss_channel_id, False),
            ("서버상태 채널", bot.settings.server_status_channel_id, True),
        )
        for label, channel_id, needs_manage in channel_specs:
            channel = bot.get_channel(channel_id)
            if channel is None:
                results.append(_line(False, label, f"채널 없음 ({channel_id})"))
                continue
            try:
                me = interaction.guild.me if interaction.guild else None
                perms = channel.permissions_for(me) if me else None
                send_ok = bool(perms and perms.send_messages and perms.embed_links)
                manage_ok = bool(not needs_manage or (perms and perms.manage_channels))
                if send_ok and manage_ok:
                    results.append(_line(True, label, "권한 정상"))
                else:
                    missing = []
                    if not send_ok:
                        missing.append("메시지/임베드")
                    if not manage_ok:
                        missing.append("채널관리")
                    results.append(_line(False, label, "권한 부족: " + ", ".join(missing)))
            except Exception as e:
                results.append(_line(False, label, type(e).__name__))

        # 공홈 공지/업데이트/에린노트 실제 조회 + 최신 글 카드 렌더링.
        groups = None
        try:
            groups = await bot.official.fetch_all()
            counts = [len(group) for group in groups]
            results.append(
                _line(
                    True,
                    "공홈 감시",
                    f"공지 {counts[0]} / 업데이트 {counts[1]} / 에린노트 {counts[2]}",
                )
            )
            latest = next((group[0] for group in groups if group), None)
            if latest:
                preview = build_notice_embed(latest)
                preview.title = "[테스트] " + (preview.title or "공홈 알림")
                previews.append(preview)
        except Exception as e:
            logger.exception("머장봇테스트 공홈 조회 오류")
            results.append(_line(False, "공홈 감시", type(e).__name__))

        # 공식 점검 상태 실제 조회 + 서버상태 카드 렌더링.
        maintenance = None
        try:
            maintenance = await bot.maintenance.fetch()
            state = "점검 중" if maintenance.get("is_maintenance") else "정상 운영"
            upcoming = maintenance.get("next_maintenance_start_time")
            detail = state + (" / 예정 점검 감지" if upcoming else " / 예정 점검 없음")
            results.append(_line(True, "서버상태/점검 감시", detail))
            preview = build_status_embed(maintenance, now_utc)
            preview.title = "[테스트] " + (preview.title or "서버상태")
            previews.append(preview)
        except Exception as e:
            logger.exception("머장봇테스트 점검 조회 오류")
            results.append(_line(False, "서버상태/점검 감시", type(e).__name__))

        # 어비스는 현재 기준점을 읽고 계산만 한다. 제보/알림 전송 기록은 건드리지 않는다.
        try:
            await bot.abyss.refresh()
            if bot.abyss.needs_report:
                results.append(_line(True, "어비스", "점검 후 첫 어구 제보 대기 상태 정상"))
            else:
                spawn, active, estimated = bot.abyss.get_abyss_status(now_utc)
                if spawn is None:
                    results.append(_line(False, "어비스", "다음 출현시간 없음"))
                else:
                    results.append(_line(True, "어비스", "일정 계산 정상"))
                    status = build_abyss_status(spawn, active, estimated, now_utc)
                    status.title = "[테스트] " + (status.title or "어비스")
                    previews.append(status)
                    alert = build_abyss_alert(spawn, 1, estimated)
                    alert.title = "[테스트] " + (alert.title or "어비스 1분 알림")
                    previews.append(alert)
        except Exception as e:
            logger.exception("머장봇테스트 어비스 오류")
            results.append(_line(False, "어비스", type(e).__name__))

        # /시세 OpenAPI 실제 호출. 테스트 검색어는 넓게 잡되 운영 데이터는 변경하지 않는다.
        try:
            items, updated_at = await bot.market.fetch_market_prices("철", limit=4)
            matched = filter_market_items(items, "철")
            results.append(_line(True, "시세 API", f"응답 정상 / 결과 {len(matched)}개"))
            if matched:
                pages = build_market_pages("철", matched, updated_at)
                if pages:
                    for embed in pages[0]:
                        if embed.title:
                            embed.title = "[테스트] " + embed.title
                        previews.append(embed)
        except Exception as e:
            logger.exception("머장봇테스트 시세 오류")
            results.append(_line(False, "시세 API", str(e)[:80]))

        # 룬통계 원본 페이지를 실제 갱신해 파서/캐시까지 확인한다.
        try:
            parsed = await bot.rune_stats.refresh()
            sample = parsed.get(RUNE_CLASSES[0])
            results.append(_line(True, "룬통계", f"실시간 갱신 정상 / {len(parsed)}직업"))
            if sample:
                preview = build_rune_stats_embed(sample)
                preview.title = "[테스트] " + (preview.title or "룬통계")
                previews.append(preview)
        except Exception as e:
            logger.exception("머장봇테스트 룬통계 오류")
            results.append(_line(False, "룬통계", type(e).__name__))

        # /청소는 실제 삭제 대신 현재 채널에서 필요한 권한만 검사한다.
        try:
            channel = interaction.channel
            me = interaction.guild.me if interaction.guild else None
            perms = channel.permissions_for(me) if channel and me else None
            cleanup_ok = bool(perms and perms.read_message_history and perms.manage_messages)
            results.append(
                _line(
                    cleanup_ok,
                    "청소",
                    "권한 정상 (삭제는 테스트에서 실행 안 함)"
                    if cleanup_ok
                    else "메시지 기록 보기/관리 권한 확인 필요",
                )
            )
        except Exception as e:
            results.append(_line(False, "청소", type(e).__name__))

        # 자동 작업 루프가 실제로 살아 있는지 확인한다.
        try:
            running = []
            stopped = []
            for job in bot.jobs:
                loops = []
                for name in ("poll", "refresh", "alerts"):
                    loop = getattr(job, name, None)
                    if loop is not None:
                        loops.append(loop.is_running())
                if loops and all(loops):
                    running.append(job.__class__.__name__)
                elif loops:
                    stopped.append(job.__class__.__name__)
            if stopped:
                results.append(_line(False, "자동 작업", "중지: " + ", ".join(stopped)))
            else:
                results.append(_line(True, "자동 작업", f"{len(running)}개 Job 실행 중"))
        except Exception as e:
            results.append(_line(False, "자동 작업", type(e).__name__))

        passed = sum(line.startswith("✅") for line in results)
        failed = sum(line.startswith("❌") for line in results)
        summary = (
            "🧪 **머장봇 전체 기능 테스트**\n"
            "실제 조회/권한/자동작업을 확인하되 **구독 변경·메시지 삭제·실제 알림 발송은 하지 않습니다.**\n\n"
            + "\n".join(results)
            + f"\n\n**결과: {passed}개 정상 / {failed}개 확인 필요**"
        )
        await interaction.followup.send(summary, ephemeral=True)

        # 실제 출력 모양도 본인에게만 보여준다. Discord 한 메시지 최대 10개 임베드 제한 대응.
        for index in range(0, len(previews), 10):
            await interaction.followup.send(
                content="🧪 **출력 미리보기**" if index == 0 else None,
                embeds=previews[index:index + 10],
                ephemeral=True,
            )
