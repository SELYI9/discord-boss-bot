import discord
from discord import app_commands
from google.oauth2.service_account import Credentials
import gspread
import asyncio
import time
import os
from datetime import datetime, timezone, timedelta

RETRY_ATTEMPTS = 3
RETRY_DELAY    = 5  # วินาที

# ==================== CONFIG ====================
BOT_TOKEN  = os.environ["BOT_TOKEN"]
SHEET_ID   = "1LaJoabXkfB-qzg9HgbB1D15FQfrI9yq4-M4GpKDN_N8"
CHANNEL_ID = 1491109222274826261
ROLE_ID    = 1442005693182906420

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEETS_CONFIG = {
    "Boss_Server":   {"label": "🟢 Boss Server",  "color": 0x3498DB, "mention": True},
    "Boss_Invasion": {"label": "🔴 Boss Invasion", "color": 0xE74C3C, "mention": False},
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

    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
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
                        "cd_hours":   int(row[1]) if row[1] else 0,
                        "death_time": row[2] if len(row) > 2 else "",
                        "spawn_time": row[3] if len(row) > 3 else "N/A",
                        "notif_5min": row[6] if len(row) > 6 else "Not Sent",
                        "notif_1min": row[7] if len(row) > 7 else "Not Sent",
                    })

            _cache = {"data": bosses, "ts": time.time()}
            return bosses

        except gspread.exceptions.APIError as e:
            status = e.args[0].get("code", 0) if isinstance(e.args[0], dict) else 0
            if status in (500, 503) and attempt < RETRY_ATTEMPTS:
                print(f"⚠️ Google API {status} (ครั้งที่ {attempt}/{RETRY_ATTEMPTS}) — รอ {RETRY_DELAY}s แล้วลองใหม่")
                time.sleep(RETRY_DELAY)
                last_err = e
                continue
            raise

    raise last_err

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

def _update_notif_status_sync(sheet_name, row, col, value):
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            client = get_gs_client()
            ws     = client.open_by_key(SHEET_ID).worksheet(sheet_name)
            col_letter = "G" if col == 7 else "H"
            ws.update(f"{col_letter}{row}", [[value]])
            return
        except gspread.exceptions.APIError as e:
            status = e.args[0].get("code", 0) if isinstance(e.args[0], dict) else 0
            if status in (500, 503) and attempt < RETRY_ATTEMPTS:
                print(f"⚠️ Google API {status} อัปเดต notif status (ครั้งที่ {attempt}/{RETRY_ATTEMPTS}) — รอ {RETRY_DELAY}s")
                time.sleep(RETRY_DELAY)
                last_err = e
                continue
            raise
    raise last_err

# ==================== ASYNC WRAPPERS ====================
async def fetch_bosses(force=False):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_bosses_sync, force)

async def update_sheet(sheet_name, row, time_str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _update_sheet_sync, sheet_name, row, time_str)

async def update_notif_status(sheet_name, row, col, value):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _update_notif_status_sync, sheet_name, row, col, value)

# ==================== HELPERS ====================
_BKK = timezone(timedelta(hours=7))

def calc_spawn_datetime(death_time_str: str, cd_hours: int, for_auto_update: bool = False):
    """
    for_auto_update=False → หา spawn ที่ใกล้จะมาถึง (ใช้กับ notification)
    for_auto_update=True  → หา spawn ล่าสุดที่ผ่านไปแล้ว (ใช้กับ auto-update)
    """
    if not death_time_str or not cd_hours:
        return None
    try:
        parts  = death_time_str.strip().split(":")
        hour   = int(parts[0])
        minute = int(parts[1])
        now    = datetime.now(_BKK)
        base   = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if for_auto_update:
            # หา spawn ล่าสุดที่ผ่านไปแล้ว (ไม่เกิน 24 ชั่วโมง)
            best = None
            for day_offset in [0, -1, -2]:
                candidate = base + timedelta(days=day_offset) + timedelta(hours=cd_hours)
                # spawn ต้องผ่านไปแล้ว (อยู่ในอดีต)
                if candidate <= now and (best is None or candidate > best):
                    best = candidate
            return best
        else:
            # หา spawn ที่ใกล้จะมาถึง (notification)
            spawn = base + timedelta(hours=cd_hours)
            if spawn > now + timedelta(hours=cd_hours, minutes=5):
                spawn = base - timedelta(days=1) + timedelta(hours=cd_hours)
            if spawn < now - timedelta(minutes=5):
                spawn = base - timedelta(days=1) + timedelta(hours=cd_hours)
            return spawn
    except Exception:
        return None

