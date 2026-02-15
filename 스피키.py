import os
import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# ==============================
# 설정
# ==============================
IDLE_TIMEOUT_SEC = 5 * 60  # ✅ 퇴장 시간(초)

# ==============================
# yt-dlp 설정
# ==============================
YTDLP_OPTIONS = {
    "format": "bestaudio[abr>=160]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
}

# ==============================
# FFmpeg 설정: (원본 느낌 유지) 48kHz + 스테레오 고정만
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

        # 무활동(명령 없음) 자동 퇴장용
        self.last_command_ts: float = time.monotonic()
        self.idle_task: Optional[asyncio.Task] = None

        # ✅ 마지막으로 명령을 친 텍스트 채널(멘트 출력용)
        self.last_text_channel_id: Optional[int] = None

music_data: Dict[int, GuildMusic] = {}

def get_music(guild_id: int) -> GuildMusic:
    if guild_id not in music_data:
        music_data[guild_id] = GuildMusic()
    return music_data[guild_id]

def touch_command(music: GuildMusic):
    """명령이 들어올 때마다 호출해서 타이머 리셋"""
    music.last_command_ts = time.monotonic()

def extract_info(제목: str) -> Track:
    """
    입력: 제목 (유튜브 URL 또는 제목)
    출력: Track(title, url, stream_url, requester)
    """
    with yt_dlp.YoutubeDL(YTDLP_OPTIONS) as ydl:
        info = ydl.extract_info(제목, download=False)

    if "entries" in info and info["entries"]:
        info = info["entries"][0]

    title = info.get("title", "Unknown Title")
    webpage_url = info.get("webpage_url", 제목)

    stream_url = info.get("url")
    if not stream_url:
        raise Exception("스트림 URL을 가져오지 못했습니다.")

    return Track(title=title, url=webpage_url, stream_url=stream_url, requester=0)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

async def connect_voice(interaction: discord.Interaction) -> discord.VoiceClient:
    """
    입력: interaction
    출력: VoiceClient
    """
    if not interaction.guild:
        raise Exception("길드(서버)에서만 사용할 수 있습니다.")

    if not interaction.user or not isinstance(interaction.user, discord.Member):
        raise Exception("사용자 정보를 가져오지 못했습니다.")

    if not interaction.user.voice or not interaction.user.voice.channel:
        raise Exception("음성채널 먼저 들어가.")

    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client

    if vc and vc.is_connected():
        if vc.channel and vc.channel.id != channel.id:
            raise Exception("다른곳에서 날 사용중이야.")
        return vc

    return await channel.connect()

async def _send_idle_message_only_last_channel(guild: discord.Guild, music: GuildMusic, message: str):
    """
    ✅ 마지막 명령 채널에만 전송 시도.
    - 실패해도 다른 채널로 보내지 않음(원하신 동작).
    """
    if not music.last_text_channel_id:
        return

    try:
        ch = guild.get_channel(music.last_text_channel_id)
        if ch is None:
            ch = await guild.fetch_channel(music.last_text_channel_id)

        if hasattr(ch, "send"):
            await ch.send(message)
    except Exception as e:
        print("자동퇴장 멘트 전송 실패:", repr(e))

async def idle_watcher(guild: discord.Guild, music: GuildMusic):
    """
    ✅ 음악이 재생 중이거나(playing/paused) 큐에 곡이 남아있으면 절대 퇴장하지 않음.
    ✅ '재생도 없고 + 큐도 비어있는' 유휴 상태에서만 5분 무명령이면 퇴장.
    """
    try:
        while True:
            await asyncio.sleep(2)

            vc = guild.voice_client
            if not vc or not vc.is_connected():
                return

            # ✅ 재생 중/일시정지 중이면 유휴가 아님 → 퇴장 체크 안 함
            if vc.is_playing() or vc.is_paused():
                continue

            # ✅ 큐에 곡이 있으면 곧 재생될 예정 → 퇴장 체크 안 함
            async with music.lock:
                has_queue = bool(music.queue)

            if has_queue:
                continue

            # ✅ 여기부터 "유휴 상태"에서만 타이머 체크
            elapsed = time.monotonic() - music.last_command_ts
            if elapsed < IDLE_TIMEOUT_SEC:
                continue

            async with music.lock:
                music.queue.clear()
                music.now_playing = None

            if vc.is_playing() or vc.is_paused():
                vc.stop()

            # ✅ 봇 멘트는 그대로 유지
            await _send_idle_message_only_last_channel(guild, music, "⏳ 5분지났어.")

            try:
                await vc.disconnect()
            except:
                pass

            if music.player_task and not music.player_task.done():
                music.player_task.cancel()

            return
    except asyncio.CancelledError:
        return

