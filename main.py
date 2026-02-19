import os
import asyncio
import time
import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# ==============================
# ✅ 부팅/동기화 로그
# ==============================
logging.basicConfig(level=logging.INFO)
bootlog = logging.getLogger("boot")
print("BOOT: main.py 실행됨", flush=True)

# ==============================
# 설정
# ==============================
IDLE_TIMEOUT_SEC = 5 * 60
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# ==============================
# 문구(통일)
# ==============================
MSG_NEED_VOICE = "통화방에 들어와야 쓸 수 있어."
MSG_BOT_NOT_IN_VOICE = "지금 봇이 통화방에 없어."
MSG_NEED_SAME_VOICE = "봇이 있는 통화방에 들어와야 쓸 수 있어."
MSG_DIFF_VOICE_IN_USE = "다른 통화방에서 날 쓰는 중이야."

# ==============================
# yt-dlp 설정 (✅ 쿠키 미사용)
# ==============================
YTDLP_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "sleep_requests": 1,
    "sleep_interval": 1,
    "max_sleep_interval": 3,
    "retries": 3,
    "fragment_retries": 3,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    },
    "remote_components": ["ejs:github"],
    "extractor_args": {
        "youtube": {
            "player_client": ["android"]
        }
    },
}

# ==============================
# FFmpeg 설정
# ==============================
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 10",
    "options": "-vn -ar 48000 -ac 2",
}

# ==============================
# 데이터 구조
# ==============================
@dataclass
class Track:
    title: str
    url: str
    stream_url: str
    requester: int
    duration: Optional[int] = None
    thumbnail: Optional[str] = None


class GuildMusic:
    def __init__(self):
        self.queue: Deque[Track] = deque()
        self.now_playing: Optional[Track] = None

        self.lock = asyncio.Lock()
        self.next_event = asyncio.Event()
        self.player_task: Optional[asyncio.Task] = None

        self.last_command_ts: float = time.monotonic()
        self.idle_task: Optional[asyncio.Task] = None

        # 마지막 명령 채널 (참고용)
        self.last_text_channel_id: Optional[int] = None

        # ✅ 패널 메시지 1개 유지
        self.panel_channel_id: Optional[int] = None
        self.panel_message_id: Optional[int] = None

        # ✅ 반복 모드: "off" | "all" | "one"
        self.repeat_mode: str = "off"

        # ✅ 스킵으로 끝난 곡은 ALL 반복에 다시 넣지 않기
        self.skip_flag: bool = False


music_data: Dict[int, GuildMusic] = {}


def get_music(guild_id: int) -> GuildMusic:
    if guild_id not in music_data:
        music_data[guild_id] = GuildMusic()
    return music_data[guild_id]


def touch_command(music: GuildMusic):
    music.last_command_ts = time.monotonic()


def fmt_time(sec: Optional[int]) -> str:
    if sec is None:
        return "--:--"
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def repeat_label(mode: str) -> str:
    if mode == "one":
        return "🔂 한곡"
    if mode == "all":
        return "🔁 전체"
    return "반복OFF"


def repeat_button_style(mode: str) -> discord.ButtonStyle:
    # off: 회색(secondary), all: 초록(success), one: 파랑(primary)
    if mode == "all":
        return discord.ButtonStyle.success
    if mode == "one":
        return discord.ButtonStyle.primary
    return discord.ButtonStyle.secondary


def shuffle_queue_inplace(music: GuildMusic):
    """
    입력값: music.queue (현재 재생중 now_playing은 건드리지 않음)
    출력값: 대기열(queue)만 랜덤 섞임
    """
    import random
    q = list(music.queue)
    random.shuffle(q)
    music.queue.clear()
    music.queue.extend(q)


def extract_info(query: str) -> Track:
    """
    입력: query (유튜브 URL 또는 검색어)
    출력: Track
    """
    with yt_dlp.YoutubeDL(YTDLP_OPTIONS) as ydl:
        info = ydl.extract_info(query, download=False)

    if "entries" in info and info["entries"]:
        info = info["entries"][0]

    title = info.get("title", "Unknown Title")
    webpage_url = info.get("webpage_url", query)

    stream_url = info.get("url")
    if not stream_url:
        raise Exception("스트림 URL을 가져오지 못했어.")

    duration = info.get("duration")
    thumbnail = info.get("thumbnail")

    return Track(
        title=title,
        url=webpage_url,
        stream_url=stream_url,
        requester=0,
        duration=duration,
        thumbnail=thumbnail,
    )


