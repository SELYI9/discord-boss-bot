import discord
from discord import app_commands
from google.oauth2.service_account import Credentials
import gspread
import asyncio
import math
import time
import os
from datetime import datetime

# ==================== CONFIG ====================
BOT_TOKEN        = os.environ["BOT_TOKEN"]
SHEET_ID         = "1LaJoabXkfB-qzg9HgbB1D15FQfrI9yq4-M4GpKDN_N8"
CHANNEL_ID       = 1491109222274826261
ROLE_ID          = 1442005693182906420

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEETS_CONFIG = {
    "Boss_Server":   {"label": "🟢 Boss Server",   "color": 0x3498DB},
    "Boss_Invasion": {"label": "🔴 Boss Invasion",  "color": 0xE74C3C},
}

# ==================== GOOGLE SHEETS (sync) ====================
def get_gs_client():
    import json
    info  = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

_cache    = {"data": [], "ts": 0}
CACHE_TTL = 60

def _fetch_bosses_sync(force=False):
    global _cache
    if not force and time.time() - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    client = get_gs_client()
    ss     = client.open_by_key(SHEET_ID)
    bosses = []

    for sheet_name in SHEETS_CONFIG:
        ws   = ss.worksheet(sheet_name)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if not row[0]:
                continue
            bosses.append({
                "name":       row[0],
                "sheet":      sheet_name,
                "row":        i,
                "spawn_time": row[3] if len(row) > 3 else "N/A",
            })

    _cache = {"data": bosses, "ts": time.time()}
    return bosses

def _update_sheet_sync(sheet_name, row, time_str):
    client = get_gs_client()
    ws     = client.open_by_key(SHEET_ID).worksheet(sheet_name)
    ws.batch_update(
        [
            {"range": f"C{row}", "values": [[time_str]]},
            {"range": f"G{row}", "values": [["Not Sent"]]},
            {"range": f"H{row}", "values": [["Not Sent"]]},
        ],
        value_input_option="USER_ENTERED",
    )

# ==================== ASYNC WRAPPERS ====================
async def fetch_bosses(force=False):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_bosses_sync, force)

async def update_sheet(sheet_name, row, time_str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _update_sheet_sync, sheet_name, row, time_str)

# ==================== BOT ====================
intents = discord.Intents.default()
bot     = discord.Client(intents=intents)
tree    = app_commands.CommandTree(bot)

@bot.event
async def on_guild_join(guild):
    await tree.sync(guild=guild)
    print(f"✅ Sync คำสั่งไปยัง Server ใหม่: {guild.name}")

@bot.event
async def on_ready():
    for guild in bot.guilds:
        await tree.sync(guild=guild)
    await tree.sync()
    try:
        await fetch_bosses(force=True)
        print(f"✅ โหลดข้อมูลบอสสำเร็จ: {len(_cache['data'])} ตัว")
    except Exception as e:
        import traceback
        print(f"⚠️ โหลดข้อมูลบอสไม่สำเร็จ: {type(e).__name__}: {e}")
        print(traceback.format_exc())
    print(f"✅ Bot พร้อมใช้งาน: {bot.user}")

