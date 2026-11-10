import discord
from discord.ext import commands, tasks
import psycopg2
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")

# ==================== CONFIG ====================
LOG_CHANNEL_ID = 1508882228166398073
ROLE_NAME = "vendetta"   # rôle suivi (insensible à la casse)
RESET_HOUR = 0           # heure (Paris) à laquelle une nouvelle journée commence (ex: 6 = 6h du matin)

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
        # Une ligne = une case cochée (jour, membre, type)
        c.execute("""
            CREATE TABLE IF NOT EXISTS remises_jour (
                day DATE NOT NULL,
                user_id BIGINT NOT NULL,
                type TEXT NOT NULL,
                checked_by BIGINT,
                checked_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (day, user_id, type)
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


def today():
    """Jour de jeu actuel (tient compte de RESET_HOUR)."""
    return (datetime.now(TZ) - timedelta(hours=RESET_HOUR)).date()


def get_members():
    with db() as c:
        c.execute("SELECT user_id, username FROM authorized_users ORDER BY LOWER(username)")
        return c.fetchall()


def get_done(day=None):
    """Ensemble des (user_id, type) cochés pour un jour."""
    with db() as c:
        c.execute("SELECT user_id, type FROM remises_jour WHERE day = %s", (day or today(),))
        return set(c.fetchall())


def toggle(user_id, key, by):
    """Coche ou décoche une case. Retourne True si la case est maintenant cochée."""
    with db() as c:
        c.execute("DELETE FROM remises_jour WHERE day = %s AND user_id = %s AND type = %s",
                  (today(), user_id, key))
        if c.rowcount:
            return False
        c.execute("INSERT INTO remises_jour (day, user_id, type, checked_by) VALUES (%s, %s, %s, %s)",
                  (today(), user_id, key, by))
        return True


def check_all(user_id, by):
    with db() as c:
        for key in TYPES:
            c.execute("""INSERT INTO remises_jour (day, user_id, type, checked_by) VALUES (%s, %s, %s, %s)
                         ON CONFLICT DO NOTHING""", (today(), user_id, key, by))


def set_type_for(key, chosen_ids, chunk_ids, by):
    """Pour un type : coche ceux de chunk_ids qui sont dans chosen_ids, décoche les autres."""
    with db() as c:
        for uid in chunk_ids:
            if uid in chosen_ids:
                c.execute("""INSERT INTO remises_jour (day, user_id, type, checked_by) VALUES (%s, %s, %s, %s)
                             ON CONFLICT DO NOTHING""", (today(), uid, key, by))
            else:
                c.execute("DELETE FROM remises_jour WHERE day = %s AND user_id = %s AND type = %s",
                          (today(), uid, key))


async def log(text):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        await channel.send(text[:1990])


# ==================== TABLEAU ====================
def build_embed():
    day = today()
    users = get_members()
    done = get_done(day)

    embed = discord.Embed(
        title=f"📋 Remises du jour - {day:%d/%m/%Y}",
        color=discord.Color.blue(),
    )

    if not users:
        embed.description = f"Aucun membre avec le rôle **{ROLE_NAME}**."
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
    embed.description = f"{legende}\n`[x]` = remis en jeu (1 fois par jour)\n```\n" + "\n".join(lines) + "\n```"
    embed.set_footer(text=f"{complets}/{len(users)} complets • MAJ {datetime.now(TZ):%H:%M}")
    return embed


async def post_tableau(channel):
    msg = await channel.send(embed=build_embed(), view=TableauView())
    set_config("tableau_channel", channel.id)
    set_config("tableau_msg", msg.id)
    set_config("tableau_day", today())


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


async def post_recap(day):
    """Récap dans les logs : qui n'a pas tout remis ce jour-là."""
    users = get_members()
    done = get_done(day)
    lignes = []
    for uid, name in users:
        manque = [f"{TYPES[k][1]} {TYPES[k][0]}" for k in TYPES if (uid, k) not in done]
        if manque:
            lignes.append(f"• **{name}** → {', '.join(manque)}")
    if lignes:
        await log(f"📊 **Récap du {day:%d/%m}** - il manquait :\n" + "\n".join(lignes))
    else:
        await log(f"📊 **Récap du {day:%d/%m}** - tout le monde a tout remis ✅")


# ==================== HISTORIQUE ====================
JOURS = ["L", "M", "M", "J", "V", "S", "D"]
JOURS_FR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]


def week_start_of(offset=0):
    """Lundi de la semaine en cours (offset=0) ou d'il y a `offset` semaines."""
    t = today()
    return t - timedelta(days=t.weekday()) - timedelta(weeks=offset)


