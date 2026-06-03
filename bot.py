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
tableau_message_id = None

# ==================== DATABASE ====================
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

# ==================== MIGRATION ====================
def migrate_database():
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DROP TABLE IF EXISTS quotas;")
            c.execute('''
                CREATE TABLE quotas (
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
            discord.SelectOption(label="Superette", value="Superette", emoji="🏪"),
            discord.SelectOption(label="Speedo", value="Speedo", emoji="⚡")
        ]
        super().__init__(placeholder="Choisis le type de quota...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        type_quota = self.values[0]
        await interaction.response.send_message(f"**{type_quota}** sélectionné.\n\nCombien en as-tu fait ?", ephemeral=True)

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
                        INSERT INTO quotas (week_start, user_id, username, type, quantity, image_url)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (week_start, interaction.user.id, interaction.user.display_name, type_quota, qty, image_url))
                    conn.commit()

            # Log
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                file = await photo_msg.attachments[0].to_file()
                embed = discord.Embed(title="📸 Quota Soumis", color=discord.Color.green())
                embed.add_field(name="Membre", value=interaction.user.mention, inline=False)
                embed.add_field(name="Type", value=type_quota, inline=True)
                embed.add_field(name="Quantité", value=qty, inline=True)
                await log_channel.send(embed=embed, file=file)

            await interaction.followup.send("✅ **Quota enregistré avec succès !**", ephemeral=True)

            # Suppression des messages
            await asyncio.sleep(1.5)
            try: await msg_nombre.delete()
            except: pass
            try: await photo_msg.delete()
            except: pass

            # Mise à jour du tableau
            await asyncio.sleep(1.5)
            await update_tableau_message()

        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Temps écoulé.", ephemeral=True)
        except ValueError:
            await interaction.followup.send("❌ Nombre invalide.", ephemeral=True)
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

# ==================== FONCTIONS ====================
async def do_rappel(ctx_or_interaction):
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
            member = ctx_or_interaction.guild.get_member(user_id)
            if member:
                try:
                    await member.send("⚠️ **Rappel Quotas Diamond City**\nTu n'as pas encore fait de quota cette semaine.")
                    reminded += 1
                except:
                    pass

    msg = f"✅ Rappel envoyé à **{reminded}** membre(s)."
    if isinstance(ctx_or_interaction, discord.Interaction):
        await ctx_or_interaction.followup.send(msg, ephemeral=True)
    else:
        await ctx_or_interaction.send(msg)

async def update_tableau_message():
    global TABLEAU_CHANNEL_ID, tableau_message_id
    
    if not TABLEAU_CHANNEL_ID or not tableau_message_id:
        print("⚠️ Tableau non configuré (TABLEAU_CHANNEL_ID ou tableau_message_id manquant)")
        return False

    try:
        channel = bot.get_channel(TABLEAU_CHANNEL_ID)
        if not channel:
            print(f"❌ Salon du tableau introuvable (ID: {TABLEAU_CHANNEL_ID})")
            return False

        message = await channel.fetch_message(tableau_message_id)

        week_start = date.today() - timedelta(days=date.today().weekday())
        week_end = week_start + timedelta(days=6)

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT username, SUM(quantity) as total
                    FROM quotas 
                    WHERE week_start = %s 
                    GROUP BY user_id, username 
                    ORDER BY total DESC
                """, (week_start,))
                data = c.fetchall()

        embed = discord.Embed(
            title=f"📊 Classement Semaine {week_start.strftime('%d/%m')} → {week_end.strftime('%d/%m')}", 
            color=discord.Color.gold()
        )

        if not data:
            embed.description = "Aucun quota enregistré pour le moment."
        else:
            description = ""
            for i, (username, total) in enumerate(data, 1):
                description += f"**{i}.** {username} → **{total}** points\n"
            embed.description = description

        embed.set_footer(text=f"MAJ : {datetime.now().strftime('%H:%M:%S')}")

        await message.edit(embed=embed)
        print(f"✅ Tableau mis à jour avec succès ({len(data)} personnes)")
        return True

    except discord.NotFound:
        print("❌ Message du tableau introuvable (il a peut-être été supprimé)")
        return False
    except Exception as e:
        print(f"❌ Erreur mise à jour tableau: {e}")
        return False

@tasks.loop(minutes=2)
async def auto_update_tableau():
    await update_tableau_message()

# ==================== COMMANDES ====================

@bot.command()
async def classement(ctx):
    week_start = date.today() - timedelta(days=date.today().weekday())
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT username, SUM(quantity) as total
                FROM quotas 
                WHERE week_start = %s 
                GROUP BY user_id, username 
                ORDER BY total DESC
            """, (week_start,))
            data = c.fetchall()

    embed = discord.Embed(title=f"📊 Classement Semaine {week_start.strftime('%d/%m')}", color=discord.Color.gold())

    if not data:
        embed.description = "Aucun quota enregistré pour le moment."
    else:
        description = ""
        for i, (username, total) in enumerate(data, 1):
            description += f"**{i}.** {username} → **{total}** points\n"
        embed.description = description

    embed.set_footer(text=f"MAJ : {datetime.now().strftime('%H:%M')}")
    await ctx.send(embed=embed)


@bot.command()
async def quotas(ctx, member: discord.Member = None):
    if member is None:
        member = ctx.author

    week_start = date.today() - timedelta(days=date.today().weekday())

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT type, SUM(quantity) as qty 
                FROM quotas 
                WHERE week_start = %s AND user_id = %s 
                GROUP BY type
            """, (week_start, member.id))
            data = c.fetchall()

    embed = discord.Embed(title=f"Quotas de {member.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=member.display_avatar.url)

    if not data:
        embed.description = "❌ Aucun quota enregistré cette semaine."
    else:
        total = sum(qty for _, qty in data)
        description = f"**Total : {total} points**\n\n"
        for type_quota, qty in data:
            description += f"• {type_quota} → **{qty}**\n"
        embed.description = description

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(title="📊 Système de Quotas Diamond City", description="Clique sur les boutons ci-dessous", color=discord.Color.blue())
    await ctx.send(embed=embed, view=QuotaView())


@bot.command()
@commands.has_permissions(administrator=True)
async def settableau(ctx):
    global TABLEAU_CHANNEL_ID, tableau_message_id
    TABLEAU_CHANNEL_ID = ctx.channel.id
    embed = discord.Embed(title="📊 Classement Hebdomadaire", description="En attente de quotas...", color=discord.Color.gold())
    msg = await ctx.send(embed=embed)
    tableau_message_id = msg.id
    await ctx.send("✅ Tableau activé dans ce salon !")


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


@bot.command()
@commands.has_permissions(administrator=True)
async def resetquotas(ctx):
    week_start = date.today() - timedelta(days=date.today().weekday())
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM quotas WHERE week_start = %s", (week_start,))
            deleted = c.rowcount
            conn.commit()
    await ctx.send(f"✅ **{deleted}** quotas supprimés cette semaine.")
    await update_tableau_message()


@bot.command()
@commands.has_permissions(administrator=True)
async def rappel(ctx):
    await do_rappel(ctx)

# ==================== LANCEMENT ====================
@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not auto_update_tableau.is_running():
        auto_update_tableau.start()

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant")
