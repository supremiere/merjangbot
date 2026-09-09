import os
import re
import json
import base64
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import tasks
from dotenv import load_dotenv
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding


# =========================================================
# 기본 설정
# - /시세: 슬래시 명령어만 사용 (부분검색 결과 전부 표시)
# - /어비스: 60/30/10/1분 전 자동 알림, 1분 전 다음 회차 표시
# - /악보: 모비라이프 악보 보관소 제목/제작자 검색 + 버튼 페이지 넘김
# - /청소: 현재 채널의 머장봇 메시지만 정리
# - 글로벌/서버 명령어 중복 자동 정리
# - DB 자동청소: 공지 최근 500개 / 어비스 최근 30일 / 주 1회 VACUUM
# - 호스팅 환경의 모비라이프 루트 403 우회: EAB 키 로컬 사용
# =========================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
NOTICE_CHANNEL_ID = int(os.getenv("NOTICE_CHANNEL_ID"))
ABYSS_CHANNEL_ID = int(os.getenv("ABYSS_CHANNEL_ID"))
BOT_CHANNEL_ID = int(os.getenv("BOT_CHANNEL_ID"))

BASE_URL = "https://mabinogimobile.nexon.com"
NOTICE_URL = f"{BASE_URL}/News/Notice"
UPDATE_URL = f"{BASE_URL}/News/Update"

MOBLIFE_PROXY_BASE = os.getenv("MOBLIFE_PROXY_BASE", "https://moblife-proxy.ninemailz.workers.dev").strip().rstrip("/")

if MOBLIFE_PROXY_BASE:
    MOBLIFE_HOME = f"{MOBLIFE_PROXY_BASE}/"
    MOBLIFE_EAB_URL = f"{MOBLIFE_PROXY_BASE}/d/api/v1/eab"
    MOBLIFE_SHEETS_URL = f"{MOBLIFE_PROXY_BASE}/sheets"
    MOBLIFE_SHEETS_API_URL = f"{MOBLIFE_PROXY_BASE}/d/api/v1/sheet-music"
else:
    MOBLIFE_HOME = "https://mabimobi.life/"
    MOBLIFE_EAB_URL = "https://mabimobi.life/d/api/v1/eab"
    MOBLIFE_SHEETS_URL = "https://mabimobi.life/sheets"
    MOBLIFE_SHEETS_API_URL = "https://mabimobi.life/d/api/v1/sheet-music"

# 시세 OpenAPI는 별도 도메인이라 프록시하지 않음
MOBLIFE_OPENAPI_URL = "https://open.mabimobi.life/v1"
MOBLIFE_API_KEY = os.getenv("MOBLIFE_API_KEY")
MOBLIFE_EAB_KEY = os.getenv("MOBLIFE_EAB_KEY", "8nvov88uc5k4o4g6apax04783thjo11l")

DB_FILE = "data.db"

KST = timezone(timedelta(hours=9))
ABYSS_CYCLE = timedelta(hours=36, minutes=15)
ABYSS_ALERT_MINUTES = (60, 30, 10, 1)


# =========================================================
# Discord 설정
# =========================================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)


# =========================================================
# 런타임 상태
# =========================================================

moblife_key = MOBLIFE_EAB_KEY
abyss_anchor = None
commands_synced = False


# =========================================================
# DB
# =========================================================

def get_db():
    return sqlite3.connect(DB_FILE)


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_posts (
            url TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS abyss_alerts (
            spawn_time TEXT NOT NULL,
            minutes_before INTEGER NOT NULL,
            PRIMARY KEY (spawn_time, minutes_before)
        )
    """)

    conn.commit()
    conn.close()


def is_post_sent(url):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_posts WHERE url = ?", (url,))
    result = cursor.fetchone()
    conn.close()
    return result is not None


def save_post(post):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO sent_posts (url, category, title)
        VALUES (?, ?, ?)
        """,
        (post["url"], post["category"], post["title"]),
    )
    conn.commit()
    conn.close()


def official_db_is_empty():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM sent_posts")
    count = cursor.fetchone()[0]
    conn.close()
    return count == 0


def is_abyss_alert_sent(spawn_time, minutes_before):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 1
        FROM abyss_alerts
        WHERE spawn_time = ? AND minutes_before = ?
        """,
        (spawn_time, minutes_before),
    )
    result = cursor.fetchone()
    conn.close()
    return result is not None


def save_abyss_alert(spawn_time, minutes_before):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO abyss_alerts (spawn_time, minutes_before)
        VALUES (?, ?)
        """,
        (spawn_time, minutes_before),
    )
    conn.commit()
    conn.close()


