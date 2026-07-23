import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import os
import asyncio

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

DB_FILE = "news_jobs.db"
BKK_TZ = pytz.timezone('Asia/Bangkok')
TARGET_CHANNEL_ID = 1527591728176037888  # The channel for the 16:30 ping


# --- The Gatekeeper Logic ---
# --- The Gatekeeper Logic ---
def validate_topic_with_gemini(topic: str) -> tuple[str, str]:
    """Asks Gemini to verify if the topic is real, a typo, or gibberish."""
    prompt = (
        f"You are an incredibly strict spelling and grammar checker. Evaluate this research topic: '{topic}'.\n"
        "You must detect ANY misspelled words (in both Thai and English).\n\n"
        "RULES:\n"
        "1. If the text contains EVEN ONE spelling error or typo, you MUST output TYPO.\n"
        "2. If it is random keyboard smashes (like 'asdfgh' or 'ฟหกด') or total nonsense, output GIBBERISH.\n"
        "3. ONLY if it is perfectly spelled and makes logical sense, output VALID.\n\n"
        "FORMAT: Respond ONLY with exactly CATEGORY|SUGGESTION\n"
        "Example 1: แอปเปิ้ลวอช -> TYPO|Apple Watch (or แอปเปิลวอตช์)\n"
        "Example 2: Solad-stete batteies -> TYPO|Solid-state batteries\n"
        "Example 3: asdasdasd -> GIBBERISH|NONE\n"
        "Example 4: เทคโนโลยี AI -> VALID|NONE"
    )
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.5-flash',
            contents=prompt,
        )
        # Strip away any markdown formatting (like **TYPO**) the AI might try to add
        raw_text = response.text.strip().replace('\n', '').replace('`', '').replace('*', '')
        result = raw_text.split('|')
        
        if len(result) >= 2:
            cat = result[0].strip().upper()
            sug = result[1].strip()
            
            if "TYPO" in cat: return "TYPO", sug
            if "GIBBERISH" in cat: return "GIBBERISH", sug
            return "VALID", "NONE"
            
        return "VALID", "NONE"
    except Exception as e:
        print(f"Validation Guardrail Error: {e}")
        return "VALID", "NONE"  # Fallback to allow it through if the API temporarily fails


