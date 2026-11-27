"""계획된 만남의 공개 모집글, 비공개 파티방, 참가·관리 화면."""
import logging
from datetime import datetime, timedelta, timezone

import discord

from discord_bot.commands.trains import can_manage_trains


logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
EPOCH_KST = datetime(1970, 1, 1, 9, tzinfo=KST)
NO_MENTIONS = discord.AllowedMentions.none()
PRIMARY = discord.ButtonStyle.primary
SECONDARY = discord.ButtonStyle.secondary
SUCCESS = discord.ButtonStyle.success
DANGER = discord.ButtonStyle.danger


def parse_departure(text):
    value = text.strip()
    if not value:
        return None
    try:
        local = datetime.strptime(value, "%Y-%m-%d %H:%M")
        if local.year < 1970:
            raise ValueError
        epoch = int((local.replace(tzinfo=KST) - EPOCH_KST).total_seconds())
        if epoch < 0:
            raise ValueError
        return epoch
    except (OverflowError, OSError, ValueError):
        raise ValueError("출발 일시는 한국시간으로 2026-12-05 22:00처럼 입력해주세요.") from None


def format_departure(timestamp):
    if timestamp is None:
        return ""
    # datetime.fromtimestamp delegates to the OS, whose date range varies on Windows.
    try:
        local = EPOCH_KST + timedelta(seconds=timestamp)
    except OverflowError:
        return "날짜 범위 초과"
    return f"{local.year:04d}-{local.month:02d}-{local.day:02d} {local.hour:02d}:{local.minute:02d}"


def safe_text(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))


def clipped(value, units):
    """Discord's UTF-16 limits count astral emoji as two units."""
    encoded = value.encode("utf-16-le")
    if len(encoded) <= units * 2:
        return value
    return encoded[:(units - 1) * 2].decode("utf-16-le", "ignore") + "…"


async def reply(interaction, content=None, *, view=None, embed=None):
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    options = {"ephemeral": True, "allowed_mentions": NO_MENTIONS}
    if view is not None:
        options["view"] = view
    if embed is not None:
        options["embed"] = embed
    await send(content, **options)


async def report_error(interaction, exc):
    if isinstance(exc, ValueError):
        message = str(exc)
    elif isinstance(exc, discord.Forbidden):
        message = "봇에 채널·메시지 관리 권한이 필요해요. 관리자에게 확인해주세요."
    else:
        logger.error("계획된 만남 처리 오류", exc_info=(type(exc), exc, exc.__traceback__))
        message = "요청을 완료하지 못했어요. 파티 상태를 다시 확인해주세요."
    await reply(interaction, message[:1800])


class PersonalView(discord.ui.View):
    def __init__(self, interaction):
        super().__init__(timeout=300)
        self.owner_id = interaction.user.id
        self.guild_id = interaction.guild_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id and interaction.guild_id == self.guild_id:
            return True
        await reply(interaction, "이 메뉴를 연 사람만 사용할 수 있어요.")
        return False

    async def on_error(self, interaction, error, item):
        await report_error(interaction, error)

    def button(self, label, callback, *, style=SECONDARY, row=None, disabled=False):
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)


