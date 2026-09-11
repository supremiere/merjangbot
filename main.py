# 설정을 읽고 머장봇을 실행하는 유일한 진입점입니다.
import logging

from bootstrap import create_bot
from config import Settings


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings.from_env()
    bot = create_bot(settings)
    bot.run(settings.token)


if __name__ == "__main__":
    main()
