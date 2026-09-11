# /청소로 현재 채널에서 이 봇이 보낸 메시지만 삭제합니다.
import discord


def register(bot):
    @bot.tree.command(
        name="청소",
        description="현재 채널에서 머장봇이 보낸 메시지만 정리합니다.",
    )
    @discord.app_commands.describe(
        개수="최근 몇 개 메시지까지 확인할지 (기본 200, 최대 500)"
    )
    async def cleanup_command(
        interaction: discord.Interaction,
        개수: discord.app_commands.Range[int, 1, 500] = 200,
    ):
        # 결과 안내 자체가 채팅을 더럽히지 않도록 본인에게만 보이게 처리
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.channel
        if channel is None or not hasattr(channel, "history"):
            await interaction.followup.send(
                "이 채널에서는 청소 기능을 사용할 수 없습니다.",
                ephemeral=True,
            )
            return

        # 채널 기록을 읽을 권한이 있어야 과거 머장봇 메시지를 찾을 수 있다.
        if interaction.guild is not None:
            me = interaction.guild.me
            if me is not None:
                permissions = channel.permissions_for(me)
                if not permissions.read_message_history:
                    await interaction.followup.send(
                        "⚠️ 머장봇에게 **메시지 기록 보기** 권한이 필요합니다.",
                        ephemeral=True,
                    )
                    return

        deleted = 0
        failed = 0

        try:
            async for message in channel.history(limit=int(개수)):
                if bot.user is None or message.author.id != bot.user.id:
                    continue

                try:
                    await message.delete()
                    deleted += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1

            result = f"🧹 이 채널에서 머장봇 메시지 **{deleted}개** 정리 완료!"
            if failed:
                result += f"\n삭제하지 못한 메시지: {failed}개"

            await interaction.followup.send(result, ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ 채널 기록을 읽을 권한이 없습니다. "
                "머장봇 권한에서 **메시지 기록 보기**를 허용해주세요.",
                ephemeral=True,
            )
        except Exception as e:
            print("[/청소 오류]", e)
            await interaction.followup.send(
                "청소 중 오류가 발생했습니다. CMD 로그를 확인해주세요.",
                ephemeral=True,
            )
