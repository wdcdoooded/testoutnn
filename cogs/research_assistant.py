import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime
from google import genai
from tavily import TavilyClient

# --- API Keys Setup ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

DB_FILE = "news_jobs.db"  # We can share the same DB file, just use a different table
BKK_TZ = pytz.timezone('Asia/Bangkok')
TARGET_CHANNEL_ID = 1527591728176037888  # The channel for the 16:30 ping


# --- UI Components ---
class ResearchModal(discord.ui.Modal, title="สั่งงานผู้ช่วยนักวิจัย (Research Task)"):
    topic_input = discord.ui.TextInput(
        label="หัวข้อที่ต้องการให้ค้นคว้าเจาะลึก",
        placeholder="เช่น: เทคโนโลยีแบตเตอรี่ Solid-state ล่าสุด",
        style=discord.TextStyle.paragraph,
        required=True
    )
    time_input = discord.ui.TextInput(
        label="เวลาที่ต้องการให้ส่งรายงาน (HH:MM พรุ่งนี้)",
        placeholder="เช่น: 09:30",
        style=discord.TextStyle.short,
        required=True,
        max_length=5
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hour, minute = map(int, self.time_input.value.strip().split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except Exception:
            await interaction.response.send_message("❌ เวลาไม่ถูกต้อง กรุณาใช้รูปแบบ HH:MM", ephemeral=True)
            return

        topic = self.topic_input.value.strip()
        time_str = f"{hour:02d}:{minute:02d}"

        # Save ticket to Database
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "INSERT INTO research_jobs (user_id, channel_id, topic, time_str) VALUES (?, ?, ?, ?)",
                (interaction.user.id, interaction.channel_id, topic, time_str)
            )
            job_id = cursor.lastrowid
            await db.commit()

        # Schedule the delivery
        self.cog.schedule_research_job(job_id, interaction.user.id, interaction.channel_id, topic, hour, minute)

        await interaction.response.send_message(
            f"✅ **รับทราบครับ!** ระบบจะทำการค้นคว้าเรื่อง **{topic}**\nและส่งรายงานให้คุณพรุ่งนี้เวลา **{time_str} น.**", 
            ephemeral=True
        )

class PingButton(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None) # Button never expires
        self.cog = cog

    @discord.ui.button(label="📝 สั่งงานวิจัย (Submit Topic)", style=discord.ButtonStyle.primary, custom_id="research_btn")
    async def request_research(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ResearchModal(self.cog))


# --- The Main Cog ---
class ResearchAssistant(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(timezone=BKK_TZ)

    async def cog_load(self):
        """Runs when the cog loads. Sets up DB and schedules the 16:30 ping."""
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS research_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    channel_id INTEGER,
                    topic TEXT,
                    time_str TEXT
                )
            """)
            await db.commit()

        self.scheduler.start()
        
        # 1. Schedule the daily 16:30 question ping
        self.scheduler.add_job(
            self.send_daily_ping,
            trigger=CronTrigger(hour=18, minute=00, timezone=BKK_TZ),
            id="daily_research_ping",
            replace_existing=True
        )
        
        # 2. Restore any pending research jobs from database
        await self.restore_jobs_from_db()

    async def send_daily_ping(self):
        """Fires at 16:30 to ask users what they want researched."""
        channel = self.bot.get_channel(TARGET_CHANNEL_ID)
        if channel:
            await channel.send(
                "🔔 **ได้เวลาสั่งงานแล้วครับ!** พรุ่งนี้อยากให้ผมค้นคว้าและสรุปข้อมูลเรื่องอะไรเป็นพิเศษไหมครับ?\n(กดปุ่มด้านล่างเพื่อพิมพ์หัวข้อและเวลาที่ต้องการรับรายงานได้เลย)",
                view=PingButton(self)
            )

    async def restore_jobs_from_db(self):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT id, user_id, channel_id, topic, time_str FROM research_jobs") as cursor:
                async for row in cursor:
                    job_id, user_id, channel_id, topic, time_str = row
                    hour, minute = map(int, time_str.split(":"))
                    self.schedule_research_job(job_id, user_id, channel_id, topic, hour, minute)
        print("Restored active Research jobs.")

    def schedule_research_job(self, job_id, user_id, channel_id, topic, hour, minute):
        trigger = CronTrigger(hour=hour, minute=minute, timezone=BKK_TZ)
        self.scheduler.add_job(
            self.execute_research,
            trigger=trigger,
            id=f"research_{job_id}",
            args=[job_id, user_id, channel_id, topic],
            replace_existing=True
        )

    async def execute_research(self, job_id, user_id, channel_id, topic):
        """Fires at target time: Tavily searches -> Gemini reads -> Posts -> Deletes DB ticket."""
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if not channel: return

        # Send a typing indicator or loading message so the user knows it's thinking
        loading_msg = await channel.send(f"🔍 กำลังสืบค้นและวิเคราะห์ข้อมูลเรื่อง **{topic}** ให้กับ <@{user_id}> ...")

        try:
            # 1. TAVILY: Search and Scrape
            # max_results=3 pulls the top 3 sites. include_raw_content=True strips the HTML and gets pure text.
            search_result = tavily_client.search(
                query=topic, 
                search_depth="advanced", 
                max_results=3, 
                include_raw_content=True
            )

            # Build the context string from the scraped websites
            context_data = ""
            source_links = []
            for result in search_result.get('results', []):
                source_links.append(f"• {result['url']}")
                # We feed Gemini the raw, clean text extracted directly from the webpage
                context_data += f"\n--- SOURCE: {result['url']} ---\n{result.get('raw_content', result.get('content', 'No content'))}\n"

            # 2. GEMINI: The Anti-Hallucination Prompt
            prompt = (
                f"You are an expert, meticulous research assistant. The user wants a detailed summary about: '{topic}'.\n\n"
                f"Here is the raw text scraped from the top search results:\n{context_data}\n\n"
                "INSTRUCTIONS:\n"
                "1. Write a comprehensive, well-structured summary answering the user's prompt in THAI.\n"
                "2. CRITICAL: You must base your answer ONLY on the provided sources. Do not include outside knowledge.\n"
                "3. If the sources conflict, mention the differing viewpoints.\n"
                "4. If the sources do not contain enough information to fully answer the prompt, explicitly state that."
            )
            
            response = ai_client.models.generate_content(
                model='gemini-1.5-flash',
                contents=prompt,
            )
            
            # 3. Format the Discord Delivery
            embed = discord.Embed(
                title=f"📑 Research Brief: {topic}",
                description=response.text,
                color=discord.Color.purple()
            )
            embed.add_field(name="📚 แหล่งที่มา (Sources Visited)", value="\n".join(source_links), inline=False)
            
            await loading_msg.edit(content=f"รายงานของคุณ <@{user_id}> พร้อมแล้วครับ!", embed=embed)

        except Exception as e:
            await loading_msg.edit(content=f"❌ ขออภัย <@{user_id}> เกิดข้อผิดพลาดในการค้นคว้า: `{e}`")

        # 4. Clean up: Delete the completed job from the database
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM research_jobs WHERE id = ?", (job_id,))
            await db.commit()
        # Remove from scheduler so it doesn't run again tomorrow unless requested
        self.scheduler.remove_job(f"research_{job_id}")

    # Manual Slash command just in case you want to trigger the modal without waiting for 16:30
    @app_commands.command(name="setupresearch", description="สั่งงานให้บอทค้นคว้าและส่งรายงานล่วงหน้า")
    async def setup_research(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ResearchModal(self))

async def setup(bot):
    await bot.add_cog(ResearchAssistant(bot))
