import asyncio
import json
import os
import random
import re
import signal
import sys
import subprocess
import time
import traceback
import urllib.request

import discord
import yt_dlp
from bs4 import BeautifulSoup
from discord.ext import commands, tasks
from discord.ui import Button, Modal, Select, TextInput, View, button
from dotenv import load_dotenv

# --- ENVIRONMENT ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID") or 947335829305577492)
PREFIX = os.getenv("COMMAND_PREFIX", "!")
CONFIG_FILE = os.path.join(BASE_DIR, "server_config.json")
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")
RESTART_FILE = os.path.join(BASE_DIR, ".restart_channel")
RESUME_FILE = os.path.join(BASE_DIR, ".resume_state.json")

if not TOKEN:
    raise ValueError("CRITICAL: DISCORD_TOKEN is missing from .env file!")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, heartbeat_timeout=60)

# key: (label, description, emoji, ffmpeg -af filter)
AUDIO_EFFECTS = {
    "flat": ("Flat / Normal", "Original studio master", None, ""),
    "bassboost": ("Bass Boost (+10dB)", "Enhanced punchy low-end", None, "bass=g=10:f=100"),
    "lofi": ("Lo-Fi (Modern Rich Bass)", "Warm modern sound with enhanced low-end", None, "bass=g=8:f=80,lowpass=f=6000,highpass=f=100"),
    "vaporwave": ("Vaporwave", "Slowed down & deep resonance", None, "asetrate=48000*0.85,aresample=48000"),
    "8d": ("8D Spatial Audio (Deep Bass)", "Spherical binaural audio orbit", None,
           "apulsator=hz=0.08:amount=0.85,extrastereo=m=1.5,aecho=0.8:0.8:60:0.35,bass=g=7.5:f=90"),
}
QUALITY = {
    "max": ("Max Quality (48 kHz, top Discord bitrate)", "Best source audio at the channel's max bitrate", "💎"),
    "low": ("Low Quality (64 kbps)", "Data saver profile", "📶"),
    "medium": ("Medium Quality (96 kbps)", "Balanced sweet spot", "📻"),
    "high": ("High Quality (128 kbps)", "Standard Discord bitrate", "🎧"),
}
QUALITY_KBPS = {"low": 64, "medium": 96, "high": 128}

def quality_bitrate(guild, quality):
    """Opus bitrate (kbps) sent to Discord; Max uses the voice channel's own limit."""
    if quality in QUALITY_KBPS:
        return QUALITY_KBPS[quality]
    vc = guild.voice_client
    channel_kbps = getattr(getattr(vc, "channel", None), "bitrate", 384000) // 1000
    return max(16, min(512, channel_kbps))
SAVE_INTERVAL = 15  # seconds between resume-file writes while a song plays
STANDBY_TITLE = "Music Lounge — Ready to Play"
EXTERNAL_DOMAINS = ("open.spotify.com", "music.apple.com", "deezer.com")
FFMPEG_BEFORE = ("-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                 "-probesize 32M -analyzeduration 10M -rw_timeout 15000000")
VOTES = {"skip": ("Skip Track", discord.Color.blue()), "stop": ("Stop Playback", discord.Color.dark_red())}

# --- PERSISTENT SERVER CONFIG ---

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

server_config = load_config()

def get_server_music_channel(guild_id):
    return server_config.get(str(guild_id), {}).get("music_channel_id")

def set_server_music_channel(guild_id, channel_id):
    server_config.setdefault(str(guild_id), {})["music_channel_id"] = int(channel_id)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(server_config, f, indent=4)
    os.replace(tmp, CONFIG_FILE)  # never leaves a half-written config behind

# --- RESTART HELPERS ---

def record_restart_channel(channel_id, user_id, message_id=None):
    """Saves channel, user, and notice message ID, ensuring immediate disk sync."""
    try:
        with open(RESTART_FILE, "w", encoding="utf-8") as f:
            f.write(f"{channel_id},{user_id},{message_id or 0}")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"Error saving restart file: {e}")