def get_week_rows(start):
    with db() as c:
        c.execute("SELECT day, user_id, type FROM remises_jour WHERE day BETWEEN %s AND %s",
                  (start, start + timedelta(days=6)))
        return c.fetchall()


def build_historique_embed(offset=0):
    start = week_start_of(offset)
    days = [start + timedelta(days=i) for i in range(7)]
    past = [d for d in days if d <= today()]
    users = get_members()

    per_day = {}  # (user_id, jour) -> types remis ce jour-là
    for d, uid, t in get_week_rows(start):
        per_day.setdefault((uid, d), set()).add(t)

    embed = discord.Embed(
        title=f"📅 Historique - du {days[0]:%d/%m} au {days[-1]:%d/%m}",
        color=discord.Color.gold(),
    )
    if not users:
        embed.description = "Aucun membre suivi."
        return embed

    h1 = f"{'Membre':<12}" + "".join(f"{j:<3}" for j in JOURS) + "Total"
    h2 = f"{'Membre':<12}" + "".join(f"{code:<6}" for _, _, code in TYPES.values())
    t1, t2 = [h1, "-" * len(h1)], [h2, "-" * len(h2)]
    maxi = len(TYPES) * len(past)

    for uid, name in users:
        short = (name or str(uid))[:11]
        cells, total = "", 0
        for d in days:
            if d > today():
                cells += f"{'-':<3}"
            else:
                n = len(per_day.get((uid, d), ()))
                total += n
                cells += f"{n:<3}"
        t1.append(f"{short:<12}{cells}{total}/{maxi}")

        par_type = ""
        for key in TYPES:
            n = sum(1 for d in past if key in per_day.get((uid, d), ()))
            par_type += f"{f'{n}/{len(past)}':<6}"
        t2.append(f"{short:<12}{par_type}")

    embed.description = (
        f"Chiffre = remises faites ce jour-là (sur {len(TYPES)})\n```\n" + "\n".join(t1) + "\n```\n"
        "Par type (nombre de jours remis)\n```\n" + "\n".join(t2) + "\n```"
    )[:4000]
    embed.set_footer(text="Semaine en cours" if offset == 0 else f"Il y a {offset} semaine(s)")
    return embed


def build_membre_embed(member, offset=0):
    start = week_start_of(offset)
    per_day = {}
    for d, uid, t in get_week_rows(start):
        if uid == member.id:
            per_day.setdefault(d, set()).add(t)

    lines, complets = [], 0
    for i in range(7):
        d = start + timedelta(days=i)
        if d > today():
            break
        types = [k for k in TYPES if k in per_day.get(d, ())]
        if len(types) == len(TYPES):
            complets += 1
        txt = " ".join(f"{TYPES[k][1]} {TYPES[k][0]}" for k in types) if types else "❌ rien"
        lines.append(f"**{JOURS_FR[i]} {d:%d/%m}** : {txt}")

    embed = discord.Embed(
        title=f"📅 {member.display_name} - semaine du {start:%d/%m}",
        description="\n".join(lines) or "Rien pour le moment.",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"{complets} jour(s) complet(s)")
    return embed


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


# ==================== VIEWS : COCHER PAR TYPE (rapide) ====================
class PresentSelect(discord.ui.Select):
    """Liste à choix multiples : on sélectionne tous ceux qui ont remis ce type aujourd'hui."""

    def __init__(self, key, chunk, done_ids, total_chunks):
        self.key = key
        self.names = {uid: (name or str(uid)) for uid, name in chunk}
        self.done_ids = {uid for uid, _ in chunk if uid in done_ids}
        options = [
            discord.SelectOption(label=self.names[uid][:100], value=str(uid), default=(uid in self.done_ids))
            for uid, _ in chunk
        ]
        label = TYPES[key][0]
        if total_chunks == 1:
            placeholder = f"{label} : qui l'a remis ?"
        else:
            first = (chunk[0][1] or "?")[:1].upper()
            last = (chunk[-1][1] or "?")[:1].upper()
            placeholder = f"{label} - membres {first} → {last}"
        super().__init__(placeholder=placeholder, options=options, min_values=0, max_values=len(options))

    async def callback(self, interaction: discord.Interaction):
        chosen = {int(v) for v in self.values}
        chunk_ids = list(self.names)
        set_type_for(self.key, chosen, chunk_ids, interaction.user.id)

        ajoutes = [self.names[u] for u in chosen - self.done_ids]
        retires = [self.names[u] for u in self.done_ids - chosen]
        self.done_ids = chosen
        for opt in self.options:
            opt.default = int(opt.value) in chosen

        await interaction.response.edit_message(view=self.view)
        await refresh_tableau()

        label, emoji, _ = TYPES[self.key]
        if ajoutes:
            await log(f"✅ {interaction.user.mention} a coché {emoji} **{label}** : {', '.join(ajoutes)}")
        if retires:
            await log(f"↩️ {interaction.user.mention} a décoché {emoji} **{label}** : {', '.join(retires)}")


