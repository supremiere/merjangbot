import rune_stats_bootstrap as rune_stats


# /룬통계 출력에서 불필요한 '한줄 요약' 필드 제거
_original_build_embed = rune_stats._build_embed


def _build_embed_without_summary(data):
    embed = _original_build_embed(data)

    for index in range(len(embed.fields) - 1, -1, -1):
        if embed.fields[index].name == "📌 한줄 요약":
            embed.remove_field(index)

    return embed


rune_stats._build_embed = _build_embed_without_summary
