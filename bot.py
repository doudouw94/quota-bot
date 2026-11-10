import discord
from discord.ext import commands, tasks
import psycopg2
import os
from datetime import date, datetime, timedelta, UTC
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==================== CONFIG ====================
LOG_CHANNEL_ID = 1508882228166398073
ROLE_VENDETTA = "Vendetta"

# ==================== DATABASE ====================
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def migrate_database():
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DROP TABLE IF EXISTS quotas;")
            c.execute("DROP TABLE IF EXISTS daily_messages;")

            c.execute('''
                CREATE TABLE IF NOT EXISTS quotas (
                    id SERIAL PRIMARY KEY,
                    quota_date DATE NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    type TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    submitted_at TIMESTAMP DEFAULT NOW()
                );
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS authorized_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT
                );
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS daily_messages (
                    quota_date DATE PRIMARY KEY,
                    channel_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL
                );
            ''')
            conn.commit()
    print("✅ Base de données prête !")

migrate_database()

def has_vendetta(member: discord.Member) -> bool:
    return any(role.name.lower() == ROLE_VENDETTA.lower() for role in member.roles)

async def sync_authorized_users(guild: discord.Guild):
    added = 0
    removed = 0
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT user_id FROM authorized_users")
            current = {row[0] for row in c.fetchall()}

            members_with_role = [m for m in guild.members if has_vendetta(m) and not m.bot]
            members_ids = {m.id for m in members_with_role}

            for member in members_with_role:
                if member.id not in current:
                    c.execute("""
                        INSERT INTO authorized_users (user_id, username)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
                    """, (member.id, member.display_name))
                    added += 1

            for user_id in current:
                if user_id not in members_ids:
                    c.execute("DELETE FROM authorized_users WHERE user_id = %s", (user_id,))
                    removed += 1

            conn.commit()

    print(f"🔄 Sync → +{added} / -{removed}")
    await update_daily_tableau()

# ==================== VIEWS MODIFICATION ====================
class ToggleView(discord.ui.View):
    def __init__(self, user_id: int, username: str):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.username = username

    async def toggle(self, interaction: discord.Interaction, qtype: str):
        today = date.today()
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT 1 FROM quotas 
                    WHERE quota_date = %s AND user_id = %s AND type = %s
                """, (today, self.user_id, qtype))
                exists = c.fetchone()

                if exists:
                    c.execute("""
                        DELETE FROM quotas 
                        WHERE quota_date = %s AND user_id = %s AND type = %s
                    """, (today, self.user_id, qtype))
                    action = "décoché"
                else:
                    c.execute("""
                        INSERT INTO quotas (quota_date, user_id, username, type, quantity)
                        VALUES (%s, %s, %s, %s, 1)
                    """, (today, self.user_id, self.username, qtype))
                    action = "coché"
                conn.commit()

        await interaction.response.send_message(f"✅ **{qtype}** {action} pour **{self.username}**", ephemeral=True)
        await update_daily_tableau()

    @discord.ui.button(label="📦 Contenair", style=discord.ButtonStyle.secondary)
    async def contenair(self, interaction: discord.Interaction, button):
        await self.toggle(interaction, "Contenair")

    @discord.ui.button(label="🏧 ATM", style=discord.ButtonStyle.secondary)
    async def atm(self, interaction: discord.Interaction, button):
        await self.toggle(interaction, "Atm")

    @discord.ui.button(label="🏪 Superette", style=discord.ButtonStyle.secondary)
    async def superette(self, interaction: discord.Interaction, button):
        await self.toggle(interaction, "Superette")

class MemberSelect(discord.ui.Select):
    def __init__(self, members: list):
        options = [
            discord.SelectOption(label=username[:100], value=str(user_id))
            for user_id, username in members[:25]
        ]
        super().__init__(placeholder="Choisis un membre...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        user_id = int(self.values[0])
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT username FROM authorized_users WHERE user_id = %s", (user_id,))
                username = c.fetchone()[0]

        await interaction.response.send_message(
            f"Modification pour **{username}**",
            view=ToggleView(user_id, username),
            ephemeral=True
        )

class ModifierView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✏️ Modifier", style=discord.ButtonStyle.blurple, custom_id="modifier_quota")
    async def modifier(self, interaction: discord.Interaction, button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Seul un admin peut modifier.", ephemeral=True)

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT user_id, username FROM authorized_users ORDER BY username")
                members = c.fetchall()

        if not members:
            return await interaction.response.send_message("❌ Aucun membre autorisé.", ephemeral=True)

        view = discord.ui.View(timeout=120)
        view.add_item(MemberSelect(members))
        await interaction.response.send_message("Choisis un membre :", view=view, ephemeral=True)

# ==================== TABLEAU QUOTIDIEN ====================
async def create_daily_tableau(channel: discord.TextChannel = None):
    today = date.today()

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT message_id FROM daily_messages WHERE quota_date = %s", (today,))
            if c.fetchone():
                await update_daily_tableau()
                return

    if not channel:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT channel_id FROM daily_messages ORDER BY quota_date DESC LIMIT 1")
                row = c.fetchone()
                if row:
                    channel = bot.get_channel(row[0])
        if not channel:
            return

    embed = discord.Embed(
        title=f"📋 Quotas du {today.strftime('%d/%m/%Y')}",
        description="Chargement...",
        color=discord.Color.blue()
    )
    msg = await channel.send(embed=embed, view=ModifierView())

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO daily_messages (quota_date, channel_id, message_id)
                VALUES (%s, %s, %s)
            """, (today, channel.id, msg.id))
            conn.commit()

    await update_daily_tableau()

