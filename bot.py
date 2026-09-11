import os
import sqlite3
import datetime

import discord
from discord import app_commands
from discord.ext import commands

DB_PATH = "confessions.db"
TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config (
            guild_id INTEGER PRIMARY KEY,
            confess_channel INTEGER,
            review_channel INTEGER,
            log_channel INTEGER,
            staff_role INTEGER,
            next_number INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS confessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            kind TEXT DEFAULT 'confession',
            number INTEGER,
            parent_number INTEGER,
            user_id INTEGER,
            username TEXT,
            content TEXT,
            status TEXT DEFAULT 'pending',
            moderator_id INTEGER,
            moderator_username TEXT,
            created_at TEXT,
            decided_at TEXT,
            public_message_id INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def get_config(guild_id):
    conn = db()
    row = conn.execute("SELECT * FROM config WHERE guild_id = ?", (guild_id,)).fetchone()
    conn.close()
    return row


def set_config(guild_id, **kwargs):
    conn = db()
    existing = conn.execute("SELECT guild_id FROM config WHERE guild_id = ?", (guild_id,)).fetchone()
    if existing:
        cols = ", ".join(f"{k} = ?" for k in kwargs)
        conn.execute(f"UPDATE config SET {cols} WHERE guild_id = ?", (*kwargs.values(), guild_id))
    else:
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        conn.execute(
            f"INSERT INTO config (guild_id, {cols}) VALUES (?, {placeholders})",
            (guild_id, *kwargs.values()),
        )
    conn.commit()
    conn.close()


def create_confession(guild_id, user_id, username, content, kind="confession", parent_number=None):
    conn = db()
    cur = conn.execute(
        """INSERT INTO confessions
           (guild_id, kind, parent_number, user_id, username, content, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (guild_id, kind, parent_number, user_id, username, content, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def get_confession(cid):
    conn = db()
    row = conn.execute("SELECT * FROM confessions WHERE id = ?", (cid,)).fetchone()
    conn.close()
    return row


def get_confession_by_number(guild_id, number):
    conn = db()
    row = conn.execute(
        "SELECT * FROM confessions WHERE guild_id = ? AND number = ? AND kind = 'confession'",
        (guild_id, number),
    ).fetchone()
    conn.close()
    return row


def decide_confession(cid, status, moderator_id, moderator_username, number=None):
    conn = db()
    conn.execute(
        "UPDATE confessions SET status=?, moderator_id=?, moderator_username=?, decided_at=?, number=? WHERE id=?",
        (
            status,
            moderator_id,
            moderator_username,
            datetime.datetime.utcnow().isoformat(),
            number,
            cid,
        ),
    )
    conn.commit()
    conn.close()


def set_public_message(cid, message_id):
    conn = db()
    conn.execute("UPDATE confessions SET public_message_id = ? WHERE id = ?", (message_id, cid))
    conn.commit()
    conn.close()


def next_number(guild_id):
    conn = db()
    row = conn.execute("SELECT next_number FROM config WHERE guild_id = ?", (guild_id,)).fetchone()
    n = row["next_number"] if row else 1
    conn.execute(
        "UPDATE config SET next_number = ? WHERE guild_id = ?",
        (n + 1, guild_id),
    )
    conn.commit()
    conn.close()
    return n


init_db()


def staff_ping(cfg):
    if cfg and cfg["staff_role"]:
        return f"<@&{cfg['staff_role']}>"
    return None


def make_decision_view(cid):
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(label="Accept", style=discord.ButtonStyle.success, custom_id=f"confess_accept_{cid}")
    )
    view.add_item(
        discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger, custom_id=f"confess_reject_{cid}")
    )
    return view


def make_public_view(number):
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(label="Reply", style=discord.ButtonStyle.secondary, custom_id=f"reply_start_{number}")
    )
    view.add_item(
        discord.ui.Button(label="Confess", style=discord.ButtonStyle.primary, custom_id="confess_start")
    )
    return view


# ---------------------------------------------------------------------
# MODALS
# ---------------------------------------------------------------------
class ConfessModal(discord.ui.Modal, title="New Confession"):
    content = discord.ui.TextInput(
        label="Write your confession",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild_id)
        if not cfg or not cfg["review_channel"]:
            await interaction.response.send_message(
                "This bot hasn't been set up yet. Ask a staff member to run /setup.",
                ephemeral=True,
            )
            return

        review_channel = interaction.guild.get_channel(cfg["review_channel"])
        if review_channel is None:
            await interaction.response.send_message(
                "Review channel not found, please contact staff.", ephemeral=True
            )
            return

        cid = create_confession(
            interaction.guild_id, interaction.user.id, str(interaction.user), str(self.content)
        )

        embed = discord.Embed(
            title=f"New confession pending review (internal #{cid})",
            description=str(self.content),
            color=discord.Color.blurple(),
            timestamp=datetime.datetime.utcnow(),
        )
        embed.add_field(
            name="Author (visible to staff only, here)",
            value=f"{interaction.user.mention} ({interaction.user.id})",
            inline=False,
        )

        await review_channel.send(
            content=staff_ping(cfg),
            embed=embed,
            view=make_decision_view(cid),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await interaction.response.send_message(
            "Your confession has been sent to staff for review. Thank you!", ephemeral=True
        )


class ReplyModal(discord.ui.Modal, title="Reply to a Confession"):
    def __init__(self, parent_number: int):
        super().__init__()
        self.parent_number = parent_number

    content = discord.ui.TextInput(
        label="Write your reply",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild_id)
        if not cfg or not cfg["review_channel"]:
            await interaction.response.send_message(
                "This bot hasn't been set up yet. Ask a staff member to run /setup.",
                ephemeral=True,
            )
            return

        review_channel = interaction.guild.get_channel(cfg["review_channel"])
        if review_channel is None:
            await interaction.response.send_message(
                "Review channel not found, please contact staff.", ephemeral=True
            )
            return

        parent = get_confession_by_number(interaction.guild_id, self.parent_number)

        cid = create_confession(
            interaction.guild_id,
            interaction.user.id,
            str(interaction.user),
            str(self.content),
            kind="reply",
            parent_number=self.parent_number,
        )

        embed = discord.Embed(
            title=f"New reply pending review (internal #{cid}) — replying to confession #{self.parent_number:03}",
            description=str(self.content),
            color=discord.Color.gold(),
            timestamp=datetime.datetime.utcnow(),
        )
        if parent:
            preview = parent["content"][:200] + ("…" if len(parent["content"]) > 200 else "")
            embed.add_field(name=f"Original confession #{self.parent_number:03}", value=preview, inline=False)
        embed.add_field(
            name="Author of the reply (visible to staff only, here)",
            value=f"{interaction.user.mention} ({interaction.user.id})",
            inline=False,
        )

        await review_channel.send(
            content=staff_ping(cfg),
            embed=embed,
            view=make_decision_view(cid),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await interaction.response.send_message(
            "Your reply has been sent to staff for review. Thank you!", ephemeral=True
        )


# ---------------------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------------------
@bot.tree.command(name="confess", description="Submit an anonymous confession")
async def confess(interaction: discord.Interaction):
    await interaction.response.send_modal(ConfessModal())


@bot.tree.command(name="setup", description="Configure the confessions bot channels (staff only)")
@app_commands.describe(
    confessions_channel="Public channel where approved confessions will be posted",
    review_channel="Private staff channel where new confessions arrive for approval",
    log_channel="Private staff channel where accept/reject decisions are logged",
    staff_role="Role allowed to accept or reject confessions",
)
@app_commands.default_permissions(manage_guild=True)
async def setup(
    interaction: discord.Interaction,
    confessions_channel: discord.TextChannel,
    review_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    staff_role: discord.Role,
):
    set_config(
        interaction.guild_id,
        confess_channel=confessions_channel.id,
        review_channel=review_channel.id,
        log_channel=log_channel.id,
        staff_role=staff_role.id,
    )
    await interaction.response.send_message(
        "Configuration saved!\n"
        f"- Public confessions: {confessions_channel.mention}\n"
        f"- Staff review: {review_channel.mention}\n"
        f"- Staff log: {log_channel.mention}\n"
        f"- Staff role: {staff_role.mention}",
        ephemeral=True,
    )


# ---------------------------------------------------------------------
# HANDLE BUTTON INTERACTIONS
# ---------------------------------------------------------------------
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return

    custom_id = interaction.data.get("custom_id", "")

    # ---- "Reply" button on a public confession ----
    if custom_id.startswith("reply_start_"):
        number = int(custom_id.rsplit("_", 1)[1])
        await interaction.response.send_modal(ReplyModal(parent_number=number))
        return

    # ---- "Confess" button on a public confession ----
    if custom_id == "confess_start":
        await interaction.response.send_modal(ConfessModal())
        return

    if not custom_id.startswith("confess_"):
        return

    # ---- Accept / Reject buttons in the review channel ----
    action, cid_str = custom_id.rsplit("_", 1)
    cid = int(cid_str)

    confession = get_confession(cid)
    if confession is None:
        await interaction.response.send_message("Entry not found in the database.", ephemeral=True)
        return
    if confession["status"] != "pending":
        await interaction.response.send_message("This entry has already been handled.", ephemeral=True)
        return

    cfg = get_config(interaction.guild_id)
    member = interaction.user

    has_perm = member.guild_permissions.administrator or member.guild_permissions.manage_guild
    if cfg and cfg["staff_role"]:
        has_perm = has_perm or any(r.id == cfg["staff_role"] for r in member.roles)

    if not has_perm:
        await interaction.response.send_message(
            "You don't have permission to manage confessions.", ephemeral=True
        )
        return

    original_embed = interaction.message.embeds[0]
    is_reply = confession["kind"] == "reply"

    if action == "confess_accept":
        confess_channel = interaction.guild.get_channel(cfg["confess_channel"]) if cfg else None

        if is_reply:
            decide_confession(cid, "approved", member.id, str(member))
            sent_message = None
            if confess_channel:
                reference = None
                parent = get_confession_by_number(interaction.guild_id, confession["parent_number"])
                if parent and parent["public_message_id"]:
                    try:
                        reference = await confess_channel.fetch_message(parent["public_message_id"])
                    except discord.NotFound:
                        reference = None
                public_embed = discord.Embed(
                    title=f"Reply to confession #{confession['parent_number']:03}",
                    description=confession["content"],
                    color=discord.Color.gold(),
                    timestamp=datetime.datetime.utcnow(),
                )
                sent_message = await confess_channel.send(embed=public_embed, reference=reference)

            log_channel = interaction.guild.get_channel(cfg["log_channel"]) if cfg and cfg["log_channel"] else None
            if log_channel:
                log_embed = discord.Embed(
                    title=f"Reply to confession #{confession['parent_number']:03} approved",
                    color=discord.Color.green(),
                )
                log_embed.add_field(name="Content", value=confession["content"], inline=False)
                log_embed.add_field(
                    name="Author", value=f"<@{confession['user_id']}> ({confession['user_id']})", inline=True
                )
                log_embed.add_field(name="Approved by", value=f"{member.mention} ({member.id})", inline=True)
                await log_channel.send(embed=log_embed)

            original_embed.add_field(name="Status", value=f"Approved by {member.mention}", inline=False)
            await interaction.response.edit_message(embed=original_embed, view=None)

        else:
            number = next_number(interaction.guild_id)
            decide_confession(cid, "approved", member.id, str(member), number)

            if confess_channel:
                public_embed = discord.Embed(
                    title=f"Confession #{number:03}",
                    description=confession["content"],
                    color=discord.Color.blurple(),
                    timestamp=datetime.datetime.utcnow(),
                )
                sent_message = await confess_channel.send(embed=public_embed, view=make_public_view(number))
                set_public_message(cid, sent_message.id)

            log_channel = interaction.guild.get_channel(cfg["log_channel"]) if cfg and cfg["log_channel"] else None
            if log_channel:
                log_embed = discord.Embed(title=f"Confession #{number:03} approved", color=discord.Color.green())
                log_embed.add_field(name="Content", value=confession["content"], inline=False)
                log_embed.add_field(
                    name="Author", value=f"<@{confession['user_id']}> ({confession['user_id']})", inline=True
                )
                log_embed.add_field(name="Approved by", value=f"{member.mention} ({member.id})", inline=True)
                await log_channel.send(embed=log_embed)

            original_embed.add_field(
                name="Status", value=f"Approved by {member.mention} — #{number:03}", inline=False
            )
            await interaction.response.edit_message(embed=original_embed, view=None)

    elif action == "confess_reject":
        decide_confession(cid, "rejected", member.id, str(member))

        log_channel = interaction.guild.get_channel(cfg["log_channel"]) if cfg and cfg["log_channel"] else None
        if log_channel:
            title = "Reply rejected" if is_reply else "Confession rejected"
            log_embed = discord.Embed(title=title, color=discord.Color.red())
            log_embed.add_field(name="Content", value=confession["content"], inline=False)
            if is_reply:
                log_embed.add_field(
                    name="In reply to", value=f"Confession #{confession['parent_number']:03}", inline=False
                )
            log_embed.add_field(
                name="Author", value=f"<@{confession['user_id']}> ({confession['user_id']})", inline=True
            )
            log_embed.add_field(name="Rejected by", value=f"{member.mention} ({member.id})", inline=True)
            await log_channel.send(embed=log_embed)

        original_embed.add_field(name="Status", value=f"Rejected by {member.mention}", inline=False)
        await interaction.response.edit_message(embed=original_embed, view=None)


MY_GUILD_ID = 873884389589811231


@bot.event
async def on_ready():
    guild = discord.Object(id=MY_GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)  # instant sync for this server
    await bot.tree.sync()  # global sync (can take up to an hour to propagate)
    print(f"Logged in as {bot.user} - commands synced")


bot.run(TOKEN)