def fetch_and_clear_restart_channel():
    """Retrieves restart metadata and clears the temp file."""
    if os.path.exists(RESTART_FILE):
        try:
            with open(RESTART_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            os.remove(RESTART_FILE)
            parts = content.split(",")
            if len(parts) >= 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
                cid = int(parts[0].strip())
                uid = int(parts[1].strip())
                mid = int(parts[2].strip()) if len(parts) >= 3 and parts[2].strip().isdigit() else None
                return cid, uid, mid
        except Exception as e:
            print(f"Error reading restart file: {e}")
    return None, None, None

is_restarting = False

def snapshot_session(guild):
    """What a guild is playing right now, in a JSON-safe form (None if nothing to resume)."""
    state = get_player(guild.id)
    vc = guild.voice_client
    voice_id = (vc.channel.id if vc and vc.channel else None) or state.voice_channel_id
    if not state.current or not voice_id:
        return None
    return {
        "voice_channel_id": voice_id,
        "position": elapsed(state),
        "tracks": [{"url": t.url, "requester": t.requester, "requester_id": t.requester_id}
                   for t in [state.current] + list(state.queue)],
        "volume": state.volume,
        "effect": state.current_effect,
        "quality": state.current_quality,
        "autoplay": state.autoplay,
        "shuffle": state.shuffle,
    }

def load_saved_sessions():
    try:
        with open(RESUME_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

saved_sessions = load_saved_sessions()

def write_saved_sessions():
    try:
        tmp = RESUME_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(saved_sessions, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, RESUME_FILE)
    except Exception as e:
        print(f"Error saving resume state: {e}")

def save_session(guild):
    """Keeps the last song/position on disk so a restart (or crash) can pick it back up."""
    snap = snapshot_session(guild)
    if snap:
        get_player(guild.id).last_save = time.time()
        saved_sessions[str(guild.id)] = snap
        write_saved_sessions()

def clear_session(guild):
    if is_restarting:
        return
    if saved_sessions.pop(str(guild.id), None) is not None:
        write_saved_sessions()

async def leave_voice_for_restart():
    """Saves what each guild is playing, removes the player cards and leaves voice."""
    global is_restarting
    is_restarting = True
    for guild in bot.guilds:
        save_session(guild)
    print(f"[restart] Saved playback for {len(saved_sessions)} server(s): "
          f"{[(g, v['voice_channel_id']) for g, v in saved_sessions.items()]}")
    for guild in bot.guilds:
        state = get_player(guild.id)
        vc = guild.voice_client
        state.is_stopped = True
        state.play_id += 1
        card, panel, controls = state.progress_message, state.last_message, state.controls_message
        state.progress_message = state.last_message = state.controls_message = None
        await safe_delete(card)
        await safe_delete(panel)
        await safe_delete(controls)
        status_channel = (vc.channel.id if vc and vc.channel else None) or state.voice_channel_id
        if state.current and status_channel:
            await set_voice_status(status_channel, "")
        if vc:
            try:
                vc.stop()
                await vc.disconnect(force=True)
            except Exception:
                pass

async def clean_stale_player_messages(guild):
    """Removes player cards/standby cards left behind by the previous bot process."""
    ch = await music_channel(guild)
    if not ch:
        return
    def is_stale(m):
        return m.author.id == bot.user.id and "the bot is online" not in (m.content or "")
    try:
        if ch.permissions_for(guild.me).manage_messages:
            await ch.purge(limit=200, check=is_stale)
        else:
            async for msg in ch.history(limit=200):
                if is_stale(msg):
                    await safe_delete(msg)
    except Exception as e:
        print(f"Startup cleanup error in {guild.name}: {e}")

async def connect_with_retry(guild, channel, attempts=4):
    for attempt in range(attempts):
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
            return True
        try:
            if vc:
                await vc.disconnect(force=True)
            await channel.connect(self_deaf=True, timeout=30)
        except Exception as e:
            print(f"[resume] Voice connect attempt {attempt + 1} to {channel.name} failed: {e!r}")
            await asyncio.sleep(3)
    vc = guild.voice_client
    return bool(vc and vc.is_connected())

async def resume_saved_session(gid, info):
    guild = bot.get_guild(int(gid))
    if not guild or not info.get("tracks"):
        return
    channel = guild.get_channel(info["voice_channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(info["voice_channel_id"])
        except Exception as e:
            return print(f"[resume] Voice channel {info['voice_channel_id']} not found in {guild.name}: {e!r}")

    first, rest = info["tracks"][0], info["tracks"][1:]
    print(f"[resume] {guild.name}: loading last song {first['url']} at {fmt_time(info.get('position', 0))}")
    try:
        track = await Track.fetch(first["url"], first.get("requester", "Unknown"), first.get("requester_id"))
    except Exception as e:
        return print(f"[resume] Could not load last song {first['url']}: {e!r}")

    if not await connect_with_retry(guild, channel):
        return print(f"[resume] Could not rejoin voice channel {channel.name}")

    state = get_player(guild.id)
    state.voice_channel_id = channel.id
    state.volume = info.get("volume", state.volume)
    state.current_effect = info.get("effect", state.current_effect)
    state.current_quality = info.get("quality", state.current_quality)
    if state.current_quality not in QUALITY:
        state.current_quality = "max"
    state.autoplay = info.get("autoplay", False)
    state.shuffle = info.get("shuffle", False)
    await start_playback(guild, track, start=info.get("position", 0))
    print(f"[resume] Rejoined {channel.name} and resumed {track.title}")

    # Load the rest of the queue after playback has started
    results = await asyncio.gather(
        *(Track.fetch(t["url"], t.get("requester", "Unknown"), t.get("requester_id")) for t in rest),
        return_exceptions=True)
    for t, res in zip(rest, results):
        if isinstance(res, Exception):
            print(f"[resume] Could not load queued {t.get('url')}: {res!r}")
        else:
            state.queue.append(res)

async def resume_after_restart():
    """Rejoins the voice channel the bot was in before restarting and continues the last saved song."""
    sessions = dict(saved_sessions)
    print(f"[resume] Found {len(sessions)} saved session(s)")
    await asyncio.gather(*(resume_saved_session(gid, info) for gid, info in sessions.items()),
                         return_exceptions=True)

def execute_restart():
    """Spawns fresh process with exact working directory, bypassing asyncio cancellation."""
    python = sys.executable
    script = os.path.abspath(__file__)
    args = [python, script] + sys.argv[1:]
    subprocess.Popen(args, cwd=BASE_DIR)
    os._exit(0)

# --- SMALL HELPERS ---

async def music_channel(guild):
    cid = get_server_music_channel(guild.id)
    if not cid:
        return None
    if ch := guild.get_channel(cid):
        return ch
    try:
        return await bot.fetch_channel(cid)
    except Exception:
        return None

async def safe_delete(msg):
    if msg:
        try:
            await msg.delete()
        except Exception:
            pass

def active(vc):
    return bool(vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()))

def listeners_of(vc):
    return [m for m in vc.channel.members if not m.bot]

def fmt_time(seconds):
    m, s = divmod(int(seconds or 0), 60)
    return f"{m}:{s:02d}"

def elapsed(state):
    """Current playback position in seconds (pause-aware)."""
    if not state.start_timestamp:
        return 0
    now = state.paused_at or time.time()
    return max(0, int(now - state.start_timestamp) + state.current_position)

def pause_playback(state, vc):
    if vc and vc.is_playing():
        vc.pause()
        state.paused_at = time.time()

def resume_playback(state, vc):
    if vc and vc.is_paused():
        vc.resume()
        if state.paused_at:
            state.start_timestamp += time.time() - state.paused_at
        state.paused_at = 0

async def check_vc(interaction) -> bool:
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        msg = "❌ The bot is not connected to voice! Tap ▶️ Tap to Play to start."
    elif not interaction.user.voice or interaction.user.voice.channel != vc.channel:
        msg = f"❌ You must be in {vc.channel.mention} with the bot to use controls!"
    else:
        return True
    await interaction.response.send_message(msg, ephemeral=True)
    return False

async def restart_bot_process(interaction):
    await interaction.response.send_message("🔄 Restarting bot... Please hold on a few seconds.", ephemeral=False)
    try:
        orig_msg = await interaction.original_response()
        msg_id = orig_msg.id
    except Exception:
        orig_msg = None
        msg_id = None

    record_restart_channel(interaction.channel_id, interaction.user.id, msg_id)
    await leave_voice_for_restart()

    await asyncio.sleep(1.0)
    if orig_msg:
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

    execute_restart()

# --- YT-DLP ---

has_cookies = os.path.isfile(COOKIE_FILE)
print("[*] cookies.txt detected. Format 141 (256k AAC) enabled." if has_cookies
      else "[!] cookies.txt not found. Falling back to default best audio.")

YTDL_OPTIONS = {
    "format": "141/bestaudio[ext=m4a]/bestaudio/best",
    "cookiefile": COOKIE_FILE if has_cookies else None,
    "noplaylist": True,
    "nocheckcertificate": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "socket_timeout": 15,
}
ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
ytdl_flat = yt_dlp.YoutubeDL({**YTDL_OPTIONS, "extract_flat": True})

async def extract(query):
    data = await asyncio.to_thread(ytdl.extract_info, query, download=False)
    return data["entries"][0] if data.get("entries") else data

def ffmpeg_options(effect="flat", start=0):
    af = AUDIO_EFFECTS.get(effect, AUDIO_EFFECTS["flat"])[3]
    opts = "-vn" + (f' -af "{af}"' if af else "") + " -ar 48000 -ac 2"
    # -ss before -i seeks in the stream itself, so +10s / -10s / resume jump instantly
    before = FFMPEG_BEFORE + (f" -ss {int(start)}" if start > 0 else "")
    return {"before_options": before, "options": opts}

# --- TRACK ---

class Track:
    def __init__(self, data, requester="Autoplay", requester_id=None):
        self.title = data.get("title", "Unknown Track")
        self.url = (data.get("webpage_url") or data.get("original_url")
                    or f"https://www.youtube.com/watch?v={data.get('id', '')}")
        self.stream_url = self.best_audio_url(data)
        self.thumbnail = data.get("thumbnail")
        self.duration = data.get("duration") or 0
        self.uploader = data.get("uploader") or data.get("channel") or "Unknown Artist"
        self.requester, self.requester_id = requester, requester_id

    @staticmethod
    def best_audio_url(info):
        if not info:
            return None
        url = info.get("url")
        if url and "manifest" not in url:
            return url
        audio = [f["url"] for f in info.get("formats", [])
                 if f.get("url") and (f.get("acodec") != "none" or f.get("vcodec") == "none")]
        return audio[-1] if audio else url

    async def ensure_stream_url(self):
        m = re.search(r"expire[=/](\d+)", self.stream_url or "")
        if not self.stream_url or (m and int(m[1]) - time.time() < 300):
            self.stream_url = self.best_audio_url(await extract(self.url)) or self.stream_url
        return self.stream_url

    @staticmethod
    def resolve_external_link(url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                soup = BeautifulSoup(r.read(), "html.parser")
            og_title = soup.find("meta", property="og:title")
            og_desc = soup.find("meta", property="og:description")
            title = (og_title and og_title.get("content")) or (soup.title and soup.title.string) or ""
            if title:
                title = re.sub(r" (?:\| Spotify|on Apple Music|\| Listen on Deezer)$", "", title)
                desc = (og_desc and og_desc.get("content")) or ""
                return f"{desc.split('·')[1].strip()} - {title}" if "Song ·" in desc else title
        except Exception as e:
            print(f"Metadata extraction error: {e}")
        return url

    @classmethod
    async def fetch(cls, query, requester="Unknown", requester_id=None):
        if any(d in query for d in EXTERNAL_DOMAINS):
            query = f"ytsearch:{await asyncio.to_thread(cls.resolve_external_link, query)}"
        elif not query.startswith("http"):
            query = f"ytsearch:{query}"
        return cls(await extract(query), requester, requester_id)

    @classmethod
    async def fetch_recommendation(cls, current, played):
        for q in (f"{current.uploader} song", f"{current.title} mix"):
            try:
                data = await asyncio.to_thread(ytdl_flat.extract_info, f"ytsearch5:{q}", download=False)
                for e in data.get("entries") or []:
                    t = e and e.get("title")
                    if t and t != current.title and t not in played:
                        info = await extract(f"https://www.youtube.com/watch?v={e['id']}")
                        return cls(info, "📻 Autoplay")
            except Exception:
                continue
        return None

# --- PER-GUILD STATE ---

class GuildMusicState:
    def __init__(self):
        self.queue, self.played_history = [], set()
        self.current = self.last_message = self.standby_message = self.active_vote_view = None
        self.controls_message = None  # Settings buttons, shown below the now-playing card
        self.autoplay = self.is_stopped = self.is_rendering = False
        self.volume = 0.5
        self.current_effect, self.current_quality = "flat", "max"
        self.start_timestamp = self.current_position = self.play_id = 0
        self.votes = {"skip": set(), "stop": set()}
        self.embed_lock = asyncio.Lock()
        self.progress_message = None  # Separate progress bar message
        self.progress_view = None  # Progress bar controls view
        self.history = []  # Previously played tracks (for the Previous button)
        self.going_back = False
        self.shuffle = False
        self.paused_at = 0
        self.sleep_minutes = 0
        self.sleep_until = 0
        self.voice_channel_id = None  # Last voice channel the player joined
        self.wiping = False  # True while !sus is clearing the music lounge
        self.last_save = 0  # last time the resume file was written

music_states = {}

# user_id -> last time we DM'd them a "text commands are disabled" notice (stops DM spam)
dm_notice_times = {}
DM_NOTICE_COOLDOWN = 30

def get_player(guild_id):
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]

# --- MODALS ---

class BugReportModal(Modal, title="Report a Bug"):
    bug_title = TextInput(label="Issue Summary", placeholder="Briefly state what went wrong...", max_length=100)
    bug_desc = TextInput(label="Detailed Description", placeholder="Describe the issue...",
                         style=discord.TextStyle.paragraph, max_length=1500)

    async def on_submit(self, interaction):
        send = interaction.response.send_message
        if not OWNER_ID:
            return await send("❌ OWNER_ID misconfigured in .env.", ephemeral=True)
        embed = discord.Embed(title="💬 In-Discord Bot Bug Report", description=f"**Summary:** {self.bug_title.value}",
                              color=discord.Color.red())
        embed.add_field(name="Description", value=self.bug_desc.value, inline=False)
        embed.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)")
        embed.add_field(name="Server", value=f"{interaction.guild.name} (`{interaction.guild_id}`)")
        try:
            owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
            await owner.send(embed=embed)
        except discord.HTTPException:
            return await send("⚠️ Failed to DM developer.", ephemeral=True)
        await send("✅ Bug report sent to developer's DMs!", ephemeral=True)

class SongModal(Modal, title="Request a Track"):
    song_query = TextInput(label="Song Name or URL", placeholder="Enter song title or link...")

    async def on_submit(self, interaction):
        if not interaction.user.voice:
            return await interaction.response.send_message("⚠️ Join a voice channel first!", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await process_play_request(interaction.guild, interaction.channel, interaction.user, self.song_query.value)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

# --- STANDBY CARD ---

class StandbyView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="▶️ Tap to Play", style=discord.ButtonStyle.success, emoji="🎶", row=0)
    async def play_btn(self, interaction, _):
        await interaction.response.send_modal(SongModal())

    @button(label="🐛 Report Bug", style=discord.ButtonStyle.secondary, emoji="🛠️", row=0)
    async def report_btn(self, interaction, _):
        await interaction.response.send_modal(BugReportModal())

    @button(label="🔄 Restart", style=discord.ButtonStyle.danger, emoji="⚡", row=0)
    async def restart_btn(self, interaction, _):
        await restart_bot_process(interaction)

async def ensure_standby_embed(guild):
    state = get_player(guild.id)
    channel = await music_channel(guild)
    if not channel:
        return

    if state.wiping:
        return
    async with state.embed_lock:
        if active(guild.voice_client) and state.current:
            await safe_delete(state.standby_message)
            state.standby_message = None
            return

        await safe_delete(state.last_message)
        await safe_delete(state.controls_message)
        state.last_message = state.controls_message = None

        if state.standby_message:
            try:
                await channel.fetch_message(state.standby_message.id)
                return
            except Exception:
                state.standby_message = None

        found_standby = False
        async for msg in channel.history(limit=25):
            if msg.author.id == bot.user.id:
                if msg.content and "Restarting bot" in msg.content:
                    await safe_delete(msg)
                    continue

                if msg.embeds and STANDBY_TITLE in (msg.embeds[0].title or ""):
                    if not found_standby:
                        try:
                            await msg.edit(view=StandbyView())
                            state.standby_message = msg
                            found_standby = True
                        except Exception:
                            pass
                    else:
                        await safe_delete(msg)
        
        if found_standby and state.standby_message:
            return

        embed = discord.Embed(
            title=f"🎵 {STANDBY_TITLE}",
            description=("**How to play:**\n"
                         "1. Join any voice channel in this server.\n"
                         "2. Tap **`▶️ Tap to Play`** below to search and play a song!"),
            color=discord.Color.teal())
        try:
            state.standby_message = await channel.send(embed=embed, view=StandbyView())
        except Exception as e:
            print(f"Error sending standby embed: {e}")

# --- VOTING ---

class PublicVoteView(View):
    def __init__(self, guild, kind, timeout=30):
        super().__init__(timeout=timeout)
        self.guild, self.kind, self.message = guild, kind, None

    @button(label="🗳️ Cast Vote (1/2)", style=discord.ButtonStyle.primary, row=0)
    async def cast_vote(self, interaction, btn):
        if not await check_vc(interaction):
            return
        state = get_player(self.guild.id)
        votes = state.votes[self.kind]
        if interaction.user.id in votes:
            return await interaction.response.send_message("⚠️ You have already cast your vote!", ephemeral=True)

        votes.add(interaction.user.id)
        vc = self.guild.voice_client
        count, required = len(votes), len(listeners_of(vc)) // 2 + 1
        btn.label = f"🗳️ Cast Vote ({count}/{required})"

        if count < required:
            embed = interaction.message.embeds[0]
            embed.set_field_at(0, name="📊 Progress", value=f"`{count} / {required}` votes needed")
            return await interaction.response.edit_message(embed=embed, view=self)

        self.stop()
        votes.clear()
        state.active_vote_view = None
        await interaction.response.edit_message(
            content=None, view=None,
            embed=discord.Embed(title=f"✅ Vote {self.kind.title()} Passed!", color=discord.Color.green(),
                                description=f"Received **{count}/{required}** votes. Executing..."))
        await asyncio.sleep(2)
        await safe_delete(self.message)
        if self.kind == "skip":
            await interaction.channel.send("⏳ **Skipping track... Finding next song!**", delete_after=5)
            vc.stop()
        else:
            await stop_player(self.guild, reason="stop vote passed")

    async def on_timeout(self):
        state = get_player(self.guild.id)
        state.votes[self.kind].clear()
        state.active_vote_view = None
        if self.message:
            try:
                await self.message.edit(content=None, view=None, embed=discord.Embed(
                    title=f"⏱️ Vote {self.kind.title()} Expired", color=discord.Color.greyple(),
                    description="Vote timed out without reaching the required threshold."))
                await asyncio.sleep(4)
                await self.message.delete()
            except Exception:
                pass

async def handle_vote_request(interaction, kind):
    if not await check_vc(interaction):
        return
    guild, user = interaction.guild, interaction.user
    vc, state = guild.voice_client, get_player(guild.id)
    cur = state.current
    if not (vc.is_playing() or vc.is_paused() or (kind == "stop" and cur)):
        return await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)

    listeners = listeners_of(vc)
    required = len(listeners) // 2 + 1
    if (len(listeners) <= 1 or user.guild_permissions.manage_channels
            or (kind == "skip" and cur and cur.requester_id == user.id)):
        state.votes[kind].clear()
        await interaction.response.defer()
        if kind == "skip":
            await interaction.channel.send("⏳ **Skipping track... Finding next song!**", delete_after=5)
            return vc.stop()
        await interaction.channel.send("⏹️ **Playback stopped.**", delete_after=5)
        return await stop_player(guild, reason=f"Stop button pressed by {user}")

    if state.active_vote_view and state.active_vote_view.kind == kind:
        return await interaction.response.send_message(f"🗳️ A {kind} vote is already active!", ephemeral=True)

    state.votes[kind].clear()
    state.votes[kind].add(user.id)
    view = PublicVoteView(guild, kind)
    view.cast_vote.label = f"🗳️ Cast Vote (1/{required})"

    label, color = VOTES[kind]
    what = ((f"skip **[{cur.title}]({cur.url})**" if cur else "skip the current song")
            if kind == "skip" else "stop music")
    embed = discord.Embed(
        title=f"🗳️ Vote to {label}", color=color,
        description=f"{user.mention} wants to {what}!\nListeners in {vc.channel.mention}, tap below to cast your vote.")
    embed.add_field(name="📊 Progress", value=f"`1 / {required}` votes needed")
    embed.add_field(name="👥 Listeners", value=f"`{len(listeners)}` active")
    embed.set_footer(text="Vote closes in 30s • Voice members only")

    await interaction.response.defer()
    view.message = await interaction.channel.send(content=f"📢 A {kind} vote has been started!", embed=embed, view=view)
    state.active_vote_view = view

# --- PLAYER CONTROLS ---

SLEEP_STEPS = (0, 15, 30, 60)

class ProgressBarControls(View):
    """Spotify-style transport row: previous, play/pause, next, sleep timer, quality."""

    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild
        self.refresh(get_player(guild.id), guild.voice_client)

    def refresh(self, state, vc):
        paused = bool(vc and vc.is_paused())
        self.play_pause.emoji = "▶️" if paused else "⏸️"
        self.shuffle_btn.style = discord.ButtonStyle.success if state.shuffle else discord.ButtonStyle.secondary
        q = QUALITY.get(state.current_quality, QUALITY["max"])
        self.quality_btn.label, self.quality_btn.emoji = q[0], q[2]
        self.effect_btn.label = AUDIO_EFFECTS.get(state.current_effect, AUDIO_EFFECTS["flat"])[0]
        if state.sleep_minutes:
            left = max(0, int((state.sleep_until - time.time()) // 60) + 1)
            self.sleep_btn.label = f"{left}m"
            self.sleep_btn.style = discord.ButtonStyle.success
        else:
            self.sleep_btn.label = None
            self.sleep_btn.style = discord.ButtonStyle.secondary

    async def _redraw(self, interaction):
        state = get_player(interaction.guild_id)
        self.refresh(state, interaction.guild.voice_client)
        if not state.current:
            return await interaction.response.defer()
        async with state.embed_lock:
            await interaction.response.edit_message(embed=build_progress_bar_message(self.guild, state.current), view=self)

    async def on_error(self, interaction, error, item):
        print(f"Player card button error ({getattr(item, 'custom_id', item)}): {error}")
        traceback.print_exception(type(error), error, error.__traceback__)
        if not interaction.response.is_done():
            await interaction.response.defer()

    @button(emoji="🔀", style=discord.ButtonStyle.secondary, row=1, custom_id="kirito:shuffle")
    async def shuffle_btn(self, interaction, _):
        if not await check_vc(interaction):
            return
        state = get_player(interaction.guild_id)
        state.shuffle = not state.shuffle
        if state.shuffle:
            random.shuffle(state.queue)
        await self._redraw(interaction)

    @button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0, custom_id="kirito:previous")
    async def previous_btn(self, interaction, _):
        if not await check_vc(interaction):
            return
        state, vc = get_player(interaction.guild_id), interaction.guild.voice_client
        # Like Spotify: restart the song if past 5s, otherwise go to the previous track
        if elapsed(state) > 5 or not state.history:
            await interaction.response.defer()
            return await restart_at(interaction.guild, -elapsed(state))
        await interaction.response.defer()
        prev = state.history.pop()
        if state.current:
            state.queue.insert(0, state.current)
        state.queue.insert(0, prev)
        state.going_back = True
        state.paused_at = 0
        vc.stop()

    @button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0, custom_id="kirito:playpause")
    async def play_pause(self, interaction, _):
        if not await check_vc(interaction):
            return
        state, vc = get_player(interaction.guild_id), interaction.guild.voice_client
        if vc.is_playing():
            pause_playback(state, vc)
        elif vc.is_paused():
            resume_playback(state, vc)
        await self._redraw(interaction)

    @button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0, custom_id="kirito:next")
    async def next_btn(self, interaction, _):
        await handle_vote_request(interaction, "skip")

    @button(emoji="⏱️", style=discord.ButtonStyle.secondary, row=0, custom_id="kirito:sleep")
    async def sleep_btn(self, interaction, _):
        if not await check_vc(interaction):
            return
        state = get_player(interaction.guild_id)
        idx = SLEEP_STEPS.index(state.sleep_minutes) if state.sleep_minutes in SLEEP_STEPS else 0
        state.sleep_minutes = SLEEP_STEPS[(idx + 1) % len(SLEEP_STEPS)]
        state.sleep_until = time.time() + state.sleep_minutes * 60 if state.sleep_minutes else 0
        await self._redraw(interaction)

    @button(label="-10s", emoji="⏪", style=discord.ButtonStyle.secondary, row=1, custom_id="kirito:back10")
    async def seek_backward(self, interaction, _):
        if await check_vc(interaction):
            await handle_seek(interaction, -10)

    @button(label="+10s", emoji="⏩", style=discord.ButtonStyle.secondary, row=1, custom_id="kirito:fwd10")
    async def seek_forward(self, interaction, _):
        if await check_vc(interaction):
            await handle_seek(interaction, 10)

    @button(label="Vol -", emoji="🔉", style=discord.ButtonStyle.secondary, row=1, custom_id="kirito:voldown")
    async def vol_down(self, interaction, _):
        await self._volume(interaction, -0.10)

    @button(label="Vol +", emoji="🔊", style=discord.ButtonStyle.secondary, row=1, custom_id="kirito:volup")
    async def vol_up(self, interaction, _):
        await self._volume(interaction, 0.10)

    async def _volume(self, interaction, delta):
        state = get_player(interaction.guild_id)
        state.volume = round(min(1.0, max(0.0, state.volume + delta)), 2)
        src = getattr(interaction.guild.voice_client, "source", None)
        if isinstance(src, discord.PCMVolumeTransformer):
            src.volume = state.volume
        await self._redraw(interaction)
        await refresh_settings_panel(self.guild)

    @button(label="Report Bug", emoji="🛠️", style=discord.ButtonStyle.secondary, row=2, custom_id="kirito:report")
    async def report_btn(self, interaction, _):
        await interaction.response.send_modal(BugReportModal())

    @button(label="Restart", emoji="🔄", style=discord.ButtonStyle.danger, row=2, custom_id="kirito:restart")
    async def restart_btn(self, interaction, _):
        await restart_bot_process(interaction)

    async def _cycle(self, interaction, attr, presets):
        if not await check_vc(interaction):
            return
        guild, state = interaction.guild, get_player(interaction.guild_id)
        keys = list(presets)
        cur = getattr(state, attr)
        setattr(state, attr, keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else keys[0])
        await self._redraw(interaction)
        await refresh_settings_panel(guild)
        if attr == "current_quality":
            encoder = getattr(guild.voice_client, "encoder", None)
            if encoder:
                encoder.set_bitrate(quality_bitrate(guild, state.current_quality))
        elif state.current and active(guild.voice_client):
            await restart_at(guild)

    @button(label="Max Quality (48 kHz, top Discord bitrate)", emoji="💎", style=discord.ButtonStyle.secondary, row=0, custom_id="kirito:quality")
    async def quality_btn(self, interaction, _):
        await self._cycle(interaction, "current_quality", QUALITY)

    @button(label="Flat / Normal", emoji="🎛️", style=discord.ButtonStyle.secondary, row=2, custom_id="kirito:effect")
    async def effect_btn(self, interaction, _):
        await self._cycle(interaction, "current_effect", AUDIO_EFFECTS)

