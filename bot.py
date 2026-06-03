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

POINTS = {
    "Contenair": 1,
    "Atm": 3,
    "Superette": 10,
    "Speedo": 5
}

# ==================== DATABASE ====================
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

# Tables
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

print("✅ Connexion PostgreSQL OK")

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
        await interaction.response.send_message(f"**{type_quota}** sélectionné.\n\nCombien ?", ephemeral=True)

        try:
            msg_nombre = await bot.wait_for('message', check=lambda m: m.author == interaction.user, timeout=60)
            qty = int(msg_nombre.content.strip())
            if qty <= 0: raise ValueError

            await interaction.followup.send("📸 Envoie ta photo", ephemeral=True)
            photo_msg = await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.attachments, timeout=120)

            today = date.today()
            week_start = today - timedelta(days=today.weekday())
            image_url = photo_msg.attachments[0].url

            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("""
                        INSERT INTO quotas (week_start, user_id, username, type, quantity, image_url)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (week_start, interaction.user.id, interaction.user.name, type_quota, qty, image_url))
                    conn.commit()

            # Log privé
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                file = await photo_msg.attachments[0].to_file()
                embed = discord.Embed(title="📸 Quota Soumis", color=discord.Color.green())
                embed.add_field(name="Membre", value=interaction.user.mention, inline=False)
                embed.add_field(name="Type", value=type_quota, inline=True)
                embed.add_field(name="Quantité", value=qty, inline=True)
                await log_channel.send(embed=embed, file=file)

            await interaction.followup.send("✅ Quota enregistré !", ephemeral=True)
            await update_tableau_message()

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
                    return await interaction.response.send_message("❌ Non autorisé.", ephemeral=True)
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

# Fonction rappel (utilisée par bouton + commande)
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
                    await member.send("⚠️ **Rappel Quotas**\nTu n'as pas encore fait de quota cette semaine.")
                    reminded += 1
                except:
                    pass

    if isinstance(ctx_or_interaction, discord.Interaction):
        await ctx_or_interaction.followup.send(f"✅ Rappel envoyé à **{reminded}** membre(s).", ephemeral=True)
    else:
        await ctx_or_interaction.send(f"✅ Rappel envoyé à **{reminded}** membre(s).")

# ==================== TABLEAU ====================
async def update_tableau_message():
    # ... (je te la remets si besoin, mais elle est déjà bonne dans la version précédente)
    # Je te la donne complète si tu veux, dis-le moi.
    pass  # Remplace par la fonction du message précédent si tu l'as

# ==================== COMMANDES ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(title="Système Quotas Diamond City", description="Utilise les boutons ci-dessous", color=discord.Color.blue())
    await ctx.send(embed=embed, view=QuotaView())

@bot.command()
@commands.has_permissions(administrator=True)
async def rappel(ctx):
    await do_rappel(ctx)

# Autres commandes : !settableau, !adduser, etc.

if __name__ == "__main__":
    bot.run(os.getenv("TOKEN"))