def cleanup_database():
    """
    DB 자동 정리
    - 공식 공지/업데이트 기록: 최근 500개만 유지
    - 어비스 알림 기록: 30일 지난 기록 삭제
    - VACUUM으로 실제 DB 파일 크기도 정리
    """
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM sent_posts")
    posts_before = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM abyss_alerts")
    abyss_before = cursor.fetchone()[0]

    # sent_posts에는 별도 생성시각 컬럼이 없으므로 SQLite rowid(삽입 순서) 기준으로
    # 최근 500개만 남긴다.
    cursor.execute(
        """
        DELETE FROM sent_posts
        WHERE rowid NOT IN (
            SELECT rowid
            FROM sent_posts
            ORDER BY rowid DESC
            LIMIT 500
        )
        """
    )

    # 어비스 중복방지 기록은 30일이면 충분하다.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cursor.execute(
        "DELETE FROM abyss_alerts WHERE spawn_time < ?",
        (cutoff,),
    )

    cursor.execute("SELECT COUNT(*) FROM sent_posts")
    posts_after = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM abyss_alerts")
    abyss_after = cursor.fetchone()[0]

    conn.commit()

    # DELETE만 하면 SQLite 파일 크기가 바로 줄지 않을 수 있어서
    # 주 1회 VACUUM으로 실제 파일도 압축한다.
    cursor.execute("VACUUM")
    conn.close()

    print(
        "[DB] 자동청소 완료 | "
        f"공지/업데이트 {posts_before}→{posts_after}개 | "
        f"어비스 {abyss_before}→{abyss_after}개"
    )


# =========================================================
# 공홈 게시물
# =========================================================

async def get_posts(page_url, category):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/152 Safari/537.36"
        )
    }

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(page_url) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"{category} 페이지 접속 실패 (HTTP {response.status})"
                )
            html = await response.text()

    soup = BeautifulSoup(html, "html.parser")

    if category == "공지":
        pattern = re.compile(r"^/News/Notice/\d+", re.IGNORECASE)
    else:
        pattern = re.compile(r"^/News/Update/\d+", re.IGNORECASE)

    posts = []
    found_urls = set()

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        title = link.get_text(" ", strip=True)

        if not pattern.match(href) or not title:
            continue

        full_url = BASE_URL + href if href.startswith("/") else href

        if full_url in found_urls:
            continue

        found_urls.add(full_url)
        posts.append(
            {
                "category": category,
                "title": title,
                "url": full_url,
            }
        )

    return posts


async def send_post(channel, post):
    if post["category"] == "공지":
        icon = "📢"
        description = "마비노기 모바일 공식 홈페이지에 새 공지사항이 올라왔습니다."
    else:
        icon = "🔧"
        description = "마비노기 모바일 공식 홈페이지에 새 업데이트 글이 올라왔습니다."

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

    await channel.send(embed=embed)


async def initialize_existing_posts():
    print("기존 공지 목록을 등록하는 중...")

    notice_posts = await get_posts(NOTICE_URL, "공지")
    update_posts = await get_posts(UPDATE_URL, "업데이트")

    for post in notice_posts:
        save_post(post)

    for post in update_posts:
        save_post(post)

    print(
        f"기존 공지 {len(notice_posts)}개 / "
        f"업데이트 {len(update_posts)}개 등록 완료"
    )
    print("이후 새로 올라오는 글부터 알림을 보냅니다.")


@tasks.loop(minutes=1)
async def check_official_site():
    channel = client.get_channel(NOTICE_CHANNEL_ID)

    if channel is None:
        print("[공홈] 공홈공지 채널을 찾을 수 없습니다.")
        return

    try:
        notice_posts = await get_posts(NOTICE_URL, "공지")
        update_posts = await get_posts(UPDATE_URL, "업데이트")

        # 각 게시판은 최신순이므로 오래된 글부터 보내기
        for posts in (notice_posts, update_posts):
            new_posts = [post for post in posts if not is_post_sent(post["url"])]

            for post in reversed(new_posts):
                await send_post(channel, post)
                save_post(post)
                print(f"[새 {post['category']}] {post['title']}")

    except Exception as e:
        print("[공홈 감시 오류]", e)


@check_official_site.before_loop
async def before_official_check():
    await client.wait_until_ready()


# =========================================================
# DB 자동청소
# =========================================================

@tasks.loop(hours=168)
async def cleanup_database_weekly():
    try:
        cleanup_database()
    except Exception as e:
        print("[DB 자동청소 오류]", e)


@cleanup_database_weekly.before_loop
async def before_database_cleanup():
    await client.wait_until_ready()


# =========================================================
# 모비라이프 어비스 데이터
# =========================================================

async def fetch_text(session, url):
    async with session.get(url) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {url}")
        return await response.text()


