머장봇 Railway 배포용 파일

GitHub에 올릴 파일:
- main.py
- requirements.txt
- .gitignore
- mise.toml
- .env.example

절대로 GitHub에 올리지 말 것:
- .env
- data.db
- .venv 폴더

Railway Variables:
DISCORD_TOKEN
NOTICE_CHANNEL_ID
ABYSS_CHANNEL_ID
BOT_CHANNEL_ID
MOBLIFE_API_KEY
DB_FILE=/data/data.db
TZ=Asia/Seoul

Railway Start Command:
python main.py

Railway Volume Mount Path:
/data
