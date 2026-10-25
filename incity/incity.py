import asyncio
import logging
import time
from typing import Union

import discord
from discord.ext import commands
from pymongo import UpdateOne

logger = logging.getLogger("Modmail")

# ------------------------------ CONFIG ------------------------------
CHANNEL_ID1 = 1506363879121354884  # where the city messages are sent
CHANNEL_ID2 = 1506363914550771742  # where the bot's warnings / errors / logs are sent
ROLE_ID = 1563945182599057568  # the "in city" role

# ---- .city messages ------------------------------------------------
# Placeholders: {user} (mention), {time} (timestamp)
ENABLE_MESSAGE = "{user} is in city at {time}."
DISABLE_MESSAGE = "{user} left the city at {time}."
REMINDER_MESSAGE = "{user}, are you still in city?"

REMINDER_DELAY = 1 * 60  # seconds until the first reminder (1 hour)
REPING_DELAY = 1 * 60  # seconds between re-pings if they don't respond (15 min)
COMMAND_DELETE_DELAY = 3  # seconds the bot's 👍 stays visible when disabling, before the command message is deleted
THUMBS_UP = "\N{THUMBS UP SIGN}"
THUMBS_DOWN = "\N{THUMBS DOWN SIGN}"

# ---- Reminder (sent to the person's DMs as an embed) ----------------
REMINDER_TITLE = "Check-in"
REMINDER_COLOR = 0xFF0000  # embed color (hex)
REMINDER_ENTRY = REMINDER_MESSAGE  # embed text. Placeholders: {user}, {time}
REMINDER_FOOTER = "ONLY REACT TO THIS MESSAGE, DO NOT SEND A MESSAGE."  # use "" for no footer
DM_FAILURES_BEFORE_CHANNEL = 2  # after this many failed DMs in a row, post the reminder in CHANNEL_ID1 instead

# ---- .who ----------------------------------------------------------
WHO_EMPTY_MESSAGE = "No one is in the city right now."  # sent when nobody has the role
WHO_TITLE = "Who's in the city"
WHO_COLOR = 0xFFFFFF  # embed color (hex)
WHO_ENTRY = "{user} — since {time}"  # one line per person. Placeholders: {user}, {time}, {ago}
WHO_FOOTER = "{count} in the city"  # Placeholders: {count}. Use "" for no footer

# ---- .time ---------------------------------------------------------
TIME_EMPTY_MESSAGE = "Nobody has a city record yet."
TIME_TITLE = "City records"
TIME_COLOR = 0xFFD966  # embed color (hex)
TIME_ENTRY = "**{rank}.** {user} — {count}"  # Placeholders: {rank}, {user}, {count}
TIME_FOOTER = "{people} people · {total} total"  # Placeholders: {people}, {total}. Use "" for no footer
TIME_VIEW_MESSAGE = "{user} has joined the city {count} time(s)."  # {user}, {count}
TIME_CHANGE_MESSAGE = "{admin} set {user}'s city record to {count}."  # {user}, {count}, {admin}
TIME_CHANGE_ROLE_MESSAGE = "{admin} set the city record of {people} member(s) with {role} to {count}."  # {admin}, {role}, {people}, {count}
TIME_ROLE_EMPTY_MESSAGE = "No one has {role}."  # {role}
TIME_NO_PERMISSION_MESSAGE = "You need the Administrator permission to change records."
TIME_INVALID_MESSAGE = "The record must be 0 or higher."
# --------------------------------------------------------------------

NO_MENTIONS = discord.AllowedMentions.none()


class _SafeDict(dict):
    """Leaves unknown {placeholders} as-is instead of crashing on a typo."""

    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, **values) -> str:
    return template.format_map(_SafeDict(values))


def now_stamp() -> str:
    """Discord timestamp (shows in each viewer's own timezone), e.g. 4:20 PM."""
    return f"<t:{int(time.time())}:t>"


def fmt(template: str, user_id: int) -> str:
    return render(template, user=f"<@{user_id}>", time=now_stamp())


