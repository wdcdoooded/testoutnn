import discord
from discord.ext import tasks, commands
import feedparser
import os
from datetime import datetime, time  # 1. Added 'time' import here
import pytz
from google import genai

# Pull keys securely from Railway Variables
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TARGET_CHANNEL_ID = 1527591728176037888

RSS_FEEDS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml"
]

ai_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

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
        "1. จัดกลุ่มข่าวเป็น 3-5 หมวดหมู่หลัก (เช่น เทคโนโลยี, โลก, ธุรกิจ ฯลฯ) เพื่อความกระชับ\n"
        "2. สรุปเนื้อหาให้สั้นและกระชับที่สุด (ไม่เกิน 1-2 ประโยคต่อข่าว) ในรูปแบบเครื่องหมายหัวข้อย่อย (Bulletpoint)\n"
        "3. ต้องแนบลิงก์แหล่งที่มา (Source Link) ต่อท้ายแต่ละข่าวเสมอ\n"
        "4. สำคัญมาก: ห้ามเขียนข้อความเกริ่นนำทักทาย หรือข้อความปิดท้ายใดๆ (เช่น 'นี่คือสรุปข่าว...' หรือ 'ขอให้สนุก...') ให้เริ่มพิมพ์ที่หมวดหมู่ข่าวตัวแรกทันที\n"
        "5. ใช้ภาษาไทยที่อ่านง่าย สะอาดตา และเว้นบรรทัดระหว่างหมวดหมู่ให้ชัดเจน\n\n"
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
        if len(summary) > 1900:
            summary = summary[:1900] + "\n\n*(Truncated for length)*"
            
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

# 2. Setup the loop to run exactly at 9:00 AM Bangkok Time every day
BKK_TZ = pytz.timezone('Asia/Bangkok')
SCHEDULED_TIME = time(hour=9, minute=0, tzinfo=BKK_TZ)

@tasks.loop(time=SCHEDULED_TIME)
async def morning_news_job():
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel:
        await generate_and_send_news(channel)

# 3. Preserved the force test command
@bot.command(name="testnews")
async def test_news(ctx):
    await ctx.send("กำลังรวบรวมและสรุปข่าวสาร กรุณารอสักครู่...")
    await generate_and_send_news(ctx.channel)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    morning_news_job.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEY:
        print("FATAL ERROR: Missing API keys in environment variables!")
    else:
        bot.run(DISCORD_TOKEN)
