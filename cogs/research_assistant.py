import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import os
import asyncio
import json
import re

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime, timedelta
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


# --- The Gatekeeper & Disambiguation Logic ---
def analyze_topic_with_gemini(topic: str) -> dict:
    """Checks for typos AND brainstorms multiple meanings (including slang/memes)."""
    prompt = (
        f"You are an expert AI researcher analyzing this user query: '{topic}'.\n\n"
        "STEP 1: Check for spelling. If there is a clear typo, set status to 'TYPO' and provide the correct word. "
        "If it is random gibberish (e.g., 'asdfg'), set status to 'GIBBERISH'.\n"
        "STEP 2: If the spelling is valid, brainstorm all possible distinct angles, meanings, or contexts for this topic. "
        "Include literal meanings, companies, pop culture, slang, or memes if applicable. "
        "If it's highly specific, just return 1 meaning. If it's broad, return up to 7 distinct meanings.\n\n"
        "OUTPUT FORMAT: You MUST return ONLY a valid JSON object in Thai language. Do not write markdown outside the JSON.\n"
        "{\n"
        "  \"status\": \"VALID\" | \"TYPO\" | \"GIBBERISH\",\n"
        "  \"suggestion\": \"Corrected text here (ONLY if TYPO, else leave empty)\",\n"
        "  \"meanings\": [\n"
        "    {\n"
        "      \"emoji\": \"❤️\",\n"
        "      \"short_title\": \"บริษัทเทคโนโลยี\",\n"
        "      \"description\": \"เจาะลึกเรื่อง Apple Inc., iPhone, Mac\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "For the emojis in the list, STRICTLY use these colored hearts in this order if multiple: ❤️, 🧡, 💛, 💚, 💙, 💜, 🖤."
    )
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.5-flash',
            contents=prompt,
        )
        raw_text = response.text.strip()
        
        # Safely extract JSON if Gemini wraps it in ```json ... ```
        match = re.search(r'```(?:json)?\n(.*?)\n```', raw_text, re.DOTALL)
        if match:
            raw_text = match.group(1)
            
        data = json.loads(raw_text)
        return data
        
    except Exception as e:
        print(f"Gatekeeper API Error: {e}")
        # Fallback to allow it through if the AI completely fails
        return {
            "status": "VALID",
            "suggestion": "",
            "meanings": [{"emoji": "❤️", "short_title": "ความหมายหลัก", "description": f"ค้นคว้าข้อมูลเกี่ยวกับ {topic}"}]
        }


# --- Dynamic UI Components ---