class Form(discord.ui.Modal):
    def __init__(self, interaction, title, fields, on_save):
        super().__init__(title=title, timeout=300)
        self.owner_id = interaction.user.id
        self.guild_id = interaction.guild_id
        self.on_save = on_save
        self.inputs = {}
        for key, label, default, limit, required, paragraph, hint in fields:
            field = discord.ui.TextInput(
                default=str(default or "") or None, max_length=limit,
                required=required, placeholder=hint or None,
                style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
            )
            self.inputs[key] = field
            self.add_item(discord.ui.Label(text=label, component=field))

    async def on_submit(self, interaction):
        if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
            return await reply(interaction, "이 양식을 연 사람만 저장할 수 있어요.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.on_save(interaction, {k: field.value.strip() for k, field in self.inputs.items()})

    async def on_error(self, interaction, error):
        await report_error(interaction, error)


def can_join(p):
    return (p["phase"] == "ready" and p["activity"] != "ended"
            and not p["closed"] and len(p["members"]) < p["capacity"])


def status(p):
    if p["phase"] in ("disbanding", "disbanded"):
        return "⚫ 해산" if p["phase"] == "disbanded" else "⚪ 해산 처리 중"
    if p["phase"] == "creating":
        return "🕒 생성 중"
    if p["activity"] == "ended":
        return "🏁 활동 종료"
    recruitment = "🔴 모집 마감" if p["closed"] else (
        "🟡 모집 완료" if len(p["members"]) >= p["capacity"] else "🟢 모집 중"
    )
    return f"⚔️ 활동 중 · {recruitment}" if p["activity"] == "active" else recruitment


def party_embed(p, surface="public"):
    occupancy = len(p["members"])
    slots = p["capacity"] - occupancy
    title = {"public": "💌 계획된 만남", "room": "🔒 파티방 안내", "ad": "💌 파티원 모집"}[surface]
    e = discord.Embed(title=title, description=clipped(safe_text(p["destination"]), 240),
                      colour=discord.Colour.green() if can_join(p) else discord.Colour.gold())
    leader = f'<@{p["leader_id"]}>' if p["leader_id"] else "관리자 인계 대기"
    e.add_field(name="파티장", value=leader)
    e.add_field(name="모집 현황", value=f'{occupancy} / {p["capacity"]}명 · 빈자리 {slots}명')
    e.add_field(name="출발 예정", value=(format_departure(p["departure"]) + " KST") if p["departure"] is not None else "미정")
    e.add_field(name="상태", value=status(p))
    spectating = "열림 · 자유 입장·채팅" if p["spectating"] else "닫힘"
    e.add_field(name="관전", value=spectating if p["phase"] == "ready" else "입장 불가")
    if surface != "ad" and occupancy:
        names = [f'<@{uid}>' for uid in p["members"][:12]]
        if occupancy > len(names):
            names.append(f'외 {occupancy-len(names)}명')
        e.add_field(name="참가자", value=" · ".join(names), inline=False)
    for key, label in (("conditions", "모집 조건"), ("description", "추가 설명")):
        if p[key]:
            e.add_field(name=label, value=clipped(safe_text(p[key]), 1024), inline=False)
    if surface == "room":
        if p["strategy"]:
            e.add_field(name="작전 및 공지", value=clipped(safe_text(p["strategy"]), 1024), inline=False)
        e.add_field(name="파티방 이용", value="아래에서 로스터와 공지를 열어볼 수 있어요. 관전자는 정원과 로스터에 포함되지 않고 함께 채팅할 수 있어요.", inline=False)
    return e


BUTTONS = {
    "join": ("참가하기", SUCCESS), "leave": ("나가기", SECONDARY), "room": ("파티방 이동", PRIMARY),
    "watch": ("관전하기", SECONDARY), "manage": ("파티 관리", SECONDARY),
    "promote": ("홍보하기", PRIMARY), "rosters": ("로스터", SECONDARY),
    "myroster": ("내 로스터 수정", SECONDARY), "notice": ("작전 및 공지", SECONDARY),
}


class MeetingButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"meeting:p:(?P<pid>[1-9][0-9]{0,18}):(?P<action>join|leave|room|watch|manage|promote|rosters|myroster|notice)"):
    def __init__(self, pid, action, *, row=None, disabled=False, item=None):
        label, style = BUTTONS[action]
        button = item if item is not None else discord.ui.Button(
            label=label, style=style, disabled=disabled, row=row, custom_id=f"meeting:p:{pid}:{action}",
        )
        super().__init__(button)
        self.pid, self.action = pid, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["pid"]), match["action"], item=item)

    async def callback(self, interaction):
        try:
            if interaction.guild_id is None:
                return await reply(interaction, "이 버튼은 해당 파티의 서버 안에서만 사용할 수 있어요.")
            message = interaction.message
            if message is None:
                return await reply(interaction, "원본 파티 메시지에서 다시 선택해주세요.")
            if message.webhook_id is None:
                owned = message.author.id == interaction.client.user.id
            else:
                # Discord delivers application interaction follow-ups via the application webhook.
                owned = message.flags.ephemeral and message.webhook_id == interaction.application_id
            if not owned:
                return await reply(interaction, "머장봇이 게시한 파티 버튼을 사용해주세요.")
            await route(interaction, interaction.client.meetings, self.pid, self.action)
        except Exception as exc:
            await report_error(interaction, exc)


