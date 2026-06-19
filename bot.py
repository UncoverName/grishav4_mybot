# -*- coding: utf-8 -*-
"""
Discord музыкальный бот на discord.py v2.x (СЛЭШ-КОМАНДЫ).

Команды (через «/»):
  /play <ссылка/название> — добавить трек в очередь и играть.
        Поддержка: YouTube (видео/плейлисты), Spotify (трек/плейлист/альбом),
        прямые ссылки на файлы, текстовый поиск (первый результат YouTube).
  /queue   — показать очередь.
  /skip    — пропустить текущий трек.
  /pause   — пауза.
  /resume  — продолжить.
  /stop    — остановить, очистить очередь и выйти из канала.

Под каждым треком — Embed «Сейчас играет» с кнопками (Пауза/Скип/Стоп).
Авто-выход из канала через 2 минуты, если очередь пуста или в канале нет людей.
Доступ к командам — у всех (никаких DJ-ролей).
"""

import os
import re
import sys
import json
import shutil
import asyncio
from collections import deque

import requests
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Консоль Windows может быть в кодировке cp1251 и падать на эмодзи/кириллице.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------------------
# ТОКЕН (из файла .env: DISCORD_TOKEN=...)
# --------------------------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("❌ Токен не найден. Впиши в .env:  DISCORD_TOKEN=твой_токен")

# --------------------------------------------------------------------------
# FFmpeg: ищем в PATH, иначе локальную копию рядом с ботом.
# --------------------------------------------------------------------------
def resolve_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "ffmpeg", "bin", "ffmpeg.exe")
    return local if os.path.isfile(local) else "ffmpeg"

FFMPEG_EXE = resolve_ffmpeg()

# Параметры FFmpeg против рывков/заиканий при стриминге:
#  -reconnect / -reconnect_streamed        — переподключаться при обрыве потока
#  -reconnect_on_network_error             — переподключаться при TCP/TLS ошибках
#  -reconnect_on_http_error 4xx,5xx        — переподключаться при HTTP-ошибках CDN
#  -reconnect_delay_max 5                  — не ждать дольше 5 сек между попытками
#  -vn                                     — без видео, только звук
FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
        "-reconnect_on_http_error 4xx,5xx -reconnect_delay_max 5"
    ),
    "options": "-vn",
}

# --------------------------------------------------------------------------
# yt-dlp
# --------------------------------------------------------------------------
YTDL_FLAT = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch",
    "ignoreerrors": True,
}
YTDL_FULL = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
}

AUTO_LEAVE_SECONDS = 120  # 2 минуты


class Track:
    """Один трек в очереди (метаданные; ссылка на поток резолвится при игре)."""
    def __init__(self, *, title, webpage_url, duration, thumbnail, requester):
        self.title = title
        self.webpage_url = webpage_url      # URL или строка вида 'ytsearch1:...'
        self.duration = duration            # секунды (int) или None
        self.thumbnail = thumbnail
        self.requester = requester          # discord.Member


class GuildPlayer:
    """Независимое состояние плеера для одного сервера."""
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.text_channel = None
        self.disconnect_task = None
        self.leaving = False


players: dict[int, GuildPlayer] = {}


def get_player(guild: discord.Guild) -> GuildPlayer:
    p = players.get(guild.id)
    if p is None:
        p = players[guild.id] = GuildPlayer()
    return p


def fmt_dur(seconds) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------
# Spotify: метаданные треков из публичной embed-страницы (без ключей/Premium)
# --------------------------------------------------------------------------
SPOTIFY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def is_spotify_url(query: str) -> bool:
    return "open.spotify.com" in query or query.startswith("spotify:")


def parse_spotify(url: str):
    m = re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|playlist|album)/([A-Za-z0-9]+)", url)
    if not m:
        m = re.search(r"spotify:(track|playlist|album):([A-Za-z0-9]+)", url)
    if not m:
        raise RuntimeError("Это не ссылка на трек/плейлист/альбом Spotify.")
    return m.group(1), m.group(2)


