import os
import re
import json
from datetime import datetime, timezone, timedelta

import aiohttp
import discord
from discord.ext import tasks


# =========================================================
# 머장봇 서버 상태 확장
# - 기존 본체 코드는 app_core.py에 그대로 보존
# - CoredLab 시작 명령은 기존처럼 python main.py
# =========================================================

SERVER_STATUS_CHANNEL_ID = int(
    os.getenv("SERVER_STATUS_CHANNEL_ID", "1547464310043844639")
)

MOBLIFE_PROXY_BASE = os.getenv(
    "MOBLIFE_PROXY_BASE",
    "https://moblife-proxy.ninemailz.workers.dev",
).strip().rstrip("/")

if MOBLIFE_PROXY_BASE:
    MOBLIFE_MAINTENANCE_STATUS_URL = (
        f"{MOBLIFE_PROXY_BASE}/d/api/v1/maintenance-status"
    )
    MOBLIFE_REFERER = f"{MOBLIFE_PROXY_BASE}/"
else:
    MOBLIFE_MAINTENANCE_STATUS_URL = (
        "https://mabimobi.life/d/api/v1/maintenance-status"
    )
    MOBLIFE_REFERER = "https://mabimobi.life/"

KST = timezone(timedelta(hours=9))
SERVER_STATUS_FOOTER_PREFIX = "머장봇 · 서버 상태"


# =========================================================
# 화면 표시 문구 정리
# - 출처/보관소 관련 표시 문구는 사용자 화면에서 숨김
# =========================================================

def _clean_display_text(value):
    if not isinstance(value, str):
        return value

    text = value

    # 기존 '모비라이프 기준' 표시 제거
    text = text.replace(" · 모비라이프 기준", "")
    text = text.replace("모비라이프 기준 ", "")
    text = text.replace("모비라이프 기준", "")

    # /시세의 '모비라이프 거래소' 표시 제거
    text = text.replace("모비라이프 거래소에서 ", "")
    text = text.replace("모비라이프 거래소", "")

    # /악보의 '악보 보관소' 표시 제거
    text = text.replace("모비라이프 악보 보관소에서 ", "")
    text = text.replace("모비라이프 악보 보관소", "")
    text = text.replace("악보 보관소 검색", "")
    text = text.replace("악보 보관소", "")

    # 문구 제거 뒤 남는 구분자/공백 정리
    text = re.sub(r"\s+·\s+·\s+", " · ", text)
    text = re.sub(r"\s*·\s*$", "", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


_original_embed_init = discord.Embed.__init__
_original_embed_set_footer = discord.Embed.set_footer
_original_embed_add_field = discord.Embed.add_field
_original_tree_command = discord.app_commands.CommandTree.command


def _embed_init_cleaned(self, *args, **kwargs):
    if "title" in kwargs:
        kwargs["title"] = _clean_display_text(kwargs["title"])
    if "description" in kwargs:
        kwargs["description"] = _clean_display_text(kwargs["description"])
    return _original_embed_init(self, *args, **kwargs)


def _embed_set_footer_cleaned(self, *, text=None, icon_url=None):
    return _original_embed_set_footer(
        self,
        text=_clean_display_text(text),
        icon_url=icon_url,
    )


def _embed_add_field_cleaned(self, *, name, value, inline=True):
    return _original_embed_add_field(
        self,
        name=_clean_display_text(name),
        value=_clean_display_text(value),
        inline=inline,
    )


def _tree_command_cleaned(self, *args, **kwargs):
    if "description" in kwargs:
        kwargs["description"] = _clean_display_text(kwargs["description"])
    return _original_tree_command(self, *args, **kwargs)


discord.Embed.__init__ = _embed_init_cleaned
discord.Embed.set_footer = _embed_set_footer_cleaned
discord.Embed.add_field = _embed_add_field_cleaned
discord.app_commands.CommandTree.command = _tree_command_cleaned


# =========================================================
# 서버 상태 유틸
# =========================================================

def parse_iso_datetime(value):
    if not value:
        return None

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_elapsed(delta):
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}일")
    if hours or days:
        parts.append(f"{hours}시간")
    parts.append(f"{minutes}분")
    return " ".join(parts)