# ==================== BOT ====================
intents = discord.Intents.default()
bot     = discord.Client(intents=intents)
tree    = app_commands.CommandTree(bot)

# tracking message_id → boss info สำหรับ auto-update
# key: message_id, value: {"boss": boss_dict, "spawn_dt": datetime, "pressed": bool}
_pending_auto_update: dict = {}

# ==================== NOTIFICATION LOOP ====================
async def notification_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"⚠️ ไม่พบ Channel ID: {CHANNEL_ID}")
        return

    print("✅ Notification loop เริ่มทำงาน")

    while not bot.is_closed():
        loop_start = time.time()
        try:
            bosses = await fetch_bosses(force=True)
            now    = datetime.now(_BKK)

            for boss in bosses:
                spawn_dt = calc_spawn_datetime(boss["death_time"], boss["cd_hours"])
                if not spawn_dt:
                    continue

                diff_min = (spawn_dt - now).total_seconds() / 60
                cfg      = SHEETS_CONFIG[boss["sheet"]]

                # แจ้งเตือน 5 นาที
                if 3 < diff_min <= 5 and boss["notif_5min"] != "Sent":
                    embed = _build_notif_embed(boss, spawn_dt, cfg, minutes=5)
                    mention = f"<@&{ROLE_ID}> " if cfg["mention"] else ""
                    await channel.send(
                        content=f"{mention}⚠️ [5 นาที] **{boss['name']}** กำลังจะเกิด!",
                        embed=embed,
                    )
                    await update_notif_status(boss["sheet"], boss["row"], 7, "Sent")
                    boss["notif_5min"] = "Sent"
                    print(f"🔔 แจ้งเตือน 5 นาที: {boss['name']} เกิด {spawn_dt.hour:02d}:{spawn_dt.minute:02d}")

                # แจ้งเตือน 1 นาที — พร้อมปุ่มป้อนเวลาตาย
                elif 0 < diff_min <= 1 and boss["notif_1min"] != "Sent":
                    embed   = _build_notif_embed(boss, spawn_dt, cfg, minutes=1)
                    mention = f"<@&{ROLE_ID}> " if cfg["mention"] else ""
                    view    = KillButtonView(boss, spawn_dt)
                    msg = await channel.send(
                        content=f"{mention}🚨 [1 นาที] **{boss['name']}** กำลังจะเกิด!",
                        embed=embed,
                        view=view,
                    )
                    await update_notif_status(boss["sheet"], boss["row"], 8, "Sent")
                    boss["notif_1min"] = "Sent"
                    print(f"🔔 แจ้งเตือน 1 นาที: {boss['name']} เกิด {spawn_dt.hour:02d}:{spawn_dt.minute:02d}")

                    # ลงทะเบียน message สำหรับ edit UI ภายหลัง
                    _pending_auto_update[msg.id] = {
                        "boss":     boss,
                        "spawn_dt": spawn_dt,
                        "pressed":  False,
                        "msg":      msg,
                    }

        except Exception as e:
            import traceback
            print(f"⚠️ Notification loop error: {e}")
            print(traceback.format_exc())

        # sleep เท่ากับเวลาที่เหลือจาก 60 วินาที ไม่ให้ drift
        elapsed = time.time() - loop_start
        await asyncio.sleep(max(0, 60 - elapsed))


def _build_notif_embed(boss, spawn_dt, cfg, minutes):
    title = "⚠️ บอสกำลังจะเกิด!" if minutes == 5 else "🚨 บอสกำลังจะเกิดใน 1 นาที!"
    embed = discord.Embed(title=title, color=cfg["color"])
    embed.set_author(name=cfg["label"])
    embed.add_field(name="👾 ชื่อบอส",  value=f"**{boss['name']}**",                          inline=True)
    embed.add_field(name="⏰ เวลาเกิด", value=f"**{spawn_dt.hour:02d}:{spawn_dt.minute:02d} น.**", inline=True)
    embed.set_footer(text=f"Boss Tracker • แจ้งเตือนก่อนเกิด {minutes} นาที")
    embed.timestamp = datetime.now(_BKK)
    return embed