def join_lines(lines, limit: int = 4000) -> str:
    """Join lines for an embed description, trimming if it gets too long."""
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit:
            out.append(f"…and {len(lines) - i} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


class City(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)
        self.tasks: dict[int, asyncio.Task] = {}  # user_id -> running timer
        # user_id -> {reminder message id: channel id it was sent in (None = their DMs)}
        self.pending: dict[int, dict[int, int | None]] = {}
        self.dm_failures: dict[int, int] = {}  # user_id -> failed DMs in a row

    # ---------------------------- lifecycle ----------------------------

    async def cog_load(self):
        """Restore timers/reminders saved in the database (survives restarts)."""
        # Timer docs have a "due" field (records for .time are stored separately).
        async for doc in self.coll.find({"due": {"$exists": True}}):
            user_id = doc["_id"]
            reminders = {int(k): v for k, v in (doc.get("reminders") or {}).items()}
            for message_id in doc.get("reminder_ids") or []:  # older saved format (DM reminders)
                reminders.setdefault(message_id, None)
            if reminders:
                self.pending[user_id] = reminders
            self._schedule(user_id, max(0, doc["due"] - time.time()))

    async def cog_unload(self):
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    # ----------------------------- helpers -----------------------------

    async def _get_channel(self, channel_id: int = CHANNEL_ID1):
        return self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)

    async def _log(self, text: str, level: int = logging.WARNING, exc_info: bool = False):
        """Write to the console and post to the log channel (CHANNEL_ID2)."""
        logger.log(level, text, exc_info=exc_info)
        try:
            channel = await self._get_channel(CHANNEL_ID2)
            await channel.send(text[:2000], allowed_mentions=NO_MENTIONS)
        except Exception:
            logger.exception("Couldn't post to the city log channel")

    async def _delete_message(self, message: discord.Message):
        try:
            await message.delete()
        except discord.HTTPException:
            pass  # already gone, or missing Manage Messages

    async def _delete_reminders(self, user_id: int, reminders: dict):
        """Delete the bot's reminder messages (from the person's DMs or from CHANNEL_ID1)."""
        if not reminders:
            return
        dm = None
        for message_id, channel_id in reminders.items():
            try:
                if channel_id is None:
                    if dm is None:
                        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                        dm = user.dm_channel or await user.create_dm()
                    await dm.get_partial_message(message_id).delete()
                else:
                    channel = await self._get_channel(channel_id)
                    await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass  # already deleted

    def _cancel_task(self, user_id: int):
        task = self.tasks.pop(user_id, None)
        if task:
            task.cancel()

    def _schedule(self, user_id: int, delay: float):
        self._cancel_task(user_id)
        self.tasks[user_id] = asyncio.create_task(self._timer(user_id, delay))

    async def _start_timer(self, user_id: int, delay: float = REMINDER_DELAY, entering: bool = False):
        """(Re)start the 1 hour timer, save it, and delete any old reminders.
        entering=True marks the moment the person entered the city (used by .who)."""
        self._cancel_task(user_id)
        old = self.pending.pop(user_id, {})
        update = {"due": time.time() + delay, "reminders": {}}
        if entering:
            update["since"] = time.time()
        await self.coll.update_one(
            {"_id": user_id},
            {"$set": update, "$unset": {"reminder_ids": ""}},
            upsert=True,
        )
        self._schedule(user_id, delay)
        await self._delete_reminders(user_id, old)

    async def _clear(self, user_id: int):
        """Forget this user's timer and delete any outstanding reminders."""
        self._cancel_task(user_id)
        old = self.pending.pop(user_id, {})
        self.dm_failures.pop(user_id, None)
        await self.coll.delete_one({"_id": user_id})
        await self._delete_reminders(user_id, old)

    async def _timer(self, user_id: int, delay: float):
        """Wait, send the reminder, then keep re-pinging until this task is cancelled
        (it gets cancelled as soon as the user responds)."""
        try:
            await self.bot.wait_until_ready()
            await asyncio.sleep(delay)
            while True:
                try:
                    await self._send_reminder(user_id)
                except Exception as exc:
                    await self._log(
                        f"❌ City reminder failed for <@{user_id}>: `{type(exc).__name__}: {exc}`",
                        logging.ERROR,
                        exc_info=True,
                    )
                await asyncio.sleep(REPING_DELAY)
        except asyncio.CancelledError:
            raise

    async def _send_reminder(self, user_id: int):
        """DM the person an embed reminder. If DMs fail DM_FAILURES_BEFORE_CHANNEL times
        in a row, post it (with a ping) in CHANNEL_ID1 instead."""
        embed = discord.Embed(
            title=REMINDER_TITLE,
            description=fmt(REMINDER_ENTRY, user_id),
            color=REMINDER_COLOR,
        )
        if REMINDER_FOOTER:
            embed.set_footer(text=REMINDER_FOOTER)

        channel_id = None  # None = delivered by DM
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            msg = await user.send(embed=embed)
            self.dm_failures.pop(user_id, None)  # DMs work
        except discord.Forbidden:
            fails = self.dm_failures.get(user_id, 0) + 1
            self.dm_failures[user_id] = fails
            await self.coll.update_one(
                {"_id": user_id}, {"$set": {"due": time.time() + REPING_DELAY}}
            )

            if fails < DM_FAILURES_BEFORE_CHANNEL:
                await self._log(
                    f"⚠️ Couldn't DM the city reminder to <@{user_id}> (their DMs may be closed). "
                    f"Failed DMs in a row: {fails}. I'll try again at the next re-ping."
                )
                return

            channel = await self._get_channel(CHANNEL_ID1)
            msg = await channel.send(f"<@{user_id}>", embed=embed)  # the mention pings them
            channel_id = CHANNEL_ID1
            await self._log(
                f"⚠️ Couldn't DM <@{user_id}> ({fails} failed DMs in a row), "
                f"so I sent their reminder in <#{CHANNEL_ID1}> instead."
            )

        self.pending.setdefault(user_id, {})[msg.id] = channel_id
        await self.coll.update_one(
            {"_id": user_id},
            {
                "$set": {
                    f"reminders.{msg.id}": channel_id,
                    "due": time.time() + REPING_DELAY,  # next re-ping
                }
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
        await channel.send(fmt(DISABLE_MESSAGE, user_id))

    # ---- records (.time) ----

    async def _add_record(self, user_id: int):
        await self.coll.update_one(
            {"_id": f"count:{user_id}"},
            {"$inc": {"count": 1}, "$set": {"user_id": user_id}},
            upsert=True,
        )

    async def _get_record(self, user_id: int) -> int:
        doc = await self.coll.find_one({"_id": f"count:{user_id}"})
        return doc["count"] if doc else 0

    # ----------------------------- .city -------------------------------

    @commands.command()
    @commands.guild_only()
    async def city(self, ctx, member: discord.Member = None):
        """Let people know you're in city."""
        member = member or ctx.author
        try:
            await self._toggle(ctx, member)
        finally:
            await self._delete_message(ctx.message)  # clean up the command message

    async def _toggle(self, ctx, member: discord.Member):
        role = ctx.guild.get_role(ROLE_ID)
        if role is None:
            await ctx.send("The city role wasn't found. Check `ROLE_ID` in the plugin.")
            await self._log(f"❌ `.city` failed: the role `{ROLE_ID}` wasn't found.", logging.ERROR)
            return

        # Already has the role -> disable
        if role in member.roles:
            try:
                await ctx.message.add_reaction(THUMBS_UP)
            except discord.HTTPException:
                pass
            try:
                await self._leave_city(ctx.guild, member.id)
            except discord.Forbidden:
                await ctx.send("I don't have permission to remove that role.")
                await self._log(
                    f"❌ I couldn't remove the city role from <@{member.id}> "
                    "(missing permissions or role hierarchy).",
                    logging.ERROR,
                )
                return
            await asyncio.sleep(COMMAND_DELETE_DELAY)  # let the 👍 be seen
            return

        # Doesn't have the role -> enable
        try:
            await member.add_roles(role, reason=f"Entered the city (set by {ctx.author})")
        except discord.Forbidden:
            await ctx.send(
                "I don't have permission to give that role. "
                "Make sure I have **Manage Roles** and my role is above it."
            )
            await self._log(
                f"❌ I couldn't give the city role to <@{member.id}> "
                "(missing permissions or role hierarchy).",
                logging.ERROR,
            )
            return

        await self._add_record(member.id)  # +1 on their .time record

        channel = await self._get_channel()
        await channel.send(fmt(ENABLE_MESSAGE, member.id))
        await self._start_timer(member.id, entering=True)

    # ------------------------------ .who -------------------------------

    @commands.command(name="who")
    @commands.guild_only()
    async def who(self, ctx):
        """Show who is currently in the city."""
        try:
            role = ctx.guild.get_role(ROLE_ID)
            if role is None:
                await ctx.send("The city role wasn't found. Check `ROLE_ID` in the plugin.")
                await self._log(f"❌ `.who` failed: the role `{ROLE_ID}` wasn't found.", logging.ERROR)
                return

            members = role.members
            if not members:
                await ctx.send(WHO_EMPTY_MESSAGE)
                return

            ids = [m.id for m in members]
            docs = {d["_id"]: d async for d in self.coll.find({"_id": {"$in": ids}})}

            # Oldest entry first; people with an unknown entry time go last
            entries = [(docs.get(m.id, {}).get("since"), m) for m in members]
            entries.sort(key=lambda e: (e[0] is None, e[0] or 0, e[1].display_name.lower()))

            lines = []
            for since, m in entries:
                if since:
                    when, ago = f"<t:{int(since)}:t>", f"<t:{int(since)}:R>"
                else:
                    when = ago = "unknown"
                lines.append(render(WHO_ENTRY, user=m.mention, time=when, ago=ago))

            embed = discord.Embed(title=WHO_TITLE, description=join_lines(lines), color=WHO_COLOR)
            if WHO_FOOTER:
                embed.set_footer(text=render(WHO_FOOTER, count=len(entries)))
            await ctx.send(embed=embed)
        finally:
            await self._delete_message(ctx.message)

    # ------------------------------ .time ------------------------------

    @commands.group(name="time", invoke_without_command=True)
    @commands.guild_only()
    async def time_group(self, ctx):
        """Show how many times each person has joined the city.
        Subcommands: time view [@user], time change @user <number> (admins)"""
        try:
            cursor = self.coll.find({"count": {"$gt": 0}}).sort([("count", -1), ("user_id", 1)])
            docs = [d async for d in cursor]
            if not docs:
                await ctx.send(TIME_EMPTY_MESSAGE)
                return

            lines = [
                render(TIME_ENTRY, rank=i, user=f"<@{d['user_id']}>", count=d["count"])
                for i, d in enumerate(docs, start=1)
            ]
            embed = discord.Embed(title=TIME_TITLE, description=join_lines(lines), color=TIME_COLOR)
            if TIME_FOOTER:
                embed.set_footer(
                    text=render(TIME_FOOTER, people=len(docs), total=sum(d["count"] for d in docs))
                )
            await ctx.send(embed=embed)
        finally:
            await self._delete_message(ctx.message)

    @time_group.command(name="view")
    @commands.guild_only()
    async def time_view(self, ctx, member: discord.Member = None):
        """Show one person's record: time view @user (yourself if no user given)"""
        member = member or ctx.author
        try:
            count = await self._get_record(member.id)
            await ctx.send(
                render(TIME_VIEW_MESSAGE, user=member.mention, count=count),
                allowed_mentions=NO_MENTIONS,
            )
        finally:
            await self._delete_message(ctx.message)

    @time_group.command(name="change")
    @commands.guild_only()
    async def time_change(self, ctx, target: Union[discord.Member, discord.Role], count: int):
        """Set a record: time change @user <number>
        Or set it for everyone with a role: time change @role <number>"""
        try:
            if not ctx.author.guild_permissions.administrator:
                await ctx.send(TIME_NO_PERMISSION_MESSAGE)
                return
            if count < 0:
                await ctx.send(TIME_INVALID_MESSAGE)
                return

            # ---- a role: mass-change everyone who has it (bots are skipped) ----
            if isinstance(target, discord.Role):
                members = [m for m in target.members if not m.bot]
                if not members:
                    await ctx.send(
                        render(TIME_ROLE_EMPTY_MESSAGE, role=target.mention),
                        allowed_mentions=NO_MENTIONS,
                    )
                    return

                await self.coll.bulk_write(
                    [
                        UpdateOne(
                            {"_id": f"count:{m.id}"},
                            {"$set": {"count": count, "user_id": m.id}},
                            upsert=True,
                        )
                        for m in members
                    ],
                    ordered=False,
                )
                await ctx.send(
                    render(
                        TIME_CHANGE_ROLE_MESSAGE,
                        admin=ctx.author.mention,
                        role=target.mention,
                        people=len(members),
                        count=count,
                    ),
                    allowed_mentions=NO_MENTIONS,
                )
                await self._log(
                    f"ℹ️ {ctx.author.mention} set the city record of {len(members)} member(s) "
                    f"with {target.mention} to {count}.",
                    logging.INFO,
                )
                return

            # ---- a single member ----
            await self.coll.update_one(
                {"_id": f"count:{target.id}"},
                {"$set": {"count": count, "user_id": target.id}},
                upsert=True,
            )
            await ctx.send(
                render(TIME_CHANGE_MESSAGE, user=target.mention, count=count, admin=ctx.author.mention),
                allowed_mentions=NO_MENTIONS,
            )
            await self._log(
                f"ℹ️ {ctx.author.mention} set <@{target.id}>'s city record to {count}.",
                logging.INFO,
            )
        finally:
            await self._delete_message(ctx.message)

    # ----------------------------- reactions ----------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        user_id = payload.user_id

        # Only the pinged user reacting to one of their own reminders counts
        if payload.message_id not in self.pending.get(user_id, {}):
            return

        emoji = str(payload.emoji)

        if emoji.startswith(THUMBS_UP):
            # Still in the city -> restart the 1 hour timer (also deletes the reminders)
            await self._start_timer(user_id)

        elif emoji.startswith(THUMBS_DOWN):
            guild = (await self._get_channel()).guild  # reactions in DMs carry no guild_id
            try:
                await self._leave_city(guild, user_id)  # also deletes the reminders
            except discord.Forbidden:
                await self._log(
                    f"❌ I couldn't remove the city role from <@{user_id}> "
                    "(missing permissions or role hierarchy).",
                    logging.ERROR,
                )


async def setup(bot):
    await bot.add_cog(City(bot))
