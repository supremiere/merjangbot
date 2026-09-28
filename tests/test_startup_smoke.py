from types import SimpleNamespace

from bootstrap import create_bot


def test_bot_constructs_and_train_panel_registers(tmp_path):
    settings = SimpleNamespace(
        token="test-token",
        notice_channel_id=1,
        abyss_channel_id=2,
        server_status_channel_id=3,
        db_file=tmp_path / "data.db",
        moblife_api_key="",
        moblife_eab_key="test",
    )

    bot = create_bot(settings)
    bot.database.initialize()

    view = bot.train_controller.panel_view
    assert view.is_persistent()

    bot.add_view(view)