class ParTypeView(discord.ui.View):
    def __init__(self, key):
        super().__init__(timeout=300)
        self.key = key
        users = get_members()
        done_ids = {uid for uid, k in get_done() if k == key}
        chunks = [users[i:i + 25] for i in range(0, len(users), 25)][:5]  # 5 listes max = 125 membres
        for chunk in chunks:
            self.add_item(PresentSelect(key, chunk, done_ids, len(chunks)))

    def texte(self):
        label, emoji, _ = TYPES[self.key]
        return f"{emoji} **{label}** : sélectionne **tous ceux qui l'ont remis aujourd'hui**, puis ferme la liste."


class TypeChoix(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        for key, (label, emoji, _) in TYPES.items():
            btn = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary)
            btn.callback = self.make_callback(key)
            self.add_item(btn)

    def make_callback(self, key):
        async def callback(interaction: discord.Interaction):
            view = ParTypeView(key)
            await interaction.response.edit_message(content=view.texte(), view=view)
        return callback


# ==================== VIEWS : COCHER PAR MEMBRE ====================
class CasesView(discord.ui.View):
    """Boutons (un par type + Tout) pour cocher / décocher un membre."""

    def __init__(self, member: discord.Member):
        super().__init__(timeout=180)
        self.member = member
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        done = {k for uid, k in get_done() if uid == self.member.id}
        for key, (label, emoji, _) in TYPES.items():
            style = discord.ButtonStyle.success if key in done else discord.ButtonStyle.secondary
            btn = discord.ui.Button(label=label, emoji=emoji, style=style)
            btn.callback = self.make_callback(key, label)
            self.add_item(btn)
        tout = discord.ui.Button(label="Tout", emoji="✅", style=discord.ButtonStyle.primary)
        tout.callback = self.tout_callback
        self.add_item(tout)

    def texte(self):
        return f"Remises de **{self.member.display_name}** aujourd'hui (vert = remis). Clique pour cocher / décocher :"

    def make_callback(self, key, label):
        async def callback(interaction: discord.Interaction):
            checked = toggle(self.member.id, key, interaction.user.id)
            self.rebuild()
            await interaction.response.edit_message(content=self.texte(), view=self)
            await refresh_tableau()
            action = "a coché" if checked else "a décoché"
            await log(f"✏️ {interaction.user.mention} {action} **{label}** pour {self.member.mention}")
        return callback

    async def tout_callback(self, interaction: discord.Interaction):
        check_all(self.member.id, interaction.user.id)
        self.rebuild()
        await interaction.response.edit_message(content=self.texte(), view=self)
        await refresh_tableau()
        await log(f"✅ {interaction.user.mention} a coché **tout** pour {self.member.mention}")


class MembreSelect(discord.ui.Select):
    """Liste déroulante (25 membres max par liste, limite Discord)."""

    def __init__(self, chunk, total_chunks):
        options = [
            discord.SelectOption(label=(name or str(uid))[:100], value=str(uid))
            for uid, name in chunk
        ]
        if total_chunks == 1:
            placeholder = "Choisis le membre..."
        else:
            first = (chunk[0][1] or "?")[:1].upper()
            last = (chunk[-1][1] or "?")[:1].upper()
            placeholder = f"Membres {first} → {last}"
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(int(self.values[0]))
        if not member:
            return await interaction.response.send_message("❌ Membre introuvable sur le serveur.", ephemeral=True)
        view = CasesView(member)
        await interaction.response.edit_message(content=view.texte(), view=view)


