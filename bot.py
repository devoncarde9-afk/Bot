"""
Audio FX Discord bot  (discord.py + FFmpeg)

Commands
  /fx      apply ONE effect to an uploaded file
  /chain   apply MANY effects in order, e.g.  bass:12, reverb:0.5, speed:1.2
  /effects list every effect with its default value and range

Setup
  pip install -U discord.py
  FFmpeg must be installed and on PATH (ffmpeg -version)
  set DISCORD_TOKEN in your environment, then: python bot.py
"""
import asyncio
import os
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

import discord
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]

MAX_INPUT_BYTES = 25 * 1024 * 1024
FFMPEG_TIMEOUT = 180  # seconds
MAX_CHAIN_LENGTH = 12
PREFIX = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"

SEM = asyncio.Semaphore(2)  # max simultaneous ffmpeg jobs


# --------------------------------------------------------------------------
# Effect registry
# --------------------------------------------------------------------------
@dataclass
class Effect:
    category: str
    name: str
    desc: str
    build: Callable[[float], str]
    default: Optional[float] = None  # None = effect takes no value
    lo: Optional[float] = None
    hi: Optional[float] = None
    unit: str = ""


EFFECTS: dict[str, Effect] = {}


def E(category, name, desc, build, default=None, lo=None, hi=None, unit=""):
    EFFECTS[name] = Effect(category, name, desc, build, default, lo, hi, unit)


def tempo(speed: float) -> str:
    """atempo only takes 0.5-2.0 per instance, so chain them for extremes."""
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.5f}")
    return ",".join(parts)


def tape(rate: float) -> str:
    return f"asetrate=44100*{rate:.6f},aresample=44100"


def pitch_shift(semitones: float) -> str:
    """Change pitch but keep the original duration."""
    r = 2 ** (semitones / 12)
    return f"{tape(r)},{tempo(1 / r)}"


# ---- Volume & dynamics ----------------------------------------------------
C = "Volume & dynamics"
E(C, "volume", "Gain change", lambda v: f"volume={v:g}dB", 10, -60, 100, "dB")
E(C, "earrape", "Massive gain boost (clips hard)", lambda v: f"volume={v:g}dB", 60, 0, 100, "dB")
E(C, "distortion", "Gain into a soft clipper", lambda v: f"volume={v:g}dB,asoftclip=type=tanh", 20, 0, 60, "dB")
E(C, "normalize", "Loudness normalize to a LUFS target", lambda v: f"loudnorm=I={v:g}:TP=-1.5:LRA=11", -14, -70, -5, "LUFS")
E(C, "dynnorm", "Dynamic normalizer (evens out loud/quiet parts)", lambda v: "dynaudnorm=f=150:g=15")
E(C, "compressor", "Dynamic range compressor", lambda v: f"acompressor=threshold=-20dB:ratio={v:g}:attack=5:release=80:makeup=4", 4, 1, 20, ":1")
E(C, "limiter", "Brickwall limiter", lambda v: f"alimiter=limit={v:g}", 0.8, 0.0625, 1, "")
E(C, "gate", "Noise gate", lambda v: f"agate=threshold={v:g}:ratio=4", 0.05, 0.001, 1, "")
E(C, "fadein", "Fade in", lambda v: f"afade=t=in:d={v:g}", 3, 0.1, 30, "s")
E(C, "fadeout", "Fade out", lambda v: f"areverse,afade=t=in:d={v:g},areverse", 3, 0.1, 30, "s")

# ---- Speed & pitch --------------------------------------------------------
C = "Speed & pitch"
E(C, "speed", "Change speed, pitch stays the same", tempo, 1.25, 0.1, 10, "x")
E(C, "tape", "Change speed AND pitch (record/tape style)", tape, 0.8, 0.25, 4, "x")
E(C, "pitch", "Shift pitch, speed stays the same", pitch_shift, 4, -24, 24, "semitones")
E(C, "nightcore", "Faster and higher", tape, 1.25, 1.05, 2, "x")
E(C, "slowed", "Slower and lower", tape, 0.8, 0.5, 0.95, "x")
E(C, "slowedreverb", "Slowed plus reverb", lambda v: f"{tape(v)},aecho=0.8:0.88:60|110|180:0.35|0.28|0.2", 0.8, 0.5, 0.95, "x")
E(C, "chipmunk", "Pitch up, same speed", pitch_shift, 7, 3, 12, "semitones")
E(C, "deepvoice", "Pitch down, same speed", lambda v: pitch_shift(-v), 6, 2, 12, "semitones")