class ResearchRetryView(discord.ui.View):
    """Stage 1.5: Shown ONLY when rejected (Typo or Gibberish)"""
    def __init__(self, cog, original_topic: str, time_str: str, suggestion: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.original_topic = original_topic
        self.time_str = time_str
        self.suggestion = suggestion

    @discord.ui.button(label="แก้ไขข้อมูล", style=discord.ButtonStyle.secondary, emoji="⚪")
    async def edit_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        fill_topic = self.suggestion if self.suggestion else ""
        modal = ResearchModal(self.cog, default_topic=fill_topic, default_time=self.time_str)
        await interaction.response.send_modal(modal)
        await interaction.message.delete()


class MeaningSelectionButton(discord.ui.Button):
    """A dynamic button for each meaning Gemini generates"""
    def __init__(self, cog, original_topic, time_str, hour, minute, meaning_data, all_meanings, row):
        super().__init__(emoji=meaning_data['emoji'], label=meaning_data['short_title'], style=discord.ButtonStyle.primary, row=row)
        self.cog = cog
        self.original_topic = original_topic
        self.time_str = time_str
        self.hour = hour
        self.minute = minute
        self.meaning_data = meaning_data
        self.all_meanings = all_meanings

    async def callback(self, interaction: discord.Interaction):
        # Stage 3: When a heart is clicked, show the Final Review page!
        view = ResearchFinalReviewView(self.cog, self.original_topic, self.time_str, self.hour, self.minute, self.meaning_data, self.all_meanings)
        
        full_context = f"{self.original_topic} (ในแง่มุม: {self.meaning_data['short_title']} - {self.meaning_data['description']})"
        
        preview_text = (
            f"📋 **ยืนยันคำสั่งงานวิจัย (Stage 3/3):**\n\n"
            f"คุณต้องการให้ระบบทำการค้นคว้าเจาะลึกในหัวข้อ:\n"
            f"📌 **{full_context}**\n"
            f"⏰ **เวลาส่งรายงาน:** {self.time_str} น. (พรุ่งนี้)\n\n"
            f"กด ✅ **ยืนยันคำสั่ง** เพื่อล็อคคิวงาน หรือกด 🔙 **ย้อนกลับ** เพื่อเลือกมุมมองอื่น"
        )
        await interaction.response.edit_message(content=preview_text, view=view)


class ResearchDisambiguationView(discord.ui.View):
    """Stage 2: The dynamic menu showing all distinct meanings"""
    def __init__(self, cog, topic: str, time_str: str, hour: int, minute: int, meanings_list: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.topic = topic
        self.time_str = time_str
        
        # Build the dynamic heart buttons (Discord limits 5 per row, so we calculate rows)
        for i, meaning in enumerate(meanings_list):
            row = 0 if i < 4 else 1  # Put first 4 on top row, rest on bottom row
            btn = MeaningSelectionButton(cog, topic, time_str, hour, minute, meaning, meanings_list, row)
            self.add_item(btn)

        # Add the cancel button to the very end
        cancel_row = 1 if len(meanings_list) >= 4 else 0
        
        cancel_btn = discord.ui.Button(label="พิมพ์หัวข้อใหม่", style=discord.ButtonStyle.secondary, emoji="⚪", row=cancel_row)
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    async def cancel_callback(self, interaction: discord.Interaction):
        # Pops the modal back up with their old text
        modal = ResearchModal(self.cog, default_topic=self.topic, default_time=self.time_str)
        await interaction.response.send_modal(modal)
        await interaction.message.delete()


class ResearchFinalReviewView(discord.ui.View):
    """Stage 3: The final confirmation before saving to database"""
    def __init__(self, cog, original_topic, time_str, hour, minute, selected_meaning, all_meanings):
        super().__init__(timeout=120)
        self.cog = cog
        self.original_topic = original_topic
        self.time_str = time_str
        self.hour = hour
        self.minute = minute
        self.selected_meaning = selected_meaning
        self.all_meanings = all_meanings

    @discord.ui.button(label="ยืนยันคำสั่ง", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # We combine the topic and the specific angle so the AI researcher knows EXACTLY what to look for
        combined_topic = f"{self.original_topic} (เน้นข้อมูลเจาะลึกด้าน: {self.selected_meaning['short_title']} - {self.selected_meaning['description']})"

        # 1. Save to DB
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "INSERT INTO research_jobs (user_id, channel_id, topic, time_str) VALUES (?, ?, ?, ?)",
                (interaction.user.id, interaction.channel_id, combined_topic, self.time_str)
            )
            job_id = cursor.lastrowid
            await db.commit()

        # 2. Schedule
        self.cog.schedule_research_job(job_id, interaction.user.id, interaction.channel_id, combined_topic, self.hour, self.minute)

        # 3. Success Message
        await interaction.response.edit_message(
            content=(
                f"💖 **รับทราบครับ!** ระบบล็อคคิวงานวิจัยเรียบร้อยแล้ว\n"
                f"📌 **หัวข้อเจาะลึก:** {combined_topic}\n"
                f"⏰ **เวลาส่งรายงาน:** {self.time_str} น. (พรุ่งนี้)"
            ),
            view=None
        )

    @discord.ui.button(label="ย้อนกลับ", style=discord.ButtonStyle.secondary, emoji="🔙")
    async def back_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Instantly takes them back to Stage 2 (The colored hearts menu)
        view = ResearchDisambiguationView(self.cog, self.original_topic, self.time_str, self.hour, self.minute, self.all_meanings)
        
        menu_text = f"📌 **พบหลายความหมายสำหรับ \"{self.original_topic}\" คุณต้องการเจาะลึกในแง่มุมไหนครับ?**\n\n"
        for m in self.all_meanings:
            menu_text += f"{m['emoji']} **{m['short_title']}:** {m['description']}\n"
            
        await interaction.response.edit_message(content=menu_text, view=view)
        
    @discord.ui.button(label="พิมพ์หัวข้อใหม่", style=discord.ButtonStyle.danger, emoji="⚪")
    async def cancel_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = ResearchModal(self.cog, default_topic=self.original_topic, default_time=self.time_str)
        await interaction.response.send_modal(modal)
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
        try:
            hour, minute = map(int, self.time_input.value.strip().split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except Exception:
            await interaction.response.send_message("❌ เวลาไม่ถูกต้อง กรุณาใช้รูปแบบ HH:MM", ephemeral=True)
            return

        topic = self.topic_input.value.strip()
        time_str = f"{hour:02d}:{minute:02d}"

        # Drop the 3-second shield
        if interaction.message:
            await interaction.response.edit_message(content="⏳ *กำลังวิเคราะห์แง่มุมต่างๆ ของหัวข้อที่คุณเลือก... (Brainstorming angles...)*", view=None)
        else:
            await interaction.response.send_message(content="⏳ *กำลังวิเคราะห์แง่มุมต่างๆ ของหัวข้อที่คุณเลือก... (Brainstorming angles...)*")

        # Call Gemini in background
        data = await asyncio.to_thread(analyze_topic_with_gemini, topic)
        category = data.get("status", "VALID")
        suggestion = data.get("suggestion", "")
        meanings = data.get("meanings", [{"emoji": "❤️", "short_title": "ความหมายหลัก", "description": "ค้นคว้าข้อมูลทั่วไป"}])

        # Handle Rejections
        if category == "GIBBERISH":
            view = ResearchRetryView(self.cog, topic, time_str, suggestion="")
            await interaction.edit_original_response(
                content=f"❌ **ปฏิเสธคำสั่ง:** '{topic}' ดูเหมือนจะไม่ใช่หัวข้อที่สามารถค้นหาได้ หรือเป็นคำที่ไม่มีความหมาย\n\nกรุณากด ⚪ **แก้ไขข้อมูล** เพื่อระบุหัวข้อใหม่", 
                view=view
            )
        elif category == "TYPO":
            view = ResearchRetryView(self.cog, topic, time_str, suggestion=suggestion)
            await interaction.edit_original_response(
                content=f"⚠️ **ข้อสังเกต:** '{topic}' อาจมีการสะกดผิด\n💡 คุณหมายถึง **'{suggestion}'** หรือเปล่า?\n\nกรุณากด ⚪ **แก้ไขข้อมูล** เพื่อตรวจสอบและแก้ไข", 
                view=view
            )
        else:
            # VALID: Show Stage 2 Menu
            view = ResearchDisambiguationView(self.cog, topic, time_str, hour, minute, meanings)
            
            menu_text = f"📌 **พบหลายความหมายสำหรับ \"{topic}\" คุณต้องการเจาะลึกในแง่มุมไหนครับ? (Stage 2/3)**\n\n"
            for m in meanings:
                menu_text += f"{m['emoji']} **{m['short_title']}:** {m['description']}\n"
                
            await interaction.edit_original_response(content=menu_text, view=view)


class PingButton(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
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
        
        self.scheduler.add_job(
            self.send_daily_ping,
            trigger=CronTrigger(hour=16, minute=30, timezone=BKK_TZ),
            id="daily_research_ping",
            replace_existing=True
        )
        
        await self.restore_jobs_from_db()

    async def send_daily_ping(self):
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

            # Build the context
            context_data = ""
            source_links = []
            for result in search_result.get('results', []):
                source_links.append(f"• {result['url']}")
                context_data += f"\n--- SOURCE: {result['url']} ---\n{result.get('raw_content', result.get('content', 'No content'))}\n"

            # 2. GEMINI: Summarize
            prompt = (
                f"You are an expert, meticulous research assistant. The user wants a detailed summary about: '{topic}'.\n\n"
                f"Here is the raw text scraped from the top search results:\n{context_data}\n\n"
                "INSTRUCTIONS:\n"
                "1. Write a well-structured summary answering the user's prompt in THAI.\n"
                "2. CRITICAL: You must base your answer ONLY on the provided sources. Do not include outside knowledge.\n"
                "3. If the sources conflict, mention the differing viewpoints.\n"
                "4. If the sources do not contain enough information to fully answer the prompt, explicitly state that.\n"
                "5. IMPORTANT: Keep the summary concise (maximum 4-5 paragraphs) to fit within Discord limits."
            )
            
            response = ai_client.models.generate_content(
                model='gemini-3.5-flash',
                contents=prompt,
            )
            
            # 3. Discord Safeguard
            summary_text = response.text
            if len(summary_text) > 4000:
                summary_text = summary_text[:4000] + "\n\n*(รายงานยาวเกินไปจึงถูกตัดทอนลงเพื่อให้รองรับกับข้อจำกัดของ Discord)*"
            
            # 4. Delivery
            embed = discord.Embed(
                title=f"📑 Research Brief",
                description=summary_text,
                color=discord.Color.purple()
            )
            embed.add_field(name="📚 แหล่งที่มา (Sources)", value="\n".join(source_links) if source_links else "ไม่มีแหล่งที่มา", inline=False)
            
            await loading_msg.edit(content=f"รายงานของคุณ <@{user_id}> พร้อมแล้วครับ!\n**หัวข้อ:** {topic}", embed=embed)

        except Exception as e:
            await loading_msg.edit(content=f"❌ ขออภัย <@{user_id}> เกิดข้อผิดพลาดในการค้นคว้า: `{e}`")

        # 5. DB Cleanup
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM research_jobs WHERE id = ?", (job_id,))
            await db.commit()
        
        self.scheduler.remove_job(f"research_{job_id}")

    @app_commands.command(name="setupresearch", description="สั่งงานให้บอทค้นคว้าและส่งรายงานล่วงหน้า")
    async def setup_research(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ResearchModal(self))

    @app_commands.command(name="quickresearch", description="[TEST] สั่งงานวิจัยแบบด่วน (รอรับผลใน 5 นาที)")
    @app_commands.describe(topic="หัวข้อที่ต้องการค้นคว้าทดสอบ")
    async def quick_research(self, interaction: discord.Interaction, topic: str):
        # We skip the complex gatekeeper for quick tests, this is just for you to test the end-delivery!
        run_time = datetime.now(BKK_TZ) + timedelta(minutes=5)
        hour = run_time.hour
        minute = run_time.minute
        time_str = f"{hour:02d}:{minute:02d}"

        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "INSERT INTO research_jobs (user_id, channel_id, topic, time_str) VALUES (?, ?, ?, ?)",
                (interaction.user.id, interaction.channel_id, topic, time_str)
            )
            job_id = cursor.lastrowid
            await db.commit()

        self.schedule_research_job(job_id, interaction.user.id, interaction.channel_id, topic, hour, minute)

        await interaction.response.send_message(
            f"🚀 **โหมดทดสอบทำงาน! (Quick Test Triggered)**\n"
            f"📌 **หัวข้อ:** {topic}\n"
            f"⏰ **ระบบจะเริ่มรันสคริปต์เวลา:** {time_str} น. (อีก 5 นาที)\n"
            f"*(This bypasses the UI menu for fast testing!)*"
        )

async def setup(bot):
    await bot.add_cog(ResearchAssistant(bot))