def party_view(p, surface="public"):
    v = discord.ui.View(timeout=None)
    done = p["phase"] in ("disbanding", "disbanded")
    rows = {
        "public": (("join", "watch", "leave", "room"), ("manage", "promote")),
        "ad": (("join", "watch"),),
        "room": (("rosters", "myroster", "notice"), ("manage", "promote", "leave")),
    }
    for row, actions in enumerate(rows[surface]):
        for action in actions:
            disabled = (done or (action in ("join", "promote") and not can_join(p))
                        or (action == "watch" and (not p["spectating"] or p["phase"] != "ready")))
            v.add_item(MeetingButton(p["id"], action, row=row, disabled=disabled))
    if surface == "ad" and p["recruit_id"] and p["recruit_channel_id"]:
        link = f'https://discord.com/channels/{p["guild_id"]}/{p["recruit_channel_id"]}/{p["recruit_id"]}'
        v.add_item(discord.ui.Button(label="원본 모집글 보기", url=link, row=0))
    return v


def room_link(p):
    if not p["room_id"]:
        return "파티방 준비 상태를 관리자에게 확인해주세요."
    return f'파티방으로 이동 → <#{p["room_id"]}>'


async def open_detail(interaction, service, party_id):
    p = service.store.get(interaction.guild_id, party_id)
    if not service.store.settings(interaction.guild_id)["enabled"] and not can_manage_trains(interaction.user):
        raise ValueError("관리자가 계획된 만남을 잠시 비활성화했어요.")
    await reply(interaction, embed=party_embed(p), view=party_view(p))


async def open_create(interaction, service, *, spectating=True):
    if not service.store.settings(interaction.guild_id)["enabled"]:
        raise ValueError("관리자가 계획된 만남을 먼저 활성화해야 해요.")
    await party_form(interaction, service, spectating=spectating)


async def party_form(interaction, service, *, party=None, spectating=True):
    p = party or {}
    fields = [
        ("destination", "행선지", p.get("destination"), 100, True, False, "예) 토요일 어비스 트라이"),
        ("capacity", "총정원 (파티장 포함)", p.get("capacity"), 6, True, False, "예) 8"),
        ("departure", "출발 예정 일시 (선택 · 한국시간)", format_departure(p.get("departure")), 16, False, False, "2026-12-05 22:00"),
        ("conditions", "모집 조건 (선택)", p.get("conditions"), 1000, False, True, ""),
        ("description", "추가 설명 (선택)", p.get("description"), 1000, False, True, ""),
    ]

    async def save(i, values):
        if not values["capacity"].isascii() or not values["capacity"].isdigit():
            raise ValueError("총정원은 파티장을 포함한 양의 정수로 입력해주세요.")
        values["capacity"] = int(values["capacity"])
        values["departure"] = parse_departure(values["departure"])
        if party is not None:
            changes = {key: value for key, value in values.items() if value != p[key]}
            if not changes:
                return await reply(i, "변경한 파티 정보가 없어요.")
            await service.act(i, p["id"], "edit", **changes)
            await reply(i, "파티 정보를 수정했어요.")
        else:
            result = await service.create(i, **values, spectating=spectating)
            await reply(i, "파티를 만들었어요. " + room_link(result))

    title = "파티 정보 수정" if party else ("새 파티 · 관전 허용" if spectating else "새 파티 · 관전 닫힘")
    await interaction.response.send_modal(Form(interaction, title, fields, save))


