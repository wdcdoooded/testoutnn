import discord
from discord.ext import tasks, commands
import feedparser
import os
from datetime import datetime, time
import pytz
from google import genai

# Pull keys securely from Railway Variables
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TARGET_CHANNEL_ID = 1527591728176037888
GUILD_ID = 1524334060372099263  # <--- FIXED: Added back your Server ID

RSS_FEEDS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml"
]

ai_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True

class NewsBot(commands.Bot):
    async def setup_hook(self):
        """Loads external Cogs and syncs Slash Commands automatically on boot."""
        # Load Dynamic News Cog
        if os.path.exists("./cogs/dynamic_news.py"):
            await self.load_extension("cogs.dynamic_news")
            print("Successfully loaded Cog: dynamic_news")

        # Load the New Deep Research Cog
        if os.path.exists("./cogs/research_assistant.py"):
            await self.load_extension("cogs.research_assistant")
            print("Successfully loaded Cog: research_assistant")
            
        # Sync directly to Guild for instant slash command availability
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"Synced {len(synced)} Slash Command(s) directly to Guild {GUILD_ID}!")

bot = NewsBot(command_prefix="!", intents=intents)

def fetch_latest_news():
    articles = []
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]:
            articles.append(f"Title: {entry.title}\nSummary: {entry.get('summary', 'No summary')}\nLink: {entry.link}")
    return "\n\n".join(articles)

def summarize_with_gemini(raw_news):
    prompt = (
        "คุณคือผู้ประกาศข่าวสไตล์ทางการและเป็นมืออาชีพ สรุปข่าวภาษาอังกฤษด้านล่างนี้เป็นภาษาไทย โดยทำตามเงื่อนไขอย่างเคร่งครัด:\n"
        "1. จัดกลุ่มข่าวลงใน 5 หมวดหมู่นี้เท่านั้น: [1. ข่าวธุรกิจ, 2. ข่าวต่างประเทศ, 3. ข่าวเศรษฐกิจ, 4. ข่าวบันเทิง, 5. ข่าววิทยาศาสตร์] \n"
        "2. หากไม่มีข่าวที่ตรงกับหมวดหมู่ใด ให้ข้ามหมวดหมู่นั้นไปเลย ห้ามสร้างหมวดหมู่ใหม่เด็ดขาด\n"
        "3. สรุปเนื้อหาให้สั้นและกระชับที่สุด ในรูปแบบ Bulletpoint\n"
        "4. ต้องแนบลิงก์แหล่งที่มา (Source Link) ต่อท้ายสรุปข่าวแต่ละหัวข้อเสมอ\n"
        "5. สำคัญมาก: ห้ามเขียนข้อความเกริ่นนำทักทาย หรือข้อความปิดท้ายใดๆ ทั้งสิ้น ให้เริ่มพิมพ์ที่ชื่อหมวดหมู่ข่าวทันที เพื่อป้องกันข้อความยาวเกินกำหนด\n"
        "6. สำคัญมาก: เนื้อหาทั้งหมดรวม Source Link ต้องไม่เกิน 2000 ตัวอักษร โปรดตรวจสอบให้แน่ใจก่อนทุกครั้ง\n\n"
        f"ข้อมูลข่าวสารดิบ (Raw News):\n{raw_news}"
    )
    response = ai_client.models.generate_content(
        model='gemini-3.5-flash',
        contents=prompt,
    )
    return response.text

async def generate_and_send_news(channel):
    bkk_tz = pytz.timezone('Asia/Bangkok')
    current_time = datetime.now(bkk_tz).strftime('%Y-%m-%d %H:%M:%S')
    print(f"Executing news run at {current_time} (BKK Time)...")

    raw_news = fetch_latest_news()
    if not raw_news:
        print("No news found.")
        return

    try:
        summary = summarize_with_gemini(raw_news)
        if len(summary) > 4000:
            summary = summary[:4000] + "\n\n*(Truncated for length)*"
            
        embed = discord.Embed(
            title="☕ Morning News Digest (สรุปข่าวเช้า)", 
            description=summary, 
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Generated at {current_time} BKK")
        
        await channel.send(embed=embed)
        print("News successfully posted!")
    except Exception as e:
        print(f"An error occurred: {e}")

# Static Schedule
BKK_TZ = pytz.timezone('Asia/Bangkok')
SCHEDULED_TIME = time(hour=8, minute=42, tzinfo=BKK_TZ)

@tasks.loop(time=SCHEDULED_TIME)
async def morning_news_job():
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel:
        await generate_and_send_news(channel)

@bot.command(name="testnews")
async def test_news(ctx):
    await ctx.send("กำลังรวบรวมและสรุปข่าวสาร กรุณารอสักครู่...")
    await generate_and_send_news(ctx.channel)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    if not morning_news_job.is_running():
        morning_news_job.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEY:
        print("FATAL ERROR: Missing API keys in environment variables!")
    else:
        bot.run(DISCORD_TOKEN)
