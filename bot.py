import discord
from discord.ext import commands, tasks
import sqlite3
import asyncio
from datetime import date

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

LOG_CHANNEL_ID = 1508882228166398073
TABLEAU_CHANNEL_ID = None
tableau_message_id = None

def get_db():
    return sqlite3.connect('quotas.db')

with get_db() as conn:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS quotas (
            date TEXT, user_id INTEGER, username TEXT,
            type TEXT, quantity INTEGER,
            PRIMARY KEY (date, user_id, type)
        );
        CREATE TABLE IF NOT EXISTS authorized_users (
            user_id INTEGER PRIMARY KEY, username TEXT
        );
    ''')


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

        # Message juste en dessous pour demander le nombre
        await interaction.response.send_message(
            f"**{type_quota}** sélectionné.\n\nCombien en as-tu fait ? Réponds avec un nombre :", 
            ephemeral=True
        )

        def check_nombre(m):
            return m.author == interaction.user and m.channel == interaction.channel

        try:
            msg_nombre = await bot.wait_for('message', check=check_nombre, timeout=60)
            qty = int(msg_nombre.content.strip())

            if qty <= 0:
                return await interaction.followup.send("❌ Le nombre doit être supérieur à 0 !", ephemeral=True)

            # Demande photo (obligatoire)
            msg_photo = await interaction.followup.send("📸 **Envoie ta photo maintenant** (obligatoire)", ephemeral=True)

            def check_photo(m):
                return m.author == interaction.user and len(m.attachments) > 0

            photo_msg = await bot.wait_for('message', check=check_photo, timeout=120)

            # === Enregistrement ===
            today = date.today().isoformat()
            with get_db() as conn:
                c = conn.cursor()
                c.execute("SELECT quantity FROM quotas WHERE date=? AND user_id=? AND type=?", 
                          (today, interaction.user.id, type_quota))
                result = c.fetchone()
                
                if result:
                    new_qty = result[0] + qty
                    c.execute("UPDATE quotas SET quantity=? WHERE date=? AND user_id=? AND type=?", 
                              (new_qty, today, interaction.user.id, type_quota))
                else:
                    c.execute("INSERT INTO quotas VALUES (?,?,?,?,?)",
                              (today, interaction.user.id, interaction.user.name, type_quota, qty))
                conn.commit()

            # Envoi dans le salon de logs
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                file = await photo_msg.attachments[0].to_file()
                embed = discord.Embed(title="📸 Nouveau Quota", color=discord.Color.gold())
                embed.add_field(name="Personne", value=interaction.user.mention)
                embed.add_field(name="Type", value=type_quota)
                embed.add_field(name="Quantité", value=qty)
                await log_channel.send(embed=embed, file=file)

            # Message de succès
            success_msg = await interaction.followup.send("✅ **Quota enregistré avec succès !**", ephemeral=True)

            # === Nettoyage des messages (sauf le menu principal) ===
            await asyncio.sleep(3)  # Petite pause pour que l'utilisateur voie le succès
            try:
                await msg_nombre.delete()
                await msg_photo.delete()
                await photo_msg.delete()
                await success_msg.delete()
            except:
                pass

        except ValueError:
            await interaction.followup.send("❌ Nombre invalide !", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Temps écoulé.", ephemeral=True)
        except Exception as e:
            print(f"Erreur: {e}")


class QuotaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Faire quota", style=discord.ButtonStyle.green, custom_id="faire_quota")
    async def faire_quota(self, interaction: discord.Interaction, button):
        with get_db() as conn:
            if not conn.execute("SELECT 1 FROM authorized_users WHERE user_id = ?", (interaction.user.id,)).fetchone():
                return await interaction.response.send_message("❌ Tu n'es pas autorisé.", ephemeral=True)
        
        await interaction.response.send_message(view=QuotaSelectView(), ephemeral=True)


class QuotaSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(QuotaSelect())


# ==================== Le reste du code (inchangé) ====================

@tasks.loop(seconds=30)
async def auto_update_tableau():
    global TABLEAU_CHANNEL_ID, tableau_message_id
    if not TABLEAU_CHANNEL_ID or not tableau_message_id: return
    channel = bot.get_channel(TABLEAU_CHANNEL_ID)
    if not channel: return
    try:
        message = await channel.fetch_message(tableau_message_id)
        today = date.today().isoformat()
        embed = discord.Embed(title=f"📊 Tableau des Quotas - {date.today().strftime('%d/%m/%Y')}", color=discord.Color.blue())
        description = "**Objectifs :** 40 Contenair | 12 ATM | 2 Superette | ? Speedo\n\n"
        
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, username FROM authorized_users")
            for user_id, db_name in c.fetchall():
                member = channel.guild.get_member(user_id)
                name = member.display_name if member else db_name
                c.execute("SELECT type, quantity FROM quotas WHERE date=? AND user_id=?", (today, user_id))
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
        embed.set_footer(text="Mis à jour automatiquement")
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Tableau Error: {e}")


@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not auto_update_tableau.is_running():
        auto_update_tableau.start()


@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(
        title="📊 Système de Quotas", 
        description="Clique sur le bouton pour faire ton quota", 
        color=discord.Color.blue()
    )
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
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO authorized_users VALUES (?,?)", (member.id, member.display_name))
        conn.commit()
    await ctx.send(f"✅ {member.display_name} ajouté.")


@bot.command()
@commands.has_permissions(administrator=True)
async def removeuser(ctx, member: discord.Member):
    with get_db() as conn:
        conn.execute("DELETE FROM authorized_users WHERE user_id=?", (member.id,))
        conn.commit()
    await ctx.send(f"❌ {member.display_name} retiré.")


@bot.command()
@commands.has_permissions(administrator=True)
async def resetquotas(ctx):
    with get_db() as conn:
        conn.execute("DELETE FROM quotas WHERE date=?", (date.today().isoformat(),))
        conn.commit()
    await ctx.send("✅ Quotas du jour reset.")


# ==================== LANCEMENT ====================
if __name__ == "__main__":
    bot.run("MTUwODgzMzkxODgxODg0NDc1Mg.GbfMNQ.5Xx9RD7KiFu7L0uI1NvXwBZPtGykCLWjRQPEH4")