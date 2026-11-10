import discord
from discord.ext import commands, tasks
import psycopg2
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")

# ==================== CONFIG ====================
LOG_CHANNEL_ID = 1508882228166398073
ROLE_NAME = "vendetta"  # nom du rôle suivi (insensible à la casse)

# clé -> (nom affiché, emoji, code court pour l'en-tête du tableau)
TYPES = {
    "Contenair": ("Contenair", "📦", "CONT"),
    "Atm": ("ATM", "🏧", "ATM"),
    "Superette": ("Superette", "🏪", "SUPE"),
}


# ==================== BOT ====================
class QuotaBot(commands.Bot):
    async def setup_hook(self):
        # Réenregistre les boutons du tableau après un redémarrage (Railway)
        self.add_view(TableauView())


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = QuotaBot(command_prefix="!", intents=intents)


# ==================== DATABASE ====================
@contextmanager
def db():
    """Ouvre une connexion, donne un curseur, commit puis FERME la connexion."""
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    try:
        with conn.cursor() as c:
            yield c
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate_database():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )""")
        # Une ligne = une case cochée (semaine, membre, type)
        c.execute("""
            CREATE TABLE IF NOT EXISTS remises (
                week_start DATE NOT NULL,
                user_id BIGINT NOT NULL,
                type TEXT NOT NULL,
                checked_by BIGINT,
                checked_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (week_start, user_id, type)
            )""")
    print("✅ Base de données prête !")


migrate_database()


def get_config(key):
    with db() as c:
        c.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = c.fetchone()
    return row[0] if row else None


def set_config(key, value):
    with db() as c:
        c.execute("""
            INSERT INTO config (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, str(value)))


def current_week_start():
    today = datetime.now(TZ).date()
    return today - timedelta(days=today.weekday())


def toggle(user_id, key, by):
    """Coche ou décoche une case. Retourne True si la case est maintenant cochée."""
    week = current_week_start()
    with db() as c:
        c.execute("DELETE FROM remises WHERE week_start = %s AND user_id = %s AND type = %s",
                  (week, user_id, key))
        if c.rowcount:
            return False
        c.execute("INSERT INTO remises (week_start, user_id, type, checked_by) VALUES (%s, %s, %s, %s)",
                  (week, user_id, key, by))
        return True


async def log(text):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        await channel.send(text)


# ==================== TABLEAU ====================
def build_embed():
    week = current_week_start()
    with db() as c:
        c.execute("SELECT user_id, username FROM authorized_users ORDER BY LOWER(username)")
        users = c.fetchall()
        c.execute("SELECT user_id, type FROM remises WHERE week_start = %s", (week,))
        done = set(c.fetchall())

    embed = discord.Embed(
        title=f"📋 Remises - Semaine du {week:%d/%m/%Y}",
        color=discord.Color.blue(),
    )

    if not users:
        embed.description = "Aucun membre. Ajoute-en avec `!adduser @membre`."
        return embed

    header = f"{'Membre':<16}" + "".join(f"{code:<6}" for _, _, code in TYPES.values())
    lines = [header, "-" * len(header)]
    complets = 0
    for uid, name in users:
        cells = ""
        for key in TYPES:
            cells += f"{'[x]' if (uid, key) in done else '[ ]':<6}"
        if all((uid, key) in done for key in TYPES):
            complets += 1
        lines.append(f"{(name or str(uid))[:15]:<16}{cells}")

    legende = " • ".join(f"{emoji} {label}" for label, emoji, _ in TYPES.values())
    embed.description = f"{legende}\n`[x]` = remis en jeu\n```\n" + "\n".join(lines) + "\n```"
    embed.set_footer(text=f"{complets}/{len(users)} complets • MAJ {datetime.now(TZ):%H:%M}")
    return embed


async def post_tableau(channel):
    msg = await channel.send(embed=build_embed(), view=TableauView())
    set_config("tableau_channel", channel.id)
    set_config("tableau_msg", msg.id)
    set_config("tableau_week", current_week_start())


async def refresh_tableau():
    ch_id, msg_id = get_config("tableau_channel"), get_config("tableau_msg")
    if not ch_id or not msg_id:
        return
    channel = bot.get_channel(int(ch_id))
    if not channel:
        return
    try:
        msg = await channel.fetch_message(int(msg_id))
        await msg.edit(embed=build_embed(), view=TableauView())
    except discord.NotFound:
        print("Message du tableau introuvable : refais !settableaux")
    except Exception as e:
        print(f"Erreur refresh tableau: {e}")