# ---- EQ & filters ---------------------------------------------------------
C = "EQ & filters"
E(C, "bass", "Bass boost", lambda v: f"bass=g={v:g}:f=100:w=0.8", 10, -20, 40, "dB")
E(C, "deepbass", "Sub-bass boost around 55 Hz", lambda v: f"equalizer=f=55:t=h:w=80:g={v:g}", 12, 0, 30, "dB")
E(C, "treble", "Treble boost", lambda v: f"treble=g={v:g}:f=3000", 8, -20, 30, "dB")
E(C, "mids", "Midrange boost/cut at 1 kHz", lambda v: f"equalizer=f=1000:t=q:w=1:g={v:g}", 6, -20, 20, "dB")
E(C, "smile", "Boost lows and highs (smile EQ)", lambda v: f"equalizer=f=80:t=q:w=1:g={v:g},equalizer=f=10000:t=q:w=1:g={v:g}", 6, -12, 20, "dB")
E(C, "lowpass", "Cut everything above a frequency", lambda v: f"lowpass=f={v:g}", 1000, 50, 20000, "Hz")
E(C, "highpass", "Cut everything below a frequency", lambda v: f"highpass=f={v:g}", 500, 20, 10000, "Hz")
E(C, "bandpass", "Keep only a band around a frequency", lambda v: f"bandpass=f={v:g}:t=h:w={v / 2:g}", 1500, 100, 10000, "Hz")

# ---- Space & time ---------------------------------------------------------
C = "Space & time"
E(C, "reverb", "Reverb (multi-tap)", lambda v: f"aecho=0.6:0.9:40|70|110|170|240:{v:g}|{v * .85:.3f}|{v * .7:.3f}|{v * .5:.3f}|{v * .35:.3f}", 0.5, 0.1, 0.9, "decay")
E(C, "echo", "Single echo", lambda v: f"aecho=0.8:0.88:{v:g}:0.5", 400, 20, 3000, "ms")
E(C, "cave", "Long dark echoes", lambda v: f"aecho=0.7:0.9:300|600|1000|1500:{v:g}|{v * .8:.3f}|{v * .6:.3f}|{v * .4:.3f},lowpass=f=5000", 0.5, 0.1, 0.8, "decay")
E(C, "hall", "Big hall reverb", lambda v: f"aecho=0.6:0.9:80|160|240|360|500|700:{v:g}|{v * .9:.3f}|{v * .8:.3f}|{v * .6:.3f}|{v * .45:.3f}|{v * .3:.3f}", 0.5, 0.1, 0.9, "decay")
E(C, "chorus", "Chorus (thickens the sound)", lambda v: "chorus=0.5:0.9:50|60|40:0.4|0.32|0.3:0.25|0.4|0.3:2|2.3|1.3")
E(C, "flanger", "Flanger", lambda v: f"flanger=delay=5:depth=5:speed={v:g}:regen=40", 0.5, 0.1, 10, "Hz")
E(C, "phaser", "Phaser", lambda v: f"aphaser=type=t:speed={v:g}:decay=0.6", 0.5, 0.1, 5, "Hz")

# ---- Modulation -----------------------------------------------------------
C = "Modulation"
E(C, "vibrato", "Pitch wobble", lambda v: f"vibrato=f={v:g}:d=0.5", 5, 0.1, 20, "Hz")
E(C, "tremolo", "Volume wobble", lambda v: f"tremolo=f={v:g}:d=0.9", 8, 0.1, 20, "Hz")
E(C, "wobble", "Extreme pitch wobble", lambda v: f"vibrato=f={v:g}:d=1", 2, 0.1, 20, "Hz")
E(C, "8d", "Audio rotates around your head (use headphones)", lambda v: f"apulsator=mode=sine:hz={v:g}:amount=1", 0.125, 0.01, 2, "Hz")

