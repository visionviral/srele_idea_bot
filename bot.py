import discord
import os
import json
import re
import asyncio
import anthropic
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MONDAY_API_KEY = os.environ.get("MONDAY_API_KEY")
MONDAY_BOARD_ID = os.environ.get("MONDAY_BOARD_ID")

BOT_USER_ID = None
SYSTEM_PROMPT = open("system_prompt.txt", "r").read() if os.path.exists("system_prompt.txt") else ""

LEARNINGS_FILE = "learnings.json"

def load_learnings():
    if os.path.exists(LEARNINGS_FILE):
        with open(LEARNINGS_FILE, "r") as f:
            return json.load(f)
    return []

def save_learning_to_file(learning):
    learnings = load_learnings()
    learnings.append({"learning": learning, "timestamp": str(datetime.now())})
    with open(LEARNINGS_FILE, "w") as f:
        json.dump(learnings, f, indent=2)

def get_learnings_text():
    learnings = load_learnings()
    if not learnings:
        return ""
    text = "\n\nYOUR LEARNED KNOWLEDGE (things users have taught you - ALWAYS apply these):\n"
    for l in learnings:
        text += f"- {l['learning']}\n"
    return text

async def save_to_monday(idea, priority="normal"):
    if not MONDAY_API_KEY or not MONDAY_BOARD_ID:
        return False
    import aiohttp
    url = "https://api.monday.com/v2"
    headers = {
        "Authorization": MONDAY_API_KEY,
        "Content-Type": "application/json"
    }
    column_values = json.dumps({"status": {"label": priority.capitalize()}})
    query = f'mutation {{ create_item (board_id: {MONDAY_BOARD_ID}, item_name: "{idea}", column_values: \'{column_values}\') {{ id }} }}'
    payload = {"query": query}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            return resp.status == 200

async def push_to_github(filename, content, commit_message):
    import aiohttp
    import base64
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return False, "GitHub token or repo not configured"
    
    url = f"https://api.github.com/repos/{repo}/contents/{filename}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    async with aiohttp.ClientSession() as session:
        sha = None
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                sha = data.get("sha")
        
        encoded = base64.b64encode(content.encode()).decode()
        payload = {
            "message": commit_message,
            "content": encoded,
            "branch": "main"
        }
        if sha:
            payload["sha"] = sha
        
        async with session.put(url, json=payload, headers=headers) as resp:
            if resp.status in [200, 201]:
                return True, "Success"
            else:
                error = await resp.text()
                return False, error

async def get_channel_history(channel, limit=50):
    messages = []
    async for msg in channel.history(limit=limit, oldest_first=False):
        author = msg.author.display_name
        content = msg.content
        if msg.attachments:
            content += " [has attachments]"
        messages.append(f"[{author}]: {content}")
    messages.reverse()
    return messages

async def resolve_mentions(guild, message_text):
    """Replace {{username}} with <@user_id>"""
    pattern = r'\{\{(.+?)\}\}'
    matches = re.findall(pattern, message_text)
    for name in matches:
        member = discord.utils.find(
            lambda m, n=name: m.display_name.lower() == n.lower() or m.name.lower() == n.lower(),
            guild.members
        )
        if member:
            message_text = message_text.replace(f"{{{{{name}}}}}", f"<@{member.id}>")
    return message_text

async def send_to_channel(guild, channel_name, message_text):
    """Send a message to a specific channel by name"""
    channel = discord.utils.find(
        lambda c: c.name.lower() == channel_name.lower() and isinstance(c, discord.TextChannel),
        guild.channels
    )
    if not channel:
        return False, f"Channel '{channel_name}' not found"
    
    try:
        resolved_message = await resolve_mentions(guild, message_text)
        await channel.send(resolved_message)
        return True, f"Message sent to #{channel_name}"
    except discord.Forbidden:
        return False, f"I don't have permission to send messages in #{channel_name}"
    except Exception as e:
        return False, str(e)

