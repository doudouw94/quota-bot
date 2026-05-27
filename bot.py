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

# Connexion PostgreSQL avec gestion d'erreur
def get_db():
    try:
        DATABASE_URL = os.getenv("DATABASE_URL")
        if not DATABASE_URL:
            print("❌ DATABASE_URL non trouvée !")
            return None
        conn = psycopg2.connect(DATABASE_URL)
        print("✅ Connexion PostgreSQL OK")
        return conn
    except Exception as e:
        print(f"❌ Erreur connexion DB: {e}")
        return None

# Création tables
conn = get_db()
if conn:
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
        conn.close()

# ==================== MENU ====================
class QuotaSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Contenair", value="Contenair", emoji="📦"),
            discord.SelectOption(label="ATM", value="Atm", emoji="🏧"),
            discord.SelectOption(label="Superette", value="Superette", emoji="🏪"),
            discord.SelectOption(label="Speedo", value="Speedo", emoji="⚡")
        ]
        super().__init__(placeholder="Choisis le type...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        type_quota = self.values[0]
        await interaction.response.send_message(f"**{type_quota}** sélectionné.\nCombien ?", ephemeral=True)

        try:
            msg = await bot.wait_for('message', check=lambda m: m.author == interaction.user, timeout=60)
            qty = int(msg.content.strip())
            if qty <= 0: raise ValueError

            await interaction.followup.send("📸 Envoie ta photo (obligatoire)", ephemeral=True)
            photo_msg = await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.attachments, timeout=120)

            today = date.today().isoformat()
            conn = get_db()
            if conn:
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
                    conn.close()

            await interaction.followup.send("✅ Quota enregistré !", ephemeral=True)

        except Exception:
            await interaction.followup.send("❌ Erreur.", ephemeral=True)


class QuotaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Faire quota", style=discord.ButtonStyle.green)
    async def faire_quota(self, interaction: discord.Interaction, button):
        await interaction.response.send_message(view=QuotaSelectView(), ephemeral=True)


class QuotaSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(QuotaSelect())


# Lancement
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        print("✅ Bot lancé !")
        bot.run(token)
    else:
        print("❌ TOKEN manquant")
