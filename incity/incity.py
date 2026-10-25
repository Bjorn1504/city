import asyncio
import logging
import time

import discord
from discord.ext import commands

logger = logging.getLogger("Modmail")

# ------------------------------ CONFIG ------------------------------
CHANNEL_ID = 1506363908405854424  # channel where the messages are sent
ROLE_ID = 1563945182599057568  # <-- REPLACE with the ID of your "in city" role
REMINDER_DELAY = 1 * 60  # seconds until the first reminder (1 hour)
REPING_DELAY = 15 * 60  # seconds between re-pings if they don't respond (15 min)
THUMBS_UP = "\N{THUMBS UP SIGN}"
THUMBS_DOWN = "\N{THUMBS DOWN SIGN}"
# --------------------------------------------------------------------


def now_stamp() -> str:
    """Discord timestamp (shows in each viewer's own timezone), e.g. 4:20 PM."""
    return f"<t:{int(time.time())}:t>"


class City(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)
        self.tasks: dict[int, asyncio.Task] = {}  # user_id -> running timer
        self.pending: dict[int, set[int]] = {}  # user_id -> their reminder message ids

    # ---------------------------- lifecycle ----------------------------

    async def cog_load(self):
        """Restore timers/reminders saved in the database (survives restarts)."""
        async for doc in self.coll.find({}):
            user_id = doc["_id"]
            ids = doc.get("reminder_ids")
            if ids is None and doc.get("reminder_id"):  # older saved format
                ids = [doc["reminder_id"]]
            if ids:
                self.pending[user_id] = set(ids)
            self._schedule(user_id, max(0, doc["due"] - time.time()))

    async def cog_unload(self):
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    # ----------------------------- helpers -----------------------------

    async def _get_channel(self):
        return self.bot.get_channel(CHANNEL_ID) or await self.bot.fetch_channel(CHANNEL_ID)

    def _cancel_task(self, user_id: int):
        task = self.tasks.pop(user_id, None)
        if task:
            task.cancel()

    def _schedule(self, user_id: int, delay: float):
        self._cancel_task(user_id)
        self.tasks[user_id] = asyncio.create_task(self._timer(user_id, delay))

    async def _start_timer(self, user_id: int, delay: float = REMINDER_DELAY):
        """(Re)start the 1 hour timer and save it to the database."""
        self._cancel_task(user_id)
        self.pending.pop(user_id, None)
        await self.coll.replace_one(
            {"_id": user_id},
            {"_id": user_id, "due": time.time() + delay, "reminder_ids": []},
            upsert=True,
        )
        self._schedule(user_id, delay)

    async def _clear(self, user_id: int):
        """Forget everything about this user's timer/reminders."""
        self._cancel_task(user_id)
        self.pending.pop(user_id, None)
        await self.coll.delete_one({"_id": user_id})

    async def _timer(self, user_id: int, delay: float):
        """Wait, send the reminder, then keep re-pinging until this task is cancelled
        (it gets cancelled as soon as the user responds)."""
        try:
            await self.bot.wait_until_ready()
            await asyncio.sleep(delay)
            while True:
                try:
                    await self._send_reminder(user_id)
                except Exception:
                    logger.exception("City reminder failed for user %s", user_id)
                await asyncio.sleep(REPING_DELAY)
        except asyncio.CancelledError:
            raise

    async def _send_reminder(self, user_id: int):
        channel = await self._get_channel()
        msg = await channel.send(f"<@{user_id}>, have you left the city yet?")
        self.pending.setdefault(user_id, set()).add(msg.id)
        await self.coll.update_one(
            {"_id": user_id},
            {
                "$push": {"reminder_ids": msg.id},
                "$set": {"due": time.time() + REPING_DELAY},  # next re-ping
            },
        )
        await msg.add_reaction(THUMBS_UP)
        await msg.add_reaction(THUMBS_DOWN)

    async def _leave_city(self, guild: discord.Guild, user_id: int):
        """Remove the role and send the disable message."""
        await self._clear(user_id)

        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            return  # they left the server entirely

        role = guild.get_role(ROLE_ID)
        if role and role in member.roles:
            await member.remove_roles(role, reason="Left the city")

        channel = await self._get_channel()
        await channel.send(f"{member.mention} left the city at {now_stamp()}.")

    # ----------------------------- command -----------------------------

    @commands.command()
    @commands.guild_only()
    async def city(self, ctx):
        """Toggle your "in city" role."""
        role = ctx.guild.get_role(ROLE_ID)
        if role is None:
            return await ctx.send("The city role wasn't found. Check `ROLE_ID` in the plugin.")

        member = ctx.author

        # Already has the role -> disable (this also stops any re-pings)
        if role in member.roles:
            await ctx.message.add_reaction(THUMBS_UP)
            try:
                await self._leave_city(ctx.guild, member.id)
            except discord.Forbidden:
                await ctx.send("I don't have permission to remove that role.")
            return

        # Doesn't have the role -> enable
        try:
            await member.add_roles(role, reason="Entered the city")
        except discord.Forbidden:
            return await ctx.send(
                "I don't have permission to give that role. "
                "Make sure I have **Manage Roles** and my role is above it."
            )

        channel = await self._get_channel()
        await channel.send(f"{member.mention} is in city at {now_stamp()}.")
        await self._start_timer(member.id)

    # ----------------------------- reactions ----------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        user_id = payload.user_id

        # Only the pinged user reacting to one of their own reminders counts
        if payload.message_id not in self.pending.get(user_id, ()):
            return

        emoji = str(payload.emoji)

        if emoji.startswith(THUMBS_UP):
            # Still in the city -> stop re-pinging and restart the 1 hour timer
            await self._start_timer(user_id)

        elif emoji.startswith(THUMBS_DOWN):
            guild = self.bot.get_guild(payload.guild_id)
            if guild is None:
                return
            try:
                await self._leave_city(guild, user_id)
            except discord.Forbidden:
                logger.warning("Missing permission to remove the city role from %s", user_id)


async def setup(bot):
    await bot.add_cog(City(bot))