class MusicControls(View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild
        state = get_player(guild.id)
        self.update_autoplay_button(state)

    def update_autoplay_button(self, state):
        self.autoplay_btn.label = f"📻 Autoplay: {'ON' if state.autoplay else 'OFF'}"
        self.autoplay_btn.style = discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary

    @button(label="📻 Autoplay: OFF", style=discord.ButtonStyle.secondary, row=0)
    async def autoplay_btn(self, interaction, _):
        if not await check_vc(interaction):
            return
        state = get_player(interaction.guild_id)
        state.autoplay = not state.autoplay
        self.update_autoplay_button(state)
        await interaction.response.edit_message(view=self)
        await refresh_settings_panel(self.guild)

    @button(label="⏹️ Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop(self, interaction, _):
        await handle_vote_request(interaction, "stop")

    @button(label="➕ Add Song", style=discord.ButtonStyle.success, row=3)
    async def add_song_btn(self, interaction, _):
        await interaction.response.send_modal(SongModal())

    @button(label="📜 Queue", style=discord.ButtonStyle.primary, row=3)
    async def view_queue(self, interaction, _):
        await interaction.response.defer()
        await display_queue_embed(interaction.channel, get_player(interaction.guild_id))

# --- PLAYBACK CORE ---

async def stop_player(guild, leave=True, reason="unknown", standby=True):
    state = get_player(guild.id)
    print(f"[card] Player stopped in {guild.name}: {reason}")
    clear_session(guild)
    state.is_stopped, state.autoplay, state.current = True, False, None
    state.history.clear()
    state.sleep_minutes = state.sleep_until = state.paused_at = 0
    state.play_id += 1
    state.queue.clear()
    for v in state.votes.values():
        v.clear()
    if state.active_vote_view:
        await safe_delete(state.active_vote_view.message)
        state.active_vote_view = None
    await safe_delete(state.last_message)
    state.last_message = None
    await safe_delete(state.controls_message)
    state.controls_message = None
    await safe_delete(state.progress_message)
    state.progress_message = None
    if state.progress_view:
        state.progress_view.stop()
        state.progress_view = None

    vc = guild.voice_client
    status_channel = (vc.channel.id if vc and vc.channel else None) or state.voice_channel_id
    if leave and vc:
        try:
            vc.stop()
            await vc.disconnect(force=True)
        except Exception:
            pass
    await clear_song_status(guild, status_channel)
    if standby:
        await ensure_standby_embed(guild)

async def set_voice_status(channel_id, text):
    """Sets the status line shown under the voice channel name (needs Set Voice Channel Status permission)."""
    if not channel_id:
        return
    try:
        route = discord.http.Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel_id)
        await bot.http.request(route, json={"status": text[:500]})
    except Exception as e:
        print(f"[status] Could not set voice channel status: {e}")

async def update_bot_presence():
    """Shows 'Listening to <song>' while anything plays, otherwise clears the activity."""
    playing = [get_player(g.id).current for g in bot.guilds
               if get_player(g.id).current and g.voice_client and g.voice_client.is_connected()]
    try:
        if playing:
            name = f"{playing[-1].title} — {playing[-1].uploader}"[:128]
            await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=name))
        else:
            await bot.change_presence(activity=None)
    except Exception as e:
        print(f"[status] Could not update bot presence: {e}")