# ==================== AUTO-UPDATE LOOP ====================
async def auto_update_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            bosses = await fetch_bosses(force=True)
            now    = datetime.now(_BKK)

            for boss in bosses:
                # ต้องมีการส่งแจ้งเตือนในรอบนี้ก่อน จึงจะ auto-update
                if boss["notif_1min"] != "Sent" and boss["notif_5min"] != "Sent":
                    continue

                spawn_dt = calc_spawn_datetime(boss["death_time"], boss["cd_hours"], for_auto_update=True)
                if not spawn_dt:
                    continue

                # auto-update เมื่อเวลาเกิดผ่านไปแล้ว 5 นาที
                deadline = spawn_dt + timedelta(minutes=5)
                if now < deadline:
                    continue

                time_str = f"{spawn_dt.hour:02d}:{spawn_dt.minute:02d}:00"
                try:
                    await update_sheet(boss["sheet"], boss["row"], time_str)
                    print(f"🔄 Auto-update: {boss['name']} → {time_str[:5]}")

                    # หา Discord message ที่ตรงกัน เพื่อ edit UI
                    for msg_id, entry in list(_pending_auto_update.items()):
                        e_boss = entry["boss"]
                        if e_boss["name"] == boss["name"] and e_boss["sheet"] == boss["sheet"]:
                            if not entry["pressed"]:
                                msg = entry.get("msg")
                                if msg:
                                    embed = discord.Embed(
                                        title="🔄 Auto-Update เวลาตาย",
                                        description="ไม่มีการป้อนเวลา ระบบบันทึกอัตโนมัติ",
                                        color=0x95A5A6,
                                    )
                                    embed.add_field(name="👾 ชื่อบอส", value=f"**{boss['name']}**",   inline=True)
                                    embed.add_field(name="💀 เวลาตาย", value=f"**{time_str[:5]} น.**", inline=True)
                                    try:
                                        await msg.edit(embed=embed, view=None)
                                    except Exception:
                                        pass
                            _pending_auto_update.pop(msg_id, None)
                            break

                except Exception as e:
                    print(f"⚠️ Auto-update error ({boss['name']}): {e}")

        except Exception as e:
            import traceback
            print(f"⚠️ Auto-update loop error: {e}")
            print(traceback.format_exc())

        await asyncio.sleep(30)