# ---- Stereo ---------------------------------------------------------------
C = "Stereo"
E(C, "widen", "Widen the stereo image", lambda v: f"stereowiden=delay={v:g}:feedback=0.3:crossfeed=0.3:drymix=0.8", 20, 1, 100, "ms")
E(C, "mono", "Collapse to mono", lambda v: "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1")
E(C, "swap", "Swap left and right channels", lambda v: "pan=stereo|c0=c1|c1=c0")
E(C, "balance", "Pan left (-1) to right (1)", lambda v: f"stereotools=balance_out={v:g}", -1, -1, 1, "")
E(C, "vocalremove", "Try to remove centered vocals", lambda v: "pan=stereo|c0=c0-c1|c1=c1-c0")

# ---- Character & lo-fi ----------------------------------------------------
C = "Character & lo-fi"
E(C, "bitcrush", "Bit depth reduction", lambda v: f"acrusher=bits={v:g}:mode=log:aa=1:mix=1", 8, 1, 16, "bits")
E(C, "lofi", "Downsample for a crunchy lo-fi sound", lambda v: f"aresample={int(v)},aresample=44100", 8000, 2000, 22050, "Hz")
E(C, "telephone", "Old phone call", lambda v: "highpass=f=300,lowpass=f=3400,acompressor=ratio=6,volume=3dB")
E(C, "radio", "Squashed AM radio", lambda v: "highpass=f=400,lowpass=f=3000,acompressor=threshold=-18dB:ratio=8,asoftclip=type=tanh,volume=2dB")
E(C, "megaphone", "Loud, clipped megaphone", lambda v: "highpass=f=500,lowpass=f=3500,volume=12dB,asoftclip=type=tanh")
E(C, "muffled", "Heard through a wall", lambda v: f"lowpass=f={v:g}:poles=2", 800, 100, 5000, "Hz")
E(C, "underwater", "Underwater sound", lambda v: "lowpass=f=500,vibrato=f=4:d=0.3,aecho=0.8:0.8:80|160:0.4|0.3")
E(C, "vinyl", "Warm, slightly wobbly record sound", lambda v: "highpass=f=60,lowpass=f=9000,vibrato=f=0.6:d=0.03")
E(C, "robot", "Robot voice", lambda v: "afftfilt=real='hypot(re,im)*cos(0)':imag='hypot(re,im)*sin(0)':win_size=512:overlap=0.75")
E(C, "whisper", "Whisper voice", lambda v: "afftfilt=real='hypot(re,im)*cos((random(0)*2-1)*2*3.14)':imag='hypot(re,im)*sin((random(1)*2-1)*2*3.14)':win_size=128:overlap=0.8")

# ---- Utility --------------------------------------------------------------
C = "Utility"
E(C, "reverse", "Play backwards", lambda v: "areverse")
E(C, "denoise", "FFT noise reduction", lambda v: f"afftdn=nr={v:g}", 12, 0.01, 97, "dB")
E(C, "trimsilence", "Trim silence from start and end", lambda v: "silenceremove=start_periods=1:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse")
E(C, "crystalizer", "Sharpen / restore highs", lambda v: f"crystalizer=i={v:g}", 2, 0, 10, "")
E(C, "padend", "Add silence at the end", lambda v: f"apad=pad_dur={v:g}", 2, 0.1, 60, "s")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
FORMATS = {
    "mp3": ("libmp3lame", ["-b:a", "192k"]),
    "wav": ("pcm_s16le", []),
    "ogg": ("libvorbis", ["-q:a", "5"]),
}


def resolve(name: str, value: Optional[float]) -> tuple[Effect, Optional[float]]:
    """Look up an effect and validate its value. Raises ValueError with a friendly message."""
    fx = EFFECTS.get(name.lower().strip())
    if fx is None:
        raise ValueError(f"Unknown effect `{name}`. Use `/effects` to see the list.")
    if fx.default is None:
        return fx, None
    if value is None:
        value = fx.default
    if not (fx.lo <= value <= fx.hi):
        raise ValueError(f"`{fx.name}` needs a value between {fx.lo:g} and {fx.hi:g} {fx.unit}".strip())
    return fx, value


def parse_chain(text: str) -> list[tuple[Effect, Optional[float]]]:
    steps = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, raw = part.partition(":")
        value = None
        if raw.strip():
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"`{raw}` isn't a number (in `{part}`).")
        steps.append(resolve(name, value))
    if not steps:
        raise ValueError("Give at least one effect, e.g. `bass:12, reverb:0.5`.")
    if len(steps) > MAX_CHAIN_LENGTH:
        raise ValueError(f"Max {MAX_CHAIN_LENGTH} effects per chain.")
    return steps


