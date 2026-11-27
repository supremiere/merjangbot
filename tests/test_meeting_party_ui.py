import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.components import _component_factory
from discord.ui.view import ViewStore

from discord_bot.commands import meeting_party as ui


def snapshot(**overrides):
    values = dict(id=9, guild_id=10, leader_id=30, members=[30], watchers=[42], banned=[], capacity=2,
                  destination="토요일 레이드", departure=None, conditions="", description="", strategy="내부 공지",
                  roster={30: {"character": "비공개캐릭터"}}, closed=False, activity="planned",
                  spectating=True, phase="ready", room_id=100, recruit_channel_id=101, recruit_id=102)
    return {**values, **overrides}


def interaction(user_id=42):
    user = SimpleNamespace(id=user_id, roles=[], guild=SimpleNamespace(owner_id=1), guild_permissions=None)
    response = SimpleNamespace(is_done=Mock(return_value=False), send_modal=AsyncMock(),
                               send_message=AsyncMock(), edit_message=AsyncMock())
    def acknowledge(**kwargs):
        response.is_done.return_value = True
    response.defer = AsyncMock(side_effect=acknowledge)
    return SimpleNamespace(
        guild_id=10, user=user, application_id=1, data={},
        message=SimpleNamespace(author=SimpleNamespace(id=1), webhook_id=None, flags=discord.MessageFlags()),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()), client=SimpleNamespace(user=SimpleNamespace(id=1)),
    )


