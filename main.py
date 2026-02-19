import os
import asyncio
import time
import logging
import base64
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

logging.basicConfig(level=logging.INFO)
bootlog = logging.getLogger("boot")

print("BOOT: main.py 실행됨", flush=True)

IDLE_TIMEOUT_SEC = 5 * 60
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# ==============================
# yt-dlp 설정
# ==============================
YTDLP_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "js_runtimes": "deno",

    # 요청 완화 + 재시도
    "sleep_requests": 1,
    "sleep_interval": 1,
    "max_sleep_interval": 3,
    "retries": 3,
    "fragment_retries": 3,
}

# ==============================
# 🔥 쿠키 복원 로직 (Railway Secret 사용)
# ==============================
def prepare_cookiefile() -> Optional[str]:
    b64 = os.getenv("YOUTUBE_COOKIES_B64")
    if not b64:
        print("쿠키 환경변수 없음", flush=True)
        return None

    try:
        path = "/tmp/cookies.txt"
        data = base64.b64decode(b64.encode("utf-8"))
        with open(path, "wb") as f:
            f.write(data)

        print("쿠키 적용 성공: /tmp/cookies.txt", flush=True)
        return path
    except Exception as e:
        print("쿠키 복원 실패:", repr(e), flush=True)
        return None


COOKIEFILE = prepare_cookiefile()
if COOKIEFILE:
    YTDLP_OPTIONS["cookiefile"] = COOKIEFILE

# ==============================
# FFmpeg 설정
# ==============================
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -ar 48000 -ac 2",
}

@dataclass
class Track:
    title: str
    url: str
    stream_url: str
    requester: int

class GuildMusic:
    def __init__(self):
        self.queue: Deque[Track] = deque()
        self.now_playing: Optional[Track] = None
        self.lock = asyncio.Lock()
        self.next_event = asyncio.Event()
        self.player_task: Optional[asyncio.Task] = None
        self.last_command_ts: float = time.monotonic()
        self.idle_task: Optional[asyncio.Task] = None
        self.last_text_channel_id: Optional[int] = None

music_data: Dict[int, GuildMusic] = {}

def get_music(guild_id: int) -> GuildMusic:
    if guild_id not in music_data:
        music_data[guild_id] = GuildMusic()
    return music_data[guild_id]

def touch_command(music: GuildMusic):
    music.last_command_ts = time.monotonic()

def extract_info(query: str) -> Track:
    with yt_dlp.YoutubeDL(YTDLP_OPTIONS) as ydl:
        info = ydl.extract_info(query, download=False)

    if "entries" in info and info["entries"]:
        info = info["entries"][0]

    return Track(
        title=info.get("title", "Unknown"),
        url=info.get("webpage_url", query),
        stream_url=info.get("url"),
        requester=0,
    )

async def extract_with_retry(query: str) -> Track:
    try:
        return await asyncio.to_thread(extract_info, query)
    except Exception:
        await asyncio.sleep(2)
        return await asyncio.to_thread(extract_info, query)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.tree.command(name="재생")
@app_commands.describe(제목="URL 또는 제목 입력")
async def play(interaction: discord.Interaction, 제목: str):
    await interaction.response.defer(thinking=True)

    try:
        if not interaction.user.voice:
            await interaction.followup.send("음성채널 먼저 들어가.")
            return

        vc = interaction.guild.voice_client
        if not vc:
            vc = await interaction.user.voice.channel.connect()

        music = get_music(interaction.guild.id)
        touch_command(music)
        music.last_text_channel_id = interaction.channel_id

        track = await extract_with_retry(제목)
        track.requester = interaction.user.id

        source = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTIONS)

        vc.play(source)

        await interaction.followup.send(
            f"▶️ 현재 재생중인 곡 : {track.title} (요청자: <@{track.requester}>)\n{track.url}"
        )

    except Exception as e:
        await interaction.followup.send(f"오류: {type(e).__name__}: {e}")

if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        raise RuntimeError("TOKEN 없음")
    bot.run(TOKEN)
