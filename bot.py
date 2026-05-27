import discord
from discord.ext import commands, tasks
import psycopg2
import asyncio
from datetime import date
import os

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

LOG_CHANNEL_ID = 1508882228166398073
TABLEAU_CHANNEL_ID = None
tableau_message_id = None

# Connexion à PostgreSQL
def get_db():
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise Exception("DATABASE_URL non configurée sur Railway !")
    return psycopg2.connect(DATABASE_URL)

# Création des tables
with get_db() as conn:
    with conn.cursor() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS quotas (
                date TEXT, user_id BIGINT, username TEXT,
                type TEXT, quantity INTEGER,
                PRIMARY KEY (date, user_id, type)
            );
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id BIGINT PRIMARY KEY, username TEXT
            );
        ''')
        conn.commit()

print("✅ Connexion à PostgreSQL réussie")

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
                return await interaction.followup.send("❌ Nombre invalide !", ephemeral=True)

            await interaction.followup.send("📸 Envoie ta photo maintenant (obligatoire)", ephemeral=True)
            photo_msg = await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.attachments, timeout=120)

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

            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel and photo_msg.attachments:
                file = await photo_msg.attachments[0].to_file()
                embed = discord.Embed(title="📸 Nouveau Quota", color=discord.Color.gold())
                embed.add_field(name="Personne", value=interaction.user.mention)
                embed.add_field(name="Type", value=type_quota)
                embed.add_field(name="Quantité", value=qty)
                await log_channel.send(embed=embed, file=file)

            await interaction.followup.send("✅ **Quota enregistré avec succès !**", ephemeral=True)

        except Exception as e:
            await interaction.followup.send("❌ Erreur ou temps écoulé.", ephemeral=True)


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


# ==================== LANCEMENT ====================
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ ERREUR : TOKEN non configuré")
    else:
        print("✅ Token trouvé, lancement du bot...")
        bot.run(token)