# ==================== /kill ====================
@tree.command(name="kill", description="บันทึกเวลาที่บอสตาย")
@app_commands.describe(
    boss="ชื่อบอส (พิมพ์เพื่อค้นหา)",
    time="เวลาที่บอสตาย รูปแบบ HH:MM เช่น 14:30",
)
async def kill(interaction: discord.Interaction, boss: str, time: str):
    try:
        dt = datetime.strptime(time, "%H:%M")
    except ValueError:
        await interaction.response.send_message(
            "⚠️ รูปแบบเวลาผิด กรุณาใช้ HH:MM เช่น `14:30`", ephemeral=True
        )
        return

    bosses = await fetch_bosses(force=True)
    found  = next((b for b in bosses if b["name"] == boss), None)

    if not found:
        await interaction.response.send_message(
            f"⚠️ ไม่พบบอส: `{boss}`", ephemeral=True
        )
        return

    await interaction.response.defer()

    time_str = f"{dt.hour:02d}:{dt.minute:02d}:00"
    await update_sheet(found["sheet"], found["row"], time_str)

    info  = SHEETS_CONFIG[found["sheet"]]
    embed = discord.Embed(title="✅ บันทึกเวลาตายบอสสำเร็จ", color=0x2ECC71)
    embed.add_field(name="👾 ชื่อบอส", value=f"**{boss}**",    inline=True)
    embed.add_field(name="📋 Sheet",   value=info["label"],     inline=True)
    embed.add_field(name="💀 เวลาตาย", value=f"**{time} น.**", inline=False)
    embed.set_footer(text=f"อัปเดตโดย {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)


@kill.autocomplete("boss")
async def kill_autocomplete(interaction: discord.Interaction, current: str):
    try:
        bosses  = _fetch_bosses_sync()
        choices = []
        for b in bosses:
            label = "Server" if b["sheet"] == "Boss_Server" else "Invasion"
            name  = f"[{label}] {b['name']}"
            if current.lower() in b["name"].lower():
                choices.append(app_commands.Choice(name=name, value=b["name"]))
        return choices[:25]
    except Exception:
        return []


# ==================== /list ====================
def has_spawn_time(b: dict) -> bool:
    t = b.get("spawn_time", "")
    return bool(t) and t != "N/A" and t.strip() != ""

class BossListView(discord.ui.View):
    PER_PAGE = 15

    def __init__(self, bosses: list, title: str, color: int, page: int = 0):
        super().__init__(timeout=120)
        def sort_key(b):
            try:
                parts = b["spawn_time"].strip().split(":")
                return int(parts[0]) * 60 + int(parts[1])
            except Exception:
                return 9999
        self.bosses      = sorted([b for b in bosses if has_spawn_time(b)], key=sort_key)
        self.title       = title
        self.color       = color
        self.page        = page
        self.total_pages = max(1, math.ceil(len(self.bosses) / self.PER_PAGE))
        self._sync()

    def _sync(self):
        self.back_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.page_btn.label    = f"{self.page + 1} / {self.total_pages}"

    def build_embed(self) -> discord.Embed:
        start = self.page * self.PER_PAGE
        chunk = self.bosses[start : start + self.PER_PAGE]
        embed = discord.Embed(title=self.title, color=self.color)

        for sheet_name, cfg in SHEETS_CONFIG.items():
            rows = [b for b in chunk if b["sheet"] == sheet_name]
            if not rows:
                continue
            def fmt_time(t: str) -> str:
                try:
                    parts = t.strip().split(":")
                    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
                except Exception:
                    return t
            lines = "\n".join(
                f"`{fmt_time(b['spawn_time'])}`  {b['name']}" for b in rows
            )
            total = sum(1 for b in self.bosses if b["sheet"] == sheet_name)
            embed.add_field(
                name=f"{cfg['label']} — {total} ตัว",
                value=lines,
                inline=False,
            )

        embed.set_footer(
            text=f"หน้า {self.page + 1}/{self.total_pages}  •  รวม {len(self.bosses)} ตัว"
        )
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, _btn):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_btn(self, interaction: discord.Interaction, _btn):
        pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _btn):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


@tree.command(name="list", description="แสดงรายชื่อบอสพร้อมเวลาเกิด")
@app_commands.describe(sheet="เลือก Sheet ที่ต้องการดู")
@app_commands.choices(sheet=[
    app_commands.Choice(name="ทั้งหมด",         value="all"),
    app_commands.Choice(name="🟢 Boss Server",   value="Boss_Server"),
    app_commands.Choice(name="🔴 Boss Invasion",  value="Boss_Invasion"),
])
async def list_bosses(interaction: discord.Interaction, sheet: str = "all"):
    await interaction.response.defer()
    all_bosses = await fetch_bosses(force=True)

    if sheet == "Boss_Server":
        bosses = [b for b in all_bosses if b["sheet"] == "Boss_Server"]
        title  = "🟢 Boss Server"
        color  = 0x3498DB
    elif sheet == "Boss_Invasion":
        bosses = [b for b in all_bosses if b["sheet"] == "Boss_Invasion"]
        title  = "🔴 Boss Invasion"
        color  = 0xE74C3C
    else:
        bosses = all_bosses
        title  = "Boss List"
        color  = 0x5865F2

    view = BossListView(bosses, title, color)
    await interaction.followup.send(embed=view.build_embed(), view=view)


# ==================== RUN ====================
bot.run(BOT_TOKEN, log_handler=None)
