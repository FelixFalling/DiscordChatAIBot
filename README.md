# DiscordChatAIBot

Discord chatbot in Python using Discord API + OpenAI, with SQLite (SQL) logging for user interactions and conversation history.

## Environment variables

- `DISCORD_BOT_TOKEN` (required)
- `OPENAI_API_KEY` (required)
- `OPENAI_MODEL` (optional, default: `gpt-4o-mini`)
- `BOT_PERSONALITY` (optional if `discord_bot_personality.txt` exists)
- `DB_PATH` (optional, default: `bot.db`)
- `PORT` (optional for the health server, default: `8080`)

## What gets stored (SQLite)

The bot creates `DB_PATH` and stores:
- `users`: basic user info + `message_count` + `mention_count`
- `messages`: every user message and every bot response (with guild/channel IDs and timestamps)

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export DISCORD_BOT_TOKEN="..."
export OPENAI_API_KEY="..."
python app.py
```

## Run with Docker

```bash
docker build -t discordchatai .
docker run --rm -e DISCORD_BOT_TOKEN -e OPENAI_API_KEY -e OPENAI_MODEL -e DB_PATH -p 8080:8080 discordchatai
```

Notes:
- For `message.content` to be available, enable the **Message Content Intent** for your bot in the Discord Developer Portal.
