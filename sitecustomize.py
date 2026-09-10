import sys
from datetime import datetime, timezone

import discord


# =========================================================
# /어구알림 구독 + 어비스 자동알림 멘션 확장
# - app_core.py는 수정하지 않고 client.run 직전에 기능만 추가한다.
# =========================================================

_original_client_run = discord.Client.run


def _install_abyss_subscriptions(client):
    module = sys.modules.get("app_core")
    if module is None:
        return

    if getattr(client, "_merjang_abyss_subscriptions_installed", False):
        return

    def get_conn():
        return module.get_db()

    def ensure_table():
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS abyss_subscribers (
                user_id INTEGER PRIMARY KEY,
                subscribed_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def get_subscriber_ids():
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM abyss_subscribers ORDER BY subscribed_at ASC"
        )
        rows = cursor.fetchall()
        conn.close()
        return [int(row[0]) for row in rows]

    def toggle_subscriber(user_id):
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM abyss_subscribers WHERE user_id = ?",
            (int(user_id),),
        )
        exists = cursor.fetchone() is not None

        if exists:
            cursor.execute(
                "DELETE FROM abyss_subscribers WHERE user_id = ?",
                (int(user_id),),
            )
            subscribed = False
        else:
            cursor.execute(
                """
                INSERT INTO abyss_subscribers (user_id, subscribed_at)
                VALUES (?, ?)
                """,
                (int(user_id), datetime.now(timezone.utc).isoformat()),
            )
            subscribed = True

        cursor.execute("SELECT COUNT(*) FROM abyss_subscribers")
        count = int(cursor.fetchone()[0])
        conn.commit()
        conn.close()
        return subscribed, count

    def mention_chunks(user_ids, max_length=1800):
        chunks = []
        current = []
        current_length = 0

        for user_id in user_ids:
            mention = f"<@{user_id}>"
            extra = len(mention) + (1 if current else 0)
            if current and current_length + extra > max_length:
                chunks.append(" ".join(current))
                current = [mention]
                current_length = len(mention)
            else:
                current.append(mention)
                current_length += extra

        if current:
            chunks.append(" ".join(current))

        return chunks

    ensure_table()

    if module.tree.get_command("어구알림") is None:
        @module.tree.command(
            name="어구알림",
            description="어비스 구멍 자동알림 멘션을 신청하거나 해제합니다.",
        )
        async def abyss_subscription_command(interaction: discord.Interaction):
            if interaction.guild is None:
                await interaction.response.send_message(
                    "이 명령어는 서버 안에서 사용해주세요.",
                    ephemeral=True,
                )
                return

            try:
                subscribed, count = toggle_subscriber(interaction.user.id)

                if subscribed:
                    message = (
                        "🔔 **어구 알림 신청 완료!**\n"
                        "앞으로 어비스 구멍 출현 **60분 / 30분 / 10분 / 1분 전** "
                        "자동알림마다 멘션해드릴게요.\n"
                        f"현재 신청자: **{count}명**"
                    )
                else:
                    message = (
                        "🔕 **어구 알림 해제 완료!**\n"
                        "앞으로 어비스 구멍 자동알림에서 멘션하지 않습니다.\n"
                        f"현재 신청자: **{count}명**"
                    )

                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )
            except Exception as exc:
                print("[/어구알림 오류]", exc)
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "어구 알림 설정 중 오류가 발생했습니다.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "어구 알림 설정 중 오류가 발생했습니다.",
                        ephemeral=True,
                    )

    async def send_abyss_alert_with_mentions(
        channel,
        spawn_utc,
        minutes_before,
        estimated,
    ):
        spawn_kst = spawn_utc.astimezone(module.KST)

        if minutes_before == 1:
            title = "🚨🚨 어비스 구멍 출현 1분 전!"
        elif minutes_before == 10:
            title = "🚨 어비스 구멍 출현 10분 전!"
        else:
            title = f"🕳️ 어비스 구멍 출현 {minutes_before}분 전!"

        description = "어비스 구멍 출현 예정 시간입니다."
        if estimated:
            description += "\n⚠️ 점검 후 예상 시간이라 실제 출현 시각이 변경될 수 있습니다."

        embed = discord.Embed(
            title=title,
            description=description,
        )

        embed.add_field(
            name="📅 날짜",
            value=f"**{module.format_korean_date(spawn_kst)}**",
            inline=False,
        )
        embed.add_field(
            name="🕒 출현 예정",
            value=f"**{module.format_korean_time(spawn_kst)}**",
            inline=True,
        )
        embed.add_field(
            name="⏰ 남은 시간",
            value=f"**{minutes_before}분**",
            inline=True,
        )

        if minutes_before == 1:
            next_spawn_kst = (spawn_utc + module.ABYSS_CYCLE).astimezone(module.KST)
            embed.add_field(
                name="🔁 다음 어비스",
                value=(
                    f"**{module.format_korean_date(next_spawn_kst)} "
                    f"{module.format_korean_time(next_spawn_kst)}**"
                ),
                inline=False,
            )

            if estimated:
                embed.add_field(
                    name="⚠️ 참고",
                    value="현재 시간이 점검 후 예상값이라 다음 회차 시간도 변경될 수 있습니다.",
                    inline=False,
                )

        embed.set_footer(text="머장봇")

        subscriber_ids = get_subscriber_ids()
        chunks = mention_chunks(subscriber_ids)
        allowed_mentions = discord.AllowedMentions(
            users=True,
            roles=False,
            everyone=False,
            replied_user=False,
        )

        if not chunks:
            await channel.send(embed=embed)
            return

        # 신청자가 많아 2,000자 제한을 넘을 경우 멘션을 여러 메시지로 나눈다.
        for chunk in chunks[:-1]:
            await channel.send(
                content=chunk,
                allowed_mentions=allowed_mentions,
            )

        await channel.send(
            content=chunks[-1],
            embed=embed,
            allowed_mentions=allowed_mentions,
        )

        print(
            f"[어구알림] {minutes_before}분 전 자동멘션 "
            f"{len(subscriber_ids)}명"
        )

    module.send_abyss_alert = send_abyss_alert_with_mentions
    client._merjang_abyss_subscriptions_installed = True
    print("[어구알림] 신청/해제 명령어 및 자동멘션 기능 등록 완료")


def _run_with_abyss_subscriptions(self, token, *args, **kwargs):
    _install_abyss_subscriptions(self)
    return _original_client_run(self, token, *args, **kwargs)


discord.Client.run = _run_with_abyss_subscriptions
