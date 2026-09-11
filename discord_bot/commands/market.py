# /시세 입력을 처리하고 조회 결과 카드와 페이지 버튼을 전송합니다.
import discord

from discord_bot.presenters.market import build_market_pages
from discord_bot.views.pagination import PaginationView
from sites.moblife.market import filter_market_items


def register(bot):
    @bot.tree.command(
        name="시세",
        description="이름에 검색어가 들어가는 모든 아이템의 시세를 조회합니다.",
    )
    @discord.app_commands.describe(아이템="검색할 아이템 이름 또는 일부 단어")
    async def market_price_command(
        interaction: discord.Interaction,
        아이템: str,
    ):
        await interaction.response.defer(thinking=True)

        query = 아이템.strip()

        if not query:
            await interaction.followup.send("검색할 아이템 이름을 입력해주세요.")
            return

        try:
            items, updated_at = await bot.market.fetch_market_prices(query, limit=100)
            matched = filter_market_items(items, query)

            if not matched:
                await interaction.followup.send(
                    f"🔎 **{query}** 가 이름에 들어가는 아이템을 찾지 못했습니다."
                )
                return

            pages = build_market_pages(query, matched, updated_at)

            if len(pages) == 1:
                await interaction.followup.send(embeds=pages[0])
            else:
                view = PaginationView(pages)
                message = await interaction.followup.send(
                    embeds=pages[0],
                    view=view,
                    wait=True,
                )
                view.message = message

        except RuntimeError as e:
            code = str(e)

            if code == "MOBLIFE_API_KEY_MISSING":
                await interaction.followup.send(
                    "모비라이프 OpenAPI 키가 아직 설정되지 않았습니다. "
                    "`.env`에 `MOBLIFE_API_KEY=발급받은키`를 추가해주세요."
                )
            elif code == "MOBLIFE_API_KEY_INVALID":
                await interaction.followup.send(
                    "모비라이프 OpenAPI 키가 올바르지 않거나 만료되었습니다."
                )
            elif code == "MOBLIFE_RATE_LIMIT":
                await interaction.followup.send(
                    "모비라이프 API 요청 한도에 걸렸습니다. 잠시 후 다시 시도해주세요."
                )
            else:
                print("[/시세 오류]", e)
                await interaction.followup.send(
                    "시세를 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
                )

        except Exception as e:
            print("[/시세 오류]", e)
            await interaction.followup.send(
                "시세를 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
            )