async def show_song_status(guild, track):
    state = get_player(guild.id)
    await set_voice_status(state.voice_channel_id, f"🎵 {track.title} — {track.uploader}")
    await update_bot_presence()

async def clear_song_status(guild, channel_id):
    await set_voice_status(channel_id, "")
    await update_bot_presence()

def play_stream(guild, url, start, pid):
    state = get_player(guild.id)
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(url, **ffmpeg_options(state.current_effect, start)), volume=state.volume)

    def after(err):
        if err:
            print(f"Playback error in after_callback: {err}")
        if state.play_id == pid and not state.is_stopped:
            bot.loop.call_soon_threadsafe(play_next, guild)

    state.current_position, state.start_timestamp, state.paused_at = start, time.time(), 0
    guild.voice_client.play(source, after=after, bitrate=quality_bitrate(guild, state.current_quality),
                            signal_type="music", bandwidth="full")

async def restart_at(guild, offset=0):
    state, vc = get_player(guild.id), guild.voice_client
    if not (vc and vc.is_connected() and state.current):
        return
    pos = max(0, elapsed(state) + offset)
    if offset > 0 and state.current.duration and pos >= state.current.duration:
        return
    state.play_id += 1
    pid = state.play_id
    url = await state.current.ensure_stream_url()
    if not url:
        return
    try:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        play_stream(guild, url, pos, pid)
        if state.current:
            await show_now_playing(guild, state.current)
    except Exception as e:
        print(f"Restart error: {e}")