class MeetingPartyUITest(unittest.IsolatedAsyncioTestCase):
    async def test_creation_primary_choice_opens_spectating_without_approval(self):
        for options, expected in (({}, True), ({"spectating": False}, False)):
            with self.subTest(expected=expected):
                svc = SimpleNamespace(store=SimpleNamespace(settings=lambda gid: {"enabled": True}),
                                      create=AsyncMock(return_value=snapshot()))
                panel, submit = interaction(), interaction()
                await ui.open_create(panel, svc, **options)
                form = panel.response.send_modal.await_args.args[0]
                self.assertIn("관전 허용" if expected else "관전 닫힘", form.title)
                self.assertEqual(5, len(form.children))
                inputs = dict(destination="레이드", capacity="3", departure="2026-12-05 22:00", conditions="", description="")
                components = form.to_components()
                for block, key in zip(components, inputs):
                    field = block["component"]
                    field["value"] = inputs[key]
                await form._scheduled_task(submit, components, {})
                kwargs = svc.create.await_args.kwargs
                self.assertEqual(expected, kwargs["spectating"])
                self.assertEqual(3, kwargs["capacity"])
                self.assertEqual("2026-12-05 22:00", ui.format_departure(kwargs["departure"]))

    async def test_public_and_ad_omit_rosters_notices_even_when_spectating_open(self):
        p = snapshot()
        for surface in ("public", "ad"):
            payload = str(ui.party_embed(p, surface).to_dict())
            self.assertNotIn("내부 공지", payload)
            self.assertNotIn("비공개캐릭터", payload)
        self.assertIn("내부 공지", str(ui.party_embed(p, "room").to_dict()))

    async def test_full_party_leaves_watch_button_open_even_when_join_is_disabled(self):
        for p in (snapshot(capacity=1), snapshot(activity="ended"), snapshot(closed=True)):
            v = ui.party_view(p, "ad")
            button = {item.action: item.item for item in v.children if isinstance(item, ui.MeetingButton)}
            self.assertTrue(button["join"].disabled)
            self.assertFalse(button["watch"].disabled)
            self.assertEqual("https://discord.com/channels/10/101/102", v.children[-1].url)
        for p in (snapshot(spectating=False), snapshot(phase="disbanded")):
            self.assertTrue(next(b.item for b in ui.party_view(p).children if b.action == "watch").disabled)

    async def test_promo_button_dispatch_and_private_channel_link_after_success(self):
        i = interaction()
        svc = SimpleNamespace(act=AsyncMock(return_value=snapshot()))
        i.client.meetings = svc
        join = ui.party_view(snapshot(), "ad").children[0]
        restored = await ui.MeetingButton.from_custom_id(i, join.item, join.template.fullmatch(join.custom_id))
        await restored.callback(i)
        svc.act.assert_awaited_once_with(i, 9, "join")
        args = i.followup.send.await_args
        self.assertIn("<#100>", args.args[0])
        self.assertTrue(args.kwargs["ephemeral"])
        self.assertFalse(args.kwargs["allowed_mentions"].everyone)

    async def test_watcher_leaves_without_using_member_leave_or_exposing_link(self):
        p = snapshot()
        svc = SimpleNamespace(store=SimpleNamespace(get=lambda gid, pid: p), act=AsyncMock(return_value=p))
        i = interaction()
        await ui.route(i, svc, p["id"], "leave")
        svc.act.assert_awaited_once_with(i, 9, "unwatch")
        self.assertNotIn("<#100>", i.followup.send.await_args.args[0])

    async def test_ephemeral_detail_and_persistent_public_buttons_through_discord_dispatch(self):
        svc = SimpleNamespace(store=SimpleNamespace(get=lambda gid, pid: snapshot(),
                                                   settings=lambda gid: {"enabled": True}),
                              act=AsyncMock(return_value=snapshot()))
        detail = interaction()
        await ui.open_detail(detail, svc, 9)
        v = detail.response.send_message.await_args.kwargs["view"]
        # Exercise discord.py's component conversion and DynamicItem lookup after a fresh registration.
        for ephemeral, webhook, author, action, allowed in [
            (True, 1, 990, "watch", True), (False, None, 1, "join", True),
            (True, 992, 1, "watch", False), (False, None, 990, "join", False),
        ]:
            with self.subTest(ephemeral=ephemeral, webhook=webhook):
                i = interaction()
                i.client.meetings = svc
                i.message.flags.ephemeral = ephemeral
                i.message.webhook_id, i.message.author.id = webhook, author
                i.message.components = [_component_factory(c) for c in v.to_components()]
                item = next(b for b in v.children if b.action == action)
                i.data = {"component_type": 2, "custom_id": item.custom_id}
                store = ViewStore(Mock())
                store.add_dynamic_items(ui.MeetingButton)
                await store.schedule_dynamic_item_call(2, ui.MeetingButton, i, item.custom_id,
                                                       item.template.fullmatch(item.custom_id))
                if allowed:
                    svc.act.assert_awaited_once_with(i, 9, action)
                    self.assertIn("<#100>", i.followup.send.await_args.args[0])
                else:
                    svc.act.assert_not_awaited()
                svc.act.reset_mock()

    async def test_unchanged_fields_in_old_modal_do_not_overwrite_another_edit(self):
        svc = SimpleNamespace(act=AsyncMock())
        i, submit = interaction(30), interaction(30)
        p = snapshot()
        await ui.party_form(i, svc, party=p)
        form = i.response.send_modal.await_args.args[0]
        await form.on_save(submit, dict(destination=p["destination"], capacity="4", departure="",
                                        description="", conditions=""))
        svc.act.assert_awaited_once_with(submit, 9, "edit", capacity=4)

    def test_departure_through_entire_accepted_kst_range(self):
        for text in ("1970-01-01 09:00", "3020-01-01 00:00", "9999-12-31 23:59"):
            self.assertEqual(text, ui.format_departure(ui.parse_departure(text)))
        self.assertIn("1970-01-01", ui.party_embed(snapshot(departure=0)).fields[2].value)

    async def test_kick_picker_distinguishes_watchers_and_excludes_leader(self):
        p = snapshot(members=[30, 31])
        i = interaction(30)
        i.guild = SimpleNamespace(get_member=lambda uid: None)
        svc = SimpleNamespace(require_manager=Mock(return_value=p))
        await ui.select_person(i, svc, 9, "kick")
        opts = i.response.send_message.await_args.kwargs["view"].children[0].options
        self.assertEqual([("31", "참가자"), ("42", "관전자")], [(opt.value, opt.description) for opt in opts])

    async def test_maximum_emoji_notice_can_be_read_in_full_on_all_pages(self):
        p = snapshot(conditions="😀"*1000, description="😀"*1000, strategy="🌲"*3500)
        e = ui.party_embed(p, "room")
        self.assertTrue(all(len(f.value.encode("utf-16-le")) <= 2048 for f in e.fields))
        i, j = interaction(), interaction()
        svc = SimpleNamespace(require_access=Mock(return_value=p))
        await ui.show_notice(i, svc, p)
        first = i.response.send_message.await_args.kwargs
        await first["view"].children[0].callback(j)
        second = j.response.send_message.await_args.kwargs
        self.assertEqual(p["strategy"], first["embed"].description + second["embed"].description)
        svc.require_access.assert_called_once_with(j, 9)

    async def test_private_view_requires_current_authority_after_role_changes(self):
        p = snapshot()
        owner, stale = interaction(30), interaction(30)
        svc = SimpleNamespace(require_manager=Mock(side_effect=[p, ValueError("권한 없음")]), act=AsyncMock())
        await ui.open_manage(owner, svc, p["id"])
        view = owner.response.send_message.await_args.kwargs["view"]
        self.assertFalse(await view.interaction_check(interaction(42)))
        with self.assertRaises(ValueError):
            await next(b for b in view.children if b.label == "관전 닫기").callback(stale)
        svc.act.assert_not_awaited()

    async def test_dissolving_requires_explicit_confirmation_with_deletion_notice(self):
        p = snapshot()
        owner, chooser, confirm = interaction(30), interaction(30), interaction(30)
        svc = SimpleNamespace(require_manager=Mock(return_value=p), act=AsyncMock(return_value=p))
        await ui.open_manage(owner, svc, p["id"])
        v = owner.response.send_message.await_args.kwargs["view"]
        await next(b for b in v.children if b.label == "파티 해산").callback(chooser)
        message = chooser.response.send_message.await_args
        self.assertIn("채팅 기록이 삭제", message.args[0])
        svc.act.assert_not_awaited()
        await message.kwargs["view"].children[0].callback(confirm)
        svc.act.assert_awaited_once_with(confirm, 9, "disband")