@client.event
async def on_ready():
    global BOT_USER_ID
    BOT_USER_ID = client.user.id
    print(f"Logged in as {client.user} (ID: {BOT_USER_ID})")

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    
    bot_mentioned = client.user.mentioned_in(message)
    is_reply_to_bot = (
        message.reference and 
        message.reference.resolved and 
        message.reference.resolved.author == client.user
    )
    
    if not bot_mentioned and not is_reply_to_bot:
        return
    
    async with message.channel.typing():
        history_messages = await get_channel_history(message.channel, limit=50)
        history_text = "\n".join(history_messages[-30:])
        
        full_system = SYSTEM_PROMPT + get_learnings_text()
        
        user_content = []
        
        clean_content = message.content.replace(f"<@{BOT_USER_ID}>", "").strip()
        if not clean_content:
            clean_content = "(user mentioned you without a message)"
        
        user_content.append({
            "type": "text",
            "text": f"[{message.author.display_name}]: {clean_content}\n\n--- Recent channel messages (for context, not directed at you) ---\n{history_text}"
        })
        
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']):
                user_content.append({
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": attachment.url
                    }
                })
        
        try:
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=full_system,
                messages=[{"role": "user", "content": user_content}]
            )
            
            reply_text = response.content[0].text
            
            # --- SAVE_IDEA ---
            if "SAVE_IDEA:" in reply_text:
                try:
                    idea_match = re.search(r'SAVE_IDEA:(\{.*\})', reply_text, re.DOTALL)
                    if idea_match:
                        idea_data = json.loads(idea_match.group(1))
                        await save_to_monday(idea_data["idea"], idea_data.get("priority", "normal"))
                        reply_text = reply_text[:idea_match.start()].strip()
                except Exception as e:
                    print(f"Error saving idea: {e}")
            
            # --- SAVE_LEARNING ---
            if "SAVE_LEARNING:" in reply_text:
                try:
                    learn_match = re.search(r'SAVE_LEARNING:(\{.*\})', reply_text, re.DOTALL)
                    if learn_match:
                        learn_data = json.loads(learn_match.group(1))
                        save_learning_to_file(learn_data["learning"])
                        reply_text = reply_text[:learn_match.start()].strip()
                except Exception as e:
                    print(f"Error saving learning: {e}")
            
            # --- PUSH_CODE ---
            if "PUSH_CODE:" in reply_text:
                try:
                    push_match = re.search(r'PUSH_CODE:(\{.*\})', reply_text, re.DOTALL)
                    if push_match:
                        push_data = json.loads(push_match.group(1))
                        success, result = await push_to_github(
                            push_data["file"],
                            push_data["content"],
                            push_data["commit_message"]
                        )
                        reply_text = reply_text[:push_match.start()].strip()
                        if success:
                            reply_text += "\n\n\u2705 Code pushed to GitHub! Restarting soon..."
                        else:
                            reply_text += f"\n\n\u274c Failed to push: {result}"
                except Exception as e:
                    print(f"Error pushing code: {e}")
            
            # --- SEND_MESSAGE ---
            if "SEND_MESSAGE:" in reply_text:
                try:
                    send_match = re.search(r'SEND_MESSAGE:(\{.*\})', reply_text, re.DOTALL)
                    if send_match:
                        send_data = json.loads(send_match.group(1))
                        channel_name = send_data["channel"]
                        msg_text = send_data["message"]
                        success, result = await send_to_channel(message.guild, channel_name, msg_text)
                        reply_text = reply_text[:send_match.start()].strip()
                        if success:
                            reply_text += f"\n\n\u2705 {result}"
                        else:
                            reply_text += f"\n\n\u274c {result}"
                except Exception as e:
                    print(f"Error sending message: {e}")
                    reply_text += f"\n\n\u274c Error sending message: {e}"
            
            if len(reply_text) <= 2000:
                await message.reply(reply_text)
            else:
                chunks = [reply_text[i:i+2000] for i in range(0, len(reply_text), 2000)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk)
                    else:
                        await message.channel.send(chunk)
        
        except Exception as e:
            print(f"Error calling Claude: {e}")
            await message.reply(f"Sorry, I hit an error: {str(e)[:200]}")

client.run(os.environ.get("DISCORD_TOKEN"))