import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import os
import urllib.parse

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import feedparser
from google import genai
from datetime import datetime

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ai_client = genai.Client(api_key=GEMINI_API_KEY)
DB_FILE = "news_jobs.db"
BKK_TZ = pytz.timezone('Asia/Bangkok')

RSS_FEEDS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml"
]

class ConfirmationView(discord.ui.View):
    def __init__(self, cog, topics: str, time_str: str, hour: int, minute: int, days: int):
        super().__init__(timeout=120)  # Buttons expire in 2 minutes
        self.cog = cog
        self.topics = topics
        self.time_str = time_str
        self.hour = hour
        self.minute = minute
        self.days = days

    @discord.ui.button(label="ยืนยันตั้งค่า", style=discord.ButtonStyle.success, emoji="🩷")
    async def confirm_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. Save to SQLite Database
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "INSERT INTO active_jobs (user_id, channel_id, topics, time_str, days_left) VALUES (?, ?, ?, ?, ?)",
                (interaction.user.id, interaction.channel_id, self.topics, self.time_str, self.days)
            )
            job_id = cursor.lastrowid
            await db.commit()

        # 2. Add job to APScheduler
        self.cog.schedule_job(job_id, interaction.channel_id, self.topics, self.hour, self.minute)

        # 3. Transform the message in-place and remove the buttons (view=None)
        await interaction.response.edit_message(
            content=(
                f"💖 **บันทึกและเปิดใช้งานเรียบร้อยแล้ว!**\n"
                f"📌 **หัวข้อ:** {self.topics}\n"
                f"⏰ **เวลา:** {self.time_str} น. (เวลาไทย)\n"
                f"📅 **ระยะเวลา:** {self.days} วัน\n"
                f"ระบบจะเริ่มสรุปข่าวส่งเข้ามาตามเวลาที่กำหนดไว้ครับ"
            ),
            view=None  # <--- Removes buttons so they can't be clicked again!
        )

    @discord.ui.button(label="แก้ไขข้อมูล", style=discord.ButtonStyle.secondary, emoji="💙")
    async def edit_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. Open the modal with pre-filled inputs
        modal = NewsSetupModal(
            cog=self.cog,
            default_topics=self.topics,
            default_time=self.time_str,
            default_days=str(self.days)
        )
        await interaction.response.send_modal(modal)

        # 2. Try to delete the old preview message to keep chat clean
        try:
            await interaction.message.delete()
        except Exception:
            pass  # Ignore if Discord restricts deleting ephemeral messages


