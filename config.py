# 환경변수를 가장 먼저 읽고 채널·API·DB 설정을 검증합니다.
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_PROXY = "https://moblife-proxy.ninemailz.workers.dev"
DEFAULT_EAB_KEY = "8nvov88uc5k4o4g6apax04783thjo11l"


@dataclass(frozen=True)
class Settings:
    token: str
    notice_channel_id: int
    abyss_channel_id: int
    server_status_channel_id: int
    db_file: Path
    moblife_api_key: str = ""
    moblife_proxy_base: str = DEFAULT_PROXY
    moblife_eab_key: str = DEFAULT_EAB_KEY

    @classmethod
    def from_env(cls, env_file=None):
        load_dotenv(env_file if env_file is not None else PROJECT_DIR / ".env")
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_TOKEN을 설정해주세요.")

        def channel_id(name, default=""):
            value = os.getenv(name, default).strip()
            try:
                result = int(value)
                if result <= 0:
                    raise ValueError
                return result
            except ValueError:
                raise ValueError(f"{name}에는 올바른 채널 ID가 필요합니다.") from None

        db_file = Path(os.getenv("DB_FILE", "data.db")).expanduser()
        if not db_file.is_absolute():
            db_file = PROJECT_DIR / db_file
        return cls(
            token=token,
            notice_channel_id=channel_id("NOTICE_CHANNEL_ID"),
            abyss_channel_id=channel_id("ABYSS_CHANNEL_ID"),
            server_status_channel_id=channel_id(
                "SERVER_STATUS_CHANNEL_ID", "1547464310043844639"
            ),
            db_file=db_file,
            moblife_api_key=os.getenv("MOBLIFE_API_KEY", "").strip(),
            moblife_proxy_base=os.getenv("MOBLIFE_PROXY_BASE", DEFAULT_PROXY)
            .strip()
            .rstrip("/"),
            moblife_eab_key=os.getenv("MOBLIFE_EAB_KEY", DEFAULT_EAB_KEY),
        )