async def handle_seek(interaction, offset):
    await interaction.response.defer()
    await restart_at(interaction.guild, offset)

def _log_task_error(fut):
    if not fut.cancelled() and (err := fut.exception()):
        print(f"[task] Background task failed: {err!r}")

def play_next(guild):
    state = get_player(guild.id)
    if state.is_stopped:
        return

    def go(coro):
        fut = asyncio.run_coroutine_threadsafe(coro, bot.loop)
        fut.add_done_callback(_log_task_error)

    if state.active_vote_view:
        go(safe_delete(state.active_vote_view.message))
        state.active_vote_view = None

    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return go(ensure_standby_embed(guild))
    if state.queue:
        idx = random.randrange(len(state.queue)) if state.shuffle and not state.going_back else 0
        go(start_playback(guild, state.queue.pop(idx)))
    elif state.autoplay and state.current:
        go(handle_autoplay(guild))
    else:
        state.current = None
        go(safe_delete(state.last_message))
        go(safe_delete(state.controls_message))
        state.last_message = state.controls_message = None
        go(clear_song_status(guild, vc.channel.id if vc.channel else state.voice_channel_id))
        go(ensure_standby_embed(guild))

async def start_playback(guild, track, start=0):
    state = get_player(guild.id)
    state.is_stopped = False
    state.play_id += 1
    pid = state.play_id
    for v in state.votes.values():
        v.clear()
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return

    try:
        url = await track.ensure_stream_url()
    except Exception as e:
        print(f"Error extracting stream for {track.title}: {e}")
        url = None
    if not url:
        print(f"Skipping {track.title}: No stream URL")
        await asyncio.sleep(2)
        return play_next(guild)

    if state.current and state.current is not track and not state.going_back:
        state.history = (state.history + [state.current])[-25:]
    state.going_back = False
    state.current = track
    state.played_history.add(track.title)
    try:
        if track.duration and start >= track.duration:
            start = 0
        play_stream(guild, url, start, pid)
        save_session(guild)
        await show_now_playing(guild, track)
        await show_song_status(guild, track)
    except Exception as e:
        print(f"Playback startup error: {e}")
        traceback.print_exc()
        await asyncio.sleep(2)
        play_next(guild)

