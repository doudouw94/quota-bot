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

            await interaction.followup.send("✅ **Quota enregistré avec succès !**", ephemeral=True)
            await update_tableau_message()

        except Exception as e:
            await interaction.followup.send("❌ Erreur lors de l'enregistrement.", ephemeral=True)
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

# ==================== FONCTIONS UTILES ====================
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
                    await member.send("⚠️ **Rappel Quotas Diamond City**\nTu n'as pas encore fait de quota cette semaine.\nMerci de faire tes quotas rapidement !")
                    reminded += 1
                except:
                    pass

    msg = f"✅ Rappel envoyé à **{reminded}** membre(s)."
    if isinstance(ctx_or_interaction, discord.Interaction):
        await ctx_or_interaction.followup.send(msg, ephemeral=True)
    else:
        await ctx_or_interaction.send(msg)

# ==================== TABLEAU HEBDO ====================
async def update_tableau_message():
    global TABLEAU_CHANNEL_ID, tableau_message_id
    if not TABLEAU_CHANNEL_ID or not tableau_message_id:
        return False

    try:
        channel = bot.get_channel(TABLEAU_CHANNEL_ID)
        message = await channel.fetch_message(tableau_message_id)

        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)

        embed = discord.Embed(
            title=f"📊 Classement Quotas - Semaine {week_start.strftime('%d/%m')} → {week_end.strftime('%d/%m')}",
            color=discord.Color.gold()
        )

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT user_id, username, type, SUM(quantity) as qty
                    FROM quotas 
                    WHERE week_start = %s
                    GROUP BY user_id, username, type
                """, (week_start,))
                data = c.fetchall()

                users_stats = {}
                for user_id, username, qtype, qty in data:
                    if user_id not in users_stats:
                        users_stats[user_id] = {"name": username, "types": {}, "points": 0}
                    users_stats[user_id]["types"][qtype] = qty
                    users_stats[user_id]["points"] += qty * POINTS.get(qtype, 1)

                ranking = sorted(users_stats.items(), key=lambda x: x[1]["points"], reverse=True)

                description = "**🏆 Classement :**\n\n"
                for rank, (uid, stats) in enumerate(ranking, 1):
                    name = stats["name"]
                    pts = stats["points"]
                    c_ = stats["types"].get("Contenair", 0)
                    a_ = stats["types"].get("Atm", 0)
                    s_ = stats["types"].get("Superette", 0)
                    sp = stats["types"].get("Speedo", 0)

                    description += f"**{rank}. {name}** — **{pts} pts**\n"
                    description += f"• Contenair: {c_} | ATM: {a_} | Superette: {s_} | Speedo: {sp}\n\n"

                c.execute("SELECT user_id FROM authorized_users")
                all_users = {row[0] for row in c.fetchall()}
                inactive = all_users - set(users_stats.keys())

                if inactive:
                    description += "**❌ Inactifs cette semaine :**\n"
                    for uid in inactive:
                        member = channel.guild.get_member(uid)
                        description += f"• {member.display_name if member else 'Inconnu'}\n"

        embed.description = description[:4000]
        embed.set_footer(text=f"Dernière MAJ : {datetime.now().strftime('%H:%M')}")

        await message.edit(embed=embed)
        return True

    except Exception as e:
        print(f"Tableau Error: {e}")
        return False

@tasks.loop(minutes=2)
async def auto_update_tableau():
    await update_tableau_message()

# ==================== COMMANDES ====================
@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not auto_update_tableau.is_running():
        auto_update_tableau.start()

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(title="📊 Système de Quotas Diamond City", 
                         description="Utilise les boutons ci-dessous", 
                         color=discord.Color.blue())
    await ctx.send(embed=embed, view=QuotaView())

@bot.command()
@commands.has_permissions(administrator=True)
async def settableau(ctx):
    global TABLEAU_CHANNEL_ID, tableau_message_id
    TABLEAU_CHANNEL_ID = ctx.channel.id
    embed = discord.Embed(title="📊 Classement Quotas Hebdomadaire", 
                         description="En attente de quotas...", 
                         color=discord.Color.gold())
    msg = await ctx.send(embed=embed)
    tableau_message_id = msg.id
    await ctx.send("✅ Tableau activé !")

@bot.command()
@commands.has_permissions(administrator=True)
async def rappel(ctx):
    await do_rappel(ctx)

@bot.command(aliases=['update'])
@commands.has_permissions(administrator=True)
async def updatetableau(ctx):
    success = await update_tableau_message()
    await ctx.send("✅ Tableau mis à jour !" if success else "❌ Erreur.")

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
async def listusers(ctx):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT username FROM authorized_users ORDER BY username")
            users = c.fetchall()
    await ctx.send("**Utilisateurs autorisés :**\n" + "\n".join([f"• {u[0]}" for u in users]) if users else "Aucun utilisateur.")

# ==================== NOUVELLES COMMANDES ====================

@bot.command()
async def mesquotas(ctx):
    """Voir ses propres quotas de la semaine"""
    week_start = date.today() - timedelta(days=date.today().weekday())

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT type, SUM(quantity) as qty
                FROM quotas 
                WHERE week_start = %s AND user_id = %s
                GROUP BY type
            """, (week_start, ctx.author.id))
            data = c.fetchall()

    if not data:
        return await ctx.send("📭 Tu n'as encore rien soumis cette semaine.")

    embed = discord.Embed(title=f"📋 Tes quotas cette semaine", color=discord.Color.blue())
    total_points = 0
    for qtype, qty in data:
        points = qty * POINTS.get(qtype, 1)
        total_points += points
        embed.add_field(name=qtype, value=f"{qty} → **{points} pts**", inline=True)

    embed.add_field(name="**Total**", value=f"**{total_points} points**", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def recap(ctx):
    """Récap détaillé de la semaine"""
    week_start = date.today() - timedelta(days=date.today().weekday())
    week_end = week_start + timedelta(days=6)

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT username, type, SUM(quantity) as qty
                FROM quotas 
                WHERE week_start = %s
                GROUP BY username, type
                ORDER BY username
            """, (week_start,))
            data = c.fetchall()

            c.execute("SELECT user_id FROM authorized_users")
            total_members = len(c.fetchall())

    if not data:
        return await ctx.send("Aucun quota cette semaine.")

    users_stats = {}
    for username, qtype, qty in data:
        if username not in users_stats:
            users_stats[username] = {"types": {}, "points": 0}
        users_stats[username]["types"][qtype] = qty
        users_stats[username]["points"] += qty * POINTS.get(qtype, 1)

    embed = discord.Embed(title=f"📊 Récap Complet Semaine {week_start.strftime('%d/%m')} → {week_end.strftime('%d/%m')}", 
                         color=discord.Color.gold())

    for username, stats in sorted(users_stats.items(), key=lambda x: x[1]["points"], reverse=True):
        c_ = stats["types"].get("Contenair", 0)
        a_ = stats["types"].get("Atm", 0)
        s_ = stats["types"].get("Superette", 0)
        sp = stats["types"].get("Speedo", 0)
        pts = stats["points"]
        embed.add_field(name=f"{username} — {pts} pts", 
                       value=f"Cont: {c_} | ATM: {a_} | Sup: {s_} | Spe: {sp}", 
                       inline=False)

    embed.set_footer(text=f"Total membres actifs : {len(users_stats)} / {total_members}")
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def resetquotas(ctx):
    """Supprime tous les quotas de la semaine en cours"""
    week_start = date.today() - timedelta(days=date.today().weekday())

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM quotas WHERE week_start = %s", (week_start,))
            deleted = c.rowcount
            conn.commit()

    await ctx.send(f"✅ **{deleted}** quotas ont été supprimés pour cette semaine.")
    await update_tableau_message()

# ==================== LANCEMENT ====================
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant")
