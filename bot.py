import discord
from discord.ext import tasks, commands
import feedparser
import os
from datetime import datetime
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

# Upgraded to commands.Bot so you can manually trigger it
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def fetch_latest_news():
    articles = []
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        # Grab only the top 2 articles per feed
        for entry in feed.entries[:2]:
            articles.append(f"Title: {entry.title}\nSummary: {entry.get('summary', 'No summary')}\nLink: {entry.link}")
    return "\n\n".join(articles)

def summarize_with_gemini(raw_news):
    prompt = (
"คุณคือผู้ประกาศข่าวสไตล์ทางการและเป็นมืออาชีพ หน้าที่ของคุณคือสรุปข่าวภาษาอังกฤษด้านล่างนี้เป็นภาษาไทย โดยทำตามเงื่อนไขต่อไปนี้อย่างเคร่งครัด:\n"
        "1. แบ่งประเภทข่าวออกเป็นหัวข้อหลักอย่างน้อย 5-7 หมวดหมู่ (เช่น ข่าวเทคโนโลยี, ปัญญาประดิษฐ์, ข่าวโลก, ธุรกิจ, วิทยาศาสตร์ ฯลฯ) เพื่อให้ครอบคลุมและเป็นระเบียบ\n"
        "2. ภายใต้แต่ละหมวดหมู่ ให้สรุปข่าวเป็นหัวข้อย่อยสั้นๆ กระชับและแม่นยำ โดยใช้เครื่องหมายแสดงหัวข้อย่อย (Bulletpoint)\n"
        "3. สำหรับข่าวแต่ละประเด็น ต้องแนบลิงก์แหล่งที่มา (Source Link) ที่ให้มาในรูปแบบข้อความดิบไว้ท้ายสรุปข่าวนั้นๆ เสมอ เพื่อให้ผู้ใช้อ่านต่อได้\n"
        "4. ผลลัพธ์ทั้งหมดต้องเป็นภาษาไทยที่สละสลวย อ่านง่าย สะอาดตา และไม่มีการใช้ฟอร์แมตที่ซับซ้อนเกินไป\n\n"
        f"ข้อมูลข่าวสารดิบ (Raw News):\n{raw_news}"
    )
    response = ai_client.models.generate_content(
        model='gemini-3.5-flash',
        contents=prompt,
    )
    return response.text

async def generate_and_send_news(channel):
    """Core logic to fetch, summarize, and post the news."""
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
            title="☕ Morning News Digest", 
            description=summary, 
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Generated at {current_time} BKK")
        
        await channel.send(embed=embed)
        print("News successfully posted!")
    except Exception as e:
        print(f"An error occurred: {e}")

# 1. The Automatic 5-Minute Timer
@tasks.loop(minutes=5)
async def morning_news_job():
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel:
        await generate_and_send_news(channel)

# 2. The Manual Trigger Command
@bot.command(name="testnews")
async def test_news(ctx):
    """Type !testnews in Discord to force the bot to run instantly."""
    await ctx.send("Gathering the news now, give me a few seconds...")
    await generate_and_send_news(ctx.channel)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    # Start the automatic loop as soon as it boots
    morning_news_job.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEY:
        print("FATAL ERROR: Missing API keys in environment variables!")
    else:
        bot.run(DISCORD_TOKEN)