async def update_daily_tableau():
    today = date.today()
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT channel_id, message_id FROM daily_messages WHERE quota_date = %s", (today,))
                row = c.fetchone()
                if not row:
                    return
                channel_id, message_id = row

        channel = bot.get_channel(channel_id)
        message = await channel.fetch_message(message_id)

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT user_id, username FROM authorized_users ORDER BY username")
                authorized = dict(c.fetchall())

                c.execute("""
                    SELECT user_id, type FROM quotas WHERE quota_date = %s
                """, (today,))
                done = {}
                for user_id, qtype in c.fetchall():
                    if user_id not in done:
                        done[user_id] = set()
                    done[user_id].add(qtype)

        desc = ""
        for user_id, username in authorized.items():
            user_done = done.get(user_id, set())
            c = "✅" if "Contenair" in user_done else "❌"
            a = "✅" if "Atm" in user_done else "❌"
            s = "✅" if "Superette" in user_done else "❌"
            desc += f"**{username}**\n📦 {c} | 🏧 {a} | 🏪 {s}\n\n"

        if not desc:
            desc = "Aucun membre autorisé."

        embed = discord.Embed(
            title=f"📋 Quotas du {today.strftime('%d/%m/%Y')}",
            description=desc,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"MAJ : {datetime.now(UTC).strftime('%H:%M:%S')}")
        await message.edit(embed=embed, view=ModifierView())

    except Exception as e:
        print(f"Erreur tableau: {e}")

# ==================== COMMANDES ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def settableaux(ctx):
    await ctx.send("✅ Channel défini. Création du tableau...")
    await create_daily_tableau(ctx.channel)

@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    await ctx.send("🔄 Synchronisation...")
    await sync_authorized_users(ctx.guild)
    await ctx.send("✅ Terminé.")

@bot.command()
@commands.has_permissions(administrator=True)
async def listusers(ctx):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT username FROM authorized_users ORDER BY username")
            users = [row[0] for row in c.fetchall()]
    if not users:
        return await ctx.send("Aucun membre.")
    await ctx.send("**Membres autorisés :**\n" + "\n".join(f"• {u}" for u in users))

# ==================== TASKS ====================
@tasks.loop(minutes=10)
async def auto_update():
    await update_daily_tableau()

@tasks.loop(time=datetime.strptime("00:05", "%H:%M").time())
async def daily_reset():
    await create_daily_tableau()

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    print(f"✅ {bot.user} en ligne")
    bot.add_view(ModifierView())
    if not auto_update.is_running():
        auto_update.start()
    if not daily_reset.is_running():
        daily_reset.start()

    for guild in bot.guilds:
        await sync_authorized_users(guild)
    await create_daily_tableau()

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if has_vendetta(before) == has_vendetta(after):
        return
    with get_db() as conn:
        with conn.cursor() as c:
            if has_vendetta(after):
                c.execute("""
                    INSERT INTO authorized_users (user_id, username)
                    VALUES (%s, %s) ON CONFLICT DO UPDATE SET username = EXCLUDED.username
                """, (after.id, after.display_name))
            else:
                c.execute("DELETE FROM authorized_users WHERE user_id = %s", (after.id,))
            conn.commit()
    await update_daily_tableau()

@bot.event
async def on_member_remove(member: discord.Member):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM authorized_users WHERE user_id = %s", (member.id,))
            conn.commit()
    await update_daily_tableau()

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant")
