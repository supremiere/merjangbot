# 서버 내 직업 순위를 강조하고 표본 종합 순위와 정산 기준일을 표시합니다.
import discord

from sites.moblife.abyss_ranking import CLASSES, SERVERS, season_day


def safe_text(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


def change_text(delta, unit):
    if delta is None:
        return "비교 기록 없음"
    if delta == 0:
        return "변동 없음"
    return f"{'▲' if delta > 0 else '▼'} {abs(delta):,}{unit}"


def build_abyss_ranking_embed(result):
    entry, snapshot = result.entry, result.snapshot
    server, klass = SERVERS[entry["server"]], CLASSES[entry["klass"]]
    embed = discord.Embed(
        title=f"🏆 {server} {klass} · {entry['rank']:,}위",
        description=f"**{safe_text(entry['character_name'])}**\n어비스 **{entry['score']:,}점**",
        color=0x9333EA,
    )
    previous = snapshot["previous_dates"].get(f"{entry['server']}:{entry['klass']}")
    if entry["is_new"]:
        changes = "✨ 새로 진입한 랭커"
    elif previous:
        changes = (f"직업 순위 {change_text(entry['rank_delta'], '위')} · "
                   f"점수 {change_text(entry['score_delta'], '점')}")
    else:
        changes = "비교할 이전 정산 기록이 없습니다."
    embed.add_field(name=f"직전 정산 대비{f' ({previous})' if previous else ''}",
                    value=changes, inline=False)
    embed.add_field(name="전체 서버 종합 · 집계 랭커 중",
                    value=f"**{result.overall_rank:,}위** / {result.overall_count:,}건", inline=True)
    embed.add_field(name=f"{server} 종합 · 집계 랭커 중",
                    value=f"**{result.server_rank:,}위** / {result.server_count:,}건", inline=True)
    embed.add_field(name=f"전체 서버 {klass} · 집계 랭커 중",
                    value=f"**{result.class_rank:,}위** / {result.class_count:,}건", inline=True)
    embed.add_field(name="시즌 · 정산일",
                    value=f"{season_day(snapshot['season_start'])} ~ {season_day(snapshot['season_end'])}\n"
                          f"**{snapshot['snapshot_date']} 정산**", inline=False)
    if result.stale:
        embed.add_field(name="갱신 지연", value="저장된 이전 기록입니다. 최신 순위와 다를 수 있습니다.",
                        inline=False)
    embed.set_footer(text="서버·직업별 상위 100명 표본 집계 · 종합 순위는 동점 공동순위")
    return embed