# ==================== KILL BUTTON + MODAL ====================
class KillModal(discord.ui.Modal, title="ป้อนเวลาที่บอสตาย"):
    death_time = discord.ui.TextInput(
        label="เวลาตาย (HH:MM)",
        placeholder="เช่น 14:30",
        min_length=4,
        max_length=5,
    )

    def __init__(self, boss: dict, msg_id: int):
        super().__init__()
        self.boss   = boss
        self.msg_id = msg_id

    async def on_submit(self, interaction: discord.Interaction):
        time_str_raw = self.death_time.value.strip()
        try:
            dt = datetime.strptime(time_str_raw, "%H:%M")
        except ValueError:
            await interaction.response.send_message(
                "⚠️ รูปแบบเวลาผิด กรุณาใช้ HH:MM เช่น `14:30`", ephemeral=True
            )
            return

        time_str = f"{dt.hour:02d}:{dt.minute:02d}:00"
        await update_sheet(self.boss["sheet"], self.boss["row"], time_str)

        if self.msg_id in _pending_auto_update:
            _pending_auto_update[self.msg_id]["pressed"] = True

        cfg   = SHEETS_CONFIG[self.boss["sheet"]]
        embed = discord.Embed(title="✅ บันทึกเวลาตายบอสสำเร็จ", color=0x2ECC71)
        embed.add_field(name="👾 ชื่อบอส", value=f"**{self.boss['name']}**",  inline=True)
        embed.add_field(name="📋 Sheet",   value=cfg["label"],                 inline=True)
        embed.add_field(name="💀 เวลาตาย", value=f"**{time_str[:5]} น.**",    inline=False)
        embed.set_footer(text=f"อัปเดตโดย {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)

        try:
            msg = _pending_auto_update.get(self.msg_id, {}).get("msg")
            if msg:
                await msg.edit(view=None)
        except Exception:
            pass


class KillButtonView(discord.ui.View):
    def __init__(self, boss: dict, spawn_dt: datetime):
        super().__init__(timeout=None)
        self.boss     = boss
        self.spawn_dt = spawn_dt

    @discord.ui.button(label="UPDATE", style=discord.ButtonStyle.success, emoji="💀")
    async def kill_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        now      = datetime.now(_BKK)
        time_str = f"{now.hour:02d}:{now.minute:02d}:00"
        await update_sheet(self.boss["sheet"], self.boss["row"], time_str)

        if interaction.message.id in _pending_auto_update:
            _pending_auto_update[interaction.message.id]["pressed"] = True

        cfg   = SHEETS_CONFIG[self.boss["sheet"]]
        embed = discord.Embed(title="✅ บันทึกเวลาตายบอสสำเร็จ", color=0x2ECC71)
        embed.add_field(name="👾 ชื่อบอส", value=f"**{self.boss['name']}**",  inline=True)
        embed.add_field(name="📋 Sheet",   value=cfg["label"],                 inline=True)
        embed.add_field(name="💀 เวลาตาย", value=f"**{time_str[:5]} น.**",    inline=False)
        embed.set_footer(text=f"อัปเดตโดย {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)

        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

    @discord.ui.button(label="UPDATE TIME", style=discord.ButtonStyle.primary, emoji="🕐")
    async def kill_time_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = KillModal(boss=self.boss, msg_id=interaction.message.id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="MISS", style=discord.ButtonStyle.danger, emoji="💤")
    async def miss_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        spawn_dt = self.spawn_dt
        time_str = f"{spawn_dt.hour:02d}:{spawn_dt.minute:02d}:00"
        await update_sheet(self.boss["sheet"], self.boss["row"], time_str)

        if interaction.message.id in _pending_auto_update:
            _pending_auto_update[interaction.message.id]["pressed"] = True

        cfg   = SHEETS_CONFIG[self.boss["sheet"]]
        embed = discord.Embed(title="❌ บันทึก MISS สำเร็จ", color=0xB22222)
        embed.add_field(name="👾 ชื่อบอส",  value=f"**{self.boss['name']}**",  inline=True)
        embed.add_field(name="📋 Sheet",    value=cfg["label"],                 inline=True)
        embed.add_field(name="💀 เวลาตาย", value=f"**{time_str[:5]} น.**",     inline=False)
        embed.set_footer(text=f"MISS โดย {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)

        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass


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
class BossListView(discord.ui.View):
    PER_PAGE = 15

    def __init__(self, bosses: list, title: str, color: int, page: int = 0):
        super().__init__(timeout=None)
        self.title = title
        self.color = color
        self.page  = page
        self.pages = self._build_pages(bosses)
        self._sync()

    def _build_pages(self, bosses: list) -> list:
        dated = []
        for b in bosses:
            spawn_dt = calc_spawn_datetime(b.get("death_time", ""), b.get("cd_hours", 0))
            if spawn_dt:
                dated.append((spawn_dt, b))

        date_groups: dict = {}
        for spawn_dt, b in dated:
            date_groups.setdefault(spawn_dt.date(), []).append((spawn_dt, b))

        for d in date_groups:
            date_groups[d].sort(key=lambda x: x[0])

        pages = []
        for date in sorted(date_groups.keys()):
            items = date_groups[date]
            for i in range(0, len(items), self.PER_PAGE):
                pages.append({"date": date, "items": items[i:i + self.PER_PAGE]})

        return pages or [{"date": datetime.now(_BKK).date(), "items": []}]

    def _sync(self):
        self.back_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self.pages) - 1
        self.page_btn.label    = f"{self.page + 1} / {len(self.pages)}"

    def build_embed(self) -> discord.Embed:
        now       = datetime.now(_BKK)
        today     = now.date()
        page_data = self.pages[self.page]
        date      = page_data["date"]
        items     = page_data["items"]

        be_year  = date.year + 543
        date_str = f"{date.day:02d}/{date.month:02d}/{be_year}"
        day_th   = ["จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา."]
        day_name = day_th[date.weekday()]

        if date == today:
            date_header = f"📅 วันนี้ — {day_name} {date_str}"
        elif date == today + timedelta(days=1):
            date_header = f"📅 พรุ่งนี้ — {day_name} {date_str}"
        else:
            date_header = f"📅 {day_name} {date_str}"

        total_bosses = sum(len(p["items"]) for p in self.pages)
        embed = discord.Embed(title=self.title, description=date_header, color=self.color)

        for sheet_name, cfg in SHEETS_CONFIG.items():
            rows = [(dt, b) for dt, b in items if b["sheet"] == sheet_name]
            if not rows:
                continue
            lines = "\n".join(
                f"`{dt.hour:02d}:{dt.minute:02d}` {b['name']}" for dt, b in rows
            )
            embed.add_field(name=cfg["label"], value=lines, inline=False)

        if not items:
            embed.add_field(name="—", value="ไม่มีบอสในช่วงนี้", inline=False)

        embed.set_footer(
            text=f"หน้า {self.page + 1}/{len(self.pages)}  •  รวม {total_bosses} ตัว"
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


# ==================== ON READY ====================
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

    bot.loop.create_task(notification_loop())
    bot.loop.create_task(auto_update_loop())


# ==================== RUN ====================
bot.run(BOT_TOKEN, log_handler=None)