async def handle_autoplay(guild):
    state = get_player(guild.id)
    if not state.current or state.is_stopped:
        return await ensure_standby_embed(guild)
    if channel := await music_channel(guild):
        try:
            await channel.send("📻 **Autoplay:** Finding next song, hold tight! ⏳", delete_after=5)
        except Exception:
            pass
    rec = await Track.fetch_recommendation(state.current, state.played_history)
    if rec and not state.is_stopped:
        await start_playback(guild, rec)
    else:
        await ensure_standby_embed(guild)

# --- EMBEDS ---

def build_now_playing_embed(guild, track):
    state = get_player(guild.id)
    q = QUALITY.get(state.current_quality, QUALITY["max"])
    embed = discord.Embed(title="🎛️ Player Settings", color=0x1DB954)
    embed.add_field(name="👤 Requested By", value=f"`{track.requester}`", inline=True)
    embed.add_field(name="📻 Autoplay", value=f"`{'ON' if state.autoplay else 'OFF'}`", inline=True)
    embed.add_field(name="📜 Up Next", value=f"`{len(state.queue)} track(s)`", inline=True)
    embed.add_field(name="🔊 Volume", value=f"`{int(state.volume * 100)}%`", inline=True)
    embed.add_field(name="🎛️ Preset", value=f"`{AUDIO_EFFECTS.get(state.current_effect, AUDIO_EFFECTS['flat'])[0]}`", inline=True)
    embed.add_field(name="📶 Quality", value=f"`{q[2]} {q[0]}`", inline=True)
    embed.set_footer(text="Now-playing card below • Buttons at the bottom")
    return embed

BAR_LENGTH = 18
BLANK = "\u2800"  # braille blank, keeps spacing in Discord

def build_progress_bar_message(guild, track):
    """Spotify-style now-playing card: title, artist, progress bar with timestamps."""
    state = get_player(guild.id)
    duration = int(track.duration or 0)
    current = min(elapsed(state), duration) if duration else elapsed(state)

    if duration > 0:
        pos = min(BAR_LENGTH - 1, int(BAR_LENGTH * current / duration))
        bar = "━" * pos + "⚪" + "─" * (BAR_LENGTH - 1 - pos)
        total = fmt_time(duration)
    else:
        bar = "━" * BAR_LENGTH + "⚪"
        total = "LIVE"

    left, right = fmt_time(current), total
    gap = BLANK * max(1, 30 - len(left) - len(right))
    embed = discord.Embed(
        title=track.title[:256],
        url=track.url,
        description=f"{track.uploader}\n\n{bar}\n`{left}`{gap}`{right}`",
        color=0x191414,  # Spotify dark
    )
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    status = "⏸️ Paused" if guild.voice_client and guild.voice_client.is_paused() else "🎵 Now Playing"
    extras = ["🔀 Shuffle" if state.shuffle else None,
              f"⏱️ Sleep in {max(0, int((state.sleep_until - time.time()) // 60) + 1)}m" if state.sleep_minutes else None]
    embed.set_author(name=" • ".join([status] + [e for e in extras if e]))
    return embed

def is_player_card(m):
    """True for a now-playing card posted by this bot (identified by its kirito:* buttons)."""
    if m.author.id != bot.user.id:
        return False
    for row in m.components:
        for c in getattr(row, "children", []):
            if (getattr(c, "custom_id", None) or "").startswith("kirito:"):
                return True
    return False

async def remove_extra_cards(channel, state, limit=50):
    """Deletes every now-playing card in the channel except the active one."""
    keep = state.progress_message.id if state.progress_message else None
    try:
        async for m in channel.history(limit=limit):
            if m.id != keep and is_player_card(m):
                await safe_delete(m)
    except Exception as e:
        print(f"Card cleanup error: {e}")

async def refresh_settings_panel(guild):
    state = get_player(guild.id)
    if state.last_message and state.current:
        try:
            await state.last_message.edit(embed=build_now_playing_embed(guild, state.current))
        except Exception as e:
            print(f"Settings box refresh failed: {e}")

async def show_now_playing(guild, track):
    state = get_player(guild.id)
    channel = await music_channel(guild)
    if not channel:
        return
    async with state.embed_lock:
        state.is_rendering = True
        try:
            await safe_delete(state.standby_message)
            state.standby_message = None
            
            view = MusicControls(guild)
            if state.progress_view is None:
                state.progress_view = ProgressBarControls(guild)
            card_view = state.progress_view
            card_view.refresh(state, guild.voice_client)
            card = build_progress_bar_message(guild, track)

            # Layout, top to bottom: settings box, now-playing card, settings buttons.
            # Once one message has to be (re)sent, everything below it is re-sent too so the order holds.
            embed = build_now_playing_embed(guild, track)
            layout = [("last_message", {"embed": embed, "view": None}),
                      ("progress_message", {"content": None, "embed": card, "view": card_view}),
                      ("controls_message", {"view": view})]
            resend, prev_id = False, 0
            for attr, kwargs in layout:
                msg = getattr(state, attr)
                if msg and (resend or msg.id < prev_id):
                    await safe_delete(msg)
                    msg = None
                if msg:
                    try:
                        await msg.edit(**kwargs)
                    except discord.NotFound:
                        msg = None
                    except Exception as e:
                        print(f"Edit of {attr} failed, keeping it: {e}")
                if not msg:
                    resend = True
                    if attr == "progress_message":
                        setattr(state, attr, None)
                        await remove_extra_cards(channel, state)
                    send_kwargs = {k: v for k, v in kwargs.items() if v is not None}
                    msg = await channel.send(**send_kwargs)
                setattr(state, attr, msg)
                prev_id = msg.id
        except Exception as e:
            print(f"Error sending now playing embed: {e}")
        finally:
            state.is_rendering = False

async def display_queue_embed(channel, state):
    cur = state.current
    embed = discord.Embed(title="📑 Playback Queue", color=discord.Color.gold())
    embed.description = (
        f"**▶️ Now Playing:**\n[{cur.title}]({cur.url}) `({fmt_time(cur.duration)})`\n\n"
        f"**📋 Up Next ({len(state.queue)} tracks):**" if cur else "Nothing is currently playing.")
    lines = "\n".join(f"`{i}.` [{t.title[:40]}]({t.url})" for i, t in enumerate(state.queue[:10], 1))
    embed.add_field(name="Next Tracks" if state.queue else "Upcoming", value=lines or "*Queue is empty!*", inline=False)
    embed.set_footer(text="Auto-destructs in 60s.")
    try:
        await channel.send(embed=embed, delete_after=60)
    except Exception:
        pass

# --- PLAY REQUEST ---

