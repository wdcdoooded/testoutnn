import discord
from discord.ext import tasks
import feedparser
import os
from datetime import datetime
import pytz
from google import genai

# 1. Configuration & Security
# These use environment variables so your keys stay hidden on GitHub
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Hardcoded Target Channel
TARGET_CHANNEL_ID = 1527591728176037888

# 2. RSS Feeds to Scrape
RSS_FEEDS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml"
]

# 3. Initialize AI and Discord Client
ai_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

def fetch_latest_news():
    """Scrapes the top 2 latest articles from each RSS feed."""
    articles = []
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        # Grab only the top 2 articles per feed to avoid overloading the prompt
        for entry in feed.entries[:2]:
            articles.append(f"Title: {entry.title}\nSummary: {entry.get('summary', 'No summary')}\nLink: {entry.link}")
    
    return "\n\n".join(articles)

def summarize_with_gemini(raw_news):
    """Sends the raw news to Gemini and asks for a structured summary."""
    prompt = (
        "You are a formal, professional news anchor. I will provide you with raw news articles. "
        "Categorize these stories (e.g., Tech, World News). Write a concise, 2-3 sentence summary for each cluster. "
        "Remove duplicates. Do not use overly complex formatting, just clean readable text.\n\n"
        f"Raw News:\n{raw_news}"
    )
    
    response = ai_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
    )
    return response.text

# 4. The Scheduler
@tasks.loop(minutes=5)
async def morning_news_job():
    channel = client.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        print(f"Error: Could not find channel {TARGET_CHANNEL_ID}")
        return

    # Log the time in GMT+7 for the server console
    bkk_tz = pytz.timezone('Asia/Bangkok')
    current_time = datetime.now(bkk_tz).strftime('%Y-%m-%d %H:%M:%S')
    print(f"Executing news run at {current_time} (BKK Time)...")

    raw_news = fetch_latest_news()
    if not raw_news:
        print("No news found.")
        return

    try:
        # Get the summary from Gemini
        summary = summarize_with_gemini(raw_news)
        
        # Discord has a 2000 character limit. Trim if necessary.
        if len(summary) > 1900:
            summary = summary[:1900] + "\n\n*(Truncated for length)*"
            
        # Format the output beautifully using a Discord Embed
        embed = discord.Embed(
            title="☕ Morning News Digest", 
            description=summary, 
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Generated at {current_time} BKK")
        
        await channel.send(embed=embed)
        print("News successfully posted!")
        
    except Exception as e:
        print(f"An error occurred during summarization or posting: {e}")

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')
    # Start the 5-minute loop as soon as the bot connects
    morning_news_job.start()

# 5. Ignite the Engine
if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEY:
        print("FATAL ERROR: Missing API keys in environment variables!")
    else:
        client.run(DISCORD_TOKEN)