# ==================== SYNC DU RÔLE ====================
async def sync_members():
    """Les membres suivis = tous ceux qui ont le rôle ROLE_NAME."""
    changed = False
    for guild in bot.guilds:
        role = discord.utils.find(lambda r: r.name.lower() == ROLE_NAME.lower(), guild.roles)
        if not role:
            print(f"⚠️ Rôle '{ROLE_NAME}' introuvable dans {guild.name}")
            continue
        if not guild.chunked:
            await guild.chunk()

        members = {m.id: m.display_name for m in role.members if not m.bot}
        with db() as c:
            c.execute("SELECT user_id, username FROM authorized_users")
            existing = dict(c.fetchall())

            for uid, name in members.items():
                if uid not in existing:
                    c.execute("INSERT INTO authorized_users (user_id, username) VALUES (%s, %s)", (uid, name))
                    changed = True
                elif existing[uid] != name:
                    c.execute("UPDATE authorized_users SET username = %s WHERE user_id = %s", (name, uid))
                    changed = True

            for uid in existing:
                if uid not in members:
                    c.execute("DELETE FROM authorized_users WHERE user_id = %s", (uid,))
                    changed = True

    if changed:
        await refresh_tableau()


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.roles != after.roles or before.display_name != after.display_name:
        await sync_members()


@bot.event
async def on_member_remove(member: discord.Member):
    await sync_members()


# ==================== VIEWS ====================
class CasesView(discord.ui.View):
    """3 boutons (un par type) pour cocher / décocher un membre."""

    def __init__(self, member: discord.Member):
        super().__init__(timeout=180)
        self.member = member
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        with db() as c:
            c.execute("SELECT type FROM remises WHERE week_start = %s AND user_id = %s",
                      (current_week_start(), self.member.id))
            done = {r[0] for r in c.fetchall()}
        for key, (label, emoji, _) in TYPES.items():
            style = discord.ButtonStyle.success if key in done else discord.ButtonStyle.secondary
            btn = discord.ui.Button(label=label, emoji=emoji, style=style)
            btn.callback = self.make_callback(key, label)
            self.add_item(btn)

    def texte(self):
        return f"Remises de **{self.member.display_name}** (vert = remis). Clique pour cocher / décocher :"

    def make_callback(self, key, label):
        async def callback(interaction: discord.Interaction):
            checked = toggle(self.member.id, key, interaction.user.id)
            self.rebuild()
            await interaction.response.edit_message(content=self.texte(), view=self)
            await refresh_tableau()
            action = "a coché" if checked else "a décoché"
            await log(f"✏️ {interaction.user.mention} {action} **{label}** pour {self.member.mention}")
        return callback