def _spotify_find_entity(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("trackList"), list):
            return obj
        for v in obj.values():
            r = _spotify_find_entity(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _spotify_find_entity(v)
            if r:
                return r
    return None


async def build_spotify_tracks(query: str, requester):
    loop = asyncio.get_running_loop()

    def _work():
        kind, sid = parse_spotify(query)
        url = f"https://open.spotify.com/embed/{kind}/{sid}"
        resp = requests.get(url, headers=SPOTIFY_HEADERS, timeout=25)
        resp.raise_for_status()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S)
        if not m:
            raise RuntimeError("Не удалось прочитать данные Spotify (изменился формат страницы).")
        ent = _spotify_find_entity(json.loads(m.group(1)))
        if not ent or not ent.get("trackList"):
            raise RuntimeError("В этой ссылке Spotify не нашлось треков.")
        name = ent.get("title") or ent.get("name")

        tracks = []
        for it in ent["trackList"]:
            title = (it.get("title") or "").strip()
            subtitle = (it.get("subtitle") or "").replace("\xa0", " ").strip()
            q = f"{subtitle} - {title}".strip(" -")
            if not q or q == "-":
                continue
            dur_ms = it.get("duration")
            tracks.append(Track(
                title=q,
                webpage_url=f"ytsearch1:{q}",
                duration=int(dur_ms / 1000) if dur_ms else None,
                thumbnail=None,
                requester=requester,
            ))
        playlist_name = name if (kind in ("playlist", "album") and len(tracks) > 1) else None
        return tracks, playlist_name

    return await loop.run_in_executor(None, _work)


# --------------------------------------------------------------------------
# yt-dlp: разбор запроса в список треков
# --------------------------------------------------------------------------
def _make_track(entry: dict, requester) -> Track:
    url = entry.get("webpage_url") or entry.get("url")
    if url and not url.startswith("http") and entry.get("id"):
        url = f"https://www.youtube.com/watch?v={entry['id']}"
    thumb = entry.get("thumbnail")
    if not thumb and entry.get("thumbnails"):
        thumb = entry["thumbnails"][-1].get("url")
    return Track(
        title=entry.get("title") or "Без названия",
        webpage_url=url,
        duration=entry.get("duration"),
        thumbnail=thumb,
        requester=requester,
    )


async def build_tracks(query: str, requester):
    if is_spotify_url(query):
        return await build_spotify_tracks(query, requester)

    loop = asyncio.get_running_loop()

    def _work():
        is_url = query.startswith("http://") or query.startswith("https://")
        target = query if is_url else f"ytsearch1:{query}"
        with yt_dlp.YoutubeDL(YTDL_FLAT) as ydl:
            info = ydl.extract_info(target, download=False)
        if not info:
            return [], None
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            playlist_title = info.get("title") if (is_url and len(entries) > 1) else None
            return [_make_track(e, requester) for e in entries], playlist_title
        return [_make_track(info, requester)], None

    return await loop.run_in_executor(None, _work)


async def resolve_stream(source: str) -> dict:
    loop = asyncio.get_running_loop()

    def _work():
        with yt_dlp.YoutubeDL(YTDL_FULL) as ydl:
            info = ydl.extract_info(source, download=False)
            if info and "entries" in info:
                info = info["entries"][0]
            return info

    return await loop.run_in_executor(None, _work)


# --------------------------------------------------------------------------
# Таймер авто-выхода
# --------------------------------------------------------------------------
def cancel_disconnect(guild):
    p = get_player(guild)
    if p.disconnect_task:
        p.disconnect_task.cancel()
        p.disconnect_task = None


def schedule_disconnect(guild):
    p = get_player(guild)
    cancel_disconnect(guild)
    p.disconnect_task = bot.loop.create_task(_disconnect_after(guild))


