'''
This program is a Discord bot that uses OpenAI to respond to user messages with a defined personality.
The bot is designed to maintain a consistent personality and respond to user messages in a conversational manner.
The bot can be configured with a custom personality and will generate responses based on the conversation context.
'''
import discord
from discord.ext import commands
from openai import OpenAI
from dotenv import load_dotenv
import json
import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import time
import sqlite3
from typing import Optional, List


DEFAULT_PERSONALITY = (
    "I am Big Floppa (also known as Gosha or Gregory), a caracal cat born in a Moscow Zoo on "
    "December 21, 2017. I live in Russia with my owners Andrey and Elena. I have distinctive "
    "big ears with tufts and an expressive face that has made me an internet sensation."
)

# Simple HTTP handler to keep Cloud Run happy
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    '''
    A simple HTTP request handler that responds with a 200 OK status code and a text message.
    This just to keep the Cloud Run container alive and healthy.
    '''
    def do_GET(self):
        '''
        Handle GET requests by responding with a 200 OK status code and a text message.
        '''
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Discord bot is running')

    def log_message(self, format, *args):
        # Suppress HTTP logs to keep the console clean
        return

def run_http_server():
    """Start a simple HTTP server to keep the container healthy."""
    port = int(os.environ.get('PORT', 8080))
    server_address = ('', port)
    httpd = HTTPServer(server_address, SimpleHTTPRequestHandler)
    logging.info(f'Starting HTTP server on port {port}')
    httpd.serve_forever()