class ChoisirMembre(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choisis le membre...", min_values=1, max_values=1)
    async def choisir(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0]
        with db() as c:
            c.execute("SELECT 1 FROM authorized_users WHERE user_id = %s", (member.id,))
            ok = c.fetchone()
        if not ok:
            return await interaction.response.send_message(
                "❌ Ce membre n'est pas dans la liste (`!adduser @membre`).", ephemeral=True)
        view = CasesView(member)
        await interaction.response.edit_message(content=view.texte(), view=view)


class TableauView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cocher / Décocher", emoji="✏️", style=discord.ButtonStyle.primary,
                       custom_id="tableau:cocher")
    async def cocher(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        await interaction.response.send_message("Choisis le membre :", view=ChoisirMembre(), ephemeral=True)

    @discord.ui.button(label="Rappel", emoji="📢", style=discord.ButtonStyle.danger,
                       custom_id="tableau:rappel")
    async def rappel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        with db() as c:
            c.execute("SELECT user_id FROM authorized_users")
            users = [r[0] for r in c.fetchall()]
            c.execute("SELECT user_id, COUNT(*) FROM remises WHERE week_start = %s GROUP BY user_id",
                      (current_week_start(),))
            counts = dict(c.fetchall())

        sent = 0
        for uid in users:
            if counts.get(uid, 0) < len(TYPES):
                member = interaction.guild.get_member(uid)
                if member:
                    try:
                        await member.send("⚠️ **Rappel Diamond City**\nIl te reste des remises à faire cette semaine.")
                        sent += 1
                    except discord.Forbidden:
                        pass
        await interaction.followup.send(f"✅ Rappel envoyé à **{sent}** membre(s).", ephemeral=True)


# ==================== COMMANDES ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def settableaux(ctx):
    """Poste le tableau dans ce salon."""
    await post_tableau(ctx.channel)


@bot.command()
@commands.has_permissions(administrator=True)
async def newweek(ctx):
    """Force un nouveau tableau (nouvelle semaine) dans le salon enregistré."""
    ch_id = get_config("tableau_channel")
    channel = bot.get_channel(int(ch_id)) if ch_id else None
    if not channel:
        return await ctx.send("❌ Utilise d'abord `!settableaux` dans le bon salon.")
    await post_tableau(channel)


@bot.command()
@commands.has_permissions(administrator=True)
async def cocher(ctx, member: discord.Member, type_: str):
    """!cocher @membre atm  -> coche / décoche une case."""
    key = next((k for k in TYPES if k.lower() == type_.lower()), None)
    if not key:
        return await ctx.send("❌ Type invalide : `contenair`, `atm` ou `superette`.")
    checked = toggle(member.id, key, ctx.author.id)
    await ctx.send(f"{'✅ Coché' if checked else '↩️ Décoché'} : **{TYPES[key][0]}** pour **{member.display_name}**.")
    await refresh_tableau()


@bot.command(name="resetquotas", aliases=["reset", "resetquota"])
@commands.has_permissions(administrator=True)
async def reset_quotas(ctx, confirm: str = None):
    week = current_week_start()
    if confirm != "confirm":
        return await ctx.send(f"⚠️ Pour décocher toute la semaine du **{week}**, tape `!resetquotas confirm`")
    with db() as c:
        c.execute("DELETE FROM remises WHERE week_start = %s", (week,))
        deleted = c.rowcount
    await ctx.send(f"✅ {deleted} case(s) décochée(s).")
    await refresh_tableau()


@bot.command()
@commands.has_permissions(administrator=True)
async def listusers(ctx):
    with db() as c:
        c.execute("SELECT username, user_id FROM authorized_users ORDER BY username")
        users = c.fetchall()
    if not users:
        return await ctx.send("❌ Aucun utilisateur autorisé.")
    desc = "\n".join(f"• **{u}** (`{uid}`)" for u, uid in users)
    await ctx.send(embed=discord.Embed(title="👥 Membres suivis", description=desc, color=discord.Color.green()))


@bot.command()
@commands.has_permissions(administrator=True)
async def adduser(ctx, member: discord.Member):
    with db() as c:
        c.execute("INSERT INTO authorized_users (user_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                  (member.id, member.display_name))
    await ctx.send(f"✅ **{member.display_name}** ajouté.")
    await refresh_tableau()


@bot.command()
@commands.has_permissions(administrator=True)
async def removeuser(ctx, member: discord.Member):
    with db() as c:
        c.execute("DELETE FROM authorized_users WHERE user_id = %s", (member.id,))
    await ctx.send(f"✅ **{member.display_name}** retiré.")
    await refresh_tableau()


@bot.command(name="removeuserid", aliases=["rid"])
@commands.has_permissions(administrator=True)
async def remove_user_id(ctx, user_id: int):
    with db() as c:
        c.execute("DELETE FROM authorized_users WHERE user_id = %s", (user_id,))
        deleted = c.rowcount
    await ctx.send(f"✅ Utilisateur `{user_id}` retiré." if deleted else f"❌ ID `{user_id}` non trouvé.")
    await refresh_tableau()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("❌ Admin seulement.")
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.MemberNotFound)):
        return await ctx.send(f"❌ Utilisation incorrecte. Tape `!help {ctx.command}`.")
    raise error


# ==================== TASKS ====================
@tasks.loop(minutes=30)
async def check_new_week():
    """Poste un nouveau tableau une seule fois quand la semaine change."""
    ch_id = get_config("tableau_channel")
    if not ch_id:
        return
    if get_config("tableau_week") != str(current_week_start()):
        channel = bot.get_channel(int(ch_id))
        if channel:
            await post_tableau(channel)


@tasks.loop(minutes=10)
async def auto_sync():
    """Filet de sécurité : resynchronise le rôle toutes les 10 minutes."""
    await sync_members()


@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not auto_sync.is_running():
        auto_sync.start()  # fait aussi une synchro immédiate au démarrage
    if not check_new_week.is_running():
        check_new_week.start()


if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant")