# --- UI Components ---
class ResearchRetryView(discord.ui.View):
    """View shown ONLY when the Gatekeeper rejects the prompt (Typo or Gibberish)"""
    def __init__(self, cog, original_topic: str, time_str: str, suggestion: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.original_topic = original_topic
        self.time_str = time_str
        self.suggestion = suggestion

    @discord.ui.button(label="แก้ไขข้อมูล", style=discord.ButtonStyle.secondary, emoji="💙")
    async def edit_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # If it was a typo, pre-fill the modal with the corrected word! If gibberish, leave it blank.
        fill_topic = self.suggestion if self.suggestion and self.suggestion != "NONE" else ""
        modal = ResearchModal(self.cog, default_topic=fill_topic, default_time=self.time_str)
        
        await interaction.response.send_modal(modal)
        await interaction.message.delete()


class ResearchConfirmationView(discord.ui.View):
    """View shown when the Gatekeeper approves the prompt (Valid)"""
    def __init__(self, cog, topic: str, time_str: str, hour: int, minute: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.topic = topic
        self.time_str = time_str
        self.hour = hour
        self.minute = minute

    @discord.ui.button(label="ยืนยันสั่งงาน", style=discord.ButtonStyle.success, emoji="🩷")
    async def confirm_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. Save ticket to Database ONLY when confirmed
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "INSERT INTO research_jobs (user_id, channel_id, topic, time_str) VALUES (?, ?, ?, ?)",
                (interaction.user.id, interaction.channel_id, self.topic, self.time_str)
            )
            job_id = cursor.lastrowid
            await db.commit()

        # 2. Schedule the delivery
        self.cog.schedule_research_job(job_id, interaction.user.id, interaction.channel_id, self.topic, self.hour, self.minute)

        # 3. Transform the message in-place to show success
        await interaction.response.edit_message(
            content=(
                f"💖 **รับทราบครับ!** ระบบล็อคคิวงานวิจัยเรียบร้อยแล้ว\n"
                f"📌 **หัวข้อ:** {self.topic}\n"
                f"⏰ **เวลาส่งรายงาน:** {self.time_str} น. (พรุ่งนี้)"
            ),
            view=None
        )

    @discord.ui.button(label="แก้ไขข้อมูล", style=discord.ButtonStyle.secondary, emoji="💙")
    async def edit_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Re-open the modal pre-filled with their current answers
        modal = ResearchModal(self.cog, default_topic=self.topic, default_time=self.time_str)
        await interaction.response.send_modal(modal)
        
        # Delete the old preview card to keep the channel completely clean
        await interaction.message.delete()


class ResearchModal(discord.ui.Modal, title="สั่งงานผู้ช่วยนักวิจัย (Research Task)"):
    def __init__(self, cog, default_topic="", default_time=""):
        super().__init__()
        self.cog = cog

        self.topic_input = discord.ui.TextInput(
            label="หัวข้อที่ต้องการให้ค้นคว้าเจาะลึก",
            placeholder="เช่น: เทคโนโลยีแบตเตอรี่ Solid-state ล่าสุด",
            default=default_topic,
            style=discord.TextStyle.paragraph,
            required=True
        )
        self.add_item(self.topic_input)

        self.time_input = discord.ui.TextInput(
            label="เวลาที่ต้องการให้ส่งรายงาน (HH:MM พรุ่งนี้)",
            placeholder="เช่น: 09:30",
            default=default_time,
            style=discord.TextStyle.short,
            required=True,
            max_length=5
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        # 1. Basic Time Validation
        try:
            hour, minute = map(int, self.time_input.value.strip().split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except Exception:
            await interaction.response.send_message("❌ เวลาไม่ถูกต้อง กรุณาใช้รูปแบบ HH:MM", ephemeral=True)
            return

        topic = self.topic_input.value.strip()
        time_str = f"{hour:02d}:{minute:02d}"

        # 2. The 3-Second Shield (Drop placeholder message immediately)
        if interaction.message:
            await interaction.response.edit_message(content="⏳ *กำลังประเมินความเป็นไปได้ของหัวข้อ... (Checking topic feasibility...)*", view=None)
        else:
            await interaction.response.send_message(content="⏳ *กำลังประเมินความเป็นไปได้ของหัวข้อ... (Checking topic feasibility...)*")

        # 3. Ask Gemini Gatekeeper (Run in background thread so Discord doesn't freeze)
        category, suggestion = await asyncio.to_thread(validate_topic_with_gemini, topic)

        # 4. Handle the 3 Possible Outcomes
        if category == "GIBBERISH":
            view = ResearchRetryView(self.cog, topic, time_str, suggestion="NONE")
            await interaction.edit_original_response(
                content=f"❌ **ปฏิเสธคำสั่ง:** '{topic}' ดูเหมือนจะไม่ใช่หัวข้อที่สามารถค้นหาได้ หรือเป็นคำที่ไม่มีความหมาย\n\nกรุณากด 💙 **แก้ไขข้อมูล** เพื่อระบุหัวข้อใหม่", 
                view=view
            )
        elif category == "TYPO":
            view = ResearchRetryView(self.cog, topic, time_str, suggestion=suggestion)
            await interaction.edit_original_response(
                content=f"⚠️ **ข้อสังเกต:** '{topic}' อาจมีการสะกดผิด\n💡 คุณหมายถึง **'{suggestion}'** หรือเปล่า?\n\nกรุณากด 💙 **แก้ไขข้อมูล** เพื่อตรวจสอบและแก้ไข (ระบบได้คัดลอกคำที่ถูกต้องเตรียมไว้ให้แล้วในหน้าต่างถัดไป)", 
                view=view
            )
        else:
            # VALID outcome
            view = ResearchConfirmationView(self.cog, topic, time_str, hour, minute)
            preview_text = (
                f"📋 **ตรวจสอบความถูกต้องของคำสั่งงาน:**\n\n"
                f"📌 **หัวข้อที่จะค้นคว้า:** {topic}\n"
                f"⏰ **เวลาส่งรายงาน:** {time_str} น. (พรุ่งนี้)\n\n"
                f"กด 🩷 **ยืนยันสั่งงาน** เพื่อส่งให้บอทเริ่มดำเนินการ หรือกด 💙 **แก้ไขข้อมูล**"
            )
            await interaction.edit_original_response(content=preview_text, view=view)


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
            trigger=CronTrigger(hour=18, minute=50, timezone=BKK_TZ),
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

        loading_msg = await channel.send(f"🔍 กำลังสืบค้นและวิเคราะห์ข้อมูลเรื่อง **{topic}** ให้กับ <@{user_id}> ...")

        try:
            # 1. TAVILY: Search and Scrape
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
                model='gemini-3.5-flash',
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
        
        self.scheduler.remove_job(f"research_{job_id}")

    @app_commands.command(name="setupresearch", description="สั่งงานให้บอทค้นคว้าและส่งรายงานล่วงหน้า")
    async def setup_research(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ResearchModal(self))


async def setup(bot):
    await bot.add_cog(ResearchAssistant(bot))
