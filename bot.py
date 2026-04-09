import discord
from discord import app_commands
from discord.ext import commands
from google.oauth2.service_account import Credentials
import gspread
import asyncio
import math
import time
import os
from datetime import datetime

# ==================== CONFIG ====================
BOT_TOKEN  = os.environ["BOT_TOKEN"]
SHEET_ID   = "1_wOqn-4TFMNHpZ01tasNigXzXvdl5wU1gJkXX4RdpHo"
CHANNEL_ID = 1491100565613314198
ROLE_ID    = 1491100731565015132

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEETS_CONFIG = {
    "Boss_Server":   {"label": "🟢 Boss Server",   "color": 0x3498DB},
    "Boss_Invasion": {"label": "🔴 Boss Invasion",  "color": 0xE74C3C},
}

# ==================== GOOGLE SHEETS (sync) ====================
def get_gs_client():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds)

_cache   = {"data": [], "ts": 0}
CACHE_TTL = 60  # วินาที

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
bot     = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    try:
        await fetch_bosses(force=True)
        print(f"✅ โหลดข้อมูลบอสสำเร็จ: {len(_cache['data'])} ตัว")
    except Exception as e:
        print(f"⚠️ โหลดข้อมูลบอสไม่สำเร็จ: {e}")
    print(f"✅ Bot พร้อมใช้งาน: {bot.user}")

# ==================== /kill ====================
@bot.tree.command(name="kill", description="บันทึกเวลาที่บอสตาย")
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
        bosses  = _fetch_bosses_sync()  # ใช้ cache เท่านั้น ไม่ fetch ใหม่
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
class BossListView(discord.ui.View):
    PER_PAGE = 10

    def __init__(self, bosses: list, page: int = 0):
        super().__init__(timeout=120)
        self.bosses      = bosses
        self.page        = page
        self.total_pages = max(1, math.ceil(len(bosses) / self.PER_PAGE))
        self._sync()

    def _sync(self):
        self.back_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.page_btn.label    = f"{self.page + 1} / {self.total_pages}"

    def build_embed(self) -> discord.Embed:
        start = self.page * self.PER_PAGE
        chunk = self.bosses[start : start + self.PER_PAGE]
        embed = discord.Embed(title="📋 รายชื่อบอสทั้งหมด", color=0x5865F2)

        for sheet_name, cfg in SHEETS_CONFIG.items():
            rows = [b for b in chunk if b["sheet"] == sheet_name]
            if rows:
                lines = "\n".join(
                    f"**{b['name']}** — `{b['spawn_time']}`" for b in rows
                )
                embed.add_field(name=cfg["label"], value=lines, inline=False)

        embed.set_footer(
            text=f"หน้า {self.page + 1}/{self.total_pages}  •  รวม {len(self.bosses)} ตัว"
        )
        return embed

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, _btn):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_btn(self, interaction: discord.Interaction, _btn):
        pass

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _btn):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


@bot.tree.command(name="list", description="แสดงรายชื่อบอสทั้งหมดพร้อมเวลาเกิด")
async def list_bosses(interaction: discord.Interaction):
    await interaction.response.defer()
    bosses = await fetch_bosses(force=True)
    view   = BossListView(bosses)
    await interaction.followup.send(embed=view.build_embed(), view=view)


# ==================== RUN ====================
bot.run(BOT_TOKEN)