async def route(i, svc, pid, action):
    if action == "manage":
        return await open_manage(i, svc, pid)
    if action == "room":
        p = svc.require_access(i, pid)
        return await reply(i, room_link(p))
    if action == "notice":
        p = svc.require_access(i, pid)
        return await show_notice(i, svc, p)
    if action == "rosters":
        return await show_roster(i, svc, pid)
    if action == "myroster":
        svc.require_member(i, pid)
        return await roster_form(i, svc, pid, i.user.id)
    if action == "leave":
        p = svc.store.get(i.guild_id, pid)
        if i.user.id == p["leader_id"]:
            return await open_manage(i, svc, pid, lead="다른 참가자에게 파티장을 위임한 뒤 나가거나, 파티를 해산할 수 있어요.")
        if i.user.id in p["watchers"]:
            action = "unwatch"
    messages = {"join": "파티에 참가했어요.", "watch": "관전을 시작했어요. 함께 채팅할 수 있어요.",
                "leave": "파티에서 나왔어요.", "unwatch": "관전을 끝냈어요.", "promote": "자유 채팅에 파티를 홍보했어요."}
    if action not in messages:
        raise ValueError("현재 모집글에서 다시 선택해주세요.")
    await i.response.defer(ephemeral=True, thinking=True)
    result = await svc.act(i, pid, action)
    message = messages[action]
    if action in ("join", "watch"):
        message += " " + room_link(result)
    if result.get("sync_warning"):
        message += "\n모집글 갱신이 대기 중이에요."
    await reply(i, message)


