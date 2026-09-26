# /열차* 명령과 "우만열차좌석도" 현황판을 관리합니다.
import asyncio
import logging

import discord
from discord import app_commands

from storage.trains import TRAIN_CAPACITY, TrainRepository, TrainStateError

logger = logging.getLogger(__name__)

CHANNEL_NAME = "우만열차좌석도"
CHAT_CHANNEL_NAME = "💬자유-채팅"
PANEL_TITLE = "🚉 **우만역 · 열차 운행 안내**"
LEGACY_PANEL_TITLES = ("🚆 **우만열차 좌석도**",)
TRAIN_ADMIN_ROLES = {"자발적 봉사자", "봉사하는 노예", "머장", "관리자"}
NO_MENTIONS = discord.AllowedMentions.none()

def can_manage_trains(member):
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    if member.id == guild.owner_id:
        return True
    permissions = getattr(member, "guild_permissions", None)
    if permissions is not None and permissions.administrator:
        return True
    return any(getattr(role, "name", "") in TRAIN_ADMIN_ROLES for role in member.roles)


class ConfirmTrainView(discord.ui.View):
    def __init__(self, *, requester_id, confirm_label, action):
        super().__init__(timeout=60)
        self.requester_id = int(requester_id)
        self.action = action
        self.confirm.label = confirm_label

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "이 확인 버튼은 명령을 실행한 사람만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="확인", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            message = await self.action(interaction)
        except Exception:
            logger.exception("열차 확인 동작 오류")
            message = "처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        self.stop()
        await interaction.edit_original_response(content=message, view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="취소했습니다.", view=None)