async def discover_moblife_key(session):
    """모비라이프 공개 프론트 JS에서 현재 EAB 복호화 키를 찾는다."""
    html = await fetch_text(session, MOBLIFE_HOME)
    soup = BeautifulSoup(html, "html.parser")

    script_urls = []
    for tag in soup.find_all("script", src=True):
        script_urls.append(urljoin(MOBLIFE_HOME, tag["src"]))

    for script_url in script_urls:
        try:
            text = await fetch_text(session, script_url)
        except Exception:
            continue

        if "DH:()=>" not in text:
            continue

        # DH:()=>d 형태에서 변수명 d를 얻는다.
        var_match = re.search(r"DH:\(\)=>([A-Za-z_$][A-Za-z0-9_$]*)", text)
        if not var_match:
            continue

        variable = var_match.group(1)
        start = max(0, var_match.start() - 1000)
        end = min(len(text), var_match.start() + 5000)
        nearby = text[start:end]

        key_match = re.search(
            rf"\b{re.escape(variable)}\s*=\s*\"([^\"]{{16,64}})\"",
            nearby,
        )

        if key_match:
            return key_match.group(1)

    raise RuntimeError("모비라이프 어비스 복호화 키를 찾지 못했습니다.")


def split_eab_payload(payload):
    if len(payload) < 12:
        raise ValueError("EAB payload가 너무 짧습니다.")

    tail_length = int(payload[1:3])
    iv_padding = int(payload[3:4])
    data_padding = int(payload[0:1])

    iv_base64 = (
        payload[4:14][::-1]
        + payload[-tail_length:]
        + ("=" * iv_padding)
    )

    encrypted_base64 = (
        payload[14:-tail_length]
        + ("=" * data_padding)
    )

    iv = base64.b64decode(iv_base64)
    encrypted = base64.b64decode(encrypted_base64)

    return iv, encrypted


def decrypt_eab_payload(payload, key):
    iv, encrypted = split_eab_payload(payload)
    key_bytes = key.encode("utf-8")

    decryptor = Cipher(
        algorithms.AES(key_bytes),
        modes.CBC(iv),
    ).decryptor()

    padded_plain = decryptor.update(encrypted) + decryptor.finalize()

    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded_plain) + unpadder.finalize()

    return json.loads(plain.decode("utf-8"))


async def get_abyss_records(force_key_refresh=False):
    global moblife_key

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/152 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": MOBLIFE_HOME,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        if moblife_key is None:
            moblife_key = MOBLIFE_EAB_KEY
            print("[어비스] 내장 데이터 키 사용")

        if force_key_refresh:
            # 호스팅 사업자 IP에서 mabimobi.life 루트가 403일 수 있으므로
            # 자동 키 탐색은 최후 수단으로만 시도한다.
            try:
                moblife_key = await discover_moblife_key(session)
                print("[어비스] 모비라이프 데이터 키 새로 확인 완료")
            except Exception as e:
                print(f"[어비스] 키 자동갱신 실패, 기존 키 유지: {e}")
                moblife_key = MOBLIFE_EAB_KEY

        async with session.get(MOBLIFE_EAB_URL) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"모비라이프 EAB 요청 실패 (HTTP {response.status})"
                )
            data = await response.json()

    payload = data.get("payload")
    if not payload:
        raise RuntimeError("모비라이프 EAB 응답에 payload가 없습니다.")

    try:
        records = decrypt_eab_payload(payload, moblife_key)
    except Exception as decrypt_error:
        # 사이트 키가 실제로 변경된 경우에만 자동 탐색을 한 번 시도한다.
        if not force_key_refresh:
            return await get_abyss_records(force_key_refresh=True)
        raise RuntimeError(f"어비스 데이터 복호화 실패: {decrypt_error}")

    if not isinstance(records, list):
        raise RuntimeError("어비스 데이터 형식이 예상과 다릅니다.")

    return records