def describe(steps) -> str:
    out = []
    for fx, v in steps:
        out.append(fx.name if v is None else f"{fx.name} {v:g}{(' ' + fx.unit) if fx.unit else ''}")
    return " → ".join(out)


async def process(interaction: discord.Interaction, file: discord.Attachment, steps, fmt: str):
    if file.size > MAX_INPUT_BYTES:
        await interaction.followup.send("That file's too big (25MB max).")
        return

    filter_chain = ",".join([PREFIX] + [fx.build(v if v is not None else 0) for fx, v in steps])
    codec, extra = FORMATS[fmt]

    async with SEM:
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "input")
            out_path = os.path.join(tmp, f"output.{fmt}")
            await file.save(in_path)

            cmd = ["ffmpeg", "-y", "-i", in_path, "-vn", "-af", filter_chain,
                   "-c:a", codec, *extra, out_path]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await interaction.followup.send("Processing took too long, so I cancelled it.")
                return

            if proc.returncode != 0:
                tail = err.decode(errors="ignore")[-600:]
                await interaction.followup.send(
                    f"FFmpeg failed. Bad file, or your FFmpeg is missing a filter.\n```{tail}```"
                )
                return

            limit = interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024
            if os.path.getsize(out_path) > limit:
                await interaction.followup.send(
                    f"Output is too big to upload here. Try `format:mp3` or a shorter file."
                )
                return

            base = os.path.splitext(file.filename)[0][:40] or "audio"
            await interaction.followup.send(
                f"**{describe(steps)}**",
                file=discord.File(out_path, filename=f"{base}_fx.{fmt}"),
            )


# --------------------------------------------------------------------------
# Bot + commands
# --------------------------------------------------------------------------
class FXBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


client = FXBot()
tree = client.tree

FormatChoice = app_commands.Choice
FORMAT_CHOICES = [FormatChoice(name=k, value=k) for k in FORMATS]


async def effect_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    hits = [fx for fx in EFFECTS.values() if cur in fx.name or cur in fx.category.lower()]
    return [
        app_commands.Choice(name=f"{fx.name} - {fx.desc}"[:100], value=fx.name)
        for fx in hits[:25]
    ]


@tree.command(name="fx", description="Apply one audio effect to a file")
@app_commands.describe(
    file="Audio or video file",
    effect="Effect name (start typing to search)",
    value="Effect amount. Leave blank for the default (see /effects)",
    format="Output format",
)
@app_commands.autocomplete(effect=effect_autocomplete)
@app_commands.choices(format=FORMAT_CHOICES)
async def fx_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    effect: str,
    value: Optional[float] = None,
    format: FormatChoice[str] = "mp3",
):
    fmt = format.value if isinstance(format, FormatChoice) else format
    try:
        step = resolve(effect, value)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    await process(interaction, file, [step], fmt)


@tree.command(name="chain", description="Apply several effects in order")
@app_commands.describe(
    file="Audio or video file",
    effects="Comma separated, e.g.  bass:12, reverb:0.5, speed:1.2",
    format="Output format",
)
@app_commands.choices(format=FORMAT_CHOICES)
async def chain_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    effects: str,
    format: FormatChoice[str] = "mp3",
):
    fmt = format.value if isinstance(format, FormatChoice) else format
    try:
        steps = parse_chain(effects)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    await process(interaction, file, steps, fmt)


@tree.command(name="effects", description="List every effect, default value, and range")
async def effects_cmd(interaction: discord.Interaction):
    cats: dict[str, list[Effect]] = {}
    for fx in EFFECTS.values():
        cats.setdefault(fx.category, []).append(fx)

    embeds = []
    for cat, items in cats.items():
        lines = []
        for fx in items:
            if fx.default is None:
                info = "no value"
            else:
                info = f"default {fx.default:g}, {fx.lo:g} to {fx.hi:g} {fx.unit}".strip()
            lines.append(f"`{fx.name}` ({info}): {fx.desc}")
        embeds.append(discord.Embed(title=cat, description="\n".join(lines), color=0x5865F2))

    embeds[-1].set_footer(text=f"{len(EFFECTS)} effects. Use /fx for one, /chain for several.")
    await interaction.response.send_message(embeds=embeds, ephemeral=True)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} with {len(EFFECTS)} effects loaded")


client.run(TOKEN)
