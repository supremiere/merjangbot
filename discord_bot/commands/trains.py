# /열차* 명령과 "우만열차좌석도" 현황판을 관리합니다.
import asyncio
import logging

import discord
from discord import app_commands

from storage.trains import TRAIN_CAPACITY, TRAIN_CARS, TrainRepository, TrainStateError

logger = logging.getLogger(__name__)

CHANNEL_NAME = "우만열차좌석도"
PANEL_TITLE = "🚆 **우만열차 좌석도**"
FORCE_SCRAP_ROLES = {"자발적 봉사자", "봉사하는 노예", "머장", "관리자"}
NO_MENTIONS = discord.AllowedMentions.none()
CAR_CHOICES = [
    app_commands.Choice(name=f"{car_no}호", value=car_no)
    for car_no in TRAIN_CARS
]


def can_force_scrap(member):
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    if member.id == guild.owner_id:
        return True
    permissions = getattr(member, "guild_permissions", None)
    if permissions is not None and permissions.administrator:
        return True
    return any(getattr(role, "name", "") in FORCE_SCRAP_ROLES for role in member.roles)


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
        lines = [PANEL_TITLE, ""]
        for car_no in TRAIN_CARS:
            state = snapshot[car_no]
            conductor_id = state["conductor_id"]
            passengers = state["passenger_ids"]
            if conductor_id is None:
                lines.append(f"**{car_no}호차**  ⚪ 운행 대기 · `0/{TRAIN_CAPACITY}`")
                lines.append("")
                continue

            count = 1 + len(passengers)
            icon = "🔴" if count >= TRAIN_CAPACITY else "🟢"
            status = "만석" if count >= TRAIN_CAPACITY else "운행중"
            conductor_name = await self.display_name(guild, conductor_id)
            passenger_names = [
                await self.display_name(guild, user_id)
                for user_id in passengers
            ]
            passenger_text = ", ".join(passenger_names) if passenger_names else "-"
            empty = TRAIN_CAPACITY - count
            empty_text = "없음" if empty <= 0 else f"{empty}석"

            lines.extend(
                [
                    f"**{car_no}호차**  {icon} {status} · `{count}/{TRAIN_CAPACITY}`",
                    f"기장 : {conductor_name}",
                    f"승객 : {passenger_text}",
                    f"빈자리 : {empty_text}",
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
                        and candidate.content.startswith(PANEL_TITLE)
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
                    allowed_mentions=NO_MENTIONS,
                )
            else:
                await message.edit(
                    content=content,
                    allowed_mentions=NO_MENTIONS,
                )
            self.repository.save_panel_location(guild.id, channel.id, message.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("%s 현황판 전송/수정 실패", CHANNEL_NAME)
            return False

        if not message.pinned:
            try:
                await message.pin(reason="우만열차 좌석 현황판")
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("%s 현황판을 고정하지 못했습니다.", CHANNEL_NAME)
        return True

    async def ensure_all_panels(self):
        for guild in self.bot.guilds:
            try:
                await self.ensure_panel(guild)
            except Exception:
                logger.exception("%s 서버 열차 현황판 초기화 오류", guild.name)

    async def error_text(self, guild, error, requester_id=None):
        if error.code == "invalid_car":
            return "1호, 2호, 3호차만 사용할 수 있습니다."
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
            return "기장은 /열차종료를 이용해 주세요."
        if error.code == "not_conductor":
            return f"이 명령은 {error.car_no}호차 기장만 사용할 수 있습니다."
        return "열차 상태를 처리할 수 없습니다."

    @staticmethod
    def panel_suffix(panel_ok):
        return "" if panel_ok else "\n⚠️ 좌석은 저장됐지만 현황판을 갱신하지 못했습니다."


def register(bot):
    controller = TrainController(bot)
    bot.train_controller = controller

    @bot.tree.command(
        name="열차출발",
        description="우만열차를 출발시키고 본인을 기장으로 등록합니다.",
    )
    @app_commands.describe(
        호차="출발시킬 열차",
        승객1="같이 출발할 승객 (선택)",
        승객2="같이 출발할 승객 (선택)",
    )
    @app_commands.choices(호차=CAR_CHOICES)
    async def train_start(
        interaction: discord.Interaction,
        호차: app_commands.Choice[int],
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
                    호차.value,
                    interaction.user.id,
                    [member.id for member in passengers],
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            await interaction.followup.send(
                f"🚆 {호차.value}호차 출발! 기장으로 등록했습니다."
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
    @app_commands.describe(호차="탑승할 열차")
    @app_commands.choices(호차=CAR_CHOICES)
    async def train_board(
        interaction: discord.Interaction,
        호차: app_commands.Choice[int],
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
                    호차.value,
                    interaction.user.id,
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            await interaction.followup.send(
                f"🎫 {호차.value}호차에 승차했습니다."
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
        description="기장이 자신의 열차에 승객을 추가합니다.",
    )
    @app_commands.describe(
        호차="승객을 추가할 열차",
        승객="추가할 승객",
    )
    @app_commands.choices(호차=CAR_CHOICES)
    async def train_add_passenger(
        interaction: discord.Interaction,
        호차: app_commands.Choice[int],
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
                    호차.value,
                    interaction.user.id,
                    승객.id,
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            await interaction.followup.send(
                f"➕ {호차.value}호차에 {discord.utils.escape_markdown(승객.display_name)}님을 추가했습니다."
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
        description="기장이 자신의 열차에서 승객을 하차시킵니다.",
    )
    @app_commands.describe(
        호차="승객을 하차시킬 열차",
        승객="하차시킬 승객",
    )
    @app_commands.choices(호차=CAR_CHOICES)
    async def train_remove_passenger(
        interaction: discord.Interaction,
        호차: app_commands.Choice[int],
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
                    호차.value,
                    interaction.user.id,
                    승객.id,
                )
            panel_ok = await controller.ensure_panel(interaction.guild)
            await interaction.followup.send(
                f"➖ {호차.value}호차에서 {discord.utils.escape_markdown(승객.display_name)}님을 하차시켰습니다."
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
        description="기장이 자신의 우만열차 운행을 종료합니다.",
    )
    @app_commands.describe(호차="종료할 열차")
    @app_commands.choices(호차=CAR_CHOICES)
    async def train_end(
        interaction: discord.Interaction,
        호차: app_commands.Choice[int],
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return

        try:
            seat = controller.repository.find_user(
                interaction.guild.id, interaction.user.id
            )
            if seat is None or seat.car_no != 호차.value or seat.role != "conductor":
                raise TrainStateError("not_conductor", car_no=호차.value)
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
                        호차.value,
                        interaction.user.id,
                    )
                panel_ok = await controller.ensure_panel(interaction.guild)
                return (
                    f"🛑 {호차.value}호차 운행을 종료했습니다."
                    + controller.panel_suffix(panel_ok)
                )
            except TrainStateError as error:
                return await controller.error_text(
                    interaction.guild, error, interaction.user.id
                )

        await interaction.response.send_message(
            f"{호차.value}호차 운행을 종료할까요? 기장과 승객 좌석이 모두 비워집니다.",
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
    @app_commands.describe(호차="강제로 초기화할 열차")
    @app_commands.choices(호차=CAR_CHOICES)
    async def train_force_scrap(
        interaction: discord.Interaction,
        호차: app_commands.Choice[int],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        if not can_force_scrap(interaction.user):
            await interaction.response.send_message(
                "강제폐차 권한이 없습니다.", ephemeral=True
            )
            return

        state = controller.repository.snapshot(interaction.guild.id)[호차.value]
        if state["conductor_id"] is None:
            await interaction.response.send_message(
                f"{호차.value}호차는 이미 운행 대기 상태입니다.", ephemeral=True
            )
            return

        async def do_scrap(button_interaction):
            member = button_interaction.user
            if not isinstance(member, discord.Member) or not can_force_scrap(member):
                return "강제폐차 권한이 없습니다."
            try:
                async with controller.lock_for(interaction.guild.id):
                    controller.repository.end(
                        interaction.guild.id,
                        호차.value,
                        force=True,
                    )
                panel_ok = await controller.ensure_panel(interaction.guild)
                return (
                    f"🧹 {호차.value}호차를 강제폐차했습니다."
                    + controller.panel_suffix(panel_ok)
                )
            except TrainStateError as error:
                return await controller.error_text(
                    interaction.guild, error, interaction.user.id
                )

        await interaction.response.send_message(
            f"{호차.value}호차를 강제폐차할까요? 현재 좌석 정보가 모두 삭제됩니다.",
            view=ConfirmTrainView(
                requester_id=interaction.user.id,
                confirm_label="강제폐차",
                action=do_scrap,
            ),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