async def _disconnect_after(guild):
    try:
        await asyncio.sleep(AUTO_LEAVE_SECONDS)
    except asyncio.CancelledError:
        return
    p = get_player(guild)
    vc = guild.voice_client
    if not vc:
        return
    humans = [m for m in vc.channel.members if not m.bot]
    idle = not (vc.is_playing() or vc.is_paused())
    if not humans or (idle and not p.queue):
        await leave_and_cleanup(guild)  # выходим молча, без сообщения


async def leave_and_cleanup(guild):
    p = get_player(guild)
    p.leaving = True
    p.queue.clear()
    p.current = None
    cancel_disconnect(guild)
    vc = guild.voice_client
    if vc:
        await vc.disconnect()


# --------------------------------------------------------------------------
# Ядро: проигрывание следующего трека
# --------------------------------------------------------------------------
async def play_next(guild):
    p = get_player(guild)
    if p.leaving:
        return
    vc = guild.voice_client
    if vc is None:
        return

    if not p.queue:
        p.current = None
        schedule_disconnect(guild)  # очередь пуста — выйдем по тайм-ауту, молча
        return

    cancel_disconnect(guild)
    track = p.queue.popleft()
    p.current = track

    try:
        info = await resolve_stream(track.webpage_url)
        stream_url = info["url"]
        track.webpage_url = info.get("webpage_url", track.webpage_url)
        track.title = info.get("title", track.title)
        track.duration = info.get("duration", track.duration)
        track.thumbnail = info.get("thumbnail", track.thumbnail)
    except Exception as e:
        if p.text_channel:
            await p.text_channel.send(f"⚠️ Пропускаю «{track.title}» (не получил поток): {e}")
        return await play_next(guild)

    source = discord.FFmpegPCMAudio(stream_url, executable=FFMPEG_EXE, **FFMPEG_OPTIONS)

    def after(error):
        if error:
            print("Ошибка воспроизведения:", error)
        asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

    vc.play(source, after=after)
    await send_now_playing(guild, track)