class NewsSetupModal(discord.ui.Modal, title="ตั้งค่าสรุปข่าวประจำวัน (Custom News Setup)"):
    def __init__(self, cog, default_topics="", default_time="", default_days=""):
        super().__init__()
        self.cog = cog

        self.topics_input = discord.ui.TextInput(
            label="หัวข้อข่าวที่สนใจ (Topics)",
            placeholder="เช่น: AI, Crypto, สตาร์ทอัพ, ตลาดหุ้น",
            default=default_topics,
            style=discord.TextStyle.short,
            required=True
        )
        self.add_item(self.topics_input)

        self.time_input = discord.ui.TextInput(
            label="เวลาที่ต้องการให้รายงาน (HH:MM ในเวลา BKK)",
            placeholder="เช่น: 09:00 หรือ 14:30",
            default=default_time,
            style=discord.TextStyle.short,
            required=True,
            max_length=5
        )
        self.add_item(self.time_input)

        self.days_input = discord.ui.TextInput(
            label="จำนวนวันที่ต้องการให้ทำซ้ำ (Days)",
            placeholder="เช่น: 5",
            default=default_days,
            style=discord.TextStyle.short,
            required=True,
            max_length=3
        )
        self.add_item(self.days_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Validate time format
        try:
            time_parts = self.time_input.value.strip().split(":")
            hour = int(time_parts[0])
            minute = int(time_parts[1])
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError
        except Exception:
            await interaction.response.send_message("❌ กรุณาระบุเวลาในรูปแบบ HH:MM ให้ถูกต้อง (เช่น 09:00 หรือ 14:30)", ephemeral=True)
            return

        # Validate days
        try:
            days = int(self.days_input.value.strip())
            if days <= 0:
                raise ValueError
        except Exception:
            await interaction.response.send_message("❌ กรุณาระบุจำนวนวันเป็นตัวเลขที่มากกว่า 0", ephemeral=True)
            return

        topics = self.topics_input.value.strip()
        time_str = f"{hour:02d}:{minute:02d}"

        # Create interactive confirmation view
        view = ConfirmationView(
            cog=self.cog,
            topics=topics,
            time_str=time_str,
            hour=hour,
            minute=minute,
            days=days
        )

        preview_text = (
            f"📋 **ตรวจสอบความถูกต้องของการตั้งค่า:**\n\n"
            f"📌 **หัวข้อ:** {topics}\n"
            f"⏰ **เวลา:** {time_str} น. (เวลาไทย)\n"
            f"📅 **ระยะเวลา:** {days} วัน\n\n"
            f"กด 🩷 **ยืนยันตั้งค่า** เพื่อเริ่มใช้งาน หรือกด 💙 **แก้ไขข้อมูล** เพื่อเปลี่ยนคำสั่ง"
        )

        await interaction.response.send_message(content=preview_text, view=view, ephemeral=True)


class DynamicNews(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(timezone=BKK_TZ)

    async def cog_load(self):
        """Runs when the cog is loaded into the bot."""
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS active_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    channel_id INTEGER,
                    topics TEXT,
                    time_str TEXT,
                    days_left INTEGER
                )
            """)
            await db.commit()

        self.scheduler.start()
        await self.restore_jobs_from_db()

    async def restore_jobs_from_db(self):
        """Reads active jobs from SQLite and loads them into APScheduler on boot."""
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT id, channel_id, topics, time_str FROM active_jobs WHERE days_left > 0") as cursor:
                async for row in cursor:
                    job_id, channel_id, topics, time_str = row
                    hour, minute = map(int, time_str.split(":"))
                    self.schedule_job(job_id, channel_id, topics, hour, minute)
        print("Restored active scheduled jobs from SQLite database.")

    def schedule_job(self, job_id, channel_id, topics, hour, minute):
        """Adds a cron trigger job to APScheduler."""
        trigger = CronTrigger(hour=hour, minute=minute, timezone=BKK_TZ)
        self.scheduler.add_job(
            self.run_custom_news_job,
            trigger=trigger,
            id=str(job_id),
            args=[job_id, channel_id, topics],
            replace_existing=True
        )

    async def run_custom_news_job(self, job_id, channel_id, topics):
        """Fires when the scheduled time hits: generates news, posts embed, updates DB."""
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if not channel:
            return

        raw_news = self.fetch_raw_news(topics)
        summary = self.summarize_custom_topics(raw_news, topics)

        current_time = datetime.now(BKK_TZ).strftime('%Y-%m-%d %H:%M:%S')
        embed = discord.Embed(
            title=f"📌 สรุปข่าวประจำวันตามหัวข้อ: {topics}",
            description=summary,
            color=discord.Color.green()
        )
        embed.set_footer(text=f"Generated at {current_time} BKK")
        await channel.send(embed=embed)

        # Update remaining days in SQLite Database
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT days_left FROM active_jobs WHERE id = ?", (job_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    days_left = row[0] - 1
                    if days_left <= 0:
                        await db.execute("DELETE FROM active_jobs WHERE id = ?", (job_id,))
                        self.scheduler.remove_job(str(job_id))
                    else:
                        await db.execute("UPDATE active_jobs SET days_left = ? WHERE id = ?", (days_left, job_id))
                    await db.commit()

    def fetch_raw_news(self, topics):
        encoded_query = urllib.parse.quote(topics)
        google_news_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
        
        feed = feedparser.parse(google_news_url)
        articles = []
        
        for entry in feed.entries[:7]:
            articles.append(f"Title: {entry.title}\nSummary: {entry.get('summary', 'No summary')}\nLink: {entry.link}")
            
        if not articles:
            for url in RSS_FEEDS:
                feed = feedparser.parse(url)
                for entry in feed.entries[:2]:
                    articles.append(f"Title: {entry.title}\nSummary: {entry.get('summary', 'No summary')}\nLink: {entry.link}")

        return "\n\n".join(articles)

    def summarize_custom_topics(self, raw_news, topics):
        prompt = (
            f"คุณคือผู้ประกาศข่าวสไตล์ทางการ สรุปข่าวภาษาอังกฤษด้านล่างนี้เป็นภาษาไทย โดยคัดเลือกเฉพาะประเด็นที่เกี่ยวข้องกับหัวข้อเหล่านี้เท่านั้น: [{topics}]\n"
            "เงื่อนไข:\n"
            "1. สรุปเป็นประโยคสั้นๆ กระชับที่สุด (1 ประโยคต่อข่าว) ในรูปแบบเครื่องหมายหัวข้อย่อย (Bulletpoint)\n"
            "2. แนบลิงก์แหล่งที่มา (Source Link) ต่อท้ายสรุปข่าวแต่ละข้อเสมอ\n"
            "3. หากไม่มีข่าวใดเกี่ยวข้องกับหัวข้อที่ระบุ ให้สรุปข่าวเด่นทั่วไปสั้นๆ แทน\n"
            "4. ห้ามเขียนข้อความเกริ่นนำหรือปิดท้ายใดๆ ทั้งสิ้น ให้เริ่มพิมพ์สรุปข่าวทันที\n\n"
            f"ข้อมูลข่าวสารดิบ:\n{raw_news}"
        )
        response = ai_client.models.generate_content(
            model='gemini-3.5-flash',
            contents=prompt,
        )
        return response.text

    @app_commands.command(name="setupnews", description="ตั้งค่าสรุปข่าวประจำวันแบบกำหนดหัวข้อ เวลา และระยะเวลาด้วยตัวเอง")
    async def setup_news(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NewsSetupModal(self))

async def setup(bot):
    await bot.add_cog(DynamicNews(bot))
