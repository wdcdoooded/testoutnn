import discord
from discord.ext import tasks
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

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

def fetch_latest_news():
    articles = []
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:2]:
            articles.append(f"Title: {entry.title}\nSummary: {entry.get('summary', 'No summary')}\nLink: {entry.link}")
    return "\n\n".join(articles)

def summarize_with_gemini(raw_news):
    prompt = (
        "You are a formal, professional news anchor. I will provide you with raw news articles. "
        "Categorize these stories. Write a concise, 2-3 sentence summary for each cluster. "
        "Remove duplicates. Do not use overly complex formatting.\n\n"
        f"Raw News:\n{raw_news}"
    )
    response = ai_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
    )
    return response.text

@tasks.loop(minutes=5)
async def morning_news_job():
    channel = client.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        print(f"Error: Could not find channel {TARGET_CHANNEL_ID}")
        return

    bkk_tz = pytz.timezone('Asia/Bangkok')
    current_time = datetime.now(bkk_tz).strftime('%Y-%m-%d %H:%M:%S')
    print(f"Executing news run at {current_time} (BKK Time)...")

    raw_news = fetch_latest_news()
    if not raw_news:
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

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')
    morning_news_job.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEY:
        print("FATAL ERROR: Missing API keys in environment variables!")
    else:
        client.run(DISCORD_TOKEN)