async def send_now_playing(guild, track: Track):
    p = get_player(guild)
    if not p.text_channel:
        return
    embed = discord.Embed(
        title="▶️ Сейчас играет",
        description=f"**[{track.title}]({track.webpage_url})**",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Длительность", value=fmt_dur(track.duration))
    if track.requester:
        embed.add_field(name="Добавил", value=track.requester.mention)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    await p.text_channel.send(embed=embed, view=PlayerControls(guild))


# --------------------------------------------------------------------------
# Кнопки управления (discord.ui.View)
# --------------------------------------------------------------------------
class PlayerControls(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild

    @discord.ui.button(emoji="⏸️", label="Пауза/Продолжить",
                       style=discord.ButtonStyle.secondary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.guild.voice_client
        if not vc:
            await interaction.response.send_message("Я не в канале.", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Пауза.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Продолжаю.", ephemeral=True)
        else:
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Пропустить",
                       style=discord.ButtonStyle.primary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.send_message("⏭️ Пропустил.", ephemeral=True)
        else:
            await interaction.response.send_message("Нечего пропускать.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="Стоп",
                       style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await leave_and_cleanup(self.guild)
        await interaction.response.send_message("⏹️ Остановил и вышел.", ephemeral=True)


# --------------------------------------------------------------------------
# Бот и интенты
# --------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ Бот запущен как: {bot.user}  (id: {bot.user.id})")
    print(f"   FFmpeg: {FFMPEG_EXE}")
    print("   Spotify: ссылки поддерживаются (ключи не нужны)")
    try:
        for g in bot.guilds:
            bot.tree.copy_global_to(guild=g)
            await bot.tree.sync(guild=g)
        print(f"   Слэш-команды синхронизированы на {len(bot.guilds)} сервер(ах).")
    except Exception as e:
        print("   Ошибка синхронизации команд:", e)


# --------------------------------------------------------------------------
# Слэш-команды
# --------------------------------------------------------------------------
@bot.tree.command(name="play", description="Играть трек: ссылка YouTube/Spotify, плейлист или название")
@app_commands.describe(query="Ссылка (YouTube/Spotify) или название трека")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()  # подтверждаем сразу (разбор может занять >3 сек)

    user = interaction.user
    if not isinstance(user, discord.Member) or user.voice is None or user.voice.channel is None:
        await interaction.followup.send("⚠️ Сначала зайди в голосовой канал.")
        return

    guild = interaction.guild
    target = user.voice.channel
    try:
        if guild.voice_client is None:
            await target.connect()
        elif guild.voice_client.channel != target:
            await guild.voice_client.move_to(target)
    except discord.Forbidden:
        await interaction.followup.send("❌ Нет прав подключиться к этому каналу.")
        return
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка подключения: {e}")
        return

    p = get_player(guild)
    p.text_channel = interaction.channel
    p.leaving = False

    try:
        tracks, playlist_title = await build_tracks(query, user)
    except Exception as e:
        await interaction.followup.send(f"❌ Не удалось разобрать запрос: {e}")
        return
    if not tracks:
        await interaction.followup.send("❌ Ничего не нашёл по запросу.")
        return

    for t in tracks:
        p.queue.append(t)

    if playlist_title:
        await interaction.followup.send(f"➕ Добавлено **{len(tracks)}** трек(ов): «{playlist_title}».")
    elif len(tracks) > 1:
        await interaction.followup.send(f"➕ Добавлено **{len(tracks)}** трек(ов) в очередь.")
    else:
        await interaction.followup.send(f"➕ В очередь: **{tracks[0].title}**")

    vc = guild.voice_client
    if vc and not vc.is_playing() and not vc.is_paused():
        await play_next(guild)


@bot.tree.command(name="queue", description="Показать очередь треков")
@app_commands.guild_only()
async def queue_cmd(interaction: discord.Interaction):
    p = get_player(interaction.guild)
    lines = []
    if p.current:
        lines.append(f"▶️ **Сейчас:** {p.current.title}  ({fmt_dur(p.current.duration)})")
    if p.queue:
        lines.append("**Очередь:**")
        for i, t in enumerate(list(p.queue)[:10], 1):
            lines.append(f"`{i}.` {t.title}  ({fmt_dur(t.duration)})")
        if len(p.queue) > 10:
            lines.append(f"…и ещё {len(p.queue) - 10}")
    await interaction.response.send_message("\n".join(lines) if lines else "Очередь пуста.")


@bot.tree.command(name="skip", description="Пропустить текущий трек")
@app_commands.guild_only()
async def skip_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭️ Пропустил.")
    else:
        await interaction.response.send_message("Нечего пропускать.")


@bot.tree.command(name="pause", description="Поставить на паузу")
@app_commands.guild_only()
async def pause_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Пауза.")
    else:
        await interaction.response.send_message("Сейчас ничего не играет.")


@bot.tree.command(name="resume", description="Продолжить воспроизведение")
@app_commands.guild_only()
async def resume_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Продолжаю.")
    else:
        await interaction.response.send_message("Нечего возобновлять.")


@bot.tree.command(name="stop", description="Остановить, очистить очередь и выйти")
@app_commands.guild_only()
async def stop_cmd(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await leave_and_cleanup(interaction.guild)
        await interaction.response.send_message("⏹️ Остановил, очистил очередь и вышел.")
    else:
        await interaction.response.send_message("Я и так не в голосовом канале.")


# --------------------------------------------------------------------------
# Авто-выход, если в канале не осталось людей
# --------------------------------------------------------------------------
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    guild = member.guild
    vc = guild.voice_client
    if not vc:
        return
    humans = [m for m in vc.channel.members if not m.bot]
    if not humans:
        schedule_disconnect(guild)
    elif vc.is_playing() or vc.is_paused():
        cancel_disconnect(guild)


# --------------------------------------------------------------------------
# Обработчик ошибок слэш-команд
# --------------------------------------------------------------------------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = f"❌ Ошибка: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass
    print("Ошибка команды:", error)


if __name__ == "__main__":
    bot.run(TOKEN)
