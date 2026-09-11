import re
import sys
import json
import asyncio
from datetime import datetime, timezone, timedelta

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import tasks

RUNE_STATS_URL = "https://erinndata.pages.dev/cheatsheet/"
RUNE_CLASSES = (
    "기사", "대검전사", "전사", "검술사", "궁수", "장궁병", "석궁사수",
    "마법사", "빙결술사", "화염술사", "전격술사", "힐러", "사제", "수도사",
    "암흑술사", "도적", "듀얼블레이드", "격투가", "음유시인", "악사", "댄서",
)
KST = timezone(timedelta(hours=9))


def _clean_line(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("▲", "").replace("＋", "")
    text = text.replace("1픽 교체", "")
    return re.sub(r"\s+", " ", text).strip(" ·")


def _find_index(lines, predicate, start=0):
    for i in range(start, len(lines)):
        if predicate(lines[i]):
            return i
    return None


def _clean_values(values):
    result = []
    skip_exact = {
        "", "확정", "유력", "분기", "제보의 영역", "시청자 제보",
        "장신구 조합", "개조", "세공", "비고", "주류 픽",
    }
    for value in values:
        text = _clean_line(value)
        if not text or text in skip_exact:
            continue
        text = re.sub(r"^(택1\s*·\s*한 자리|세트\s*2자리)\s*", "", text)
        text = re.sub(r"^대체\s*·\s*", "대체: ", text)
        if text and text not in result:
            result.append(text)
    return result


def _parse_accessory(lines):
    start = _find_index(lines, lambda x: "장신구 채용률" in x)
    end = _find_index(lines, lambda x: "방어구" in x and "계열" in x, (start or 0) + 1)
    groups = {"60": [], "30": [], "10": []}
    if start is None:
        return groups
    section = lines[start + 1:end if end is not None else len(lines)]
    current = None
    for raw in section:
        line = _clean_line(raw)
        if not line:
            continue
        matched = False
        for key, pattern in (
            ("60", r"^60%\s*↑?"),
            ("30", r"^(?:30(?:~59)?%|30%)"),
            ("10", r"^(?:10(?:~29)?%|10%)"),
        ):
            m = re.match(pattern, line)
            if m:
                current = key
                remainder = _clean_line(line[m.end():])
                if remainder:
                    groups[key].append(remainder)
                matched = True
                break
        if matched:
            continue
        if current:
            groups[current].append(line)
    return {k: _clean_values(v) for k, v in groups.items()}


def _parse_defense(lines):
    start = _find_index(lines, lambda x: "방어구" in x and "계열" in x)
    end = _find_index(lines, lambda x: x.strip() == "무기", (start or 0) + 1)
    result = {"각성": [], "용문장": [], "침식": [], "그 외": []}
    if start is None:
        return result
    current = None
    section = lines[start + 1:end if end is not None else len(lines)]
    for raw in section:
        line = _clean_line(raw)
        if not line:
            continue
        detected = None
        for label in ("각성", "용문장", "침식", "그 외"):
            if line.startswith(label):
                detected = label
                break
        if detected:
            current = detected
            remainder = line[len(detected):].strip()
            remainder = re.sub(r"^(택1\s*·\s*한 자리|세트\s*2자리)\s*", "", remainder)
            remainder = _clean_line(remainder)
            if remainder:
                result[current].append(remainder)
        elif current:
            result[current].append(line)
    return {k: _clean_values(v) for k, v in result.items()}


def _parse_simple_section(lines, start_label, stop_labels):
    start = _find_index(lines, lambda x: x.strip() == start_label)
    if start is None:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i].strip()
        if any((line == s) or line.startswith(s) for s in stop_labels):
            end = i
            break
    return _clean_values(lines[start + 1:end])


def _find_table_for_heading(heading):
    for tag in heading.find_all_next():
        if tag is heading:
            continue
        if getattr(tag, "name", None) == "h3":
            return None
        if getattr(tag, "name", None) == "table":
            return tag
    return None