async def show_roster(i, svc, pid, page=0):
    p = svc.require_access(i, pid)
    members = p["members"]
    per_page = 5
    pages = max(1, (len(members) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    embed = discord.Embed(title=f"파티 로스터 · {page + 1}/{pages}", colour=discord.Colour.blurple())
    if not members:
        embed.description = "현재 참가자가 없어요."
    for uid in members[page*per_page:(page+1)*per_page]:
        data = p["roster"].get(uid, {})
        identity = [safe_text(data[key]) for key in ("character", "job", "role") if data.get(key)]
        note = safe_text(data.get("note") or "")
        value = f"<@{uid}> · " + (" / ".join(identity) if identity else "미등록")
        if note:
            value += "\n" + note
        embed.add_field(name="👑 파티장" if uid == p["leader_id"] else "파티원", value=clipped(value, 1024), inline=False)
    v = PersonalView(i)

    async def previous(j):
        await show_roster(j, svc, pid, page - 1)

    async def following(j):
        await show_roster(j, svc, pid, page + 1)

    if pages > 1:
        v.button("이전", previous, disabled=page == 0)
        v.button("다음", following, disabled=page + 1 == pages)
    await reply(i, embed=embed, view=v if v.children else None)


async def show_notice(i, svc, p, page=0):
    text = p["strategy"] or "아직 등록된 공지가 없어요."
    # Two UTF-16 units per code point covers the longest possible emoji notice.
    pages = [text[start:start+2000] for start in range(0, len(text), 2000)]
    page = min(page, len(pages)-1)
    e = discord.Embed(title="📌 작전 및 공지", description=pages[page])
    v = PersonalView(i)
    if len(pages) > 1:
        async def switch(j):
            current = svc.require_access(j, p["id"])
            await show_notice(j, svc, current, 1 if page == 0 else 0)
        v.button("다음" if page == 0 else "이전", switch)
        e.set_footer(text=f"{page+1} / {len(pages)}")
    await reply(i, embed=e, view=v if v.children else None)


async def roster_form(i, svc, pid, target):
    p = svc.require_member(i, pid) if target == i.user.id and not can_manage_trains(i.user) else svc.require_manager(i, pid)
    if target not in p["members"]:
        raise ValueError("현재 파티에 참가 중인 사람의 로스터만 수정할 수 있어요.")
    data = p["roster"].get(target, {})
    fields = [(key, label, data.get(key), limit, False, paragraph, "") for key, label, limit, paragraph in (
        ("character", "캐릭터명", 100, False), ("job", "직업", 100, False),
        ("role", "담당 역할", 100, False), ("note", "기타 비고", 300, True),
    )]

    async def save(j, values):
        changes = {key: value for key, value in values.items() if value != (data.get(key) or "")}
        if not changes:
            return await reply(j, "변경한 로스터 정보가 없어요.")
        await svc.act(j, pid, "roster", target=target, **changes)
        await reply(j, "로스터를 저장했어요.")

    await i.response.send_modal(Form(i, "로스터 수정", fields, save))


async def strategy_form(i, svc, pid):
    p = svc.require_manager(i, pid)
    fields = [("text", "작전·역할 분배·준비물·공지", p["strategy"], 3500, False, True, "파티방에서 공유할 내용을 입력하세요.")]

    async def save(j, values):
        await svc.act(j, pid, "strategy", **values)
        await reply(j, "파티방의 작전 및 공지를 저장했어요.")

    await i.response.send_modal(Form(i, "작전 및 공지 수정", fields, save))


async def confirm(i, prompt, label, callback):
    v = PersonalView(i)

    async def yes(j):
        v.stop()
        await callback(j)

    async def cancel(j):
        v.stop()
        await j.response.edit_message(content="취소했어요.", view=None, embed=None, allowed_mentions=NO_MENTIONS)

    v.button(label, yes, style=DANGER)
    v.button("취소", cancel)
    await reply(i, prompt, view=v)


async def select_person(i, svc, pid, action, page=0):
    p = svc.require_manager(i, pid)
    labels = {"kick": "추방 대상", "allow": "재입장 허용 대상",
              "transfer": "파티장 위임 대상", "roster": "로스터 수정 대상"}
    if action == "allow":
        candidates = p["banned"]
    elif action == "kick":
        candidates = [uid for uid in p["members"] if uid != p["leader_id"]] + p["watchers"]
    elif action == "transfer":
        candidates = [uid for uid in p["members"] if uid != p["leader_id"]]
    else:
        candidates = p["members"]
    if not candidates:
        return await reply(i, labels[action] + "이 없어요.")
    per_page = 10
    pages = (len(candidates) + per_page - 1) // per_page
    page = min(max(page, 0), pages - 1)
    options = []
    lines = []
    for n, uid in enumerate(candidates[page*per_page:(page+1)*per_page], start=page*per_page+1):
        member = i.guild.get_member(uid)
        display = member.display_name if member else str(uid)
        label = "관전자" if uid in p["watchers"] else ("재입장 제한" if action == "allow" else "참가자")
        options.append(discord.SelectOption(label=clipped(f"{n}. {display}", 100), value=str(uid), description=label))
        lines.append(f"{n}. <@{uid}> · {label}")
    v = PersonalView(i)
    picker = discord.ui.Select(placeholder=labels[action], options=options, row=0)

    async def selected(j):
        svc.require_manager(j, pid)
        target = int(picker.values[0])
        if action == "roster":
            return await roster_form(j, svc, pid, target)

        async def apply(k):
            await k.response.defer(ephemeral=True, thinking=True)
            await svc.act(k, pid, action, target=target)
            messages = {"kick": "추방했어요. 다시 허용할 때까지 참가와 관전이 차단돼요.",
                        "allow": "이제 다시 참가하거나 관전할 수 있어요.", "transfer": "파티장을 위임했어요."}
            await reply(k, f"<@{target}> · " + messages[action])

        if action in ("kick", "transfer"):
            label = "추방" if action == "kick" else "파티장 위임"
            await confirm(j, f"<@{target}>님에게 **{label}** 작업을 진행할까요?", label, apply)
        else:
            await apply(j)

    picker.callback = selected
    v.add_item(picker)
    if pages > 1:
        async def previous(j):
            await select_person(j, svc, pid, action, page - 1)

        async def following(j):
            await select_person(j, svc, pid, action, page + 1)

        v.button("이전", previous, row=1, disabled=page == 0)
        v.button("다음", following, row=1, disabled=page + 1 == pages)
    await reply(i, f"**{labels[action]} 선택** · {page+1}/{pages}\n" + "\n".join(lines), view=v)


async def open_manage(i, svc, party_id, *, lead=None):
    p = svc.require_manager(i, party_id)
    v = PersonalView(i)

    def mutation(action, message):
        async def apply(j):
            await j.response.defer(ephemeral=True, thinking=True)
            await svc.act(j, party_id, action)
            await reply(j, message)
        return apply

    async def edit(j):
        current = svc.require_manager(j, party_id)
        await party_form(j, svc, party=current)

    async def notice(j):
        await strategy_form(j, svc, party_id)

    def pick(action):
        async def choose(j):
            await select_person(j, svc, party_id, action)
        return choose

    async def disband(j):
        svc.require_manager(j, party_id)
        await confirm(j, "**파티를 해산하면 모집글, 홍보글, 전용 채널과 그 안의 채팅 기록이 삭제됩니다.** 이 작업은 되돌릴 수 없어요.",
                      "채널 삭제 및 해산", mutation("disband", "파티를 해산했어요."))

    async def toggle_watch(j):
        svc.require_manager(j, party_id)
        if p["spectating"]:
            await confirm(j, "관전을 닫으면 현재 관전자들의 파티방 접근도 회수돼요. 관전을 닫을까요?",
                          "관전 닫기", mutation("close_watch", "관전을 닫았어요."))
        else:
            await mutation("open_watch", "관전을 열었어요. 서버 구성원이 즉시 관전할 수 있어요.")(j)

    async def repair(j):
        svc.require_admin(j)
        await mutation("repair", "파티방과 모집 메시지를 동기화했어요.")(j)

    admin = can_manage_trains(i.user)
    if p["phase"] == "ready":
        v.button("파티 정보 수정", edit, style=PRIMARY, row=0)
        v.button("로스터 수정", pick("roster"), row=0)
        v.button("작전·공지 수정", notice, row=0)
        v.button("참가자·관전자 추방", pick("kick"), row=1)
        v.button("재입장 허용", pick("allow"), row=1)
        v.button("파티장 위임", pick("transfer"), row=1)
        v.button("관전 닫기" if p["spectating"] else "관전 열기", toggle_watch, row=2)
        if p["closed"]:
            v.button("모집 재개", mutation("open", "모집을 재개했어요."), row=2)
        else:
            v.button("모집 마감", mutation("close", "모집을 마감했어요."), row=2)
        if p["activity"] == "ended":
            v.button("활동 재개", mutation("resume", "활동을 재개했어요."), row=2)
        else:
            if p["activity"] == "planned":
                v.button("활동 시작", mutation("start", "활동을 시작했어요."), row=2)
            v.button("활동 종료", mutation("end", "활동을 종료했어요. 파티방과 로스터는 그대로 이용할 수 있어요."), row=2)
        v.button("홍보하기", mutation("promote", "자유 채팅에 모집글을 게시했어요."), disabled=not can_join(p), row=3)
    if p["phase"] in ("creating", "ready"):
        v.button("파티 해산", disband, style=DANGER, row=3)
    if admin and p["phase"] != "disbanded":
        v.button("권한·메시지 복구", repair, row=3)
    await reply(i, lead or f'**{safe_text(p["destination"])}** · 파티 관리\n{status(p)} · 참가 {len(p["members"])}명 · 관전 {len(p["watchers"])}명\n{room_link(p)}',
                view=v if v.children else None)
