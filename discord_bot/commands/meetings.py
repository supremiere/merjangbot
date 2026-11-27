"""계획된 만남의 고정 패널, 파티 탐색, 서버별 관리자 설정."""
import logging

import discord
from discord import app_commands

from discord_bot.commands import meeting_party
from discord_bot.commands.trains import can_manage_trains
from discord_bot.meetings_sync import MeetingDiscordSync
from services.meetings import ACTION_LABELS, MeetingService
from storage.meetings import MeetingError, MeetingStore


logger = logging.getLogger(__name__)
PAGE_SIZE = 8
CHANNEL_LABELS = {
    "panel_channel_id": "메인 패널 채널",
    "recruit_channel_id": "모집 채널",
    "promo_channel_id": "홍보 채널 (자유-채팅)",
    "category_id": "파티방 위치 (선택)",
    "log_channel_id": "관리자 로그 채널 (선택)",
}


async def private(interaction, content=None, **kwargs):
    kwargs.update(ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    if interaction.response.is_done():
        return await interaction.followup.send(content, **kwargs)
    return await interaction.response.send_message(content, **kwargs)


async def require_admin(interaction):
    try:
        interaction.client.meetings.require_admin(interaction)
        return True
    except MeetingError as error:
        await private(interaction, str(error))
        return False


class PanelView(discord.ui.View):
    """Only the requester can operate a temporary panel. Retest admin rights on every click."""

    def __init__(self, requester=None, *, admin=False, timeout=300):
        super().__init__(timeout=timeout)
        self.requester = requester.user.id if requester else None
        self.guild_id = requester.guild_id if requester else None
        self.admin_only = admin

    async def interaction_check(self, interaction):
        if not interaction.guild or (self.requester and (
            self.requester != interaction.user.id or self.guild_id != interaction.guild_id
        )):
            await private(interaction, "이 메뉴는 처음 연 사용자만 이용할 수 있어요.")
            return False
        return not self.admin_only or await require_admin(interaction)

    async def on_error(self, interaction, error, item):
        if isinstance(error, ValueError):
            await private(interaction, str(error))
            return
        logger.exception("계획된 만남 패널 처리 실패", exc_info=error)
        await private(interaction, "요청을 처리하지 못했어요. 다시 시도하거나 관리자에게 알려주세요.")


class MainPanel(PanelView):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        message = interaction.message
        if message and not message.flags.ephemeral:
            cfg = interaction.client.meetings.store.settings(interaction.guild_id)
            if (interaction.channel_id, message.id) != (cfg["panel_channel_id"], cfg["panel_message_id"]):
                await private(interaction, "이전 패널이에요. /계획된만남으로 현재 패널을 열어주세요.")
                return False
        return True

    @discord.ui.button(label="파티 생성", emoji="💌", style=discord.ButtonStyle.success, custom_id="meet:main:create")
    async def create(self, interaction, button):
        await meeting_party.open_create(interaction, interaction.client.meetings)

    @discord.ui.button(label="파티 찾기", emoji="🔎", style=discord.ButtonStyle.primary, custom_id="meet:main:find")
    async def find(self, interaction, button):
        await show_list(interaction, "open")

    @discord.ui.button(label="내 파티", emoji="👥", custom_id="meet:main:mine")
    async def mine(self, interaction, button):
        await show_list(interaction, "mine")

    @discord.ui.button(label="관전 닫고 생성", emoji="🔒", row=1, custom_id="meet:main:private")
    async def create_private(self, interaction, button):
        await meeting_party.open_create(interaction, interaction.client.meetings, spectating=False)

    @discord.ui.button(label="이용 안내", emoji="📖", row=1, custom_id="meet:main:help")
    async def help(self, interaction, button):
        await private(interaction,
            "**파티원**은 모집글에서 참가하면 좌석과 전용 파티방이 생겨요. "
            "파티방에서 로스터에 캐릭터명과 비고를 적고 작전을 확인하세요.\n"
            "**관전자**는 관전이 열린 파티에서 승인 없이 들어가 이전 대화를 읽고 채팅할 수 있어요. "
            "관전자는 모집 인원에 포함되지 않아요. 파티장이 관전을 닫으면 관전자 접근이 회수돼요.\n"
            "**파티장**은 일정과 정원을 수정하고 자유-채팅에 홍보할 수 있어요. "
            "출발 시간이 지나거나 활동을 종료해도 파티방은 유지돼요.")

    @discord.ui.button(label="관리자 모드", emoji="🛠️", row=1, custom_id="meet:main:admin")
    async def admin(self, interaction, button):
        if await require_admin(interaction):
            await dashboard(interaction)


def panel_embed(settings=None):
    embed = discord.Embed(title="💌 계획된 만남", color=0xCB617B,
        description="함께할 파티를 찾거나 행선지와 정원을 정해 새 파티를 만드세요.")
    if settings is not None and not settings["enabled"]:
        embed.description = "현재 비활성화되어 있어요. 관리자는 관리자 모드에서 설정과 기존 파티를 확인할 수 있어요."
    else:
        embed.add_field(name="관전", value="새 파티는 기본으로 관전이 열려요. 관전자는 모집 자리를 차지하지 않고 채팅할 수 있어요.", inline=False)
    return embed


def main_payload():
    return {"embed": panel_embed(), "view": MainPanel()}


LIST_LABELS = {"open": "모집 중", "watch": "관전 가능", "mine": "내 파티", "all": "전체 파티",
               "inactive": "장기 미활동", "errors": "복구 필요"}
ADMIN_LISTS = {"all", "inactive", "errors"}


def date_label(stamp):
    return (meeting_party.format_departure(stamp).replace("-", ".") + " KST"
            if stamp is not None else "일정 미정")


class PartyList(PanelView):
    def __init__(self, interaction, rows, mode, page, has_next, query):
        super().__init__(interaction, admin=mode in ADMIN_LISTS)
        self.mode, self.page, self.query = mode, page, query
        self.previous.disabled = page == 0
        self.next.disabled = not has_next
        if rows:
            select = discord.ui.Select(placeholder="파티 상세 보기", row=0, options=[
                discord.SelectOption(label=meeting_party.clipped(p['destination'], 100), value=str(p["id"]),
                                     description=f"#{p['id']} · {date_label(p['departure'])}"[:100])
                for p in rows
            ])
            select.callback = self.select_party
            self.add_item(select)
        modes = ("all", "inactive", "errors") if self.admin_only else ("open", "watch", "mine")
        switch = discord.ui.Select(placeholder="목록 선택", row=1, options=[
            discord.SelectOption(label=LIST_LABELS[m], value=m, default=m == mode) for m in modes
        ])
        switch.callback = self.switch_mode
        self.add_item(switch)

    async def select_party(self, interaction):
        party_id = int(interaction.data["values"][0])
        if self.admin_only:
            await admin_party(interaction, party_id)
        else:
            await meeting_party.open_detail(interaction, interaction.client.meetings, party_id)

    async def switch_mode(self, interaction):
        mode = interaction.data["values"][0]
        await show_list(interaction, mode, query=self.query if mode in ADMIN_LISTS else "", edit=True)

    @discord.ui.button(label="이전", row=2)
    async def previous(self, interaction, button):
        await show_list(interaction, self.mode, self.page - 1, self.query, edit=True)

    @discord.ui.button(label="다음", row=2)
    async def next(self, interaction, button):
        await show_list(interaction, self.mode, self.page + 1, self.query, edit=True)


class AdminModal(discord.ui.Modal):
    def __init__(self, interaction):
        super().__init__(timeout=300)
        self.requester = interaction.user.id
        self.guild_id = interaction.guild_id

    async def interaction_check(self, interaction):
        if self.requester != interaction.user.id or self.guild_id != interaction.guild_id:
            await private(interaction, "이 양식을 열었던 서버와 사용자로 제출해주세요.")
            return False
        return await require_admin(interaction)

    async def on_error(self, interaction, error):
        logger.exception("계획된 만남 관리자 양식 처리 실패", exc_info=error)
        await private(interaction, "관리자 양식을 처리하지 못했어요. 패널에서 다시 열어주세요.")


class PartySearch(AdminModal, title="전체 파티 검색"):
    query = discord.ui.TextInput(label="행선지 또는 파티 번호", required=False, max_length=100)

    async def on_submit(self, interaction):
        if await require_admin(interaction):
            await show_list(interaction, "all", query=self.query.value.strip())


async def show_list(interaction, mode, page=0, query="", *, edit=False):
    if mode not in LIST_LABELS:
        await private(interaction, "알 수 없는 목록이에요. 패널을 다시 열어주세요.")
        return
    if mode in ADMIN_LISTS and not await require_admin(interaction):
        return
    store = interaction.client.meetings.store
    gid = interaction.guild.id
    if mode not in ADMIN_LISTS and not store.settings(gid)["enabled"]:
        await private(interaction, "이 서버의 계획된 만남이 비활성화되어 있어요.")
        return
    page = max(0, page)
    def load_page(index):
        return store.page(gid, mode, user_id=interaction.user.id, query=query,
                          offset=index * PAGE_SIZE, limit=PAGE_SIZE)
    rows, total = load_page(page)
    last_page = max(0, (total - 1) // PAGE_SIZE)
    if page > last_page:
        page = last_page
        rows, total = load_page(page)
    embed = discord.Embed(title=f"💌 {LIST_LABELS[mode]}", color=0xCB617B)
    if not rows:
        embed.description = "조건에 맞는 파티가 없어요."
    for party in rows:
        free = party["capacity"] - len(party["members"])
        slots = f"{len(party['members'])}/{party['capacity']}명 · 빈자리 {free}"
        if mode == "mine" and interaction.user.id in party["watchers"]:
            slots += " · 관전 중"
        embed.add_field(name=f"#{party['id']} · {party['destination']}"[:250],
                        value=f"{slots}\n{date_label(party['departure'])}", inline=False)
    embed.set_footer(text=f"총 {total}개 · {page + 1}/{last_page + 1}페이지")
    view = PartyList(interaction, rows, mode, page, page < last_page, query)
    if edit:
        await interaction.response.edit_message(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
    else:
        await private(interaction, embed=embed, view=view)


class AdminParty(PanelView):
    def __init__(self, interaction, party):
        super().__init__(interaction, admin=True)
        self.party_id = party["id"]
        if party["room_id"] and party["phase"] != "disbanded":
            self.add_item(discord.ui.Button(label="파티방 이동", url=f"https://discord.com/channels/{interaction.guild.id}/{party['room_id']}"))

    @discord.ui.button(label="파티 관리", style=discord.ButtonStyle.primary)
    async def manage(self, interaction, button):
        await meeting_party.open_manage(interaction, interaction.client.meetings, self.party_id)

    @discord.ui.button(label="권한·메시지 복구")
    async def repair(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.client.meetings.act(interaction, self.party_id, "repair")
        except ValueError as exc:
            await private(interaction, str(exc))
            return
        await private(interaction, "파티 채널 권한과 연결된 메시지를 동기화했어요.")


async def admin_party(interaction, party_id):
    if not await require_admin(interaction):
        return
    svc = interaction.client.meetings
    party = svc.store.get(interaction.guild.id, party_id)
    embed = meeting_party.party_embed(party, "public")
    if party["error"]:
        embed.add_field(name="최근 복구 오류", value=meeting_party.clipped(str(party["error"]), 850), inline=False)
    await private(interaction, embed=embed, view=AdminParty(interaction, party))


class AdminPanel(PanelView):
    def __init__(self, requester):
        super().__init__(requester, admin=True)

    @discord.ui.button(label="전체 파티", style=discord.ButtonStyle.primary)
    async def parties(self, interaction, button):
        await show_list(interaction, "all")

    @discord.ui.button(label="파티 검색")
    async def search(self, interaction, button):
        await interaction.response.send_modal(PartySearch(interaction))

    @discord.ui.button(label="복구 필요", emoji="🔧")
    async def errors(self, interaction, button):
        await show_list(interaction, "errors")

    @discord.ui.button(label="장기 미활동", row=1)
    async def inactive(self, interaction, button):
        await show_list(interaction, "inactive")

    @discord.ui.button(label="서버 설정", emoji="⚙️", row=1)
    async def setup(self, interaction, button):
        await show_settings(interaction)

    @discord.ui.button(label="최근 관리자 로그", row=1)
    async def logs(self, interaction, button):
        rows = interaction.client.meetings.store.recent_logs(interaction.guild_id, limit=8)
        embed = discord.Embed(title="최근 운영 기록", color=0x40577B)
        if not rows:
            embed.description = "저장된 기록이 없어요."
        for row in rows:
            actor = f"<@{row['actor']}>" if row['actor'] else "시스템"
            target = f"#{row['party_id']}" if row['party_id'] else "서버 설정"
            result = "성공" if row["success"] else "실패"
            desc = discord.utils.escape_mentions(str(row["detail"] or ""))[:300]
            action = ACTION_LABELS.get(row["action"], row["action"])
            embed.add_field(name=f"{date_label(row['at'])} · {result}",
                            value=f"{actor} · {target} · {action}\n{desc}".rstrip(), inline=False)
        await private(interaction, embed=embed)


async def dashboard(interaction):
    store = interaction.client.meetings.store
    gid = interaction.guild.id
    stats = store.stats(gid)
    config = store.settings(gid)
    embed = discord.Embed(title="🛠️ 계획된 만남 관리자", color=0x40577B)
    embed.add_field(name="모집", value=f"모집 중 {stats['open']} · 정원 충족 {stats['full']} · 수동 마감 {stats['closed']}", inline=False)
    embed.add_field(name="활동", value=f"활동 중 {stats['active']} · 종료 {stats['ended']} · 전체 참가 {stats['members']}명", inline=False)
    embed.add_field(name="점검", value=f"미활동 {stats['inactive']} · 복구 필요 {stats['errors']}", inline=False)
    state = "활성" if config["enabled"] else "비활성"
    embed.set_footer(text=f"전체 파티 {stats['total']}개 · 기능 {state}")
    await private(interaction, embed=embed, view=AdminPanel(interaction))


async def save_setting(interaction, **changes):
    if not await require_admin(interaction):
        return False
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await interaction.client.meetings.configure(interaction, **changes)
    except ValueError as exc:
        await private(interaction, str(exc))
        return False
    except Exception:
        logger.exception("계획된 만남 설정 적용 오류")
        await private(interaction, "설정 적용 중 오류가 발생했어요. 서버 설정을 다시 열어 저장 상태와 봇 권한을 확인해주세요.")
        return False
    await show_settings(interaction, saved=True)
    return True


class LimitsModal(AdminModal, title="홍보 및 모집 설정"):
    def __init__(self, interaction, settings):
        super().__init__(interaction)
        self.fields = {}
        for key, label in [
            ("cooldown", "파티별 홍보 쿨타임 (초)"), ("max_capacity", "파티 모집 정원 상한"),
            ("max_created", "사용자별 파티 생성 상한 (0은 무제한)"), ("inactive_days", "미활동 표시 기준 (일)"),
        ]:
            item = discord.ui.TextInput(label=label, default=str(settings[key]), required=True, max_length=8)
            self.add_item(item)
            self.fields[key] = item

    async def on_submit(self, interaction):
        if not await require_admin(interaction):
            return
        try:
            values = {k: int(v.value.strip()) for k, v in self.fields.items()}
        except ValueError:
            await private(interaction, "모든 항목에 정수를 입력해주세요.")
            return
        await save_setting(interaction, **values)


class ChannelPicker(PanelView):
    def __init__(self, interaction, key):
        super().__init__(interaction, admin=True)
        self.key = key
        kinds = [discord.ChannelType.category if key == "category_id" else discord.ChannelType.text]
        select = discord.ui.ChannelSelect(channel_types=kinds, placeholder=CHANNEL_LABELS[key])
        select.callback = self.channel_selected
        self.add_item(select)
        if key in ("log_channel_id", "category_id"):
            label = "모집 채널 위치 사용" if key == "category_id" else "로그 채널 연결 해제"
            clear = discord.ui.Button(label=label)
            clear.callback = self.clear
            self.add_item(clear)

    async def clear(self, interaction):
        await save_setting(interaction, **{self.key: None})

    async def channel_selected(self, interaction):
        cid = int(interaction.data["values"][0])
        channel = interaction.guild.get_channel(cid)
        expected = discord.CategoryChannel if self.key == "category_id" else discord.TextChannel
        if not isinstance(channel, expected) or channel.guild.id != interaction.guild.id:
            await private(interaction, "현재 서버의 올바른 채널을 선택해주세요.")
            return
        await save_setting(interaction, **{self.key: cid})


class SettingsPanel(PanelView):
    def __init__(self, interaction, settings):
        super().__init__(interaction, admin=True)
        self.enabled = bool(settings["enabled"])
        self.toggle.label = "기능 비활성화" if self.enabled else "기능 활성화"
        self.toggle.style = discord.ButtonStyle.danger if self.enabled else discord.ButtonStyle.success
        select = discord.ui.Select(placeholder="바꿀 채널 또는 카테고리", row=0,
            options=[discord.SelectOption(label=label, value=key) for key, label in CHANNEL_LABELS.items()])
        select.callback = self.pick_channel
        self.add_item(select)

    async def pick_channel(self, interaction):
        key = interaction.data["values"][0]
        if key not in CHANNEL_LABELS:
            await private(interaction, "채널 항목을 다시 선택해주세요.")
            return
        await private(interaction, f"**{CHANNEL_LABELS[key]}**에 사용할 곳을 선택해주세요.",
                      view=ChannelPicker(interaction, key))

    @discord.ui.button(label="홍보·모집 설정", row=1)
    async def limits(self, interaction, button):
        settings = interaction.client.meetings.store.settings(interaction.guild.id)
        await interaction.response.send_modal(LimitsModal(interaction, settings))

    @discord.ui.button(label="기능 활성화", row=1)
    async def toggle(self, interaction, button):
        await save_setting(interaction, enabled=not self.enabled)

    @discord.ui.button(label="메인 패널 복구", row=1)
    async def panel(self, interaction, button):
        svc = interaction.client.meetings
        svc.require_admin(interaction)
        settings = svc.store.settings(interaction.guild.id)
        if not settings["panel_channel_id"]:
            await private(interaction, "먼저 메인 패널 채널을 설정해주세요.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await svc.restore_panel(interaction.guild)
        await private(interaction, "메인 패널을 확인했어요.")

    @discord.ui.button(label="관리자 현황", row=2)
    async def overview(self, interaction, button):
        await dashboard(interaction)


async def show_settings(interaction, *, saved=False):
    settings = interaction.client.meetings.store.settings(interaction.guild.id)
    state = "활성" if settings["enabled"] else "비활성"
    created_limit = f"{settings['max_created']}개" if settings["max_created"] else "무제한"
    embed = discord.Embed(title="⚙️ 계획된 만남 서버 설정", color=0x40577B)
    embed.description = f"현재 **{state}** · 메인·모집·홍보 채널을 선택한 뒤 기능을 활성화하세요. 파티방은 기본으로 모집 채널과 같은 위치에 생겨요."
    embed.add_field(name="채널", value="\n".join(
        f"{label} · " + (f"<#{settings[key]}>" if settings[key] else
                         ("모집 채널 위치 사용" if key == "category_id" else "미설정"))
        for key, label in CHANNEL_LABELS.items()), inline=False)
    embed.add_field(name="홍보·모집", value=(
        f"홍보 쿨타임 {settings['cooldown']}초 · 최대 정원 {settings['max_capacity']}명\n"
        f"1인당 파티 {created_limit} · 미활동 표시 {settings['inactive_days']}일"), inline=False)
    await private(interaction, "설정을 저장했어요." if saved else None, embed=embed,
                  view=SettingsPanel(interaction, settings))


class MeetingUI:
    party_embed = staticmethod(meeting_party.party_embed)
    party_view = staticmethod(meeting_party.party_view)
    panel_view = MainPanel
    panel_embed = staticmethod(panel_embed)


def setup_views(bot):
    bot.add_view(MainPanel())
    bot.add_dynamic_items(meeting_party.MeetingButton)


def register(bot):
    store = MeetingStore(bot.database)
    bot.meetings = MeetingService(bot, store, MeetingDiscordSync(bot, store, MeetingUI()))

    @bot.tree.command(name="계획된만남", description="파티 모집 패널을 열거나 관리자 설정을 시작합니다.")
    @app_commands.guild_only()
    async def planned_meetings(interaction: discord.Interaction):
        if not bot.meetings.store.settings(interaction.guild_id)["enabled"]:
            if can_manage_trains(interaction.user):
                await show_settings(interaction)
            else:
                await private(interaction, "이 서버의 계획된 만남이 비활성화되어 있어요.")
            return
        await private(interaction, **main_payload())