def parse_iso_datetime(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def select_latest_abyss_record(records):
    valid = []

    for record in records:
        if not isinstance(record, dict):
            continue

        start = record.get("start_datetime")
        if not start:
            continue

        try:
            start_dt = parse_iso_datetime(start)
        except Exception:
            continue

        valid.append((start_dt, record))

    if not valid:
        return None

    valid.sort(key=lambda item: item[0], reverse=True)
    return valid[0][1]


def get_next_abyss_spawn(now_utc):
    if not abyss_anchor:
        return None, False

    start_text = abyss_anchor.get("start_datetime")
    if not start_text:
        return None, False

    spawn = parse_iso_datetime(start_text)
    is_estimated = bool(abyss_anchor.get("is_post_maintenance_estimate", False))

    # 점검 후 추정값은 사이트에서 갱신될 때까지 그 1회 예상시간만 사용
    if is_estimated:
        if spawn <= now_utc:
            return None, True
        return spawn, True

    # 일반 데이터는 모비라이프와 동일하게 36시간 15분 주기로 다음 시간을 계산
    if spawn <= now_utc:
        elapsed = now_utc - spawn
        cycles = int(elapsed.total_seconds() // ABYSS_CYCLE.total_seconds()) + 1
        spawn = spawn + (ABYSS_CYCLE * cycles)

    return spawn, False


async def refresh_abyss_once():
    global abyss_anchor

    records = await get_abyss_records()
    latest = select_latest_abyss_record(records)

    if latest is None:
        raise RuntimeError("사용 가능한 어비스 출현 데이터가 없습니다.")

    old_start = abyss_anchor.get("start_datetime") if abyss_anchor else None
    abyss_anchor = latest
    new_start = latest.get("start_datetime")

    if old_start != new_start:
        next_spawn, estimated = get_next_abyss_spawn(datetime.now(timezone.utc))

        if next_spawn:
            kst = next_spawn.astimezone(KST)
            suffix = " (점검 후 예상)" if estimated else ""
            print(
                "[어비스] 기준시간 갱신:",
                kst.strftime("%Y-%m-%d %H:%M:%S"),
                "KST" + suffix,
            )


@tasks.loop(minutes=1)
async def refresh_abyss_data():
    try:
        await refresh_abyss_once()
    except Exception as e:
        print("[어비스 데이터 오류]", e)


@refresh_abyss_data.before_loop
async def before_refresh_abyss():
    await client.wait_until_ready()


WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")


def format_korean_date(dt_kst):
    weekday = WEEKDAYS_KO[dt_kst.weekday()]
    return f"{dt_kst.year}년 {dt_kst.month}월 {dt_kst.day}일 ({weekday})"


def format_korean_time(dt_kst):
    hour = dt_kst.hour
    minute = dt_kst.minute

    if hour == 0:
        return f"오전 12:{minute:02d}"
    if hour < 12:
        return f"오전 {hour}:{minute:02d}"
    if hour == 12:
        return f"오후 12:{minute:02d}"
    return f"오후 {hour - 12}:{minute:02d}"


async def send_abyss_alert(channel, spawn_utc, minutes_before, estimated):
    spawn_kst = spawn_utc.astimezone(KST)

    if minutes_before == 1:
        title = "🚨🚨 어비스 구멍 출현 1분 전!"
    elif minutes_before == 10:
        title = "🚨 어비스 구멍 출현 10분 전!"
    else:
        title = f"🕳️ 어비스 구멍 출현 {minutes_before}분 전!"

    description = "모비라이프 기준 어비스 구멍 출현 예정 시간입니다."
    if estimated:
        description += "\n⚠️ 점검 후 예상 시간이라 실제 출현 시각이 변경될 수 있습니다."

    embed = discord.Embed(
        title=title,
        description=description,
    )

    embed.add_field(
        name="📅 날짜",
        value=f"**{format_korean_date(spawn_kst)}**",
        inline=False,
    )
    embed.add_field(
        name="🕒 출현 예정",
        value=f"**{format_korean_time(spawn_kst)}**",
        inline=True,
    )
    embed.add_field(
        name="⏰ 남은 시간",
        value=f"**{minutes_before}분**",
        inline=True,
    )

    # 1분 전 알림에서는 다음 회차의 출현 시간도 같이 안내한다.
    if minutes_before == 1:
        next_spawn_kst = (spawn_utc + ABYSS_CYCLE).astimezone(KST)
        embed.add_field(
            name="🔁 다음 어비스",
            value=(
                f"**{format_korean_date(next_spawn_kst)} "
                f"{format_korean_time(next_spawn_kst)}**"
            ),
            inline=False,
        )

        if estimated:
            embed.add_field(
                name="⚠️ 참고",
                value="현재 시간이 점검 후 예상값이라 다음 회차 시간도 변경될 수 있습니다.",
                inline=False,
            )

    embed.set_footer(text="머장봇 · 모비라이프 기준")

    await channel.send(embed=embed)


@tasks.loop(seconds=15)
async def check_abyss_alerts():
    if not abyss_anchor:
        return

    channel = client.get_channel(ABYSS_CHANNEL_ID)
    if channel is None:
        print("[어비스] 어비스알림 채널을 찾을 수 없습니다.")
        return

    now_utc = datetime.now(timezone.utc)
    spawn_utc, estimated = get_next_abyss_spawn(now_utc)

    if not spawn_utc:
        return

    remaining_seconds = (spawn_utc - now_utc).total_seconds()
    spawn_key = spawn_utc.isoformat()

    for minutes_before in ABYSS_ALERT_MINUTES:
        target_seconds = minutes_before * 60

        # 기준 시점부터 약 1분 안쪽에서 전송.
        # 15초 주기로 확인하므로 일반적인 지연/네트워크 흔들림을 흡수한다.
        if target_seconds - 59 <= remaining_seconds <= target_seconds:
            if is_abyss_alert_sent(spawn_key, minutes_before):
                continue

            await send_abyss_alert(
                channel,
                spawn_utc,
                minutes_before,
                estimated,
            )
            save_abyss_alert(spawn_key, minutes_before)

            print(
                f"[어비스 알림] {minutes_before}분 전 / "
                f"{spawn_utc.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S')} KST"
            )


@check_abyss_alerts.before_loop
async def before_check_abyss():
    await client.wait_until_ready()



# =========================================================
# /어비스 명령어
# =========================================================

def get_abyss_status(now_utc):
    """현재 출현 중인지, 아니면 다음 출현 시각이 언제인지 계산한다."""
    if not abyss_anchor:
        return None, False, False

    start_text = abyss_anchor.get("start_datetime")
    if not start_text:
        return None, False, False

    spawn = parse_iso_datetime(start_text)
    estimated = bool(abyss_anchor.get("is_post_maintenance_estimate", False))

    # 점검 후 예상값은 아직 오지 않은 1회 예상 시각만 표시
    if estimated:
        if spawn <= now_utc:
            return None, False, True
        return spawn, False, True

    active_duration = timedelta(minutes=15)

    # 기준 시각에서 36시간 15분 단위로 현재/다음 회차를 찾는다.
    while spawn + active_duration <= now_utc:
        spawn += ABYSS_CYCLE

    is_active = spawn <= now_utc < spawn + active_duration
    return spawn, is_active, False


def format_remaining_time(seconds):
    seconds = max(0, int(seconds))
    minutes = (seconds + 59) // 60

    if minutes < 1:
        return "1분 미만"

    hours, mins = divmod(minutes, 60)

    if hours and mins:
        return f"{hours}시간 {mins}분"
    if hours:
        return f"{hours}시간"
    return f"{mins}분"


@tree.command(
    name="어비스",
    description="어비스 구멍의 현재 상태와 다음 출현 시간을 확인합니다.",
)
async def abyss_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        # 명령어를 칠 때마다 모비라이프 데이터를 한 번 새로 읽는다.
        await refresh_abyss_once()

        now_utc = datetime.now(timezone.utc)
        spawn_utc, is_active, estimated = get_abyss_status(now_utc)

        if spawn_utc is None:
            await interaction.followup.send(
                "지금은 어비스 출현 시간을 확인할 수 없습니다. 잠시 후 다시 시도해주세요."
            )
            return

        spawn_kst = spawn_utc.astimezone(KST)

        if is_active:
            active_end = spawn_utc + timedelta(minutes=15)
            remaining = (active_end - now_utc).total_seconds()

            embed = discord.Embed(
                title="🕳️ 어비스 구멍 출현 중!",
                description="모비라이프 기준 현재 어비스 구멍이 출현 중입니다.",
            )
            embed.add_field(
                name="📅 날짜",
                value=f"**{format_korean_date(spawn_kst)}**",
                inline=False,
            )
            embed.add_field(
                name="🕒 출현 시각",
                value=f"**{format_korean_time(spawn_kst)}**",
                inline=True,
            )
            embed.add_field(
                name="⏰ 상태",
                value=f"**출현 중 · 약 {format_remaining_time(remaining)} 남음**",
                inline=True,
            )
        else:
            remaining = (spawn_utc - now_utc).total_seconds()

            description = "모비라이프 기준 다음 어비스 구멍 출현 예정 시간입니다."
            if estimated:
                description += "\n⚠️ 점검 후 예상 시간이라 실제 출현 시각이 변경될 수 있습니다."

            embed = discord.Embed(
                title="🕳️ 다음 어비스 구멍",
                description=description,
            )
            embed.add_field(
                name="📅 날짜",
                value=f"**{format_korean_date(spawn_kst)}**",
                inline=False,
            )
            embed.add_field(
                name="🕒 출현 예정",
                value=f"**{format_korean_time(spawn_kst)}**",
                inline=True,
            )
            embed.add_field(
                name="⏰ 남은 시간",
                value=f"**{format_remaining_time(remaining)}**",
                inline=True,
            )

        embed.set_footer(text="머장봇 · 모비라이프 기준")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        print("[/어비스 오류]", e)
        await interaction.followup.send(
            "어비스 시간을 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        )



# =========================================================
# 모비라이프 거래소 /시세 명령어
# =========================================================

def normalize_item_name(value):
    return re.sub(r"\s+", "", value or "").lower()


def format_number(value):
    if value is None:
        return "-"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def format_percent(value):
    if value is None:
        return "-"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)

    if value > 0:
        return f"+{value:.2f}%"
    return f"{value:.2f}%"


def format_api_updated_at(value):
    if not value:
        return None

    try:
        dt = parse_iso_datetime(value).astimezone(KST)
        return dt.strftime("%Y-%m-%d %H:%M:%S KST")
    except Exception:
        return str(value)


async def fetch_market_prices(search_text, limit=100):
    if not MOBLIFE_API_KEY:
        raise RuntimeError("MOBLIFE_API_KEY_MISSING")

    headers = {
        "Authorization": f"Bearer {MOBLIFE_API_KEY}",
        "Accept": "application/json",
        "User-Agent": "머장봇/1.0",
    }

    params = {
        "search": search_text,
        "sort": "pct_change_24h_desc",
        "limit": limit,
        "offset": 0,
    }

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(
            f"{MOBLIFE_OPENAPI_URL}/market/prices",
            params=params,
        ) as response:
            if response.status == 401 or response.status == 403:
                raise RuntimeError("MOBLIFE_API_KEY_INVALID")
            if response.status == 429:
                raise RuntimeError("MOBLIFE_RATE_LIMIT")
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"모비라이프 시세 API 오류 (HTTP {response.status}): {body[:200]}"
                )

            data = await response.json()

    items = data.get("data") or []
    updated_at = data.get("last_updated_at")
    return items, updated_at


def filter_market_items(items, query):
    """검색어가 이름에 들어가는 모든 아이템을 반환한다."""
    q = normalize_item_name(query)

    matched = [
        item for item in items
        if q in normalize_item_name(item.get("name"))
    ]

    # 같은 아이템이 중복으로 들어오는 경우 kind_id 기준 제거
    unique = []
    seen = set()

    for item in matched:
        key = item.get("kind_id") or (
            normalize_item_name(item.get("name")),
            item.get("parent_category"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    # 이름순으로 보기 좋게 정렬
    unique.sort(key=lambda x: normalize_item_name(x.get("name")))
    return unique


def build_market_embeds(query, items, updated_at):
    """Discord 제한에 걸리지 않도록 검색 결과를 여러 embed로 나눈다."""
    embeds = []
    per_embed = 10
    total = len(items)

    for start in range(0, total, per_embed):
        chunk = items[start:start + per_embed]
        page = start // per_embed + 1
        total_pages = (total + per_embed - 1) // per_embed

        embed = discord.Embed(
            title=f"💰 '{query}' 시세 검색 결과 ({total}개)",
            description=(
                "검색어가 이름에 들어가는 아이템을 모두 표시합니다."
                + (f"  ·  {page}/{total_pages} 페이지" if total_pages > 1 else "")
            ),
        )

        for item in chunk:
            name = item.get("name") or "이름 없음"
            min_price = item.get("min_price")
            total_count = item.get("total_count")
            sold_out = bool(item.get("is_sold_out"))
            category = item.get("parent_category") or "-"

            if sold_out:
                price_text = "매물 없음"
            else:
                price_text = format_number(min_price)

            value = (
                f"💵 최저가 **{price_text}**  ·  "
                f"📦 수량 **{format_number(total_count)}**\n"
                f"🗂️ {category}  ·  "
                f"24시간 **{format_percent(item.get('pct_change_24h'))}**"
            )

            embed.add_field(
                name=f"• {name}",
                value=value,
                inline=False,
            )

        updated_text = format_api_updated_at(updated_at)
        footer = "머장봇 · 모비라이프 거래소"
        if updated_text:
            footer += f" · 갱신 {updated_text}"
        embed.set_footer(text=footer)
        embeds.append(embed)

    return embeds


@tree.command(
    name="시세",
    description="모비라이프 거래소에서 이름에 검색어가 들어가는 모든 아이템의 시세를 조회합니다.",
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
        items, updated_at = await fetch_market_prices(query, limit=100)
        matched = filter_market_items(items, query)

        if not matched:
            await interaction.followup.send(
                f"🔎 **{query}** 가 이름에 들어가는 아이템을 찾지 못했습니다."
            )
            return

        embeds = build_market_embeds(query, matched, updated_at)

        # Discord는 한 메시지당 embed 최대 10개.
        # 결과가 많으면 10개씩 나눠 여러 메시지로 전송한다.
        if len(embeds) == 1:
            await interaction.followup.send(embed=embeds[0])
        else:
            view = SheetPaginationView(embeds)
            message = await interaction.followup.send(
                embed=embeds[0],
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



# =========================================================
# 모비라이프 악보 보관소 /악보 명령어
# =========================================================

def _first_nonempty(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _text_value(value):
    if value is None:
        return None

    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or None

    if isinstance(value, dict):
        nested = _first_nonempty(
            value,
            "name",
            "nickname",
            "title",
            "display_name",
            "label",
            "value",
        )
        if nested is not None:
            return _text_value(nested)

    return None


def extract_sheet_items(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    for key in ("items", "results", "rows", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    for key in ("data", "payload", "result"):
        value = payload.get(key)

        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

        if isinstance(value, dict):
            for subkey in ("items", "results", "rows", "list", "data"):
                subvalue = value.get(subkey)
                if isinstance(subvalue, list):
                    return [x for x in subvalue if isinstance(x, dict)]

    return []


def get_sheet_title(item):
    value = _first_nonempty(
        item,
        "title",
        "name",
        "song_title",
        "sheet_title",
        "music_title",
    )
    return _text_value(value) or "제목 없음"


def get_sheet_creator(item):
    value = _first_nonempty(
        item,
        "creator",
        "author",
        "uploader",
        "created_by",
        "writer",
        "nickname",
        "user_name",
    )
    return _text_value(value)


def get_sheet_source_url(item):
    value = _first_nonempty(
        item,
        "source_url",
        "original_url",
        "post_url",
        "url",
        "link",
    )
    text = _text_value(value)
    if text and (text.startswith("http://") or text.startswith("https://")):
        return text
    return None


def get_sheet_meta(item):
    """디스코드에 보여줄 만한 정보만 깔끔하게 추립니다."""
    parts = []

    creator = get_sheet_creator(item)
    if creator:
        creator_clean = creator.strip()
        # API/원본 데이터에서 의미 없는 값은 화면에 노출하지 않음
        if creator_clean.lower() not in {"unknown", "none", "null", "-"} and creator_clean not in {"광고", "알 수 없음"}:
            parts.append(("👤", creator_clean))

    play_type = _text_value(
        _first_nonempty(item, "play_type", "type", "performance_type")
    )
    if play_type:
        play_map = {
            "solo": "솔로",
            "ensemble": "합주",
            "both": "솔로/합주",
            "unknown": None,
        }
        label = play_map.get(play_type.lower(), play_type)
        if label and label.lower() not in {"unknown", "none", "null", "-"}:
            parts.append(("🎵", label))

    harmony = _first_nonempty(item, "harmony_count", "part_count")
    if harmony not in (None, ""):
        try:
            harmony_num = int(harmony)
            if harmony_num > 0:
                parts.append(("🎹", f"{harmony_num}파트"))
        except (TypeError, ValueError):
            harmony_text = str(harmony).strip()
            if harmony_text and harmony_text.lower() not in {"unknown", "none", "null", "-"}:
                parts.append(("🎹", f"{harmony_text}파트"))

    return parts


async def fetch_sheet_music(search_text, page_size=30):
    headers = {
        "Accept": "application/json",
        "User-Agent": "머장봇/1.0",
        "Referer": MOBLIFE_SHEETS_URL,
    }

    params = {
        "q": search_text,
        "page": 1,
        "page_size": page_size,
    }

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(MOBLIFE_SHEETS_API_URL, params=params) as response:
            body = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"모비라이프 악보 API 오류 (HTTP {response.status}): {body[:300]}"
                )

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"모비라이프 악보 API JSON 파싱 실패: {body[:300]}"
                )

    return extract_sheet_items(payload)


def build_sheet_embeds(query, items):
    unique = []
    seen = set()

    for item in items:
        title = get_sheet_title(item)
        source_url = get_sheet_source_url(item)

        key = source_url or (
            title.lower(),
            (get_sheet_creator(item) or "").lower(),
        )
        if key in seen:
            continue

        seen.add(key)
        unique.append(item)

    # 제목에 검색어가 직접 들어간 결과를 위로 올려서 보기 좋게 정렬
    q = query.casefold()
    unique.sort(
        key=lambda item: (
            0 if q in get_sheet_title(item).casefold() else 1,
            get_sheet_title(item).casefold(),
        )
    )

    # API에서 받아온 결과는 가능한 한 모두 페이지로 보여준다.
    # 현재 검색 요청은 최대 30개를 받아오므로 최대 5페이지(페이지당 6개) 정도가 된다.
    if not unique:
        return []

    embeds = []
    per_embed = 6
    total = len(unique)
    total_pages = (total + per_embed - 1) // per_embed
    for start in range(0, total, per_embed):
        chunk = unique[start:start + per_embed]
        page = start // per_embed + 1

        embed = discord.Embed(
            title=f"🎼 {query} · 악보 검색 결과",
            description=f"검색 결과 **{total}개** · {page}/{total_pages} 페이지",
            color=discord.Color.from_rgb(88, 101, 242),
        )

        for index, item in enumerate(chunk, start=start + 1):
            title = get_sheet_title(item)
            source_url = get_sheet_source_url(item)
            meta_parts = get_sheet_meta(item)

            lines = []
            if meta_parts:
                lines.append("  ·  ".join(f"{icon} {text}" for icon, text in meta_parts))

            if source_url:
                lines.append(f"[🔗 바로 열기]({source_url})")
            else:
                lines.append("🔗 원본 링크 없음")

            field_name = f"{index}. {title}"
            if len(field_name) > 250:
                field_name = field_name[:247] + "..."

            embed.add_field(
                name=field_name,
                value="\n".join(lines),
                inline=False,
            )

        embed.set_footer(text="머장봇 · 악보 보관소 검색")
        embeds.append(embed)

    return embeds


class SheetPaginationView(discord.ui.View):
    """악보 검색 결과를 같은 메시지 안에서 이전/다음 버튼으로 넘깁니다."""

    def __init__(self, embeds, timeout=300):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.page = 0
        self.message = None

        self.prev_button = discord.ui.Button(
            label="◀ 이전",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        self.page_button = discord.ui.Button(
            label=f"1 / {len(self.embeds)}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        self.next_button = discord.ui.Button(
            label="다음 ▶",
            style=discord.ButtonStyle.primary,
            disabled=len(self.embeds) <= 1,
        )

        self.prev_button.callback = self.go_previous
        self.next_button.callback = self.go_next

        self.add_item(self.prev_button)
        self.add_item(self.page_button)
        self.add_item(self.next_button)

    def refresh_buttons(self):
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= len(self.embeds) - 1
        self.page_button.label = f"{self.page + 1} / {len(self.embeds)}"

    async def go_previous(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
        self.refresh_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.page],
            view=self,
        )

    async def go_next(self, interaction: discord.Interaction):
        if self.page < len(self.embeds) - 1:
            self.page += 1
        self.refresh_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.page],
            view=self,
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


@tree.command(
    name="악보",
    description="모비라이프 악보 보관소에서 제목 또는 제작자로 악보를 검색합니다.",
)
@discord.app_commands.describe(검색어="검색할 곡 제목 또는 제작자 이름")
async def sheet_search_command(
    interaction: discord.Interaction,
    검색어: str,
):
    await interaction.response.defer(thinking=True)

    query = 검색어.strip()

    if len(query) < 2:
        await interaction.followup.send("악보 검색어는 2글자 이상 입력해주세요.")
        return

    try:
        items = await fetch_sheet_music(query, page_size=30)

        if not items:
            await interaction.followup.send(
                f"🎼 **{query}** 검색 결과를 찾지 못했습니다."
            )
            return

        embeds = build_sheet_embeds(query, items)

        if not embeds:
            await interaction.followup.send(
                f"🎼 **{query}** 검색 결과를 표시할 수 없습니다."
            )
            return

        if len(embeds) == 1:
            await interaction.followup.send(embed=embeds[0])
        else:
            view = SheetPaginationView(embeds)
            message = await interaction.followup.send(
                embed=embeds[0],
                view=view,
                wait=True,
            )
            view.message = message

    except Exception as e:
        print("[/악보 오류]", e)
        await interaction.followup.send(
            "악보 검색 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        )


# =========================================================
# /청소 명령어
# =========================================================

@tree.command(
    name="청소",
    description="현재 채널에서 머장봇이 보낸 메시지만 정리합니다.",
)
@discord.app_commands.describe(개수="최근 몇 개 메시지까지 확인할지 (기본 200, 최대 500)")
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
            if client.user is None or message.author.id != client.user.id:
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


# =========================================================
# 봇 접속
# =========================================================

@client.event
async def on_ready():
    global commands_synced
    print("=" * 55)
    if MOBLIFE_PROXY_BASE:
        print(f"[모비라이프] 프록시 사용: {MOBLIFE_PROXY_BASE}")
    else:
        print("[모비라이프] 직접 연결 사용")
    print("머장봇 접속 성공!")
    print(f"봇 이름 : {client.user}")
    print(f"접속 서버 수 : {len(client.guilds)}")
    print("=" * 55)

    if not commands_synced:
        try:
            # /어비스, /시세, /악보, /청소는 현재 봇이 들어가 있는 서버에만 등록한다.
            # 먼저 전역으로 정의된 명령어를 각 서버용 명령어로 복사/동기화한다.
            for guild in client.guilds:
                tree.copy_global_to(guild=guild)
                synced_guild = await tree.sync(guild=guild)
                print(
                    f"[명령어] {guild.name} 서버 전용 등록 완료 "
                    f"({len(synced_guild)}개)"
                )

            # 예전에 등록되어 남아 있는 글로벌 명령어를 삭제한다.
            # 이 작업으로 디스코드 명령어 목록에 2개씩 보이던 중복을 정리한다.
            tree.clear_commands(guild=None)
            synced_global = await tree.sync()
            print(
                f"[명령어] 글로벌 중복 명령어 제거 완료 "
                f"({len(synced_global)}개 남음)"
            )
            commands_synced = True
        except Exception as e:
            print("[명령어 등록 오류]", e)

    if official_db_is_empty():
        try:
            await initialize_existing_posts()
        except Exception as e:
            print("[초기 공지 등록 실패]", e)

    # 어비스 데이터는 시작할 때 즉시 한 번 읽어 둔다.
    if abyss_anchor is None:
        try:
            await refresh_abyss_once()
        except Exception as e:
            print("[초기 어비스 데이터 실패]", e)

    if not check_official_site.is_running():
        check_official_site.start()
        print("[공홈] 자동감시 시작 (1분 주기)")

    if not cleanup_database_weekly.is_running():
        cleanup_database_weekly.start()
        print("[DB] 자동청소 시작 (7일 주기 / 공지 500개 / 어비스 30일)")

    if not refresh_abyss_data.is_running():
        refresh_abyss_data.start()
        print("[어비스] 데이터 갱신 시작 (1분 주기)")

    if not check_abyss_alerts.is_running():
        check_abyss_alerts.start()
        print("[어비스] 60/30/10/1분 알림 감시 시작 (15초 주기)")


# =========================================================
# 실행
# =========================================================

if not TOKEN:
    raise RuntimeError(".env에서 DISCORD_TOKEN을 찾을 수 없습니다.")

init_db()
client.run(TOKEN)
