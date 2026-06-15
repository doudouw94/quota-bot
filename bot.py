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
CLASSEMENT_MESSAGE_ID = None
QUOTAS_MESSAGE_ID = None

# ==================== OBJECTIFS ====================
OBJECTIFS = {
    "Contenair": 40,
    "Atm": 12,
    "Superette": 2,
    "Speedo": 1
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
                    subtype TEXT,
                    quantity INTEGER NOT NULL,
                    image_url TEXT,
                    submitted_at TIMESTAMP DEFAULT NOW()
                );
            ''')
            c.execute("ALTER TABLE quotas ADD COLUMN IF NOT EXISTS subtype TEXT;")
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
# (Je garde exactement tes views sans changement)
class QuotaSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Contenair", value="Contenair", emoji="📦"),
            discord.SelectOption(label="ATM", value="Atm", emoji="🏧"),
            discord.SelectOption(label="Superette", value="Superette", emoji="🏪"),
            discord.SelectOption(label="Speedo", value="Speedo", emoji="⚡")
        ]
        super().__init__(placeholder="Choisis le type de quota...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        type_quota = self.values[0]
       
        if type_quota == "Speedo":
            await interaction.response.send_message(view=SpeedoSubtypeView(), ephemeral=True)
        else:
            await self.ask_quantity(interaction, type_quota)

    async def ask_quantity(self, interaction: discord.Interaction, type_quota: str, subtype: str = None):
        try:
            await interaction.followup.send(f"**{type_quota}**{' - ' + subtype if subtype else ''} sélectionné.\nCombien en as-tu fait ?", ephemeral=True)
           
            msg_nombre = await bot.wait_for('message', check=lambda m: m.author == interaction.user, timeout=60)
            qty = int(msg_nombre.content.strip())
            if qty <= 0: raise ValueError

            await interaction.followup.send("📸 Envoie ta photo maintenant", ephemeral=True)
            photo_msg = await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.attachments, timeout=120)

            today = date.today()
            week_start = today - timedelta(days=today.weekday())
            image_url = photo_msg.attachments[0].url

            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("""
                        INSERT INTO quotas (week_start, user_id, username, type, subtype, quantity, image_url)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (week_start, interaction.user.id, interaction.user.display_name, type_quota, subtype, qty, image_url))
                    conn.commit()

            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                file = await photo_msg.attachments[0].to_file()
                embed = discord.Embed(title="📸 Quota Soumis", color=discord.Color.green())
                embed.add_field(name="Membre", value=interaction.user.mention, inline=False)
                embed.add_field(name="Type", value=type_quota, inline=True)
                if subtype:
                    embed.add_field(name="Sous-type", value=subtype, inline=True)
                embed.add_field(name="Quantité", value=qty, inline=True)
                await log_channel.send(embed=embed, file=file)

            await interaction.followup.send("✅ **Quota enregistré avec succès !**", ephemeral=True)
           
            await asyncio.sleep(2)
            try: await msg_nombre.delete()
            except: pass
            try: await photo_msg.delete()
            except: pass

            await update_tableaux()

        except Exception as e:
            await interaction.followup.send("❌ Erreur.", ephemeral=True)
            print(e)

class QuotaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Faire quota", style=discord.ButtonStyle.green)
    async def faire_quota(self, interaction: discord.Interaction, button):
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT 1 FROM authorized_users WHERE user_id = %s", (interaction.user.id,))
                if not c.fetchone():
                    return await interaction.response.send_message("❌ Tu n'es pas autorisé.", ephemeral=True)
        await interaction.response.send_message(view=QuotaSelectView(), ephemeral=True)

    @discord.ui.button(label="📢 Rappel Inactifs", style=discord.ButtonStyle.red)
    async def rappel(self, interaction: discord.Interaction, button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        await do_rappel(interaction)

class QuotaSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(QuotaSelect())

class SpeedoSubtypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(SpeedoSubtypeSelect())

class SpeedoSubtypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Cook", value="Cook"),
            discord.SelectOption(label="Acide", value="Acide"),
            discord.SelectOption(label="Diamond Cook", value="Diamond Cook"),
            discord.SelectOption(label="Safrole", value="Safrole"),
            discord.SelectOption(label="Phosphore Rouge", value="Phosphore Rouge"),
            discord.SelectOption(label="Acide Anhydrique", value="Acide Anhydrique"),
        ]
        super().__init__(placeholder="Quel type de Speedo ?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        subtype = self.values[0]
        await interaction.response.send_message(f"**Speedo - {subtype}** sélectionné.\nCombien en as-tu fait ?", ephemeral=True)
        
        try:
            msg_nombre = await bot.wait_for('message', check=lambda m: m.author == interaction.user, timeout=60)
            qty = int(msg_nombre.content.strip())
            if qty <= 0: raise ValueError

            await interaction.followup.send("📸 Envoie ta photo maintenant", ephemeral=True)
            photo_msg = await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.attachments, timeout=120)

            today = date.today()
            week_start = today - timedelta(days=today.weekday())
            image_url = photo_msg.attachments[0].url

            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("""
                        INSERT INTO quotas (week_start, user_id, username, type, subtype, quantity, image_url)
                        VALUES (%s, %s, %s, 'Speedo', %s, %s, %s)
                    """, (week_start, interaction.user.id, interaction.user.display_name, subtype, qty, image_url))
                    conn.commit()

            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                file = await photo_msg.attachments[0].to_file()
                embed = discord.Embed(title="📸 Quota Soumis", color=discord.Color.green())
                embed.add_field(name="Membre", value=interaction.user.mention, inline=False)
                embed.add_field(name="Type", value="Speedo", inline=True)
                embed.add_field(name="Sous-type", value=subtype, inline=True)
                embed.add_field(name="Quantité", value=qty, inline=True)
                await log_channel.send(embed=embed, file=file)

            await interaction.followup.send("✅ **Speedo enregistré avec succès !**", ephemeral=True)
            
            await asyncio.sleep(2)
            try: await msg_nombre.delete()
            except: pass
            try: await photo_msg.delete()
            except: pass

            await update_tableaux()

        except Exception as e:
            await interaction.followup.send("❌ Erreur.", ephemeral=True)
            print(e)

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
    msg = f"✅ Rappel envoyé à **{reminded}** membre(s)."
    try:
        await interaction.response.send_message(msg, ephemeral=True)
    except:
        await interaction.followup.send(msg, ephemeral=True)

async def update_tableaux():
    await update_classement()
    await update_quotas_tableau()

async def update_classement():
    if not CLASSEMENT_MESSAGE_ID: return
    try:
        channel = bot.get_channel(TABLEAU_CHANNEL_ID)
        message = await channel.fetch_message(CLASSEMENT_MESSAGE_ID)
        week_start = date.today() - timedelta(days=date.today().weekday())
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT username,
                           SUM(quantity *
                               CASE type
                                   WHEN 'Contenair' THEN 1
                                   WHEN 'Atm' THEN 2.5
                                   WHEN 'Superette' THEN 3
                                   WHEN 'Speedo' THEN 15
                               END) as total_points
                    FROM quotas
                    WHERE week_start = %s
                    GROUP BY user_id, username
                    ORDER BY total_points DESC
                """, (week_start,))
                data = c.fetchall()
        embed = discord.Embed(title="🏆 Classement par Points", color=discord.Color.gold())
        if data:
            desc = "\n".join([f"**{i}.** {user} → **{pts:.1f}** points" for i, (user, pts) in enumerate(data, 1)])
            embed.description = desc
        else:
            embed.description = "Aucun quota enregistré."
        embed.set_footer(text=f"MAJ : {datetime.now().strftime('%H:%M:%S')}")
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Erreur classement: {e}")

async def update_quotas_tableau():
    if not QUOTAS_MESSAGE_ID: return
    try:
        channel = bot.get_channel(TABLEAU_CHANNEL_ID)
        message = await channel.fetch_message(QUOTAS_MESSAGE_ID)
        week_start = date.today() - timedelta(days=date.today().weekday())
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT
                        a.username,
                        COALESCE(SUM(CASE WHEN q.type = 'Contenair' THEN q.quantity ELSE 0 END), 0) as contenair,
                        COALESCE(SUM(CASE WHEN q.type = 'Atm' THEN q.quantity ELSE 0 END), 0) as atm,
                        COALESCE(SUM(CASE WHEN q.type = 'Superette' THEN q.quantity ELSE 0 END), 0) as superette,
                        COALESCE(SUM(CASE WHEN q.type = 'Speedo' THEN q.quantity ELSE 0 END), 0) as speedo,
                        STRING_AGG(DISTINCT q.subtype, ', ') as speedo_types
                    FROM authorized_users a
                    LEFT JOIN quotas q ON a.user_id = q.user_id AND q.week_start = %s
                    GROUP BY a.user_id, a.username
                    ORDER BY (COALESCE(SUM(q.quantity),0)) DESC, a.username
                """, (week_start,))
                data = c.fetchall()
        embed = discord.Embed(title="📋 Progression des Quotas", color=discord.Color.blue())
        obj_str = "**Objectifs Hebdomadaires :**\n"
        for t, nb in OBJECTIFS.items():
            obj_str += f"• {t} → **{nb}**\n"
        obj_str += "\n"
        if data:
            desc = obj_str
            for row in data:
                username = row[0]
                c = row[1]
                a = row[2]
                s = row[3]
                sp = row[4]
                speedo_types = row[5] or "Aucun"
                desc += f"**{username}**\n"
                desc += f"📦 Contenair : **{c}/{OBJECTIFS['Contenair']}** | "
                desc += f"🏧 ATM : **{a}/{OBJECTIFS['Atm']}** | "
                desc += f"🏪 Superette : **{s}/{OBJECTIFS['Superette']}** | "
                desc += f"⚡ Speedo : **{sp}/{OBJECTIFS['Speedo']}** ({speedo_types})\n\n"
            embed.description = desc
        else:
            embed.description = obj_str + "Aucun membre autorisé."
        embed.set_footer(text=f"MAJ : {datetime.now().strftime('%H:%M:%S')}")
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Erreur tableau quotas: {e}")

@tasks.loop(minutes=2)
async def auto_update():
    await update_tableaux()

# ==================== COMMANDES ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def settableaux(ctx):
    global TABLEAU_CHANNEL_ID, CLASSEMENT_MESSAGE_ID, QUOTAS_MESSAGE_ID
    TABLEAU_CHANNEL_ID = ctx.channel.id
    embed1 = discord.Embed(title="🏆 Classement par Points", description="En attente...", color=discord.Color.gold())
    msg1 = await ctx.send(embed=embed1)
    CLASSEMENT_MESSAGE_ID = msg1.id
    embed2 = discord.Embed(title="📋 Progression des Quotas", description="Chargement...", color=discord.Color.blue())
    msg2 = await ctx.send(embed=embed2)
    QUOTAS_MESSAGE_ID = msg2.id
    await ctx.send("✅ **Deux tableaux créés !**")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(title="📊 Système de Quotas Diamond City", description="Clique sur les boutons ci-dessous", color=discord.Color.blue())
    await ctx.send(embed=embed, view=QuotaView())

@bot.command()
@commands.has_permissions(administrator=True)
async def adduser(ctx, member: discord.Member):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO authorized_users (user_id, username)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO NOTHING
            """, (member.id, member.display_name))
            conn.commit()
    await ctx.send(f"✅ **{member.display_name}** ajouté.")

@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not auto_update.is_running():
        auto_update.start()

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant")