def _parse_combo_table(heading):
    table = _find_table_for_heading(heading)
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr"):
        cells = [_clean_line(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
        if not cells or cells[0] in {"장신구 조합", ""}:
            continue
        while len(cells) < 4:
            cells.append("")
        rows.append({
            "combo": cells[0],
            "upgrade": cells[1],
            "engraving": cells[2],
            "note": cells[3],
        })
    return rows[:5]


def _parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text("\n", strip=True)
    all_lines = [_clean_line(x) for x in page_text.splitlines() if _clean_line(x)]
    date_match = re.search(r"데이터\s*기준\s*(\d{4}-\d{2}-\d{2})", page_text)
    data_date = date_match.group(1) if date_match else ""

    headings = {}
    for h3 in soup.find_all("h3"):
        name = _clean_line(h3.get_text(" ", strip=True))
        if name in RUNE_CLASSES:
            headings[name] = h3

    indices = {}
    cursor = 0
    for name in RUNE_CLASSES:
        idx = _find_index(all_lines, lambda x, n=name: x == n, cursor)
        if idx is not None:
            indices[name] = idx
            cursor = idx + 1

    parsed = {}
    for pos, name in enumerate(RUNE_CLASSES):
        if name not in indices:
            continue
        start = indices[name] + 1
        next_positions = [indices[n] for n in RUNE_CLASSES[pos + 1:] if n in indices]
        end = min(next_positions) if next_positions else len(all_lines)
        lines = all_lines[start:end]

        accessory = _parse_accessory(lines)
        defense = _parse_defense(lines)
        weapon = _parse_simple_section(lines, "무기", ("엠블럼",))
        emblem = _parse_simple_section(
            lines,
            "엠블럼",
            ("기본기", "제보의 영역", "장신구 조합"),
        )

        joined = " ".join(lines)
        basic_match = re.search(r"기본기\s*(\d+)%", joined)
        basic_ratio = int(basic_match.group(1)) if basic_match else None
        combos = _parse_combo_table(headings[name]) if name in headings else []

        parsed[name] = {
            "class_name": name,
            "accessory": accessory,
            "defense": defense,
            "weapon": weapon,
            "emblem": emblem,
            "combos": combos,
            "basic_ratio": basic_ratio,
            "data_date": data_date,
        }

    if len(parsed) < 15:
        raise RuntimeError(f"직업 데이터 파싱 수가 너무 적습니다. ({len(parsed)}개)")
    return parsed


def _format_tokens(values):
    if not values:
        return "-"
    return "  ".join(f"`{value}`" for value in values)


def _build_summary(data):
    acc = data.get("accessory", {})
    top = acc.get("60", [])
    mid = acc.get("30", [])
    if len(top) >= 3:
        return f"{' · '.join(top[:3])} 조합이 확정권으로 가장 많이 채용됩니다."
    if len(top) == 2 and mid:
        return f"{' · '.join(top)}는 확정권, 남은 한 자리는 {mid[0]} 중심입니다."
    if top:
        return f"확정권은 {' · '.join(top)} 중심으로 채용됩니다."
    if mid:
        return f"주류 선택지는 {' · '.join(mid)}입니다."
    return "현재 채용 통계를 확인해주세요."


def _build_embed(data):
    name = data["class_name"]
    acc = data.get("accessory", {})
    defense = data.get("defense", {})
    embed = discord.Embed(
        title=f"🟣 {name} 룬 통계",
        description="현재 많이 채용되는 룬 구성을 한눈에 정리했습니다.",
        color=discord.Color.purple(),
    )

    accessory_lines = []
    if acc.get("60"):
        accessory_lines.append(f"🟢 **확정권 · 60%↑**\n{_format_tokens(acc['60'])}")
    if acc.get("30"):
        accessory_lines.append(f"🟡 **주류 · 30~59%**\n{_format_tokens(acc['30'])}")
    if acc.get("10"):
        accessory_lines.append(f"⚪ **대안 · 10~29%**\n{_format_tokens(acc['10'])}")
    embed.add_field(
        name="💍 장신구 채용률",
        value="\n\n".join(accessory_lines) if accessory_lines else "확인 가능한 데이터가 없습니다.",
        inline=False,
    )

    defense_lines = []
    for label, icon in (("각성", "🌗"), ("용문장", "🐉"), ("침식", "🌫️"), ("그 외", "🛡️")):
        values = defense.get(label) or []
        if values:
            defense_lines.append(f"{icon} **{label}**\n{_format_tokens(values)}")
    if defense_lines:
        embed.add_field(name="🛡️ 방어구", value="\n\n".join(defense_lines), inline=False)

    weapon = data.get("weapon") or []
    emblem = data.get("emblem") or []
    embed.add_field(name="⚔️ 무기", value="\n".join(weapon) if weapon else "-", inline=True)
    embed.add_field(name="🏅 엠블럼", value="\n".join(emblem) if emblem else "-", inline=True)

    combos = data.get("combos") or []
    if combos:
        combo_lines = []
        for row in combos[:3]:
            line = f"**{row['combo']}**"
            details = []
            if row.get("upgrade"):
                details.append(f"개조 `{row['upgrade']}`")
            if row.get("engraving"):
                details.append(f"세공 `{row['engraving']}`")
            if row.get("note"):
                details.append(row["note"])
            if details:
                line += "\n" + " · ".join(details)
            combo_lines.append(line)
        embed.add_field(name="🧩 많이 쓰는 장신구 조합", value="\n\n".join(combo_lines), inline=False)

    basic_ratio = data.get("basic_ratio")
    if basic_ratio is not None:
        embed.add_field(name="📊 기본기 잔존율", value=f"**{basic_ratio}%**", inline=True)

    embed.add_field(name="📌 한줄 요약", value=_build_summary(data), inline=False)
    date_text = data.get("data_date") or "확인 불가"
    embed.set_footer(text=f"머장봇 · 데이터 {date_text}")
    return embed


def install_rune_stats(client):
    module = sys.modules.get("app_core")
    if module is None or getattr(client, "_merjang_rune_stats_installed", False):
        return

    cache = {}
    refresh_lock = asyncio.Lock()

    def ensure_table():
        conn = module.get_db()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS rune_stats_cache (
                class_name TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                data_date TEXT,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def load_cache():
        conn = module.get_db()
        cur = conn.cursor()
        cur.execute("SELECT class_name, data_json FROM rune_stats_cache")
        rows = cur.fetchall()
        conn.close()
        for class_name, data_json in rows:
            try:
                data = json.loads(data_json)
                if isinstance(data, dict):
                    cache[class_name] = data
            except Exception:
                continue

    def save_cache(parsed):
        fetched_at = datetime.now(timezone.utc).isoformat()
        conn = module.get_db()
        cur = conn.cursor()
        for class_name, data in parsed.items():
            cur.execute(
                """
                INSERT INTO rune_stats_cache (class_name, data_json, data_date, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(class_name) DO UPDATE SET
                    data_json=excluded.data_json,
                    data_date=excluded.data_date,
                    fetched_at=excluded.fetched_at
                """,
                (
                    class_name,
                    json.dumps(data, ensure_ascii=False),
                    data.get("data_date") or "",
                    fetched_at,
                ),
            )
        conn.commit()
        conn.close()

    async def refresh_once():
        async with refresh_lock:
            timeout = aiohttp.ClientTimeout(total=20)
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; MerjangBot/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(RUNE_STATS_URL) as response:
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                    html = await response.text()
            parsed = _parse_page(html)
            cache.clear()
            cache.update(parsed)
            save_cache(parsed)
            print(f"[룬통계] 데이터 갱신 완료 ({len(parsed)}직업)")
            return parsed

    ensure_table()
    load_cache()

    if module.tree.get_command("룬통계") is None:
        @module.tree.command(
            name="룬통계",
            description="직업별 룬 채용 통계를 확인합니다.",
        )
        @discord.app_commands.describe(직업="확인할 직업")
        async def rune_stats_command(interaction: discord.Interaction, 직업: str):
            await interaction.response.defer(thinking=True)
            class_name = (직업 or "").strip()
            if class_name not in RUNE_CLASSES:
                await interaction.followup.send(
                    "직업을 찾지 못했습니다. 직업 자동완성에서 선택해주세요."
                )
                return

            data = cache.get(class_name)
            if data is None:
                try:
                    await refresh_once()
                except Exception as e:
                    print("[/룬통계 초기 갱신 오류]", e)
                data = cache.get(class_name)

            if data is None:
                await interaction.followup.send(
                    "룬 통계를 불러오지 못했습니다. 잠시 후 다시 시도해주세요."
                )
                return

            await interaction.followup.send(embed=_build_embed(data))

        @rune_stats_command.autocomplete("직업")
        async def rune_stats_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ):
            needle = (current or "").strip().lower()
            names = [name for name in RUNE_CLASSES if not needle or needle in name.lower()]
            return [
                discord.app_commands.Choice(name=name, value=name)
                for name in names[:25]
            ]

    @tasks.loop(hours=6)
    async def refresh_rune_stats():
        try:
            await refresh_once()
        except Exception as e:
            print("[룬통계] 자동 갱신 실패 - 기존 캐시 유지:", e)

    @refresh_rune_stats.before_loop
    async def before_refresh_rune_stats():
        await client.wait_until_ready()

    original_setup_hook = client.setup_hook

    async def setup_hook_with_rune_stats():
        await original_setup_hook()
        if not refresh_rune_stats.is_running():
            refresh_rune_stats.start()
            print("[룬통계] 자동 갱신 시작 (6시간 주기)")

    client.setup_hook = setup_hook_with_rune_stats
    client._merjang_rune_stats_task = refresh_rune_stats
    client._merjang_rune_stats_installed = True
    print(f"[룬통계] 명령어 등록 완료 / 캐시 {len(cache)}직업")


_original_client_run = discord.Client.run


def _run_with_rune_stats(self, token, *args, **kwargs):
    install_rune_stats(self)
    return _original_client_run(self, token, *args, **kwargs)


discord.Client.run = _run_with_rune_stats