class TrainController:
    def __init__(self, bot):
        self.bot = bot
        self.repository = TrainRepository(bot.database)
        self._locks = {}
        self.panel_view = None

    def lock_for(self, guild_id):
        return self._locks.setdefault(int(guild_id), asyncio.Lock())

    async def display_name(self, guild, user_id):
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        if member is None:
            return f"사용자 {int(user_id)}"
        return discord.utils.escape_markdown(member.display_name)

    async def panel_text(self, guild):
        snapshot = self.repository.snapshot(guild.id)
        lines = [
            PANEL_TITLE,
            "",
            "📢 탑승하실 열차를 확인해주세요.",
            "빈 좌석이 있는 열차는 아래 `열차 탑승` 버튼으로 이용할 수 있습니다.",
            "",
            "━━━━━━━━━━━━━━━━━━",
            "",
        ]

        active_cars = [
            car_no
            for car_no in sorted(snapshot)
            if snapshot[car_no]["conductor_id"] is not None
        ]
        if not active_cars:
            lines.extend(
                [
                    "📢 현재 플랫폼에 대기 중인 열차가 없습니다.",
                    "",
                    "🚆 `새 열차 만들기`를 눌러",
                    "첫 번째 열차를 운행해주세요.",
                ]
            )
            return "\n".join(lines)

        for car_no in active_cars:
            state = snapshot[car_no]
            conductor_id = state["conductor_id"]
            passengers = state["passenger_ids"]
            count = 1 + len(passengers)
            icon = "🔴" if count >= TRAIN_CAPACITY else "🟢"
            status = "만석" if count >= TRAIN_CAPACITY else "운행중"
            conductor_name = await self.display_name(guild, conductor_id)
            passenger_names = [
                await self.display_name(guild, user_id)
                for user_id in passengers
            ]
            passenger_text = " · ".join(passenger_names) if passenger_names else "승객 없음"

            lines.extend(
                [
                    f"┏━━━━━━ 🚃 {car_no}호차 ━━━━━━┓",
                    f"┃  {icon} {status}  ·  `{count} / {TRAIN_CAPACITY}`",
                    "┃",
                    f"┃  👨‍✈️ 기장  {conductor_name}",
                    f"┃  💺 승객  {passenger_text}",
                    "┗━━━━━━◉━━━━◉━━━━━━┛",
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    async def _find_or_create_channel(self, guild):
        channel = discord.utils.get(guild.text_channels, name=CHANNEL_NAME)
        if channel is not None:
            return channel

        me = guild.me
        if me is None or not me.guild_permissions.manage_channels:
            logger.warning(
                "%s 서버에 #%s 채널이 없고 채널 생성 권한도 없습니다.",
                guild.name,
                CHANNEL_NAME,
            )
            return None

        try:
            channel = await guild.create_text_channel(
                CHANNEL_NAME,
                reason="우만열차 좌석 현황판",
            )
            logger.info("%s 서버에 #%s 채널을 생성했습니다.", guild.name, CHANNEL_NAME)
            return channel
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("%s 채널 생성 실패", CHANNEL_NAME)
            return None

    async def ensure_panel(self, guild):
        channel = await self._find_or_create_channel(guild)
        if channel is None:
            return False

        message = None
        location = self.repository.panel_location(guild.id)
        if location is not None:
            channel_id, message_id = location
            known_channel = guild.get_channel(channel_id)
            if known_channel is not None:
                channel = known_channel
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None

        if message is None:
            try:
                async for candidate in channel.history(limit=50):
                    if (
                        self.bot.user is not None
                        and candidate.author.id == self.bot.user.id
                        and (
                            candidate.content.startswith(PANEL_TITLE)
                            or any(
                                candidate.content.startswith(title)
                                for title in LEGACY_PANEL_TITLES
                            )
                        )
                    ):
                        message = candidate
                        break
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("%s 채널의 기존 현황판을 검색하지 못했습니다.", CHANNEL_NAME)

        content = await self.panel_text(guild)
        try:
            if message is None:
                message = await channel.send(
                    content,
                    view=self.panel_view,
                    allowed_mentions=NO_MENTIONS,
                )
            else:
                await message.edit(
                    content=content,
                    view=self.panel_view,
                    allowed_mentions=NO_MENTIONS,
                )
            self.repository.save_panel_location(guild.id, channel.id, message.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("%s 현황판 전송/수정 실패", CHANNEL_NAME)
            return False

        if not message.pinned:
            me = guild.me
            permissions = channel.permissions_for(me) if me is not None else None
            can_pin = bool(
                permissions is not None
                and getattr(permissions, "pin_messages", False)
            )
            if not can_pin:
                logger.warning(
                    "%s 현황판 고정 생략: 봇에게 이 채널의 '메시지 고정(Pin Messages)' 권한이 필요합니다.",
                    CHANNEL_NAME,
                )
            else:
                try:
                    await message.pin(reason="우만열차 좌석 현황판")
                    logger.info("%s 현황판을 고정했습니다.", CHANNEL_NAME)
                except discord.Forbidden as exc:
                    logger.warning(
                        "%s 현황판 고정 실패(Forbidden): status=%s code=%s text=%s",
                        CHANNEL_NAME,
                        getattr(exc, "status", None),
                        getattr(exc, "code", None),
                        getattr(exc, "text", None),
                    )
                except discord.HTTPException as exc:
                    logger.warning(
                        "%s 현황판 고정 실패(HTTP): status=%s code=%s text=%s",
                        CHANNEL_NAME,
                        getattr(exc, "status", None),
                        getattr(exc, "code", None),
                        getattr(exc, "text", None),
                    )
        return True

    async def announce_train_arrival(self, guild, car_no):
        channel = discord.utils.get(guild.text_channels, name=CHAT_CHANNEL_NAME)
        if channel is None:
            # 일부 서버/클라이언트에서 이모지 없이 채널명이 구성된 경우도 허용합니다.
            channel = discord.utils.get(guild.text_channels, name="자유-채팅")
        if channel is None:
            logger.warning(
                "%s 서버에서 #%s 채널을 찾지 못해 열차 도착 안내를 보내지 못했습니다.",
                guild.name,
                CHAT_CHANNEL_NAME,
            )
            return False

        try:
            await channel.send(
                f"🚆 {car_no}호차가 플랫폼에 도착했습니다. 서둘러 탑승해주세요!",
                allowed_mentions=NO_MENTIONS,
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "%s 채널에 %s호차 도착 안내 전송 실패",
                CHAT_CHANNEL_NAME,
                car_no,
            )
            return False

    async def ensure_all_panels(self):
        for guild in self.bot.guilds:
            try:
                await self.ensure_panel(guild)
            except Exception:
                logger.exception("%s 서버 열차 현황판 초기화 오류", guild.name)

    async def error_text(self, guild, error, requester_id=None):
        if error.code == "invalid_car":
            return "호차 번호는 1 이상의 숫자로 입력해주세요."
        if error.code == "car_active":
            return f"{error.car_no}호차는 이미 운행 중입니다."
        if error.code == "car_inactive":
            return f"{error.car_no}호차는 현재 운행 중이 아닙니다."
        if error.code == "full":
            return f"{error.car_no}호차는 이미 만석입니다."
        if error.code == "duplicate_participants":
            return "같은 사람을 승객으로 중복 지정할 수 없습니다."
        if error.code == "already_boarded":
            if requester_id is not None and int(error.user_id) == int(requester_id):
                return f"이미 {error.existing_car}호차에 탑승 중입니다."
            name = await self.display_name(guild, error.user_id)
            return f"{name}님은 이미 {error.existing_car}호차에 탑승 중입니다."
        if error.code == "not_boarded":
            return "현재 탑승 중인 열차가 없습니다."
        if error.code == "not_passenger":
            name = await self.display_name(guild, error.user_id)
            return f"{name}님은 {error.car_no}호차 승객이 아닙니다."
        if error.code == "conductor_cannot_leave":
            return "기장은 좌석도의 '내 열차 종료' 버튼을 이용해 주세요."
        if error.code == "not_conductor":
            return f"이 명령은 {error.car_no}호차 기장만 사용할 수 있습니다."
        return "열차 상태를 처리할 수 없습니다."

    @staticmethod
    def panel_suffix(panel_ok):
        return "" if panel_ok else "\n⚠️ 좌석은 저장됐지만 현황판을 갱신하지 못했습니다."



class TrainPassengerView(discord.ui.View):
    """탑승 직후 해당 사용자에게만 보여주는 빠른 하차 UI."""

    def __init__(self, controller, requester_id, car_no):
        super().__init__(timeout=300)
        self.controller = controller
        self.requester_id = int(requester_id)
        self.car_no = int(car_no)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "이 버튼은 탑승한 본인만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="🚪 하차하기", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 기능은 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.controller.lock_for(interaction.guild.id):
                car_no = self.controller.repository.leave(
                    interaction.guild.id,
                    interaction.user.id,
                )
            panel_ok = await self.controller.ensure_panel(interaction.guild)
            await interaction.edit_original_response(
                content=(
                    f"👋 {car_no}호차에서 하차했습니다."
                    + self.controller.panel_suffix(panel_ok)
                ),
                view=None,
            )
        except TrainStateError as error:
            await interaction.edit_original_response(
                content=await self.controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                view=None,
            )
        except Exception:
            logger.exception("좌석도 하차 버튼 오류")
            await interaction.edit_original_response(
                content="하차 처리 중 오류가 발생했습니다.",
                view=None,
            )


class TrainBoardSelect(discord.ui.Select):
    def __init__(self, controller, requester_id, snapshot):
        self.controller = controller
        self.requester_id = int(requester_id)

        options = []
        for car_no in sorted(snapshot):
            state = snapshot[car_no]
            if state["conductor_id"] is None:
                continue
            count = 1 + len(state["passenger_ids"])
            if count >= TRAIN_CAPACITY:
                continue
            options.append(
                discord.SelectOption(
                    label=f"{car_no}호차",
                    description=f"현재 {count}/{TRAIN_CAPACITY}명 탑승",
                    value=str(car_no),
                    emoji="🚆",
                )
            )
            if len(options) >= 25:
                break

        super().__init__(
            placeholder="탑승할 열차를 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="train:panel:board-select",
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 탑승 메뉴는 요청한 본인만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 기능은 서버 안에서 사용해주세요.", ephemeral=True
            )
            return

        car_no = int(self.values[0])
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.controller.lock_for(interaction.guild.id):
                self.controller.repository.board(
                    interaction.guild.id,
                    car_no,
                    interaction.user.id,
                )
            panel_ok = await self.controller.ensure_panel(interaction.guild)
            await interaction.edit_original_response(
                content=(
                    f"🎫 {car_no}호차에 탑승했습니다."
                    + self.controller.panel_suffix(panel_ok)
                ),
                view=TrainPassengerView(
                    self.controller,
                    interaction.user.id,
                    car_no,
                ),
            )
        except TrainStateError as error:
            await interaction.edit_original_response(
                content=await self.controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                view=None,
            )
        except Exception:
            logger.exception("좌석도 탑승 선택 오류")
            await interaction.edit_original_response(
                content="열차 탑승 처리 중 오류가 발생했습니다.",
                view=None,
            )


class TrainBoardPickerView(discord.ui.View):
    def __init__(self, controller, requester_id, snapshot):
        super().__init__(timeout=120)
        self.requester_id = int(requester_id)
        self.add_item(TrainBoardSelect(controller, requester_id, snapshot))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "이 탑승 메뉴는 요청한 본인만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False


async def build_passenger_manage_view(controller, guild, requester_id, car_no):
    state = controller.repository.snapshot(guild.id).get(int(car_no))
    if state is None or state["conductor_id"] != int(requester_id):
        return (
            "현재 기장으로 운행 중인 열차가 없습니다.",
            None,
        )

    passenger_ids = list(state["passenger_ids"])
    passenger_options = []
    passenger_names = []
    for user_id in passenger_ids:
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        name = member.display_name if member is not None else f"사용자 {int(user_id)}"
        passenger_names.append(discord.utils.escape_markdown(name))
        passenger_options.append(
            discord.SelectOption(
                label=name[:100],
                value=str(int(user_id)),
                description=f"{int(car_no)}호차에서 하차시키기",
                emoji="🚪",
            )
        )

    count = 1 + len(passenger_ids)
    content = (
        f"👥 **{int(car_no)}호차 승객 관리**\n"
        f"현재 좌석: {count}/{TRAIN_CAPACITY}\n"
        f"승객: {', '.join(passenger_names) if passenger_names else '-'}"
    )
    return (
        content,
        TrainPassengerManageView(
            controller,
            requester_id,
            car_no,
            passenger_options,
            can_add=count < TRAIN_CAPACITY,
        ),
    )


class TrainPassengerAddSelect(discord.ui.UserSelect):
    def __init__(self, controller, requester_id, car_no):
        self.controller = controller
        self.requester_id = int(requester_id)
        self.car_no = int(car_no)
        super().__init__(
            placeholder="➕ 추가할 승객을 선택하세요",
            min_values=1,
            max_values=1,
            custom_id=f"train:manage:add:{self.car_no}",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 승객 관리 메뉴는 해당 열차 기장만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 기능은 서버 안에서 사용해주세요.", ephemeral=True
            )
            return

        target = self.values[0]
        if getattr(target, "bot", False):
            await interaction.response.send_message(
                "봇 계정은 승객으로 추가할 수 없습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with self.controller.lock_for(interaction.guild.id):
                self.controller.repository.add_passenger(
                    interaction.guild.id,
                    self.car_no,
                    interaction.user.id,
                    target.id,
                )
            panel_ok = await self.controller.ensure_panel(interaction.guild)
            content, view = await build_passenger_manage_view(
                self.controller,
                interaction.guild,
                interaction.user.id,
                self.car_no,
            )
            name = discord.utils.escape_markdown(
                getattr(target, "display_name", target.name)
            )
            await interaction.edit_original_response(
                content=(
                    f"➕ {name}님을 {self.car_no}호차에 추가했습니다.\n\n"
                    + content
                    + self.controller.panel_suffix(panel_ok)
                ),
                view=view,
            )
        except TrainStateError as error:
            await interaction.edit_original_response(
                content=await self.controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                view=None,
            )
        except Exception:
            logger.exception("좌석도 승객 추가 UI 오류")
            await interaction.edit_original_response(
                content="승객 추가 처리 중 오류가 발생했습니다.",
                view=None,
            )


class TrainPassengerRemoveSelect(discord.ui.Select):
    def __init__(self, controller, requester_id, car_no, passenger_options):
        self.controller = controller
        self.requester_id = int(requester_id)
        self.car_no = int(car_no)
        super().__init__(
            placeholder="➖ 하차시킬 승객을 선택하세요",
            min_values=1,
            max_values=1,
            options=passenger_options,
            custom_id=f"train:manage:remove:{self.car_no}",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 승객 관리 메뉴는 해당 열차 기장만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 기능은 서버 안에서 사용해주세요.", ephemeral=True
            )
            return

        user_id = int(self.values[0])
        member = interaction.guild.get_member(user_id)
        name = (
            discord.utils.escape_markdown(member.display_name)
            if member is not None
            else f"사용자 {user_id}"
        )

        await interaction.response.defer(ephemeral=True)
        try:
            async with self.controller.lock_for(interaction.guild.id):
                self.controller.repository.remove_passenger(
                    interaction.guild.id,
                    self.car_no,
                    interaction.user.id,
                    user_id,
                )
            panel_ok = await self.controller.ensure_panel(interaction.guild)
            content, view = await build_passenger_manage_view(
                self.controller,
                interaction.guild,
                interaction.user.id,
                self.car_no,
            )
            await interaction.edit_original_response(
                content=(
                    f"➖ {name}님을 {self.car_no}호차에서 하차시켰습니다.\n\n"
                    + content
                    + self.controller.panel_suffix(panel_ok)
                ),
                view=view,
            )
        except TrainStateError as error:
            await interaction.edit_original_response(
                content=await self.controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                view=None,
            )
        except Exception:
            logger.exception("좌석도 승객 하차 UI 오류")
            await interaction.edit_original_response(
                content="승객 하차 처리 중 오류가 발생했습니다.",
                view=None,
            )


class TrainPassengerManageView(discord.ui.View):
    def __init__(
        self,
        controller,
        requester_id,
        car_no,
        passenger_options,
        *,
        can_add,
    ):
        super().__init__(timeout=180)
        self.requester_id = int(requester_id)
        if can_add:
            self.add_item(
                TrainPassengerAddSelect(controller, requester_id, car_no)
            )
        if passenger_options:
            self.add_item(
                TrainPassengerRemoveSelect(
                    controller,
                    requester_id,
                    car_no,
                    passenger_options,
                )
            )

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "이 승객 관리 메뉴는 해당 열차 기장만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False


class TrainPanelView(discord.ui.View):
    """좌석도 메시지에 항상 붙어 있는 사용자용 열차 UI."""

    def __init__(self, controller):
        super().__init__(timeout=None)
        self.controller = controller

    @discord.ui.button(
        label="새 열차 만들기",
        emoji="🚆",
        style=discord.ButtonStyle.primary,
        custom_id="train:panel:create",
    )
    async def create_train(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 기능은 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.controller.lock_for(interaction.guild.id):
                existing = self.controller.repository.find_user(
                    interaction.guild.id, interaction.user.id
                )
                if existing is not None:
                    raise TrainStateError(
                        "already_boarded",
                        user_id=interaction.user.id,
                        existing_car=existing.car_no,
                    )
                car_no = self.controller.repository.next_available_car(
                    interaction.guild.id
                )
                self.controller.repository.start(
                    interaction.guild.id,
                    car_no,
                    interaction.user.id,
                )
            panel_ok = await self.controller.ensure_panel(interaction.guild)
            await self.controller.announce_train_arrival(interaction.guild, car_no)
            await interaction.followup.send(
                (
                    f"🚆 {car_no}호차를 만들었습니다. 기장으로 등록했습니다."
                    + self.controller.panel_suffix(panel_ok)
                ),
                ephemeral=True,
            )
        except TrainStateError as error:
            await interaction.followup.send(
                await self.controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                ephemeral=True,
            )
        except Exception:
            logger.exception("좌석도 새 열차 만들기 오류")
            await interaction.followup.send(
                "열차 생성 처리 중 오류가 발생했습니다.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="열차 탑승",
        emoji="🎫",
        style=discord.ButtonStyle.success,
        custom_id="train:panel:board",
    )
    async def board_train(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 기능은 서버 안에서 사용해주세요.", ephemeral=True
            )
            return

        existing = self.controller.repository.find_user(
            interaction.guild.id, interaction.user.id
        )
        if existing is not None:
            if existing.role == "passenger":
                await interaction.response.send_message(
                    f"이미 {existing.car_no}호차에 탑승 중입니다.",
                    view=TrainPassengerView(
                        self.controller,
                        interaction.user.id,
                        existing.car_no,
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    (
                        f"현재 {existing.car_no}호차 기장입니다. "
                        "운행을 끝내려면 '내 열차 종료' 버튼을 이용해 주세요."
                    ),
                    ephemeral=True,
                )
            return

        snapshot = self.controller.repository.snapshot(interaction.guild.id)
        available = {
            car_no: state
            for car_no, state in snapshot.items()
            if state["conductor_id"] is not None
            and 1 + len(state["passenger_ids"]) < TRAIN_CAPACITY
        }
        if not available:
            await interaction.response.send_message(
                "현재 탑승 가능한 열차가 없습니다.",
                ephemeral=True,
            )
            return

        extra = (
            "\n운행 중인 열차가 많아 앞의 25개만 표시합니다."
            if len(available) > 25
            else ""
        )
        await interaction.response.send_message(
            "탑승할 열차를 선택하세요." + extra,
            view=TrainBoardPickerView(
                self.controller,
                interaction.user.id,
                available,
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="내 열차 하차",
        emoji="🚪",
        style=discord.ButtonStyle.secondary,
        custom_id="train:panel:leave",
    )
    async def leave_train(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 기능은 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.controller.lock_for(interaction.guild.id):
                car_no = self.controller.repository.leave(
                    interaction.guild.id,
                    interaction.user.id,
                )
            panel_ok = await self.controller.ensure_panel(interaction.guild)
            await interaction.followup.send(
                (
                    f"👋 {car_no}호차에서 하차했습니다."
                    + self.controller.panel_suffix(panel_ok)
                ),
                ephemeral=True,
            )
        except TrainStateError as error:
            await interaction.followup.send(
                await self.controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                ephemeral=True,
            )
        except Exception:
            logger.exception("좌석도 내 열차 하차 오류")
            await interaction.followup.send(
                "하차 처리 중 오류가 발생했습니다.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="승객 관리",
        emoji="👥",
        style=discord.ButtonStyle.secondary,
        custom_id="train:panel:passengers",
    )
    async def manage_passengers(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 기능은 서버 안에서 사용해주세요.", ephemeral=True
            )
            return

        seat = self.controller.repository.find_user(
            interaction.guild.id, interaction.user.id
        )
        if seat is None or seat.role != "conductor":
            await interaction.response.send_message(
                "현재 기장으로 운행 중인 열차가 없습니다.",
                ephemeral=True,
            )
            return

        content, view = await build_passenger_manage_view(
            self.controller,
            interaction.guild,
            interaction.user.id,
            seat.car_no,
        )
        await interaction.response.send_message(
            content,
            view=view,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @discord.ui.button(
        label="내 열차 종료",
        emoji="🛑",
        style=discord.ButtonStyle.danger,
        custom_id="train:panel:end",
    )
    async def end_train(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 기능은 서버 안에서 사용해주세요.", ephemeral=True
            )
            return

        seat = self.controller.repository.find_user(
            interaction.guild.id, interaction.user.id
        )
        if seat is None or seat.role != "conductor":
            await interaction.response.send_message(
                "현재 기장으로 운행 중인 열차가 없습니다.",
                ephemeral=True,
            )
            return
        car_no = seat.car_no

        async def do_end(button_interaction):
            try:
                async with self.controller.lock_for(interaction.guild.id):
                    self.controller.repository.end(
                        interaction.guild.id,
                        car_no,
                        interaction.user.id,
                    )
                panel_ok = await self.controller.ensure_panel(interaction.guild)
                return (
                    f"🛑 {car_no}호차 운행을 종료했습니다."
                    + self.controller.panel_suffix(panel_ok)
                )
            except TrainStateError as error:
                return await self.controller.error_text(
                    interaction.guild, error, interaction.user.id
                )

        await interaction.response.send_message(
            f"{car_no}호차 운행을 종료할까요? 기장과 승객 좌석이 모두 비워집니다.",
            view=ConfirmTrainView(
                requester_id=interaction.user.id,
                confirm_label="운행 종료",
                action=do_end,
            ),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

def register(bot):
    controller = TrainController(bot)
    controller.panel_view = TrainPanelView(controller)
    bot.train_controller = controller

    @bot.tree.command(
        name="열차구성",
        description="관리자가 실제 운행 중인 열차의 기장과 승객 구성을 직접 맞춥니다.",
    )
    @app_commands.describe(
        호차="구성할 열차 번호 (1 이상)",
        기장="해당 열차의 기장",
        승객1="첫 번째 승객 (선택)",
        승객2="두 번째 승객 (선택)",
    )
    async def train_set_composition(
        interaction: discord.Interaction,
        호차: int,
        기장: discord.Member,
        승객1: discord.Member | None = None,
        승객2: discord.Member | None = None,
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        if not can_manage_trains(interaction.user):
            await interaction.response.send_message(
                "열차 관리 권한이 없습니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        passengers = [member for member in (승객1, 승객2) if member is not None]
        try:
            async with controller.lock_for(interaction.guild.id):
                controller.repository.set_composition(
                    interaction.guild.id,
                    호차,
                    기장.id,
                    [member.id for member in passengers],
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            names = [discord.utils.escape_markdown(기장.display_name)]
            names.extend(
                discord.utils.escape_markdown(member.display_name)
                for member in passengers
            )
            await interaction.followup.send(
                f"🛠️ {호차}호차 구성을 직접 반영했습니다. "
                f"기장: {names[0]}"
                + (f" / 승객: {', '.join(names[1:])}" if len(names) > 1 else " / 승객: 없음")
                + controller.panel_suffix(panel_ok),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        except TrainStateError as error:
            await interaction.followup.send(
                await controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        except Exception:
            logger.exception("/열차구성 오류")
            await interaction.followup.send(
                "열차 구성 처리 중 오류가 발생했습니다.", ephemeral=True
            )

    @bot.tree.command(
        name="열차출발",
        description="우만열차를 출발시키고 본인을 기장으로 등록합니다.",
    )
    @app_commands.describe(
        호차="출발시킬 열차 번호 (1 이상)",
        승객1="같이 출발할 승객 (선택)",
        승객2="같이 출발할 승객 (선택)",
    )
    async def train_start(
        interaction: discord.Interaction,
        호차: int,
        승객1: discord.Member | None = None,
        승객2: discord.Member | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        passengers = [member for member in (승객1, 승객2) if member is not None]
        try:
            async with controller.lock_for(interaction.guild.id):
                controller.repository.start(
                    interaction.guild.id,
                    호차,
                    interaction.user.id,
                    [member.id for member in passengers],
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            await controller.announce_train_arrival(interaction.guild, 호차)
            await interaction.followup.send(
                f"🚆 {호차}호차 출발! 기장으로 등록했습니다."
                + controller.panel_suffix(panel_ok),
                ephemeral=True,
            )
        except TrainStateError as error:
            await interaction.followup.send(
                await controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                ephemeral=True,
            )
        except Exception:
            logger.exception("/열차출발 오류")
            await interaction.followup.send(
                "열차 출발 처리 중 오류가 발생했습니다.", ephemeral=True
            )

    @bot.tree.command(
        name="열차승차",
        description="운행 중인 우만열차의 빈자리에 탑승합니다.",
    )
    @app_commands.describe(호차="탑승할 열차 번호 (1 이상)")
    async def train_board(
        interaction: discord.Interaction,
        호차: int,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with controller.lock_for(interaction.guild.id):
                controller.repository.board(
                    interaction.guild.id,
                    호차,
                    interaction.user.id,
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            await interaction.followup.send(
                f"🎫 {호차}호차에 승차했습니다."
                + controller.panel_suffix(panel_ok),
                ephemeral=True,
            )
        except TrainStateError as error:
            await interaction.followup.send(
                await controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                ephemeral=True,
            )
        except Exception:
            logger.exception("/열차승차 오류")
            await interaction.followup.send(
                "열차 승차 처리 중 오류가 발생했습니다.", ephemeral=True
            )

    @bot.tree.command(
        name="열차하차",
        description="현재 타고 있는 우만열차에서 하차합니다.",
    )
    async def train_leave(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with controller.lock_for(interaction.guild.id):
                car_no = controller.repository.leave(
                    interaction.guild.id,
                    interaction.user.id,
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            await interaction.followup.send(
                f"👋 {car_no}호차에서 하차했습니다."
                + controller.panel_suffix(panel_ok),
                ephemeral=True,
            )
        except TrainStateError as error:
            await interaction.followup.send(
                await controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                ephemeral=True,
            )
        except Exception:
            logger.exception("/열차하차 오류")
            await interaction.followup.send(
                "열차 하차 처리 중 오류가 발생했습니다.", ephemeral=True
            )

    @bot.tree.command(
        name="열차승객추가",
        description="기장 또는 관리자가 열차에 승객을 추가합니다.",
    )
    @app_commands.describe(
        호차="승객을 추가할 열차 번호 (1 이상)",
        승객="추가할 승객",
    )
    async def train_add_passenger(
        interaction: discord.Interaction,
        호차: int,
        승객: discord.Member,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with controller.lock_for(interaction.guild.id):
                controller.repository.add_passenger(
                    interaction.guild.id,
                    호차,
                    interaction.user.id,
                    승객.id,
                    force=can_manage_trains(interaction.user)
                    if isinstance(interaction.user, discord.Member)
                    else False,
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            await interaction.followup.send(
                f"➕ {호차}호차에 {discord.utils.escape_markdown(승객.display_name)}님을 추가했습니다."
                + controller.panel_suffix(panel_ok),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        except TrainStateError as error:
            await interaction.followup.send(
                await controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        except Exception:
            logger.exception("/열차승객추가 오류")
            await interaction.followup.send(
                "승객 추가 처리 중 오류가 발생했습니다.", ephemeral=True
            )

    @bot.tree.command(
        name="열차승객하차",
        description="기장 또는 관리자가 열차에서 승객을 하차시킵니다.",
    )
    @app_commands.describe(
        호차="승객을 하차시킬 열차 번호 (1 이상)",
        승객="하차시킬 승객",
    )
    async def train_remove_passenger(
        interaction: discord.Interaction,
        호차: int,
        승객: discord.Member,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with controller.lock_for(interaction.guild.id):
                controller.repository.remove_passenger(
                    interaction.guild.id,
                    호차,
                    interaction.user.id,
                    승객.id,
                    force=can_manage_trains(interaction.user)
                    if isinstance(interaction.user, discord.Member)
                    else False,
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            await interaction.followup.send(
                f"➖ {호차}호차에서 {discord.utils.escape_markdown(승객.display_name)}님을 하차시켰습니다."
                + controller.panel_suffix(panel_ok),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        except TrainStateError as error:
            await interaction.followup.send(
                await controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        except Exception:
            logger.exception("/열차승객하차 오류")
            await interaction.followup.send(
                "승객 하차 처리 중 오류가 발생했습니다.", ephemeral=True
            )

    @bot.tree.command(
        name="열차종료",
        description="기장 또는 관리자가 우만열차 운행을 종료합니다.",
    )
    @app_commands.describe(호차="종료할 열차 번호 (1 이상)")
    async def train_end(
        interaction: discord.Interaction,
        호차: int,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return

        manager_override = (
            isinstance(interaction.user, discord.Member)
            and can_manage_trains(interaction.user)
        )
        try:
            state = controller.repository.snapshot(interaction.guild.id).get(호차)
            if state is None or state["conductor_id"] is None:
                raise TrainStateError("car_inactive", car_no=호차)
            if not manager_override:
                seat = controller.repository.find_user(
                    interaction.guild.id, interaction.user.id
                )
                if (
                    seat is None
                    or seat.car_no != 호차
                    or seat.role != "conductor"
                ):
                    raise TrainStateError("not_conductor", car_no=호차)
        except TrainStateError as error:
            await interaction.response.send_message(
                await controller.error_text(
                    interaction.guild, error, interaction.user.id
                ),
                ephemeral=True,
            )
            return

        async def do_end(button_interaction):
            try:
                async with controller.lock_for(interaction.guild.id):
                    controller.repository.end(
                        interaction.guild.id,
                        호차,
                        interaction.user.id,
                        force=manager_override,
                    )
                panel_ok = await controller.ensure_panel(interaction.guild)
                return (
                    f"🛑 {호차}호차 운행을 종료했습니다."
                    + controller.panel_suffix(panel_ok)
                )
            except TrainStateError as error:
                return await controller.error_text(
                    interaction.guild, error, interaction.user.id
                )

        await interaction.response.send_message(
            f"{호차}호차 운행을 종료할까요? 기장과 승객 좌석이 모두 비워집니다.",
            view=ConfirmTrainView(
                requester_id=interaction.user.id,
                confirm_label="운행 종료",
                action=do_end,
            ),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @bot.tree.command(
        name="강제폐차",
        description="관리 권한으로 우만열차 좌석을 강제로 초기화합니다.",
    )
    @app_commands.describe(호차="강제로 초기화할 열차 번호 (1 이상)")
    async def train_force_scrap(
        interaction: discord.Interaction,
        호차: int,
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        if not can_manage_trains(interaction.user):
            await interaction.response.send_message(
                "강제폐차 권한이 없습니다.", ephemeral=True
            )
            return

        state = controller.repository.snapshot(interaction.guild.id).get(호차)
        if state is None or state["conductor_id"] is None:
            await interaction.response.send_message(
                f"{호차}호차는 이미 운행 대기 상태입니다.", ephemeral=True
            )
            return

        async def do_scrap(button_interaction):
            member = button_interaction.user
            if not isinstance(member, discord.Member) or not can_manage_trains(member):
                return "강제폐차 권한이 없습니다."
            try:
                async with controller.lock_for(interaction.guild.id):
                    controller.repository.end(
                        interaction.guild.id,
                        호차,
                        force=True,
                    )
                panel_ok = await controller.ensure_panel(interaction.guild)
                return (
                    f"🧹 {호차}호차를 강제폐차했습니다."
                    + controller.panel_suffix(panel_ok)
                )
            except TrainStateError as error:
                return await controller.error_text(
                    interaction.guild, error, interaction.user.id
                )

        await interaction.response.send_message(
            f"{호차}호차를 강제폐차할까요? 현재 좌석 정보가 모두 삭제됩니다.",
            view=ConfirmTrainView(
                requester_id=interaction.user.id,
                confirm_label="강제폐차",
                action=do_scrap,
            ),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
