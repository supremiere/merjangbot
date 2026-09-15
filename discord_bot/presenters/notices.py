# 공식 공지·업데이트·에린 노트 링크를 디스코드 카드로 만듭니다.
import discord


def build_notice_embed(post):
    category = post["category"]

    if category == "공지":
        icon = "📢"
        description = "마비노기 모바일 공식 홈페이지에 새 공지사항이 올라왔습니다."
    elif category == "업데이트":
        icon = "🔧"
        description = "마비노기 모바일 공식 홈페이지에 새 업데이트 글이 올라왔습니다."
    elif category == "에린노트":
        icon = "📓"
        description = "마비노기 모바일 공식 홈페이지 에린 노트에 새 글이 올라왔습니다."
    else:
        icon = "📰"
        description = "마비노기 모바일 공식 홈페이지에 새 글이 올라왔습니다."

    embed = discord.Embed(
        title=f"{icon} {post['title']}",
        url=post["url"],
        description=description,
    )

    embed.add_field(
        name="바로가기",
        value=f"[👉 해당 글 바로 보기]({post['url']})",
        inline=False,
    )
    embed.set_footer(text="머장봇")

    return embed
