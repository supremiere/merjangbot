# 사이트별로 분리된 머장봇의 Railway 배포 방법을 안내합니다.
파일 구조와 기능은 README.md를 참고하세요.

배포할 파일:
- main.py, app_core.py, config.py, bootstrap.py
- sites/, storage/, discord_bot/ 폴더 전체 (__init__.py 포함)
- requirements.txt, mise.toml, .gitignore, .env.example
- README.md, README_DEPLOY.txt

GitHub에 올리지 않을 파일:
- .env, 운영 data.db, .venv/, __pycache__/

Railway Variables:
DISCORD_TOKEN
NOTICE_CHANNEL_ID
ABYSS_CHANNEL_ID
SERVER_STATUS_CHANNEL_ID
MOBLIFE_API_KEY (시세 조회용)
DB_FILE=/data/data.db
TZ=Asia/Seoul

설치 명령: pip install -r requirements.txt
시작 명령: python main.py
Railway Volume Mount Path: /data

기존 data.db를 그대로 사용할 수 있습니다. 룬 통계 캐시도 기존 테이블 형식을 유지합니다.
MOBLIFE_PROXY_BASE는 선택 설정이며 빈 값이면 모비라이프에 직접 연결합니다.
룬 통계는 에린 데이터에서 직접 조회하며 별도 API 키가 필요 없습니다.
BOT_CHANNEL_ID는 원래 기능에서 사용되지 않던 설정으로 더 이상 필요하지 않습니다.
