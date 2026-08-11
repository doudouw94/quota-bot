import discord
from discord.ext import commands, tasks
import psycopg2
import os
from datetime import date, datetime, timedelta
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==================== CONFIG ====================
LOG_CHANNEL_ID = 1508882228166398073
TABLEAU_CHANNEL_ID = None
CURRENT_CLASSEMENT_MSG_ID = None
CURRENT_QUOTAS_MSG_ID = None
LAST_CREATED_WEEK = None

# ==================== OBJECTIFS ====================
OBJECTIFS = {
    "Contenair": 40,
    "Atm": 12,
    "Superette": 2,
    "Cambriolage": 2
}

# ==================== DATABASE ====================
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def migrate_database():
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute('''
                CREATE TABLE IF NOT EXISTS quotas (
                    id SERIAL PRIMARY KEY,
                    week_start DATE NOT NULL,
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
            conn.commit()
    print("✅ Base de données prête !")

migrate_database()

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
        type_quota = self.values[0]
        await self.ask_quantity(interaction, type_quota)

    async def ask_quantity(self, interaction: discord.Interaction, type_quota: str):
        try:
            await interaction.followup.send(f"**{type_quota}** sélectionné.\n**Combien en as-tu fait ?** (réponds avec un nombre)", ephemeral=True)
            msg_nombre = await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.channel == interaction.channel, timeout=90)
            qty = int(msg_nombre.content.strip())
            if qty <= 0: raise ValueError
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
            week_start = today - timedelta(days=today.weekday())
            image_url = photo_msg.attachments[0].url
            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("""
                        INSERT INTO quotas (week_start, user_id, username, type, quantity, image_url)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (week_start, interaction.user.id, interaction.user.display_name, type_quota, qty, image_url))
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
                if msg_nombre: await msg_nombre.delete()
                await photo_msg.delete()
            except:
                pass
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
        # Répond immédiatement pour éviter le timeout
        await interaction.response.defer(ephemeral=True)

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT 1 FROM authorized_users WHERE user_id = %s", (interaction.user.id,))
                if not c.fetchone():
                    return await interaction.followup.send("❌ Tu n'es pas autorisé.", ephemeral=True)

        objectifs_text = "**Objectifs Hebdomadaires :**\n"
        for t, nb in OBJECTIFS.items():
            if t != "Cambriolage":
                objectifs_text += f"• **{t}** → **{nb}**\n"

        await interaction.followup.send(objectifs_text + "\nChoisis ton quota :", view=QuotaSelectView(), ephemeral=True)

    @discord.ui.button(label="📢 Rappel Inactifs", style=discord.ButtonStyle.red)
    async def rappel(self, interaction: discord.Interaction, button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)

        # Répond immédiatement
        await interaction.response.defer(ephemeral=True)
        await do_rappel(interaction)

# ==================== FONCTIONS ====================
async def do_rappel(interaction: discord.Interaction):
    week_start = date.today() - timedelta(days=date.today().weekday())
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT DISTINCT user_id FROM quotas WHERE week_start = %s", (week_start,))
            active = {row[0] for row in c.fetchall()}
            c.execute("SELECT user_id FROM authorized_users")
            all_users = [row[0] for row in c.fetchall()]
    reminded = 0
    for user_id in all_users:
        if user_id not in active:
            member = interaction.guild.get_member(user_id)
            if member:
                try:
                    await member.send("⚠️ **Rappel Quotas Diamond City**\nTu n'as pas encore fait de quota cette semaine.")
                    reminded += 1
                except:
                    pass
    await interaction.followup.send(f"✅ Rappel envoyé à **{reminded}** membre(s).", ephemeral=True)

async def create_new_tableaux(force=False):
    global CURRENT_CLASSEMENT_MSG_ID, CURRENT_QUOTAS_MSG_ID, LAST_CREATED_WEEK
    if not TABLEAU_CHANNEL_ID:
        return
    channel = bot.get_channel(TABLEAU_CHANNEL_ID)
    if not channel:
        return

    week_start = date.today() - timedelta(days=date.today().weekday())

    if not force and LAST_CREATED_WEEK == week_start:
        return

    embed1 = discord.Embed(title=f"🏆 Classement par Points - Semaine du {week_start}", color=discord.Color.gold())
    embed1.description = "Chargement..."
    msg1 = await channel.send(embed=embed1)
    CURRENT_CLASSEMENT_MSG_ID = msg1.id

    embed2 = discord.Embed(title=f"📋 Progression des Quotas - Semaine du {week_start}", color=discord.Color.blue())
    embed2.description = "Chargement..."
    msg2 = await channel.send(embed=embed2)
    CURRENT_QUOTAS_MSG_ID = msg2.id

    LAST_CREATED_WEEK = week_start
    await update_tableaux()

async def update_classement():
    if not CURRENT_CLASSEMENT_MSG_ID or not TABLEAU_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(TABLEAU_CHANNEL_ID)
        message = await channel.fetch_message(CURRENT_CLASSEMENT_MSG_ID)
        week_start = date.today() - timedelta(days=date.today().weekday())
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT username,
                           SUM(quantity * CASE type
                               WHEN 'Contenair' THEN 1
                               WHEN 'Atm' THEN 4.5
                               WHEN 'Superette' THEN 6
                               WHEN 'Cambriolage' THEN 10 END) as total_points
                    FROM quotas WHERE week_start = %s
                    GROUP BY user_id, username
                    ORDER BY total_points DESC NULLS LAST
                """, (week_start,))
                data = c.fetchall()
        embed = discord.Embed(title=f"🏆 Classement par Points - Semaine du {week_start}", color=discord.Color.gold())
        if data:
            desc = "\n".join([f"**{i}.** {user} → **{float(pts):.1f}** pts" for i, (user, pts) in enumerate(data, 1)])
            embed.description = desc
        else:
            embed.description = "Aucun quota enregistré cette semaine."
        embed.set_footer(text=f"MAJ : {datetime.now().strftime('%H:%M:%S')}")
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Erreur classement: {e}")

async def update_quotas_tableau():
    if not CURRENT_QUOTAS_MSG_ID or not TABLEAU_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(TABLEAU_CHANNEL_ID)
        message = await channel.fetch_message(CURRENT_QUOTAS_MSG_ID)
        week_start = date.today() - timedelta(days=date.today().weekday())
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT a.username,
                           COALESCE(SUM(CASE WHEN q.type = 'Contenair' THEN q.quantity ELSE 0 END), 0),
                           COALESCE(SUM(CASE WHEN q.type = 'Atm' THEN q.quantity ELSE 0 END), 0),
                           COALESCE(SUM(CASE WHEN q.type = 'Superette' THEN q.quantity ELSE 0 END), 0)
                    FROM authorized_users a
                    LEFT JOIN quotas q ON a.user_id = q.user_id AND q.week_start = %s
                    GROUP BY a.user_id, a.username
                    ORDER BY a.username
                """, (week_start,))
                data = c.fetchall()

        embed = discord.Embed(title=f"📋 Progression des Quotas - Semaine du {week_start}", color=discord.Color.blue())
        
        desc = "**Objectifs Hebdomadaires :**\n"
        desc += f"• Contenair → **{OBJECTIFS['Contenair']}**\n"
        desc += f"• Atm → **{OBJECTIFS['Atm']}**\n"
        desc += f"• Superette → **{OBJECTIFS['Superette']}**\n\n"

        for row in data:
            username = row[0]
            c, a, s = row[1], row[2], row[3]
            desc += f"**{username}**\n"
            desc += f"📦 Contenair : **{c}/{OBJECTIFS['Contenair']}** | "
            desc += f"🏧 ATM : **{a}/{OBJECTIFS['Atm']}** | "
            desc += f"🏪 Superette : **{s}/{OBJECTIFS['Superette']}**\n\n"

        embed.description = desc
        embed.set_footer(text=f"MAJ : {datetime.now().strftime('%H:%M:%S')}")
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Erreur tableau quotas: {e}")

async def update_tableaux():
    await update_classement()
    await update_quotas_tableau()

# ==================== COMMANDES ====================
@bot.command(name="resetquotas", aliases=["reset", "resetquota"])
@commands.has_permissions(administrator=True)
async def reset_quotas(ctx, confirm: str = None):
    week_start = date.today() - timedelta(days=date.today().weekday())
    if confirm != "confirm":
        return await ctx.send(embed=discord.Embed(title="⚠️ Confirmation", description=f"Pour supprimer les quotas de la semaine du **{week_start}**, tape `!resetquotas confirm`", color=discord.Color.red()))
   
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM quotas WHERE week_start = %s", (week_start,))
            deleted = c.rowcount
            conn.commit()
    await ctx.send(f"✅ {deleted} quotas supprimés.")
    await update_tableaux()

@bot.command()
@commands.has_permissions(administrator=True)
async def checklastweek(ctx):
    week_start = date.today() - timedelta(days=date.today().weekday() + 7)
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM quotas WHERE week_start = %s", (week_start,))
            count = c.fetchone()[0]
    await ctx.send(f"📊 Semaine dernière ({week_start}) : **{count}** quotas enregistrés.")

@bot.command(name="classementpasse", aliases=["lastclassement", "classementlast"])
@commands.has_permissions(administrator=True)
async def classement_passe(ctx):
    week_start = date.today() - timedelta(days=date.today().weekday() + 7)
    
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT username,
                       SUM(quantity * CASE type
                           WHEN 'Contenair' THEN 1
                           WHEN 'Atm' THEN 4.5
                           WHEN 'Superette' THEN 6
                           WHEN 'Cambriolage' THEN 10 END) as total_points
                FROM quotas WHERE week_start = %s
                GROUP BY user_id, username
                ORDER BY total_points DESC NULLS LAST
            """, (week_start,))
            data = c.fetchall()

    embed = discord.Embed(
        title=f"🏆 Classement de la semaine passée ({week_start})",
        color=discord.Color.gold()
    )

    if data:
        desc = "\n".join([f"**{i}.** {user} → **{float(pts):.1f}** pts" for i, (user, pts) in enumerate(data, 1)])
        embed.description = desc
    else:
        embed.description = "Aucun quota enregistré la semaine dernière."

    embed.set_footer(text=f"Demandé par {ctx.author.display_name}")
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def newweek(ctx):
    await ctx.send("🔄 Création des tableaux de la nouvelle semaine...")
    await create_new_tableaux(force=True)

@bot.command()
@commands.has_permissions(administrator=True)
async def settableaux(ctx):
    global TABLEAU_CHANNEL_ID
    TABLEAU_CHANNEL_ID = ctx.channel.id
    await ctx.send("✅ Channel des tableaux défini.")
    await create_new_tableaux(force=True)

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
    await ctx.send(embed=discord.Embed(title="👥 Utilisateurs Autorisés", description=desc, color=discord.Color.green()))

@bot.command()
@commands.has_permissions(administrator=True)
async def adduser(ctx, member: discord.Member):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO authorized_users (user_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      (member.id, member.display_name))
            conn.commit()
    await ctx.send(f"✅ **{member.display_name}** ajouté.")

@bot.command()
@commands.has_permissions(administrator=True)
async def removeuser(ctx, member: discord.Member):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM authorized_users WHERE user_id = %s", (member.id,))
            conn.commit()
    await ctx.send(f"✅ **{member.display_name}** retiré.")

@bot.command(name="removeuserid", aliases=["rid"])
@commands.has_permissions(administrator=True)
async def remove_user_id(ctx, user_id: int):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM authorized_users WHERE user_id = %s", (user_id,))
            deleted = c.rowcount
            conn.commit()
    await ctx.send(f"✅ Utilisateur `{user_id}` retiré." if deleted > 0 else f"❌ ID `{user_id}` non trouvé.")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(title="📊 Système de Quotas Diamond City",
                          description="Clique sur le bouton ci-dessous pour faire ton quota.",
                          color=discord.Color.blue())
    await ctx.send(embed=embed, view=QuotaView())

# ==================== TASKS ====================
@tasks.loop(minutes=2)
async def auto_update():
    await update_tableaux()

@tasks.loop(minutes=30)
async def check_new_week():
    if TABLEAU_CHANNEL_ID and date.today().weekday() == 0 and datetime.now().hour < 3:
        await create_new_tableaux()

@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    
    # Enregistre la vue comme persistante (important après redémarrage)
    bot.add_view(QuotaView())

    if not auto_update.is_running():
        auto_update.start()
    if not check_new_week.is_running():
        check_new_week.start()

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant")