def format_datetime(value):
    if not value:
        return "확인 불가"

    try:
        dt = parse_iso_datetime(value)
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    except Exception:
        return str(value)


def strip_status_dot(channel_name):
    """채널명 맨 앞의 기존 상태 동그라미만 제거한다."""
    name = (channel_name or "").strip()
    while name.startswith("🟢") or name.startswith("🔴"):
        name = name[1:].lstrip()
    return name


def build_status_channel_name(base_name, is_maintenance):
    dot = "🔴" if is_maintenance else "🟢"
    clean_name = strip_status_dot(base_name)
    if not clean_name:
        clean_name = "🖥️모비노기-서버상태"
    return f"{dot}{clean_name}"


async def fetch_maintenance_status():
    headers = {
        "Accept": "application/json",
        "User-Agent": "머장봇/1.0",
        "Referer": MOBLIFE_REFERER,
    }
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(MOBLIFE_MAINTENANCE_STATUS_URL) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"모비라이프 점검상태 API 오류 "
                    f"(HTTP {response.status}): {body[:200]}"
                )

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"모비라이프 점검상태 JSON 파싱 실패: {e}")

    if not isinstance(data, dict) or "is_maintenance" not in data:
        raise RuntimeError("모비라이프 점검상태 응답 형식이 올바르지 않습니다.")

    return data


def build_status_embed(data, now_utc):
    is_maintenance = bool(data.get("is_maintenance"))
    last_end_text = data.get("last_maintenance_end_time")
    current_start_text = data.get("current_maintenance_start_time")

    if is_maintenance:
        embed = discord.Embed(
            title="🔴 마비노기 모바일 서버 점검 중",
            description="현재 서버 점검이 진행 중입니다.",
            color=discord.Color.red(),
        )

        elapsed_text = "확인 불가"
        if current_start_text:
            try:
                elapsed_text = format_elapsed(
                    now_utc - parse_iso_datetime(current_start_text)
                )
            except Exception:
                pass

        embed.add_field(
            name="⏱️ 점검 경과",
            value=f"**{elapsed_text}**",
            inline=False,
        )
        embed.add_field(
            name="🛠️ 점검 시작",
            value=f"**{format_datetime(current_start_text)}**",
            inline=False,
        )
    else:
        embed = discord.Embed(
            title="🟢 마비노기 모바일 서버 정상 운영 중",
            description="최근 점검 종료 시각을 기준으로 계산한 서버 업타임입니다.",
            color=discord.Color.green(),
        )

        uptime_text = "확인 불가"
        if last_end_text:
            try:
                uptime_text = format_elapsed(
                    now_utc - parse_iso_datetime(last_end_text)
                )
            except Exception:
                pass

        embed.add_field(
            name="⏱️ 서버 업타임",
            value=f"**{uptime_text}**",
            inline=False,
        )
        embed.add_field(
            name="🛠️ 최근 점검 종료",
            value=f"**{format_datetime(last_end_text)}**",
            inline=False,
        )

    embed.add_field(
        name="🔄 마지막 확인",
        value=f"**{now_utc.astimezone(KST).strftime('%Y-%m-%d %H:%M KST')}**",
        inline=False,
    )
    embed.set_footer(
        text=f"{SERVER_STATUS_FOOTER_PREFIX} · 1분마다 갱신"
    )
    return embed