class BotDatabase:
    """SQLite-backed storage for users, interactions, and conversation history."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    discriminator TEXT,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    mention_count INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    user_id INTEGER,
                    username TEXT,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    message_id INTEGER,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel_id, ts);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_ts ON messages(user_id, ts);")

    def upsert_user(
        self,
        user_id: int,
        username: str,
        discriminator: Optional[str],
        is_bot: bool,
        now_ts: int,
        increment_message: bool,
        increment_mention: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, discriminator, is_bot, first_seen_at, last_seen_at, message_count, mention_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    discriminator=excluded.discriminator,
                    is_bot=excluded.is_bot,
                    last_seen_at=excluded.last_seen_at,
                    message_count=users.message_count + ?,
                    mention_count=users.mention_count + ?;
                """,
                (
                    user_id,
                    username,
                    discriminator,
                    1 if is_bot else 0,
                    now_ts,
                    now_ts,
                    1 if increment_message else 0,
                    1 if increment_mention else 0,
                    1 if increment_message else 0,
                    1 if increment_mention else 0,
                ),
            )

    def log_message(
        self,
        ts: int,
        user_id: Optional[int],
        username: str,
        guild_id: Optional[int],
        channel_id: Optional[int],
        message_id: Optional[int],
        is_bot: bool,
        content: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (ts, user_id, username, guild_id, channel_id, message_id, is_bot, content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    ts,
                    user_id,
                    username,
                    guild_id,
                    channel_id,
                    message_id,
                    1 if is_bot else 0,
                    content,
                ),
            )

    def get_recent_messages_for_channel(self, channel_id: int, limit: int = 100) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT username, content, is_bot
                FROM messages
                WHERE channel_id = ?
                ORDER BY ts DESC
                LIMIT ?;
                """,
                (channel_id, limit),
            ).fetchall()

        # Return oldest -> newest
        lines: List[str] = []
        for row in reversed(rows):
            prefix = "Bot" if row["is_bot"] else row["username"]
            lines.append(f"{prefix}: {row['content']}")
        return lines

class DiscordChatBot:
    """A Discord bot that uses OpenAI to respond to user messages with a defined personality."""
    
    def __init__(self):
        """Initialize the Discord bot with configuration, memory, and client setup."""
        # Load env vars from .env if present
        load_dotenv()

        # Setup logging
        self._setup_logging()
        
        # First try to load from environment variables
        self.openai_api_key = os.environ.get("OPENAI_API_KEY")
        self.discord_bot_token = os.environ.get("DISCORD_BOT_TOKEN")
        
        # Only try to load from files if environment variables aren't set
        if not self.openai_api_key or not self.discord_bot_token:
            try:
                # Load OpenAI API key
                openai_key_file = "OPENAI_API_KEY.json"
                with open(openai_key_file, 'r') as file:
                    openai_config = json.load(file)
                    self.openai_api_key = openai_config.get("key")
                    logging.info("OpenAI API key loaded successfully from file")
                    
                # Load Discord bot token
                discord_token_file = "DISCORD_BOT_TOKEN.json"
                with open(discord_token_file, 'r') as file:
                    discord_config = json.load(file)
                    self.discord_bot_token = discord_config.get("token")
                    logging.info("Discord bot token loaded successfully from file")
            except Exception as e:
                logging.error(f"Failed to load configuration files: {e}")
                exit(1)
        else:
            logging.info("API keys loaded successfully from environment variables")
        
        # Initialize OpenAI client
        self.openai_client = OpenAI(api_key=self.openai_api_key)
        
        # Load custom personality from file or use default
        try:
            # First try to read from file
            filepath = 'discord_bot_personality.txt'
            with open(filepath, 'r', encoding='utf-8') as file:
                self.bot_personality = file.read()
                logging.info("Bot personality loaded successfully from file")
        except Exception:
            # If file isn't available, use a default personality
            self.bot_personality = os.environ.get("BOT_PERSONALITY", DEFAULT_PERSONALITY)
            logging.info("Using default bot personality")
        
        # Setup Discord bot
        intents = discord.Intents.default()
        intents.messages = True
        intents.guilds = True
        intents.message_content = True
        self.bot = commands.Bot(command_prefix='$', intents=intents)
        
        # Message storage
        self.global_message_memory = []

        # SQL storage
        self.db = BotDatabase(os.environ.get("DB_PATH", "bot.db"))
        
        # Register event handlers
        self._register_events()
    
    def _setup_logging(self):
        """Configure logging for the application."""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("discord_bot.log")
            ]
        )
    
    def _register_events(self):
        """Register Discord event handlers."""
        @self.bot.event
        async def on_ready():
            logging.info(f'{self.bot.user.name} has connected to Discord!')
        
        @self.bot.event
        async def on_message(message):
            await self._handle_message(message)
            await self.bot.process_commands(message)
    
    async def _handle_message(self, message):
        """Process incoming messages and generate responses when mentioned."""
        if not self.bot.user:
            return
        if message.author == self.bot.user:
            return  # Ignore messages from the bot itself

        now_ts = int(time.time())
        guild_id = message.guild.id if message.guild else None
        channel_id = message.channel.id if message.channel else None
        mentioned = self.bot.user.mentioned_in(message)

        # Track user interaction + message log in SQL
        try:
            self.db.upsert_user(
                user_id=message.author.id,
                username=message.author.name,
                discriminator=getattr(message.author, "discriminator", None),
                is_bot=bool(getattr(message.author, "bot", False)),
                now_ts=now_ts,
                increment_message=True,
                increment_mention=mentioned,
            )
            self.db.log_message(
                ts=now_ts,
                user_id=message.author.id,
                username=message.author.name,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message.id,
                is_bot=False,
                content=message.content or "",
            )
        except Exception as e:
            logging.error(f"Failed to log incoming message to DB: {e}")
        
        # Append new message to the global memory
        self.global_message_memory.append(f"{message.author.name}: {message.content}")
        
        # Ensure the memory does not exceed 100 messages
        if len(self.global_message_memory) > 100:
            self.global_message_memory.pop(0)
        
        if mentioned:
            await self._generate_response(message)
    
    async def _generate_response(self, message):
        """Generate and send AI-powered response to a user message."""
        # Create context (prefer SQL history for this channel)
        context_lines: List[str] = []
        try:
            if message.channel:
                context_lines = self.db.get_recent_messages_for_channel(message.channel.id, limit=100)
        except Exception as e:
            logging.error(f"Failed to fetch context from DB: {e}")

        if not context_lines:
            context_lines = self.global_message_memory[-100:]

        context = "\n".join(context_lines)
        clean_message = message.content.replace(f'<@{self.bot.user.id}>', '').replace(f'<@!{self.bot.user.id}>', '').strip()
        
        # Build the prompt based on conversation state
        prompt = self._build_prompt(message.author.name, clean_message, context)
        
        try:
            # Call OpenAI API using the new client-based approach
            response = self.openai_client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": clean_message},
                ],
                max_tokens=500,
            )
            response_text = response.choices[0].message.content.strip()
            await message.channel.send(response_text)
            
            # Add bot's response to memory
            self.global_message_memory.append(f"Bot: {response_text}")

            # Log bot response to SQL
            try:
                now_ts = int(time.time())
                guild_id = message.guild.id if message.guild else None
                channel_id = message.channel.id if message.channel else None
                self.db.upsert_user(
                    user_id=self.bot.user.id,
                    username=self.bot.user.name,
                    discriminator=getattr(self.bot.user, "discriminator", None),
                    is_bot=True,
                    now_ts=now_ts,
                    increment_message=True,
                    increment_mention=False,
                )
                self.db.log_message(
                    ts=now_ts,
                    user_id=self.bot.user.id,
                    username=self.bot.user.name,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=None,
                    is_bot=True,
                    content=response_text,
                )
            except Exception as e:
                logging.error(f"Failed to log bot response to DB: {e}")
        except Exception as e:
            error_message = "Sorry, there was an error processing your request."
            logging.error(f"Failed to call OpenAI API: {e}")
            await message.channel.send(error_message)
            self.global_message_memory.append(f"Bot: {error_message}")
    
    def _build_prompt(self, author_name, message_content, context):
        """Build prompt for the AI based on conversation history and length."""
        memory_length = len(self.global_message_memory)
        logging.info(f"Conversation count: {memory_length}")
        
        if memory_length % 5 == 0 or memory_length == 1:
            # Refresh personality every 5 messages or on first message
            prompt = (
                f"Previous conversation context:\n{context}\n"
                f"You are: {self.bot_personality}\n"
                f"Respond to {author_name} in first person. Stay in character."
            )
            logging.info(f"Personality refreshed, talking to: {author_name}")
        elif memory_length > 40:
            # Get annoyed after 40 messages but still maintain personality cuz its funny
            prompt = (
                f"Previous conversation context:\n{context}\n"
                f"You are: {self.bot_personality}\n"
                f"You will never admit to being an AI. You are currently annoyed. "
                f"Respond to {author_name} in first person with a slightly irritated tone."
            )
            logging.info(f"Now annoyed, talking to: {author_name}")
        else:
            # Standard response with personality
            prompt = (
                f"Previous conversation context:\n{context}\n"
                f"You are: {self.bot_personality}\n"
                f"You will never admit to being an AI. Respond to {author_name} in first person."
            )
        
        return prompt
    
    def run(self):
        """Start the Discord bot."""
        logging.info("Starting Discord bot...")
        self.bot.run(self.discord_bot_token)


if __name__ == "__main__":
    logging.info("Starting application...")
    
    try:
        # Start HTTP server in a separate thread
        http_thread = threading.Thread(target=run_http_server, daemon=True)
        http_thread.start()
        logging.info("HTTP server thread started")
        
        # Give HTTP server time to start (important for Cloud Run health checks)
        time.sleep(2)
        
        # Start the Discord bot
        logging.info("Starting Discord bot...")
        bot = DiscordChatBot()
        bot.run()
    except Exception as e:
        logging.error(f"Error in main thread: {e}")