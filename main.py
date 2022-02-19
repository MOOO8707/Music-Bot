import asyncio
import itertools
import sys
from discord.ext import commands
import discord
import traceback
import async_timeout
from functools import partial
from discord import Embed, Member

from discord.ext.commands import Paginator
from youtube_dl import YoutubeDL
import re
import requests
import bs4
import platform
import psutil

# 음악기능과는 관련 없는 모듈
import datetime  # 날짜 / 시간 기록을 위해 있는 모듈
import time  # sleep 명령 사용 위해 있는 모듈
import random
from bs4 import BeautifulSoup


bot = commands.Bot(command_prefix=["!"], help_command=None)
client = discord.Client()

error_embed_color = 0xFF5900


@bot.event
async def on_ready():  # 봇이 준비가 되면 1회 실행되는 부분입니다.
    print("로그인완료 {}".format(client.user))


def clean_text10(text):
    cleaned_text = " ".join(re.split("\s+", text, flags=re.UNICODE))
    return cleaned_text


def random_color():
    return random.randint(0, 0xFFFFFF)


ytdlopts = {
    "format": "bestaudio/best",
    "outtmpl": "downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0", 
}

ffmpegopts = {"before_options": "-nostdin", "options": "-vn"}
ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
    """Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Exception for cases of invalid Voice Channels."""


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester
        self.title = data.get("title")
        self.web_url = data.get("webpage_url")
        self.id = data.get("id")
        self.duration = data.get("duration")

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, melon: int, *, loop, download=False):
        loop = loop or asyncio.get_event_loop()
        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)
        if "entries" in data:
            try:
                data = data["entries"][0]
            except IndexError:
                return 1
        if melon == 0:
            d = data["title"] + " **[" + str(datetime.timedelta(seconds=int(data["duration"]))) + "]**"
            embed = discord.Embed(title="<:check:829154299003011102> 대기열에 음악을 추가했어요!", description=d,
                                  color=random_color())
            embed.set_image(url=f"https://img.youtube.com/vi/{data['id']}/maxresdefault.jpg")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            await ctx.channel.send(embed=embed)
        if download:
            source = ytdl.prepare_filename(data)
        else:
            return {"webpage_url": data["webpage_url"], "requester": ctx.author, "title": data["title"]}
        return cls(discord.FFmpegPCMAudio(source), data=data, requester=ctx.author)

    @classmethod
    async def regather_stream(cls, data, *, loop):
        loop = loop or asyncio.get_event_loop()
        requester = data["requester"]
        to_run = partial(ytdl.extract_info, url=data["webpage_url"], download=False)
        data = await loop.run_in_executor(None, to_run)
        return cls(discord.FFmpegPCMAudio(data["url"],
                                          before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 1"),
                   data=data, requester=requester)


class MusicPlayer:
    __slots__ = ("bot", "_guild", "_channel", "_cog", "queue", "next", "current", "np", "volume")

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog
        self.queue = asyncio.Queue()
        self.next = asyncio.Event()
        self.np = None 
        self.volume = 1.0
        self.current = None
        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Our main player loop."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            self.next.clear()
            try:
                async with async_timeout.timeout(40):
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)
            if not isinstance(source, YTDLSource):
                source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
            self.current = source
            source.volume = self.volume
            try:
                self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            except Exception:
                embed = discord.Embed(title="무슨 오류인지 알 수 없어요...", color=0xFF5900)
                await self._channel.send(embed=embed)
            d = source.title + " **[" + str(datetime.timedelta(seconds=source.duration)) + "]**"
            embed = discord.Embed(title=d, description=f"요청자 : {source.requester}", color=random_color())
            embed.set_author(name="재생중인 곡")
            embed.set_image(url=f"https://img.youtube.com/vi/{source.id}/maxresdefault.jpg")
            embed.timestamp = datetime.datetime.utcnow()
            self.np = await self._channel.send(embed=embed)
            await self.next.wait()
            source.cleanup()
            self.current = None
            try:
                await self.np.delete()
            except discord.HTTPException:
                pass

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ("bot", "players")

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass
        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send("DM채널에서는 사용할수 없는 명령이네요")
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            embed = discord.Embed(title=":x: 음성채널에 접속하는데 문제가 생겼어요", color=error_embed_color,
                                  description="유효한 음성채널인지 확인해주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            await ctx.channel.send(embed=embed)
        print("Ignoring exception in command {}:".format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player
        return player

    @commands.command(name="접속", aliases=["이동", "connect", "join", "입장"])
    async def connect_(self, ctx, *, channel: discord.VoiceChannel = None):
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                embed = discord.Embed(title=":x: 들어갈 채널이 없는데요?", color=error_embed_color,
                                      description="유효한 채널을 지정하거나 한곳에 접속해주세요")
                embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
                embed.timestamp = datetime.datetime.utcnow()
                await ctx.channel.send(embed=embed)
        vc = ctx.voice_client
        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f"Moving to channel: <{channel}> timed out.")
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f"Connecting to channel: <{channel}> timed out.")
        await ctx.send(f"**{channel}** 에서 노래를 부를게요")

    @commands.command(name="재생", aliases=["play", "p"])
    async def play_(self, ctx, *, search=None):
        if search == None:
            return
        await ctx.trigger_typing()
        vc = ctx.voice_client
        if not vc:
            await ctx.invoke(self.connect_)
        player = self.get_player(ctx)
        source = await YTDLSource.create_source(ctx, search, 0, loop=self.bot.loop, download=False)
        if source == 1:
            embed = discord.Embed(title=":x: 노래 정보 불러오기 실패", color=error_embed_color, description="다시 시도해 주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            await ctx.channel.send(embed=embed)
        await player.queue.put(source)

    @commands.command(name="일시정지", aliases=["pause", "stop"])
    async def pause_(self, ctx):
        vc = ctx.voice_client
        if not vc or not vc.is_playing():
            embed = discord.Embed(title=":x: 재생중인 노래가 없는거 같은데요...?", color=error_embed_color,
                                  description="노래를 재생시킨 후에 사용해주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            await ctx.channel.send(embed=embed)
        elif vc.is_paused():
            return
        vc.pause()
        embed = discord.Embed(title=f"**`{ctx.author}`** 님이 노래를 일시 정지했어요", color=random_color())
        embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
        embed.timestamp = datetime.datetime.utcnow()
        await ctx.channel.send(embed=embed)

    @commands.command(name="다시시작", aliases=["restart", "r"])
    async def resume_(self, ctx):
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title=":x: 재생중인 노래가 없는거 같은데요...?", color=error_embed_color,
                                  description="노래를 재생시킨 후에 사용해주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            return await ctx.channel.send(embed=embed)
        elif not vc.is_paused():
            return
        vc.resume()
        embed = discord.Embed(title=f"**`{ctx.author}`** 님이 노래를 다시 시작했어요", color=random_color())
        embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
        embed.timestamp = datetime.datetime.utcnow()
        await ctx.channel.send(embed=embed)

    @commands.command(name="건너뛰기", aliases=["스킵", "skip", "s"])
    async def skip_(self, ctx):
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title=":x: 흠...지금 재생중인 노래가 없는거 같은데요?", color=error_embed_color,
                                  description="노래를 재생시킨 다음에 사용해 주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            return await ctx.channel.send(embed=embed)
        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return
        vc.stop()
        embed = discord.Embed(title=f"**`{ctx.author}`** 님이 노래를 건너뛰었어요", color=random_color())
        embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
        embed.timestamp = datetime.datetime.utcnow()
        await ctx.channel.send(embed=embed)

    @commands.command(name="pl", aliases=["q", "queue", "playlist", "재생목록"])
    async def queue_info(self, ctx):
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title=":x: 음성채널에 접속해 주세요", color=error_embed_color,
                                  description="음성채널에 접속한 뒤 사용해 주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            return await ctx.channel.send(embed=embed)
        player = self.get_player(ctx)
        if player.queue.empty():
            if not player.current:
                embed = discord.Embed(title=":x: 흠...지금 재생중인 노래가 없는거 같은데요?", color=error_embed_color,
                                      description="노래를 재생시킨 다음에 사용해 주세요")
                embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
                embed.timestamp = datetime.datetime.utcnow()
                return await ctx.channel.send(embed=embed)
                d = vc.source.title + " **[" + str(datetime.timedelta(seconds=vc.source.duration)) + "]**"
                embed = discord.Embed(title=d, description=f"요청자 : {vc.source.requester}\n이 곡이 마지막 곡입니다",
                                      color=random_color())
                embed.set_author(name="재생중인 곡")
                embed.set_image(url=f"https://img.youtube.com/vi/{vc.source.id}/maxresdefault.jpg")
                embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
                embed.timestamp = datetime.datetime.utcnow()
                return await ctx.send(embed=embed)
        upcoming = list(itertools.islice(player.queue._queue, 0, 5))
        fmt = "\n".join(f':track_next: {_["title"]}' for _ in upcoming)
        embed = discord.Embed(title=f"대기열 - {len(upcoming)}곡이 남았어요",
                              description=f"**지금 재생중!**\n▶ **{vc.source.title} [{str(datetime.timedelta(seconds=vc.source.duration))}]**\n\n{fmt}")
        await ctx.send(embed=embed)

    @commands.command(name="재생중", aliases=["np", "now playing", "currentsong", "playing"])
    async def now_playing_(self, ctx):
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title=":x: 음성채널에 접속해 주세요", color=error_embed_color,
                                  description="음성채널에 접속한 뒤 사용해 주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            return await ctx.channel.send(embed=embed)
        player = self.get_player(ctx)
        if not player.current:
            embed = discord.Embed(title=":x: 재생중인 노래가 없는거 같은데요..?", color=error_embed_color,
                                  description="노래를 재생시킨 다음에 사용해 주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            return await ctx.channel.send(embed=embed)
        try:
            await player.np.delete()
        except discord.HTTPException:
            pass
        d = vc.source.title + " **[" + str(datetime.timedelta(seconds=vc.source.duration)) + "]**"
        embed = discord.Embed(title=d, description=f"요청자 : {vc.source.requester}", color=random_color())
        embed.set_author(name="재생중인 곡")
        embed.set_image(url=f"https://img.youtube.com/vi/{vc.source.id}/maxresdefault.jpg")
        embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
        embed.timestamp = datetime.datetime.utcnow()
        player.np = await ctx.send(embed=embed)

    @commands.command(name="vol", aliases=["볼륨", "volume"])
    async def change_volume(self, ctx, *, vol: float):
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title=":x: 현재 음성채널에 접속해있지 않아요!", color=error_embed_color,
                                  description="노래를 먼저 재생시킨 다음에 사용해 주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            return await ctx.channel.send(embed=embed)
        if not 0 < vol <= 100:
            embed = discord.Embed(title=":loud_sound: 볼륨은 1 ~ 100 사이의 숫자로 입력해주세요!", color=error_embed_color)
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            return await ctx.channel.send(embed=embed)
        player = self.get_player(ctx)
        if vc.source:
            vc.source.volume = vol / 100
        player.volume = vol / 100
        embed = discord.Embed(title=f":loud_sound: **`{ctx.author}`**님이 볼륨을 **{vol}%**로 설정했어요", color=random_color())
        embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
        embed.timestamp = datetime.datetime.utcnow()
        await ctx.channel.send(embed=embed)

    @commands.command(name="exit", aliases=["quit", "끝내기", "정지", "leave", "퇴장"])
    async def stop_(self, ctx):
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            embed = discord.Embed(title=":x: 흠...지금 재생중인 노래가 없는거 같은데요?", color=error_embed_color,
                                  description="노래를 재생시킨 다음에 사용해 주세요")
            embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
            embed.timestamp = datetime.datetime.utcnow()
            return await ctx.channel.send(embed=embed)
        await self.cleanup(ctx.guild)
        embed = discord.Embed(title="노래 그만 부를래요", color=random_color())
        embed.set_footer(text=ctx.author.name, icon_url=ctx.author.avatar_url)
        embed.timestamp = datetime.datetime.utcnow()
        return await ctx.channel.send(embed=embed)

bot.add_cog(Music(bot))
bot.run("")