def install_server_status(client):
    state = {
        "message": None,
        "last_state": None,
        "base_channel_name": None,
    }

    async def find_existing_message(channel):
        if client.user is None:
            return None

        try:
            async for message in channel.history(limit=50):
                if message.author.id != client.user.id or not message.embeds:
                    continue

                footer_text = message.embeds[0].footer.text or ""
                if footer_text.startswith(SERVER_STATUS_FOOTER_PREFIX):
                    return message
        except discord.Forbidden:
            print(
                "[서버상태] 메시지 기록 보기 권한이 없어 "
                "기존 상태 메시지를 찾지 못했습니다."
            )
        except discord.HTTPException as e:
            print("[서버상태] 기존 상태 메시지 검색 실패:", e)

        return None

    async def update_once():
        channel = client.get_channel(SERVER_STATUS_CHANNEL_ID)
        if channel is None:
            print(
                f"[서버상태] 채널을 찾을 수 없습니다. "
                f"ID={SERVER_STATUS_CHANNEL_ID}"
            )
            return

        if state["base_channel_name"] is None:
            state["base_channel_name"] = strip_status_dot(
                getattr(channel, "name", "")
            )

        # API 오류는 점검으로 오인하지 않고 기존 상태를 유지한다.
        try:
            data = await fetch_maintenance_status()
        except Exception as e:
            print(
                "[서버상태] 모비라이프 상태 확인 실패 - 기존 상태 유지:",
                e,
            )
            return

        now_utc = datetime.now(timezone.utc)
        is_maintenance = bool(data.get("is_maintenance"))
        desired_name = build_status_channel_name(
            state["base_channel_name"],
            is_maintenance,
        )

        # 채널명 본문은 유지하고 앞의 상태 동그라미만 바꾼다.
        if getattr(channel, "name", None) != desired_name:
            try:
                await channel.edit(
                    name=desired_name,
                    reason="머장봇 서버 상태 자동 반영",
                )
                print(f"[서버상태] 채널명 변경: {desired_name}")
            except discord.Forbidden:
                print(
                    "[서버상태] 채널명 변경 권한이 없습니다. "
                    "머장봇의 '채널 관리' 권한을 확인해주세요."
                )
            except discord.HTTPException as e:
                print("[서버상태] 채널명 변경 실패:", e)

        embed = build_status_embed(data, now_utc)

        try:
            if state["message"] is None:
                state["message"] = await find_existing_message(channel)

            if state["message"] is None:
                state["message"] = await channel.send(embed=embed)
                print(
                    f"[서버상태] 상태 메시지 생성: "
                    f"{state['message'].id}"
                )
            else:
                try:
                    await state["message"].edit(embed=embed)
                except discord.NotFound:
                    state["message"] = await channel.send(embed=embed)
                    print(
                        f"[서버상태] 상태 메시지 재생성: "
                        f"{state['message'].id}"
                    )
        except discord.Forbidden:
            print("[서버상태] 메시지 전송/수정 권한이 없습니다.")
        except discord.HTTPException as e:
            print("[서버상태] 상태 메시지 갱신 실패:", e)

        if state["last_state"] is None or state["last_state"] != is_maintenance:
            state_text = "점검 중" if is_maintenance else "정상 운영"
            print(f"[서버상태] 상태 확인: {state_text}")

        state["last_state"] = is_maintenance

    @tasks.loop(minutes=1)
    async def refresh_server_status():
        try:
            await update_once()
        except Exception as e:
            print("[서버상태] 갱신 루프 오류:", e)

    @refresh_server_status.before_loop
    async def before_refresh_server_status():
        await client.wait_until_ready()

    # discord.Client에는 add_listener()가 없으므로 setup_hook에 붙인다.
    original_setup_hook = client.setup_hook

    async def setup_hook_with_server_status():
        await original_setup_hook()
        if not refresh_server_status.is_running():
            refresh_server_status.start()
            print(
                "[서버상태] 자동 갱신 시작 "
                f"(1분 주기 / 채널 ID {SERVER_STATUS_CHANNEL_ID})"
            )

    client.setup_hook = setup_hook_with_server_status
    client._merjang_server_status_task = refresh_server_status


# 기존 본체가 client.run()을 호출하기 직전에 서버상태 확장을 붙인다.
_original_client_run = discord.Client.run


def _run_with_server_status(self, token, *args, **kwargs):
    if not getattr(self, "_merjang_server_status_installed", False):
        install_server_status(self)
        self._merjang_server_status_installed = True
    return _original_client_run(self, token, *args, **kwargs)


discord.Client.run = _run_with_server_status


# 기존 머장봇 본체. import 과정의 마지막에서 client.run()이 실행된다.
import app_core  # noqa: E402,F401