async def process_play_request(guild, channel, author, query):
    if not author.voice:
        return await channel.send(f"⚠️ {author.mention}, join a voice channel first!", delete_after=8)

    target, vc = author.voice.channel, guild.voice_client
    if not vc or not vc.is_connected():
        try:
            vc = await target.connect(self_deaf=True, timeout=30)
        except asyncio.TimeoutError:
            return await channel.send("⚠️ Voice connection timed out. Please try again.", delete_after=8)
        except Exception as e:
            print(f"Voice connection error: {e}")
            return await channel.send(f"⚠️ Voice connection failed: `{e}`", delete_after=8)
    elif vc.channel != target:
        try:
            await vc.move_to(target)
        except Exception as e:
            print(f"Voice move error: {e}")
            return await channel.send(f"⚠️ Failed to move to voice channel: `{e}`", delete_after=8)

    state = get_player(guild.id)
    state.is_stopped = False
    state.voice_channel_id = vc.channel.id
    status = await channel.send("🔎 Loading...")
    try:
        track = await Track.fetch(query, author.display_name, author.id)
        await safe_delete(status)
        if active(vc):
            state.queue.append(track)
            await display_queue_embed(channel, state)
        else:
            await start_playback(guild, track)
    except Exception as err:
        try:
            await status.edit(content=f"❌ Error: `{err}`", delete_after=5)
        except Exception:
            pass
        if not active(vc):
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        await ensure_standby_embed(guild)

# --- EVENTS ---

@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id:
        if before.channel and not after.channel:
            guild = before.channel.guild
            # Voice drops (e.g. close code 4006) auto-reconnect; only reset if the voice client is gone
            for _ in range(15):
                await asyncio.sleep(2)
                vc = guild.voice_client
                if vc and vc.is_connected():
                    return
            if guild.voice_client:
                print(f"[card] Voice still reconnecting in {guild.name}, keeping player")
                return
            state = get_player(guild.id)
            if state.wiping or (state.is_stopped and not state.current):
                return
            await stop_player(guild, leave=False, reason="bot left the voice channel")
        return
    ch = before.channel
    if ch:
        vc = ch.guild.voice_client
        if vc and vc.channel == ch and not listeners_of(vc):
            await stop_player(ch.guild, reason="everyone left the voice channel")

@bot.event
async def on_raw_message_delete(payload):
    if not payload.guild_id:
        return
    state = get_player(payload.guild_id)
    if state.progress_message and payload.message_id == state.progress_message.id:
        print("[card] Now-playing card was deleted from the channel, re-sending it")
        state.progress_message = None
        guild = bot.get_guild(payload.guild_id)
        if guild and state.current and not state.is_rendering:
            await show_now_playing(guild, state.current)

@tasks.loop(seconds=12)
async def channel_sweeper_task():
    for guild in bot.guilds:
        try:
            channel = await music_channel(guild)
            if not channel or not channel.permissions_for(guild.me).manage_messages:
                continue
            state = get_player(guild.id)
            if state.wiping:
                continue
            if active(guild.voice_client) and state.current:
                if not state.is_rendering and (state.last_message is None or state.controls_message is None):
                    await show_now_playing(guild, state.current)
            elif state.standby_message is None and not state.is_rendering:
                await ensure_standby_embed(guild)

            now = discord.utils.utcnow()
            
            def should_purge(m):
                # Never delete messages sent in the last 30 seconds
                if (now - m.created_at).total_seconds() < 30:
                    return False
                # Never delete the active now-playing card
                if state.last_message and m.id == state.last_message.id:
                    return False
                if state.controls_message and m.id == state.controls_message.id:
                    return False
                # Never delete the active standby card
                if state.standby_message and m.id == state.standby_message.id:
                    return False
                # Never delete the progress bar message
                if state.progress_message and m.id == state.progress_message.id:
                    return False
                # Leftover now-playing cards (not the active one) are swept
                if is_player_card(m):
                    return True
                # Duplicate standby cards are swept; other bot player cards/panels are kept
                if m.author.id == bot.user.id and m.embeds and STANDBY_TITLE in (m.embeds[0].title or ""):
                    return True
                if m.author.id == bot.user.id and (m.embeds or m.components):
                    return False
                # Protect online confirmation notices
                if "the bot is online" in (m.content or ""):
                    return False
                return True
                
            await channel.purge(limit=25, check=should_purge)
        except Exception as e:
            print(f"Sweeper error: {e}")

# --- PROGRESS UPDATE TASK ---
@tasks.loop(seconds=3)
async def progress_update_task():
    """Updates the separate progress bar message with real-time progress."""
    for guild in bot.guilds:
        try:
            state = get_player(guild.id)
            if not active(guild.voice_client) or not state.current:
                continue
            if not state.progress_message:
                if not state.is_rendering:
                    await show_now_playing(guild, state.current)
                continue
            
            # Skip if currently rendering to avoid conflicts
            if state.is_rendering:
                continue

            if state.sleep_until and time.time() >= state.sleep_until:
                await stop_player(guild, reason="sleep timer finished")
                continue
            
            # Update the progress bar message
            async with state.embed_lock:
                if state.progress_message and state.current and not state.is_rendering:
                    try:
                        new_progress = build_progress_bar_message(guild, state.current)
                        if state.progress_view is None:
                            state.progress_view = ProgressBarControls(guild)
                        state.progress_view.refresh(state, guild.voice_client)
                        await state.progress_message.edit(content=None, embed=new_progress, view=state.progress_view)
                        if time.time() - state.last_save >= SAVE_INTERVAL:
                            save_session(guild)
                    except discord.NotFound:
                        print("Now-playing card was deleted, re-sending on next tick")
                        state.progress_message = None
                    except Exception as e:
                        print(f"Progress update error: {e}")
        except Exception as e:
            print(f"Progress task error: {e}")

_startup_done = False

@bot.event
async def on_ready():
    global _startup_done
    print(f"Ready: {bot.user} | command prefix: {PREFIX!r} | commands: {sorted(c.name for c in bot.commands)}")
    if _startup_done:
        return  # on_ready fires again after gateway reconnects; startup work already done
    _startup_done = True
    
    restart_cid, restart_uid, restart_mid = fetch_and_clear_restart_channel()
    
    if restart_cid:
        try:
            target_ch = bot.get_channel(restart_cid) or await bot.fetch_channel(restart_cid)
            if target_ch:
                if restart_uid:
                    await target_ch.send(f"✅ <@{restart_uid}>, the bot is online, have a great time!", delete_after=10)
                else:
                    await target_ch.send("✅ The bot is online, have a great time!", delete_after=10)
        except Exception as e:
            print(f"Failed to send restart online message: {e}")
    else:
        # Manual restart notification sent to configured lounges
        for guild in bot.guilds:
            ch = await music_channel(guild)
            if ch:
                try:
                    await ch.send("✅ The bot is online, have a great time!", delete_after=10)
                except Exception:
                    pass

    # Remove old cards/panels from the previous process, then resume, then show standby where idle
    await asyncio.gather(*(clean_stale_player_messages(g) for g in bot.guilds), return_exceptions=True)
    await resume_after_restart()
    await asyncio.gather(*(ensure_standby_embed(g) for g in bot.guilds), return_exceptions=True)
    channel_sweeper_task.start()
    if not progress_update_task.is_running():
        progress_update_task.start()