def ensure_idle_task(guild: discord.Guild, music: GuildMusic):
    """
    ✅ 기존 idle_task가 있으면 유지하고, 없으면 생성
    """
    if music.idle_task and not music.idle_task.done():
        return
    music.idle_task = asyncio.create_task(idle_watcher(guild, music))

async def player_loop(guild: discord.Guild, music: GuildMusic):
    while True:
        music.next_event.clear()

        async with music.lock:
            if not music.queue:
                music.now_playing = None

        while True:
            async with music.lock:
                if music.queue:
                    break
            await asyncio.sleep(0.5)

        async with music.lock:
            track = music.queue.popleft()
            music.now_playing = track

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return

        source = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTIONS)

        def after_play(error):
            if error:
                print("재생 after 에러:", repr(error))
            bot.loop.call_soon_threadsafe(music.next_event.set)

        try:
            vc.play(source, after=after_play)
            print(f"[재생 시작] {track.title}")
        except Exception as e:
            print("vc.play 에러:", repr(e))
            bot.loop.call_soon_threadsafe(music.next_event.set)
            continue

        await music.next_event.wait()

        # ✅ (핵심) "마지막 곡이 끝난 뒤"부터 5분을 세고 싶으므로,
        # 큐가 비어있다면 지금 시각을 타이머 기준으로 갱신
        async with music.lock:
            if not music.queue:
                touch_command(music)

@bot.tree.command(name="재생", description="유튜브 URL 또는 제목으로 음악 재생(대기열 추가)")
@app_commands.describe(제목="URL 또는 제목 입력")
async def play(interaction: discord.Interaction, 제목: str):
    await interaction.response.defer(thinking=True)

    try:
        await connect_voice(interaction)
        music = get_music(interaction.guild.id)

        touch_command(music)
        music.last_text_channel_id = interaction.channel_id
        ensure_idle_task(interaction.guild, music)

        track = await asyncio.to_thread(extract_info, 제목)
        track.requester = interaction.user.id

        async with music.lock:
            music.queue.append(track)
            position = len(music.queue)

        if not music.player_task or music.player_task.done():
            music.player_task = asyncio.create_task(player_loop(interaction.guild, music))

        await interaction.followup.send(
            f"🎵 **{track.title}** 대기열 추가 (위치: {position})\n{track.url}"
        )

    except Exception as e:
        await interaction.followup.send(f"오류: {type(e).__name__}: {e}")

@bot.tree.command(name="스킵", description="현재 곡만 스킵하고 다음 곡 재생")
async def skip(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    vc = interaction.guild.voice_client if interaction.guild else None
    if not vc or not vc.is_connected():
        await interaction.followup.send("음성 채널에 없어.")
        return

    music = get_music(interaction.guild.id)
    touch_command(music)
    music.last_text_channel_id = interaction.channel_id
    ensure_idle_task(interaction.guild, music)

    if not (vc.is_playing() or vc.is_paused()):
        await interaction.followup.send("재생중인 음악이 없어.")
        return

    vc.stop()
    await interaction.followup.send("⏭️ 다음꺼야.")

@bot.tree.command(name="나가", description="음악 종료 + 대기열 비움 + 봇 퇴장")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    if not interaction.guild:
        await interaction.followup.send("길드(서버)에서만 사용할 수 있습니다.")
        return

    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.followup.send("채널부터 들어가.")
        return

    music = get_music(interaction.guild.id)
    touch_command(music)
    music.last_text_channel_id = interaction.channel_id

    async with music.lock:
        music.queue.clear()
        music.now_playing = None

    if vc.is_playing() or vc.is_paused():
        vc.stop()

    await vc.disconnect()

    if music.player_task and not music.player_task.done():
        music.player_task.cancel()
    if music.idle_task and not music.idle_task.done():
        music.idle_task.cancel()

    await interaction.followup.send("응.")

if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        raise RuntimeError("환경변수 TOKEN이 설정되어 있지 않아. (CMD: set TOKEN=토큰)")
    bot.run(TOKEN)