class ChoisirMembre(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        users = get_members()
        chunks = [users[i:i + 25] for i in range(0, len(users), 25)][:5]
        for chunk in chunks:
            self.add_item(MembreSelect(chunk, len(chunks)))


# ==================== VIEW : HISTORIQUE ====================
class HistoriqueView(discord.ui.View):
    def __init__(self, offset=0):
        super().__init__(timeout=300)
        self.offset = offset
        self.suivant.disabled = (offset == 0)

    @discord.ui.button(label="Semaine précédente", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def precedent(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = HistoriqueView(self.offset + 1)
        await interaction.response.edit_message(embed=build_historique_embed(view.offset), view=view)

    @discord.ui.button(label="Semaine suivante", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def suivant(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = HistoriqueView(self.offset - 1)
        await interaction.response.edit_message(embed=build_historique_embed(view.offset), view=view)


# ==================== VIEW DU TABLEAU ====================
class TableauView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cocher par type", emoji="⚡", style=discord.ButtonStyle.primary,
                       custom_id="tableau:type")
    async def par_type(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        await interaction.response.send_message("Quel type viens-tu de recevoir ?", view=TypeChoix(), ephemeral=True)

    @discord.ui.button(label="Par membre", emoji="👤", style=discord.ButtonStyle.secondary,
                       custom_id="tableau:membre")
    async def par_membre(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        await interaction.response.send_message("Choisis le membre :", view=ChoisirMembre(), ephemeral=True)

    @discord.ui.button(label="Historique", emoji="📅", style=discord.ButtonStyle.secondary,
                       custom_id="tableau:histo")
    async def historique_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        await interaction.response.send_message(embed=build_historique_embed(0), view=HistoriqueView(0),
                                                ephemeral=True)

    @discord.ui.button(label="Rappel", emoji="📢", style=discord.ButtonStyle.danger,
                       custom_id="tableau:rappel")
    async def rappel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        done = get_done()
        sent = 0
        for uid, _ in get_members():
            if any((uid, k) not in done for k in TYPES):
                member = interaction.guild.get_member(uid)
                if member:
                    try:
                        await member.send("⚠️ **Rappel Diamond City**\nIl te reste des remises à faire aujourd'hui.")
                        sent += 1
                    except discord.Forbidden:
                        pass
        await interaction.followup.send(f"✅ Rappel envoyé à **{sent}** membre(s).", ephemeral=True)


# ==================== COMMANDES ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def settableaux(ctx):
    """Poste le tableau dans ce salon (à faire une seule fois)."""
    await post_tableau(ctx.channel)


@bot.command()
@commands.has_permissions(administrator=True)
async def cocher(ctx, member: discord.Member, type_: str):
    """!cocher @membre atm  -> coche / décoche une case pour aujourd'hui."""
    key = next((k for k in TYPES if k.lower() == type_.lower()), None)
    if not key:
        return await ctx.send("❌ Type invalide : `contenair`, `atm` ou `superette`.")
    checked = toggle(member.id, key, ctx.author.id)
    await ctx.send(f"{'✅ Coché' if checked else '↩️ Décoché'} : **{TYPES[key][0]}** pour **{member.display_name}**.")
    await refresh_tableau()


@bot.command(name="resetquotas", aliases=["reset", "resetquota"])
@commands.has_permissions(administrator=True)
async def reset_quotas(ctx, confirm: str = None):
    """Décoche toutes les cases d'aujourd'hui."""
    if confirm != "confirm":
        return await ctx.send(f"⚠️ Pour tout décocher aujourd'hui ({today():%d/%m}), tape `!resetquotas confirm`")
    with db() as c:
        c.execute("DELETE FROM remises_jour WHERE day = %s", (today(),))
        deleted = c.rowcount
    await ctx.send(f"✅ {deleted} case(s) décochée(s).")
    await refresh_tableau()


@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    """Force la synchro avec le rôle."""
    await sync_members()
    await refresh_tableau()
    await ctx.send(f"✅ Synchronisé : **{len(get_members())}** membre(s) avec le rôle **{ROLE_NAME}**.")


@bot.command(aliases=["histo"])
@commands.has_permissions(administrator=True)
async def historique(ctx, member: discord.Member = None, semaines: int = 0):
    """!historique | !historique @membre | !historique @membre 1 (semaine dernière)"""
    if member:
        return await ctx.send(embed=build_membre_embed(member, max(0, semaines)))
    await ctx.send(embed=build_historique_embed(0), view=HistoriqueView(0))


@bot.command()
@commands.has_permissions(administrator=True)
async def listusers(ctx):
    users = get_members()
    if not users:
        return await ctx.send("❌ Aucun membre suivi.")
    desc = "\n".join(f"• **{u}** (`{uid}`)" for uid, u in users)
    await ctx.send(embed=discord.Embed(title="👥 Membres suivis", description=desc[:4000], color=discord.Color.green()))


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
@tasks.loop(minutes=5)
async def check_new_day():
    """Quand le jour change : récap de la veille dans les logs, puis le tableau repart à zéro."""
    last = get_config("tableau_day")
    if not last or not get_config("tableau_channel"):
        return
    if last != str(today()):
        set_config("tableau_day", today())
        await post_recap(date.fromisoformat(last))
        await refresh_tableau()


@tasks.loop(minutes=10)
async def auto_sync():
    """Filet de sécurité : resynchronise le rôle toutes les 10 minutes."""
    await sync_members()


@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not auto_sync.is_running():
        auto_sync.start()  # fait aussi une synchro immédiate au démarrage
    if not check_new_day.is_running():
        check_new_day.start()


if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant")