@bot.event
async def on_command_error(ctx, error):
    """Simple permission handling - delete command and send one simple message in DM."""
    if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
        await safe_delete(ctx.message)
        
        # Send one simple message in DM
        try:
            await ctx.author.send(
                f"⛔ You don't have permission to use commands in **{ctx.guild.name}**."
            )
        except Exception:
            pass
        return

    if isinstance(error, commands.CommandNotFound):
        return

    print(f"Command error in {ctx.command}: {error}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
        
    if message.guild and message.channel.id == get_server_music_channel(message.guild.id):
        ctx = await bot.get_context(message)
        if ctx.valid and ctx.command.name in ("installkirito", "restart", "show", "track", "sus"):
            return await bot.process_commands(message)
            
        await safe_delete(message)
        
        now = time.time()
        if now - dm_notice_times.get(message.author.id, 0) >= DM_NOTICE_COOLDOWN:
            dm_notice_times[message.author.id] = now
            try:
                await message.author.send(
                    f"⚠️ Text commands are disabled in **#{message.channel.name}**. Use the music player buttons instead."
                )
            except Exception:
                pass
        return
        
    await bot.process_commands(message)

# --- ADMIN COMMANDS ---

class ChannelSelect(Select):
    def __init__(self, channels, here_id):
        options = [discord.SelectOption(label=f"#{c.name}"[:100], value=str(c.id),
                                        description="This channel" if c.id == here_id else None)
                   for c in channels]
        super().__init__(placeholder="Select music channel...", options=options)

    async def callback(self, interaction):
        cid = int(self.values[0])
        set_server_music_channel(interaction.guild_id, cid)
        await interaction.response.edit_message(content=f"✅ Bound to <#{cid}>!", view=None)
        await ensure_standby_embed(interaction.guild)

class ChannelPicker(View):
    """Channel dropdown with Prev/Next pages, since Discord shows at most 25 options at once.
    The channel the command was typed in is listed first."""
    PER_PAGE = 25

    def __init__(self, channels, here_id, author_id):
        super().__init__(timeout=180)
        self.channels = sorted(channels, key=lambda c: c.id != here_id)  # stable: keeps server order
        self.here_id, self.author_id, self.page = here_id, author_id, 0
        self.pages = max(1, -(-len(self.channels) // self.PER_PAGE))
        self.build()

    def build(self):
        self.clear_items()
        start = self.page * self.PER_PAGE
        self.add_item(ChannelSelect(self.channels[start:start + self.PER_PAGE], self.here_id))
        if self.pages > 1:
            for label, step in (("◀ Prev", -1), ("Next ▶", 1)):
                btn = Button(label=label, style=discord.ButtonStyle.secondary,
                             disabled=not 0 <= self.page + step < self.pages)
                btn.callback = self.make_flip(step)
                self.add_item(btn)
            self.add_item(Button(label=f"Page {self.page + 1}/{self.pages}", disabled=True))

    def make_flip(self, step):
        async def flip(interaction):
            self.page += step
            self.build()
            await interaction.response.edit_message(view=self)
        return flip

    async def interaction_check(self, interaction):
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("⚠️ Only the person who ran the command can pick.", ephemeral=True)
        return False

@bot.command(name="installkirito")
@commands.has_permissions(manage_channels=True)
async def installkirito_command(ctx):
    await safe_delete(ctx.message)
    channels = [c for c in ctx.guild.text_channels if c.permissions_for(ctx.guild.me).send_messages]
    if not channels:
        return await ctx.send("❌ I can't send messages in any text channel here.", delete_after=8)
    view = ChannelPicker(channels, ctx.channel.id, ctx.author.id)
    await ctx.send("Select the new music lounge channel:", view=view)

@bot.command(name="show")
async def show_player_command(ctx):
    """Recovers the music player embed if it was deleted."""
    await safe_delete(ctx.message)
    state = get_player(ctx.guild.id)
    vc = ctx.guild.voice_client
    
    if not active(vc) or not state.current:
        return await ctx.send("⚠️ No music is currently playing.", delete_after=5)
    
    try:
        await show_now_playing(ctx.guild, state.current)
        await ctx.send("✅ Music player recovered!", delete_after=3)
    except Exception as e:
        print(f"Error recovering player: {e}")
        await ctx.send("❌ Failed to recover music player.", delete_after=5)

@bot.command(name="track")
async def track_info_command(ctx):
    """Extracts and displays current track information in simple format."""
    await safe_delete(ctx.message)
    state = get_player(ctx.guild.id)
    vc = ctx.guild.voice_client
    
    if not active(vc) or not state.current:
        return await ctx.send("⚠️ No music is currently playing.", delete_after=5)
    
    track = state.current
    current_time = elapsed(state)
    
    track_info = f"""🎵 **Current Track Info:**
**Title:** {track.title}
**Artist:** {track.uploader}
**Duration:** {fmt_time(track.duration)}
**Current Position:** {fmt_time(current_time)}
**URL:** {track.url}
**Requested By:** {track.requester}"""
    
    try:
        await ctx.author.send(track_info)
        await ctx.send("✅ Track info sent to your DMs!", delete_after=3)
    except Exception:
        await ctx.send(track_info, delete_after=30)

@bot.command(name="sus")
@commands.has_permissions(manage_messages=True)
async def sus_command(ctx):
    """Wipes the music lounge and starts over from the standby banner."""
    await safe_delete(ctx.message)
    channel = await music_channel(ctx.guild)
    if not channel:
        return await ctx.send(f"⚠️ Music channel not found. Use `{PREFIX}installkirito`.", delete_after=5)
    if ctx.channel.id != channel.id:
        return await ctx.send(f"⚠️ `{PREFIX}sus` only works in {channel.mention}.", delete_after=5)
    if not channel.permissions_for(ctx.guild.me).manage_messages:
        return await ctx.send("❌ I need the **Manage Messages** permission in the music lounge.", delete_after=8)

    state = get_player(ctx.guild.id)
    state.wiping = True
    try:
        await stop_player(ctx.guild, reason=f"{PREFIX}sus used by {ctx.author}", standby=False)
        state.standby_message = state.last_message = state.progress_message = state.controls_message = None
        total = 0
        for _ in range(5):
            try:
                total += len(await channel.purge(limit=None))
            except discord.NotFound:
                continue  # a message vanished mid-purge; go again
            except Exception as e:
                print(f"[sus] Purge error in {ctx.guild.name}: {e}")
            leftovers = [m async for m in channel.history(limit=100)]
            if not leftovers:
                break
            for m in leftovers:
                await safe_delete(m)
                total += 1
        print(f"[sus] Cleared {total} message(s) in #{channel.name}")
    finally:
        state.wiping = False
    await ensure_standby_embed(ctx.guild)

@bot.command(name="restart")
async def restart_command(ctx):
    await safe_delete(ctx.message)
    msg = await ctx.send("🔄 Restarting bot... Please hold on a few seconds.")
    record_restart_channel(ctx.channel.id, ctx.author.id, msg.id)
    await leave_voice_for_restart()
            
    await asyncio.sleep(1.0)
    await safe_delete(msg)
    execute_restart()

LOCK_FILE = os.path.join(BASE_DIR, ".kirito.lock")
PID_FILE = os.path.join(BASE_DIR, ".kirito.pid")
_instance_lock = None

def _try_lock(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

def _stop_old_copy():
    """Ends the copy that holds the lock (e.g. a hidden process left behind by a restart)."""
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
    except Exception:
        return
    if not pid or pid == os.getpid():
        return
    print(f"[startup] Stopping the old bot copy still running in the background (PID {pid})")
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as e:
        print(f"[startup] Could not stop PID {pid}: {e}")

def acquire_single_instance_lock(wait_seconds=20):
    """Makes sure only one copy of the bot runs (two copies post duplicate cards).
    The newest start wins: an older copy that still holds the lock is stopped.
    The OS releases the lock automatically when a process exits, even after a crash."""
    global _instance_lock
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT)
    deadline = time.time() + wait_seconds
    stopped_old = False
    while True:
        try:
            _try_lock(fd)
            _instance_lock = fd
            with open(PID_FILE, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            return True
        except OSError:
            if not stopped_old:
                _stop_old_copy()
                stopped_old = True
            if time.time() >= deadline:
                os.close(fd)
                return False
            time.sleep(1)

if __name__ == "__main__":
    if not acquire_single_instance_lock():
        print("CRITICAL: could not take over from the old bot copy. End python.exe in Task Manager and start again.")
        sys.exit(1)
    bot.run(TOKEN)