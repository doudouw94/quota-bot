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

OBJECTIFS = {
    "Contenair": 40,
    "Atm": 12,
    "Superette": 2
}

# ==================== DATABASE ====================
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def migrate_database():
    with get_db() as conn:
        with conn.cursor() as c:
            # Nettoyage des anciennes tables
            c.execute("DROP TABLE IF EXISTS quotas;")
            c.execute("DROP TABLE IF EXISTS tableau_messages;")
            c.execute("DROP TABLE IF EXISTS daily_messages;")

            c.execute('''
                CREATE TABLE IF NOT EXISTS quotas (
                    id SERIAL PRIMARY KEY,
                    quota_date DATE NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    image_url TEXT,
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

    print(f"🔄 Sync quotas → +{added} / -{removed}")
    await update_daily_tableau()

# ==================== VIEWS ====================
class QuotaSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Contenair", value="Contenair", emoji="📦"),
            discord.SelectOption(label="ATM", value="Atm", emoji="🏧"),
            discord.SelectOption(label="Superette", value="Superette", emoji="🏪")
        ]
        super().__init__(placeholder="Choisis le type de quota...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.ask_quantity(interaction, self.values[0])

    async def ask_quantity(self, interaction: discord.Interaction, type_quota: str):
        try:
            await interaction.followup.send(f"**{type_quota}** sélectionné.\n**Combien en as-tu fait ?** (réponds avec un nombre)", ephemeral=True)
            msg_nombre = await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.channel == interaction.channel, timeout=90)
            qty = int(msg_nombre.content.strip())
            if qty <= 0:
                raise ValueError
            await interaction.followup.send("📸 Envoie ta photo maintenant", ephemeral=True)
            photo_msg = await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.attachments and m.channel == interaction.channel, timeout=150)
            await self.process_quota(interaction, type_quota, qty, photo_msg, msg_nombre)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Temps écoulé.", ephemeral=True)
        except ValueError:
            await interaction.followup.send("❌ Nombre invalide.", ephemeral=True)
        except Exception as e:
            print(e)
            await interaction.followup.send("❌ Erreur.", ephemeral=True)

    async def process_quota(self, interaction: discord.Interaction, type_quota: str, qty: int, photo_msg, msg_nombre=None):
        try:
            today = date.today()
            image_url = photo_msg.attachments[0].url

            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("""
                        INSERT INTO quotas (quota_date, user_id, username, type, quantity, image_url)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (today, interaction.user.id, interaction.user.display_name, type_quota, qty, image_url))
                    conn.commit()

            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                file = await photo_msg.attachments[0].to_file()
                embed = discord.Embed(title="📸 Quota Soumis", color=discord.Color.green())
                embed.add_field(name="Membre", value=interaction.user.mention, inline=False)
                embed.add_field(name="Type", value=type_quota, inline=True)
                embed.add_field(name="Quantité", value=qty, inline=True)
                await log_channel.send(embed=embed, file=file)

            await interaction.followup.send("✅ **Quota enregistré avec succès !**", ephemeral=True)

            try:
                if msg_nombre:
                    await msg_nombre.delete()
                await photo_msg.delete()
            except:
                pass

            await update_daily_tableau()

        except Exception as e:
            print(e)
            await interaction.followup.send("❌ Erreur d'enregistrement.", ephemeral=True)

class QuotaSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(QuotaSelect())

class QuotaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Faire quota", style=discord.ButtonStyle.green)
    async def faire_quota(self, interaction: discord.Interaction, button):
        if not has_vendetta(interaction.user):
            return await interaction.response.send_message("❌ Tu n'as pas le rôle Vendetta.", ephemeral=True)

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT 1 FROM authorized_users WHERE user_id = %s", (interaction.user.id,))
                if not c.fetchone():
                    return await interaction.response.send_message("❌ Tu n'es pas autorisé.", ephemeral=True)

        objectifs_text = "**Objectifs du jour :**\n"
        for t, nb in OBJECTIFS.items():
            objectifs_text += f"• **{t}** → **{nb}**\n"
        await interaction.response.send_message(objectifs_text + "\nChoisis ton quota :", view=QuotaSelectView(), ephemeral=True)

# ==================== TABLEAU QUOTIDIEN ====================
async def create_daily_tableau(channel: discord.TextChannel = None):
    today = date.today()

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT message_id FROM daily_messages WHERE quota_date = %s", (today,))
            if c.fetchone():
                print(f"Tableau déjà existant pour {today}")
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
            print("❌ Aucun channel défini")
            return

    embed = discord.Embed(
        title=f"📋 Quotas du {today.strftime('%d/%m/%Y')}",
        description="**Coche si fait aujourd'hui**",
        color=discord.Color.blue()
    )
    embed.set_footer(text="Mis à jour automatiquement")

    msg = await channel.send(embed=embed)

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO daily_messages (quota_date, channel_id, message_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (quota_date) DO NOTHING
            """, (today, channel.id, msg.id))
            conn.commit()

    await update_daily_tableau()
    print(f"✅ Nouveau tableau créé pour {today}")

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
        if not channel:
            return
        message = await channel.fetch_message(message_id)

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT user_id, username FROM authorized_users ORDER BY username")
                authorized = dict(c.fetchall())

                c.execute("""
                    SELECT user_id, type, SUM(quantity)
                    FROM quotas
                    WHERE quota_date = %s
                    GROUP BY user_id, type
                """, (today,))
                done = {}
                for user_id, qtype, total in c.fetchall():
                    if user_id not in done:
                        done[user_id] = {}
                    done[user_id][qtype] = total

        desc = ""
        for user_id, username in authorized.items():
            user_done = done.get(user_id, {})
            contenair = "✅" if user_done.get("Contenair", 0) > 0 else "❌"
            atm = "✅" if user_done.get("Atm", 0) > 0 else "❌"
            superette = "✅" if user_done.get("Superette", 0) > 0 else "❌"
            desc += f"**{username}**\n"
            desc += f"📦 {contenair} | 🏧 {atm} | 🏪 {superette}\n\n"

        if not desc:
            desc = "Aucun membre autorisé."

        embed = discord.Embed(
            title=f"📋 Quotas du {today.strftime('%d/%m/%Y')}",
            description=desc,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"MAJ : {datetime.now(UTC).strftime('%H:%M:%S')}")
        await message.edit(embed=embed)

    except Exception as e:
        print(f"Erreur tableau quotidien: {e}")

# ==================== COMMANDES ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def settableaux(ctx):
    await ctx.send("✅ Channel des tableaux défini. Création du tableau du jour...")
    await create_daily_tableau(ctx.channel)

@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    await ctx.send("🔄 Synchronisation en cours...")
    await sync_authorized_users(ctx.guild)
    await ctx.send("✅ Synchronisation terminée.")

@bot.command()
@commands.has_permissions(administrator=True)
async def listusers(ctx):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT username, user_id FROM authorized_users ORDER BY username")
            users = c.fetchall()
    if not users:
        return await ctx.send("❌ Aucun utilisateur autorisé.")
    desc = "\n".join([f"• **{u}** (`{uid}`)" for u, uid in users])
    await ctx.send(embed=discord.Embed(title="👥 Utilisateurs Autorisés (Vendetta)", description=desc, color=discord.Color.green()))

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(
        title="📊 Système de Quotas Diamond City",
        description="Clique sur le bouton ci-dessous pour faire ton quota.",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=QuotaView())

# ==================== TASKS ====================
@tasks.loop(minutes=5)
async def auto_update():
    await update_daily_tableau()

@tasks.loop(time=datetime.strptime("00:05", "%H:%M").time())
async def daily_reset():
    print("🔄 Création du tableau du jour...")
    await create_daily_tableau()

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not auto_update.is_running():
        auto_update.start()
    if not daily_reset.is_running():
        daily_reset.start()

    for guild in bot.guilds:
        await sync_authorized_users(guild)

    await create_daily_tableau()

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_has = has_vendetta(before)
    after_has = has_vendetta(after)

    if before_has == after_has:
        return

    try:
        with get_db() as conn:
            with conn.cursor() as c:
                if after_has and not before_has:
                    c.execute("""
                        INSERT INTO authorized_users (user_id, username)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
                    """, (after.id, after.display_name))
                    print(f"✅ {after.display_name} a reçu Vendetta → ajouté")
                elif before_has and not after_has:
                    c.execute("DELETE FROM authorized_users WHERE user_id = %s", (after.id,))
                    print(f"❌ {after.display_name} a perdu Vendetta → retiré")
                conn.commit()

        await update_daily_tableau()
    except Exception as e:
        print(f"Erreur on_member_update: {e}")

@bot.event
async def on_member_remove(member: discord.Member):
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("DELETE FROM authorized_users WHERE user_id = %s", (member.id,))
                conn.commit()
                print(f"🗑️ {member.display_name} a quitté → retiré")
        await update_daily_tableau()
    except Exception as e:
        print(f"Erreur on_member_remove: {e}")

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant")