async def extract_with_retry(query: str) -> Track:
    """
    입력: query
    출력: Track (성공) / 예외(최종 실패)
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, 5):  # 1~4회
        try:
            return await asyncio.to_thread(extract_info, query)
        except Exception as e:
            last_err = e
            print(f"{attempt}차 추출 실패:", repr(e), flush=True)
            await asyncio.sleep(min(2 * attempt, 6))
    raise last_err if last_err else Exception("알 수 없는 추출 실패")


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ==============================
# ✅ 슬래시 커맨드 공통 권한 체크(통일)
# ==============================
def require_user_in_voice(interaction: discord.Interaction) -> discord.VoiceChannel:
    """
    입력: interaction
    출력: user voice channel
    조건: 사용자는 반드시 어떤 통화방이든 들어가 있어야 함
    """
    if not interaction.guild:
        raise Exception("길드(서버)에서만 쓸 수 있어.")
    if not isinstance(interaction.user, discord.Member):
        raise Exception("사용자 정보를 못 가져왔어.")
    if not interaction.user.voice or not interaction.user.voice.channel:
        raise Exception(MSG_NEED_VOICE)
    return interaction.user.voice.channel


def require_user_in_bot_voice(interaction: discord.Interaction) -> discord.VoiceClient:
    """
    입력: interaction
    출력: voice client
    조건:
      - 봇이 통화방에 있어야 함
      - 사용자가 통화방에 있어야 함
      - 사용자의 통화방 == 봇의 통화방
    """
    if not interaction.guild:
        raise Exception("길드(서버)에서만 쓸 수 있어.")

    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected() or not vc.channel:
        raise Exception(MSG_BOT_NOT_IN_VOICE)

    user_ch = require_user_in_voice(interaction)
    if user_ch.id != vc.channel.id:
        raise Exception(MSG_NEED_SAME_VOICE)

    return vc


# ==============================
# 패널(임베드+버튼) 유틸
# ==============================
def _get_status_text(guild: discord.Guild) -> str:
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return "연결 안 됨"
    if vc.is_paused():
        return "⏸️ 일시정지"
    if vc.is_playing():
        return "▶️ 재생중"
    return "대기중"


def _requester_name(guild: discord.Guild, requester_id: int) -> str:
    if not requester_id:
        return "알 수 없음"
    m = guild.get_member(requester_id)
    return m.display_name if m else "알 수 없음"


def build_panel_embed(guild: discord.Guild, music: GuildMusic) -> discord.Embed:
    status = _get_status_text(guild)
    vc = guild.voice_client
    channel_name = vc.channel.name if (vc and vc.is_connected() and vc.channel) else "-"

    now = music.now_playing
    next_track = music.queue[0] if music.queue else None

    embed = discord.Embed(title="곽덕춘")

    requester_name = _requester_name(guild, now.requester) if now else "-"

    embed.add_field(
        name="",
        value=(
            f"상태: {status} | 요청자: {requester_name} | 음성 채널: {channel_name}\n"
            f"{repeat_label(music.repeat_mode)}"
        ),
        inline=False,
    )

    embed.add_field(name="\u200b", value="\u200b", inline=False)

    if now:
        duration = fmt_time(now.duration)
        embed.add_field(
            name="현재 재생중",
            value=f"🎵 **{now.title} ({duration})**",
            inline=False,
        )
        if now.thumbnail:
            embed.set_thumbnail(url=now.thumbnail)
    else:
        embed.add_field(name="현재 재생중", value="없음", inline=False)

    embed.add_field(name="\u200b", value="\u200b", inline=False)

    if next_track:
        embed.add_field(name="다음 노래", value=f"{next_track.title}", inline=False)
    else:
        embed.add_field(name="다음 노래", value="없음", inline=False)

    return embed


async def fetch_panel_channel(guild: discord.Guild, music: GuildMusic):
    if not music.panel_channel_id:
        return None

    ch = guild.get_channel(music.panel_channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(music.panel_channel_id)
        except Exception:
            return None

    if not hasattr(ch, "send"):
        return None
    if not hasattr(ch, "fetch_message"):
        return None
    return ch


async def delete_panel(guild: discord.Guild, music: GuildMusic):
    if not music.panel_channel_id or not music.panel_message_id:
        music.panel_channel_id = None
        music.panel_message_id = None
        return

    ch = await fetch_panel_channel(guild, music)
    if not ch:
        music.panel_channel_id = None
        music.panel_message_id = None
        return

    try:
        msg = await ch.fetch_message(music.panel_message_id)
        await msg.delete()
    except Exception:
        pass

    music.panel_channel_id = None
    music.panel_message_id = None


async def upsert_panel(guild: discord.Guild, music: GuildMusic):
    ch = await fetch_panel_channel(guild, music)
    if not ch:
        return

    async with music.lock:
        repeat_mode_snapshot = music.repeat_mode

    embed = build_panel_embed(guild, music)
    view = MusicControlView(repeat_mode=repeat_mode_snapshot)

    if not music.panel_message_id:
        try:
            msg = await ch.send(embed=embed, view=view)
            music.panel_message_id = msg.id
        except Exception as e:
            print("패널 생성 실패:", repr(e), flush=True)
        return

    try:
        msg = await ch.fetch_message(music.panel_message_id)
        await msg.edit(embed=embed, view=view)
    except Exception:
        music.panel_message_id = None
        await upsert_panel(guild, music)


# ==============================
# 버튼 UI (✅ Persistent)
# ==============================
class MusicControlView(discord.ui.View):
    """
    버튼 배치:
      1행: 일시정지 / 재생 / 셔플
      2행: 반복 / 스킵 / 목록 / 퇴장
    반복 버튼 색상:
      off=회색, all=초록, one=파랑
    """
    def __init__(self, repeat_mode: str = "off"):
        super().__init__(timeout=None)
        self.repeat_btn.style = repeat_button_style(repeat_mode)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected() or not vc.channel:
            await interaction.response.send_message(MSG_BOT_NOT_IN_VOICE, ephemeral=True)
            return False

        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.voice
            or not interaction.user.voice.channel
        ):
            await interaction.response.send_message(MSG_NEED_VOICE, ephemeral=True)
            return False

        if interaction.user.voice.channel.id != vc.channel.id:
            await interaction.response.send_message(MSG_NEED_SAME_VOICE, ephemeral=True)
            return False

        return True

    @discord.ui.button(label="일시정지", style=discord.ButtonStyle.secondary, emoji="⏸️", row=0, custom_id="music_pause")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(interaction.guild.id)
        touch_command(music)

        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()

        await upsert_panel(interaction.guild, music)
        await interaction.response.defer()

    @discord.ui.button(label="재생", style=discord.ButtonStyle.success, emoji="▶️", row=0, custom_id="music_resume")
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(interaction.guild.id)
        touch_command(music)

        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()

        await upsert_panel(interaction.guild, music)
        await interaction.response.defer()

    @discord.ui.button(label="셔플", style=discord.ButtonStyle.primary, emoji="🔀", row=0, custom_id="music_shuffle_once")
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(interaction.guild.id)
        touch_command(music)

        async with music.lock:
            if len(music.queue) >= 2:
                shuffle_queue_inplace(music)

        await upsert_panel(interaction.guild, music)
        await interaction.response.defer()

    @discord.ui.button(label="반복", style=discord.ButtonStyle.secondary, emoji="🔁", row=1, custom_id="music_repeat_cycle")
    async def repeat_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(interaction.guild.id)
        touch_command(music)

        async with music.lock:
            if music.repeat_mode == "off":
                music.repeat_mode = "all"
            elif music.repeat_mode == "all":
                music.repeat_mode = "one"
            else:
                music.repeat_mode = "off"
            button.style = repeat_button_style(music.repeat_mode)

        await upsert_panel(interaction.guild, music)
        await interaction.response.defer()

    @discord.ui.button(label="스킵", style=discord.ButtonStyle.danger, emoji="⏭️", row=1, custom_id="music_skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(interaction.guild.id)
        touch_command(music)

        async with music.lock:
            music.skip_flag = True
            music.now_playing = None

        vc = interaction.guild.voice_client
        if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
            vc.stop()

        await upsert_panel(interaction.guild, music)
        await interaction.response.defer()

    @discord.ui.button(label="목록", style=discord.ButtonStyle.secondary, emoji="📃", row=1, custom_id="music_list")
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(interaction.guild.id)
        touch_command(music)

        async with music.lock:
            if not music.queue:
                await interaction.response.send_message("대기열이 비어있어.", ephemeral=True)
                return

            items = list(music.queue)[:20]
            lines = [f"{i}. {t.title}" for i, t in enumerate(items, start=1)]
            more = len(music.queue) - len(items)
            if more > 0:
                lines.append(f"...그리고 {more}개 더 있어.")

        await upsert_panel(interaction.guild, music)
        await interaction.response.send_message("📃 대기열\n" + "\n".join(lines), ephemeral=True)

    @discord.ui.button(label="퇴장", style=discord.ButtonStyle.danger, emoji="🚪", row=1, custom_id="music_leave")
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(interaction.guild.id)
        touch_command(music)

        await do_leave(interaction.guild, music)
        await interaction.response.defer()


# ==============================
# 보이스 연결/퇴장 공통
# ==============================
async def connect_voice(interaction: discord.Interaction) -> discord.VoiceClient:
    if not interaction.guild:
        raise Exception("길드(서버)에서만 쓸 수 있어.")
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        raise Exception("사용자 정보를 못 가져왔어.")
    if not interaction.user.voice or not interaction.user.voice.channel:
        raise Exception(MSG_NEED_VOICE)

    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client

    if vc and vc.is_connected():
        if vc.channel and vc.channel.id != channel.id:
            raise Exception(MSG_DIFF_VOICE_IN_USE)
        return vc

    return await channel.connect()


async def do_leave(guild: discord.Guild, music: GuildMusic):
    """
    출력: 재생 중지 + 큐 초기화 + 음성 해제 + 태스크 정리 + 패널 삭제
    """
    current = asyncio.current_task()
    vc = guild.voice_client

    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()

    async with music.lock:
        music.queue.clear()
        music.now_playing = None
        music.skip_flag = False

    try:
        if vc and vc.is_connected():
            await vc.disconnect()
    except Exception:
        pass

    # ✅ 자기 자신은 취소하지 않음
    if music.player_task and not music.player_task.done() and music.player_task is not current:
        music.player_task.cancel()

    if music.idle_task and not music.idle_task.done() and music.idle_task is not current:
        music.idle_task.cancel()

    # ✅ 자동퇴장 시 패널 삭제가 끊기지 않게 보호
    try:
        await asyncio.shield(delete_panel(guild, music))
    except Exception:
        pass


# ==============================
# 유휴 감시
# ==============================
async def idle_watcher(guild: discord.Guild, music: GuildMusic):
    try:
        while True:
            await asyncio.sleep(2)

            vc = guild.voice_client
            if not vc or not vc.is_connected():
                return

            # 재생/일시정지 중이면 유휴 아님
            if vc.is_playing() or vc.is_paused():
                continue

            # 아무것도 재생중이 아닐 때만 카운트
            async with music.lock:
                has_queue = bool(music.queue)
                has_now = (music.now_playing is not None)

            if has_queue or has_now:
                continue

            elapsed = time.monotonic() - music.last_command_ts
            if elapsed < IDLE_TIMEOUT_SEC:
                continue

            await do_leave(guild, music)
            return

    except asyncio.CancelledError:
        return


def ensure_idle_task(guild: discord.Guild, music: GuildMusic):
    if music.idle_task and not music.idle_task.done():
        return
    music.idle_task = asyncio.create_task(idle_watcher(guild, music))


# ==============================
# 재생 루프
# ==============================
async def player_loop(guild: discord.Guild, music: GuildMusic):
    while True:
        music.next_event.clear()

        async with music.lock:
            if not music.queue:
                music.now_playing = None

        # 큐 대기
        while True:
            async with music.lock:
                if music.queue:
                    break
            await asyncio.sleep(0.5)

        # 다음 곡
        async with music.lock:
            track = music.queue.popleft()
            music.now_playing = track

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return

        source = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTIONS)

        def after_play(error):
            if error:
                print("재생 after 에러:", repr(error), flush=True)
            bot.loop.call_soon_threadsafe(music.next_event.set)

        try:
            vc.play(source, after=after_play)
            print(f"[재생 시작] {track.title}", flush=True)
            await upsert_panel(guild, music)
        except Exception as e:
            print("vc.play 에러:", repr(e), flush=True)
            bot.loop.call_soon_threadsafe(music.next_event.set)
            continue

        await music.next_event.wait()

        # 곡 종료 후 반복 처리 + 유휴 타이머 기준점(재생이 완전히 끝난 시점)
        async with music.lock:
            was_skip = music.skip_flag
            music.skip_flag = False

            if (not was_skip) and music.repeat_mode == "all":
                music.queue.append(track)
            elif (not was_skip) and music.repeat_mode == "one":
                music.queue.appendleft(track)

            # 재생이 끝나서 "아무것도 없어진 시점"부터 5분 카운트
            if not music.queue:
                music.now_playing = None
                touch_command(music)

        await upsert_panel(guild, music)


# ==============================
# 이벤트
# ==============================
@bot.event
async def on_ready():
    bootlog.info("READY_HIT: %s", bot.user)

    # ✅ Persistent View 등록
    bot.add_view(MusicControlView())

    try:
        if GUILD_ID and GUILD_ID != 0:
            guild = discord.Object(id=GUILD_ID)
            cmds = await asyncio.wait_for(bot.tree.sync(guild=guild), timeout=30)
            bootlog.info("SYNC_OK(GUILD): %d commands", len(cmds))
        else:
            cmds = await asyncio.wait_for(bot.tree.sync(), timeout=30)
            bootlog.info("SYNC_OK(GLOBAL): %d commands", len(cmds))
    except asyncio.TimeoutError:
        bootlog.warning("SYNC_TIMEOUT: 30초 내 끝나지 않음")
    except Exception as e:
        bootlog.exception("SYNC_FAIL: %r", e)


# ==============================
# 슬래시 커맨드
# ==============================
@bot.tree.command(name="재생", description="유튜브 URL 또는 제목으로 음악 재생(대기열 추가)")
@app_commands.describe(제목="URL 또는 제목 입력")
async def play(interaction: discord.Interaction, 제목: str):
    await interaction.response.defer(thinking=True)

    try:
        # ✅ 첫 /재생: 어느 통화방이든 들어가 있어야 함
        require_user_in_voice(interaction)

        # ✅ 봇이 이미 다른 채널에 있으면 여기서 차단됨
        await connect_voice(interaction)

        music = get_music(interaction.guild.id)
        touch_command(music)
        music.last_text_channel_id = interaction.channel_id

        music.panel_channel_id = interaction.channel_id
        ensure_idle_task(interaction.guild, music)

        await upsert_panel(interaction.guild, music)

        track = await extract_with_retry(제목)
        track.requester = interaction.user.id

        async with music.lock:
            music.queue.append(track)
            position = len(music.queue)

        if not music.player_task or music.player_task.done():
            music.player_task = asyncio.create_task(player_loop(interaction.guild, music))

        await upsert_panel(interaction.guild, music)

        msg = await interaction.followup.send(
            f"🎵 **{track.title}** 대기열 추가 (위치: {position})\n{track.url}",
            suppress_embeds=True
        )
        await asyncio.sleep(2)
        try:
            await msg.delete()
        except Exception:
            pass

    except Exception as e:
        await interaction.followup.send(str(e))


@bot.tree.command(name="우선예약", description="유튜브 URL 또는 제목을 다음 곡(대기열 맨 앞)으로 예약")
@app_commands.describe(제목="URL 또는 제목 입력")
async def priority_play(interaction: discord.Interaction, 제목: str):
    await interaction.response.defer(thinking=True)

    try:
        # ✅ 봇이 이미 있으면 같은 통화방이어야 함
        # (없으면 첫 /재생과 동일하게 어디든 들어가 있으면 연결됨)
        require_user_in_voice(interaction)
        await connect_voice(interaction)

        music = get_music(interaction.guild.id)
        touch_command(music)
        music.last_text_channel_id = interaction.channel_id

        music.panel_channel_id = interaction.channel_id
        ensure_idle_task(interaction.guild, music)

        await upsert_panel(interaction.guild, music)

        track = await extract_with_retry(제목)
        track.requester = interaction.user.id

        async with music.lock:
            music.queue.appendleft(track)

        if not music.player_task or music.player_task.done():
            music.player_task = asyncio.create_task(player_loop(interaction.guild, music))

        await upsert_panel(interaction.guild, music)

        msg = await interaction.followup.send(
            f"⏩ 우선예약 완료: **{track.title}** (다음 곡)\n{track.url}",
            suppress_embeds=True
        )
        await asyncio.sleep(2)
        try:
            await msg.delete()
        except Exception:
            pass

    except Exception as e:
        await interaction.followup.send(str(e))


@bot.tree.command(name="셔플", description="대기열을 1회 섞기(현재 재생중인 곡은 유지)")
async def shuffle_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        require_user_in_bot_voice(interaction)

        music = get_music(interaction.guild.id)
        touch_command(music)
        music.last_text_channel_id = interaction.channel_id
        ensure_idle_task(interaction.guild, music)

        async with music.lock:
            if len(music.queue) < 2:
                ok = False
            else:
                shuffle_queue_inplace(music)
                ok = True

        await upsert_panel(interaction.guild, music)
        await interaction.followup.send("🔀 대기열을 섞었어." if ok else "대기열이 2개 이상 있어야 섞을 수 있어.")

    except Exception as e:
        await interaction.followup.send(str(e))


@bot.tree.command(name="반복", description="반복 모드 변경: OFF -> 전체 -> 한곡 -> OFF")
async def repeat_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        require_user_in_bot_voice(interaction)

        music = get_music(interaction.guild.id)
        touch_command(music)
        music.last_text_channel_id = interaction.channel_id
        ensure_idle_task(interaction.guild, music)

        async with music.lock:
            if music.repeat_mode == "off":
                music.repeat_mode = "all"
            elif music.repeat_mode == "all":
                music.repeat_mode = "one"
            else:
                music.repeat_mode = "off"
            label = repeat_label(music.repeat_mode)

        await upsert_panel(interaction.guild, music)
        await interaction.followup.send(f"{label} 로 바꿨어.")

    except Exception as e:
        await interaction.followup.send(str(e))


@bot.tree.command(name="스킵", description="현재 곡만 스킵하고 다음 곡 재생")
async def skip(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        vc = require_user_in_bot_voice(interaction)

        music = get_music(interaction.guild.id)
        touch_command(music)
        music.last_text_channel_id = interaction.channel_id
        ensure_idle_task(interaction.guild, music)

        if not (vc.is_playing() or vc.is_paused()):
            await interaction.followup.send("재생중인 음악이 없어.")
            return

        async with music.lock:
            music.skip_flag = True
            music.now_playing = None

        vc.stop()
        await upsert_panel(interaction.guild, music)
        await interaction.followup.send("⏭️ 다음꺼야.")

    except Exception as e:
        await interaction.followup.send(str(e))


@bot.tree.command(name="퇴장", description="음악 종료 + 대기열 비움 + 봇 퇴장")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        require_user_in_bot_voice(interaction)

        music = get_music(interaction.guild.id)
        touch_command(music)
        music.last_text_channel_id = interaction.channel_id

        await do_leave(interaction.guild, music)
        await interaction.followup.send("응.")

    except Exception as e:
        await interaction.followup.send(str(e))


@bot.tree.command(name="목록", description="현재 예약(대기열)된 노래 목록 확인")
async def queue_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        require_user_in_bot_voice(interaction)

        music = get_music(interaction.guild.id)
        touch_command(music)
        music.last_text_channel_id = interaction.channel_id
        ensure_idle_task(interaction.guild, music)

        async with music.lock:
            if not music.queue:
                await interaction.followup.send("대기열이 비어있어.")
                return

            items = list(music.queue)[:20]
            lines = [f"{i}. **{t.title}**" for i, t in enumerate(items, start=1)]
            more = len(music.queue) - len(items)
            if more > 0:
                lines.append(f"...그리고 {more}개 더 있어.")

            msg = "📃 대기열 목록\n" + "\n\n".join(lines)

        await upsert_panel(interaction.guild, music)
        await interaction.followup.send(msg)

    except Exception as e:
        await interaction.followup.send(str(e))


@bot.tree.command(name="취소", description="대기열에서 특정 번호의 곡을 삭제(예약 취소)")
@app_commands.describe(번호="목록에서 보이는 번호(1부터)")
async def queue_remove(interaction: discord.Interaction, 번호: int):
    await interaction.response.defer(thinking=True)

    try:
        require_user_in_bot_voice(interaction)

        if 번호 <= 0:
            await interaction.followup.send("그 번호는 없어.")
            return

        music = get_music(interaction.guild.id)
        touch_command(music)
        music.last_text_channel_id = interaction.channel_id
        ensure_idle_task(interaction.guild, music)

        async with music.lock:
            if not music.queue:
                await interaction.followup.send("대기열이 비어있어.")
                return

            if 번호 > len(music.queue):
                await interaction.followup.send("그 번호는 없어.")
                return

            q_list = list(music.queue)
            removed = q_list.pop(번호 - 1)
            music.queue.clear()
            music.queue.extend(q_list)

        await upsert_panel(interaction.guild, music)
        await interaction.followup.send(f"✅ 취소됨: **{removed.title}**")

    except Exception as e:
        await interaction.followup.send(str(e))


if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        raise RuntimeError("환경변수 TOKEN이 설정되어 있지 않아. (CMD: set TOKEN=토큰)")
    bot.run(TOKEN)
