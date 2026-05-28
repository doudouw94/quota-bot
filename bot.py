import discord
from discord.ext import commands, tasks
import psycopg2
import os
from datetime import date
import asyncio  # ← Nécessaire pour la suppression

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

LOG_CHANNEL_ID = 1508882228166398073
TABLEAU_CHANNEL_ID = None
tableau_message_id = None

# ==================== CONNEXION DATABASE ====================
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

# Création des tables
with get_db() as conn:
    with conn.cursor() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS quotas (
                date TEXT,
                user_id BIGINT,
                username TEXT,
                type TEXT,
                quantity INTEGER,
                PRIMARY KEY (date, user_id, type)
            );
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT
            );
        ''')
        conn.commit()

print("✅ Connexion PostgreSQL OK")

# ==================== MENU DÉROULANT ====================
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
            if qty <= 0:
                raise ValueError

            await interaction.followup.send("📸 Envoie ta photo maintenant (obligatoire)", ephemeral=True)
            photo_msg = await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.attachments, timeout=120)

            # ==================== ENREGISTREMENT EN BDD ====================
            today = date.today().isoformat()
            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("SELECT quantity FROM quotas WHERE date=%s AND user_id=%s AND type=%s",
                              (today, interaction.user.id, type_quota))
                    result = c.fetchone()
                   
                    if result:
                        c.execute("UPDATE quotas SET quantity=%s WHERE date=%s AND user_id=%s AND type=%s",
                                  (result[0] + qty, today, interaction.user.id, type_quota))
                    else:
                        c.execute("INSERT INTO quotas VALUES (%s,%s,%s,%s,%s)",
                                  (today, interaction.user.id, interaction.user.name, type_quota, qty))
                    conn.commit()

            # ==================== LOG AVEC PHOTO ====================
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                file = await photo_msg.attachments[0].to_file()
                embed = discord.Embed(title="📸 Nouveau Quota", color=discord.Color.gold())
                embed.add_field(name="Personne", value=interaction.user.mention)
                embed.add_field(name="Type", value=type_quota)
                embed.add_field(name="Quantité", value=qty)
                await log_channel.send(embed=embed, file=file)

            # ==================== SUCCÈS ====================
            await interaction.followup.send("✅ **Quota enregistré avec succès !**", ephemeral=True)

            # === SUPPRESSION des messages (nombre + photo) ===
            await asyncio.sleep(0.8)
            try:
                await msg_nombre.delete()
            except:
                pass
            try:
                await photo_msg.delete()
            except:
                pass

        except asyncio.TimeoutError:
            await interaction.followup.send("❌ Temps écoulé.", ephemeral=True)
        except ValueError:
            await interaction.followup.send("❌ Nombre invalide.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)
            print(f"Quota Error: {e}")


class QuotaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Faire quota", style=discord.ButtonStyle.green, custom_id="faire_quota")
    async def faire_quota(self, interaction: discord.Interaction, button):
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT 1 FROM authorized_users WHERE user_id = %s", (interaction.user.id,))
                if not c.fetchone():
                    return await interaction.response.send_message("❌ Tu n'es pas autorisé.", ephemeral=True)
        
        await interaction.response.send_message(view=QuotaSelectView(), ephemeral=True)


class QuotaSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(QuotaSelect())


# ==================== TABLEAU AUTO ====================
@tasks.loop(seconds=30)
async def auto_update_tableau():
    global TABLEAU_CHANNEL_ID, tableau_message_id
    if not TABLEAU_CHANNEL_ID or not tableau_message_id:
        return
    channel = bot.get_channel(TABLEAU_CHANNEL_ID)
    if not channel:
        return
    try:
        message = await channel.fetch_message(tableau_message_id)
        today = date.today().isoformat()
        embed = discord.Embed(title=f"📊 Tableau des Quotas - {date.today().strftime('%d/%m/%Y')}", color=discord.Color.blue())
        description = "**Objectifs :** 40 Contenair | 12 ATM | 2 Superette | ? Speedo\n\n"
        
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT user_id, username FROM authorized_users ORDER BY username")
                for user_id, db_name in c.fetchall():
                    member = channel.guild.get_member(user_id)
                    name = member.display_name if member else db_name
                    c.execute("SELECT type, quantity FROM quotas WHERE date=%s AND user_id=%s", (today, user_id))
                    quotas = {t: q for t, q in c.fetchall()}
                    c_ = quotas.get("Contenair", 0)
                    a_ = quotas.get("Atm", 0)
                    s_ = quotas.get("Superette", 0)
                    sp = quotas.get("Speedo", 0)
                    description += f"✅ **{name}**\n"
                    description += f"• Contenair: **{c_}**/40\n"
                    description += f"• ATM: **{a_}**/12\n"
                    description += f"• Superette: **{s_}**/2\n"
                    description += f"• Speedo: **{sp}**\n\n"
        
        embed.description = description
        embed.set_footer(text="Auto • 30s")
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Tableau Error: {e}")


# ==================== ON READY ====================
@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not auto_update_tableau.is_running():
        auto_update_tableau.start()


# ==================== COMMANDES ADMIN ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(title="📊 Système de Quotas",
                         description="Clique sur le bouton pour faire ton quota",
                         color=discord.Color.blue())
    await ctx.send(embed=embed, view=QuotaView())


@bot.command()
@commands.has_permissions(administrator=True)
async def settableau(ctx):
    global TABLEAU_CHANNEL_ID, tableau_message_id
    TABLEAU_CHANNEL_ID = ctx.channel.id
    embed = discord.Embed(title="📊 Tableau des Quotas", description="En attente de quotas...", color=discord.Color.blue())
    msg = await ctx.send(embed=embed)
    tableau_message_id = msg.id
    await ctx.send("✅ Tableau activé !")


@bot.command()
@commands.has_permissions(administrator=True)
async def adduser(ctx, member: discord.Member):
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO authorized_users (user_id, username)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO NOTHING;
                """, (member.id, member.display_name))
                conn.commit()
               
                if c.rowcount == 0:
                    await ctx.send(f"⚠️ **{member.display_name}** était déjà autorisé.")
                else:
                    await ctx.send(f"✅ **{member.display_name}** a été ajouté avec succès.")
    except Exception as e:
        await ctx.send("❌ Erreur lors de l'ajout de l'utilisateur.")
        print(f"Adduser Error: {e}")


@bot.command()
@commands.has_permissions(administrator=True)
async def removeuser(ctx, member: discord.Member):
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("DELETE FROM authorized_users WHERE user_id = %s", (member.id,))
                deleted = c.rowcount
                conn.commit()
        if deleted > 0:
            await ctx.send(f"❌ **{member.display_name}** a été retiré des utilisateurs autorisés.")
        else:
            await ctx.send(f"⚠️ **{member.display_name}** n'était pas dans la liste.")
    except Exception as e:
        await ctx.send("❌ Erreur lors de la suppression.")
        print(f"Removeuser Error: {e}")


@bot.command()
@commands.has_permissions(administrator=True)
async def listusers(ctx):
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT username FROM authorized_users ORDER BY username")
                users = c.fetchall()
        if not users:
            return await ctx.send("📋 Aucun utilisateur autorisé pour le moment.")
        embed = discord.Embed(title="👥 Utilisateurs Autorisés", color=discord.Color.green())
        user_list = "\n".join([f"• {user[0]}" for user in users])
        embed.description = user_list
        embed.set_footer(text=f"Total : {len(users)} utilisateur(s)")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send("❌ Erreur lors de la récupération de la liste.")
        print(f"Listusers Error: {e}")


@bot.command()
@commands.has_permissions(administrator=True)
async def resetquotas(ctx):
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("DELETE FROM quotas WHERE date = %s", (date.today().isoformat(),))
                conn.commit()
        await ctx.send("✅ Quotas du jour reset avec succès.")
    except Exception as e:
        await ctx.send("❌ Erreur lors du reset des quotas.")
        print(f"Reset Error: {e}")


# ==================== LANCEMENT ====================
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant dans les variables d'environnement.")
