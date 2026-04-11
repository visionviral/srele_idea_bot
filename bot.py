"""
Srele — AI Assistant for the React Biome Discord Server

Srele is a full-power AI assistant powered by Claude Opus.
- Chats and answers any question (as smart as Claude gets)
- Writes code in any language
- Saves ideas/tasks to Monday.com when asked
- Persistent memory in learnings.json (survives code updates)
- Reads images, screenshots, Discord embeds, and URLs
- Reads full channel history for context
- Summarizes conversations
- Can modify its own code and push to GitHub (with confirmation)
"""

import os
import asyncio
import datetime
import json
import re
import discord
from discord.ext import commands
import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

GITHUB_REPO = "visionviral/srele_idea_bot"
GITHUB_BRANCH = "main"

MONDAY_BOARD_ID = 5089081467
MONDAY_GROUP_ID = "group_mm1m2py5"       # SRELE IDEAS group
MONDAY_API_URL = "https://api.monday.com/v2"

CLAUDE_MODEL = "claude-opus-4-6"

COL_PRIORITY = "status"
COL_DATE_ADDED = "datum"
COL_WHO = "person"

PRIORITY_TO_BE_MADE = 2
PRIORITY_WORKING_ON_IT = 0
PRIORITY_DONE = 1

# Srele's system prompt — built dynamically with learnings
SRELE_SYSTEM_BASE = """You are Srele, a powerful AI assistant for the React Biome Discord server. You run on Claude Opus — the most capable AI model available.

You are an expert in EVERYTHING — coding, business strategy, content creation, marketing, design, data analysis, writing, brainstorming, and more. You give thorough, smart, actionable answers. You write production-quality code. You think deeply before responding.

You are direct, witty, and confident. You don't hedge or give wishy-washy answers. If you know something, say it clearly.

YOUR CAPABILITIES:
1. Normal conversation — chat about anything, answer any question
2. Code generation — write code in any language, debug, explain, refactor
3. Save ideas/tasks to Monday.com — when explicitly asked
4. Learn and remember — permanently store things users teach you
5. Self-modify — you can update your own code (bot.py) and push to GitHub
6. Read images/screenshots — analyze anything visual
7. Read channel history — you always know what's been discussed
8. Summarize conversations — give recaps of channel discussions
9. Send messages to other channels — you can post in any channel on the server
10. Mention users — you can @mention anyone by name

SAVING IDEAS TO MONDAY.COM:
Only save when the user EXPLICITLY asks (e.g. "save this", "add to to-do", "track this", "put this as an idea").
When saving, output at the END of your message:
SAVE_IDEA:{"idea": "<the full idea text>", "priority": "normal"}
Set priority to "high" only if marked urgent/important/asap/critical.

LEARNING NEW THINGS:
When users teach you something ("learn this:", "remember that:", "from now on:", "when I say X do Y"):
Output at the END of your message:
SAVE_LEARNING:{"learning": "<what you learned, written as a clear instruction>"}

MODIFYING YOUR OWN CODE:
When users ask you to add features, fix bugs, or change your behavior by modifying code:
- Write the complete updated code
- Show it in a code block so they can review it
- Ask them to confirm with "push it" or "deploy it"
- When they confirm, output at the END of your message:
PUSH_CODE:{"file": "<filename>", "content": "<the full file content>", "commit_message": "<short description of change>"}
- You can modify: bot.py, requirements.txt, learnings.json, or create new files
- NEVER push code without showing it first and getting confirmation
- IMPORTANT: When modifying bot.py, include the COMPLETE file content, not just the changed parts

SENDING MESSAGES TO OTHER CHANNELS:
When a user asks you to send a message to another channel or @mention someone:
- You CAN do this. Output at the END of your message:
SEND_MESSAGE:{"channel": "<channel-name>", "message": "<the message to send>"}
- The channel name should NOT include the # symbol, just the name (e.g. "general", "ideas", "random")
- To mention a user in the message, use their display name wrapped in double curly braces: {{username}}
  Example: "Hey {{Matej}}, are you coming to the office?"
  The bot will resolve the name to the correct Discord @mention.
- You can mention multiple users in one message.
- Always confirm to the user that you'll send the message.

RULES:
- Only output ONE command per message (SAVE_IDEA, SAVE_LEARNING, PUSH_CODE, or SEND_MESSAGE — never combine them)
- Place the command at the END, after your conversational response
- For Discord, keep chat responses reasonably concise but don't sacrifice quality
- For code and technical answers, be as thorough as needed"""

# Per-channel conversation history (keeps last messages for context)
conversation_history = {}
MAX_HISTORY = 20

# How many recent channel messages to fetch for background context
CHANNEL_CONTEXT_LIMIT = 50

# Cached learnings from Monday.com (loaded on startup, updated live)
srele_learnings = []

# ============================================================
# URL FETCHING — extract info from links (fallback)
# ============================================================

URL_PATTERN = re.compile(r'https?://\S+')

def fetch_url_context(url):
    """Fetch a URL and extract title + description from meta tags."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        title = None
        description = None

        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"]
        elif soup.title and soup.title.string:
            title = soup.title.string.strip()

        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            description = og_desc["content"]
        else:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                description = meta_desc["content"]

        if not title and not description:
            return None

        result = ""
        if title:
            result += f"Title: {title}\n"
        if description:
            result += f"Description: {description}\n"
        return result.strip()

    except Exception as e:
        print(f"Could not fetch URL {url}: {e}")
        return None


def extract_url_context(text):
    """Find URLs in text, fetch their metadata, and return context string."""
    urls = URL_PATTERN.findall(text)
    if not urls:
        return ""

    context_parts = []
    for url in urls[:3]:
        info = fetch_url_context(url)
        if info:
            context_parts.append(f"[Content from {url}]\n{info}")

    if not context_parts:
        return ""

    return "\n\n".join(context_parts)


# ============================================================
# MONDAY.COM API
# ============================================================

def monday_request(query, variables=None):
    headers = {
        "Authorization": MONDAY_API_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    response = requests.post(MONDAY_API_URL, json=payload, headers=headers)
    response.raise_for_status()
    data = response.json()

    if "errors" in data:
        raise Exception(f"Monday.com API error: {data['errors']}")

    return data


def create_monday_item(item_name, priority_label_id=PRIORITY_TO_BE_MADE, group_id=None):
    today = datetime.date.today().isoformat()

    column_values = json.dumps({
        COL_PRIORITY: {"index": priority_label_id},
        COL_DATE_ADDED: {"date": today},
    })

    query = """
    mutation ($boardId: ID!, $groupId: String!, $itemName: String!, $columnValues: JSON!) {
        create_item(
            board_id: $boardId,
            group_id: $groupId,
            item_name: $itemName,
            column_values: $columnValues
        ) {
            id
            name
        }
    }
    """

    variables = {
        "boardId": str(MONDAY_BOARD_ID),
        "groupId": group_id or MONDAY_GROUP_ID,
        "itemName": item_name,
        "columnValues": column_values,
    }

    result = monday_request(query, variables)
    return result["data"]["create_item"]


def add_item_update(item_id, body_text):
    query = """
    mutation ($itemId: ID!, $body: String!) {
        create_update(
            item_id: $itemId,
            body: $body
        ) {
            id
        }
    }
    """

    variables = {
        "itemId": str(item_id),
        "body": body_text,
    }

    result = monday_request(query, variables)
    return result["data"]["create_update"]


# ============================================================
# GITHUB API — self-modification (push code changes)
# ============================================================

def github_push_file(file_path, content, commit_message):
    """Push a file to the GitHub repo. Returns True on success."""
    if not GITHUB_TOKEN:
        print("No GITHUB_TOKEN set — cannot push")
        return False, "No GitHub token configured. Add GITHUB_TOKEN to Railway env vars."

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Get the current file's SHA (needed for updates)
    resp = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH})
    sha = None
    if resp.status_code == 200:
        sha = resp.json().get("sha")

    import base64
    encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    payload = {
        "message": commit_message,
        "content": encoded_content,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(api_url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        print(f"Pushed {file_path} to GitHub: {commit_message}")
        return True, f"Pushed `{file_path}` to GitHub. Railway will auto-deploy."
    else:
        error = resp.json().get("message", resp.text[:100])
        print(f"GitHub push failed: {error}")
        return False, f"GitHub push failed: {error}"


# ============================================================
# SRELE MEMORY — persistent learnings in learnings.json
# This file is SEPARATE from bot.py. Never touch bot.py for memory.
# Learnings survive all code updates because they live in their own file.
# ============================================================

LEARNINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learnings.json")


def load_learnings():
    """Load all learnings from learnings.json."""
    global srele_learnings
    try:
        with open(LEARNINGS_FILE, "r") as f:
            data = json.load(f)
        srele_learnings = data.get("learnings", [])
        print(f"Loaded {len(srele_learnings)} learnings from learnings.json")
    except FileNotFoundError:
        print("learnings.json not found, starting with empty memory")
        srele_learnings = []
    except Exception as e:
        print(f"Could not load learnings: {e}")
        srele_learnings = []


def save_learning(learning_text, taught_by=""):
    """Save a new learning to learnings.json."""
    global srele_learnings
    try:
        # Load current file (in case another process updated it)
        try:
            with open(LEARNINGS_FILE, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"learnings": []}

        # Add the new learning
        data["learnings"].append(learning_text)

        # Write back
        with open(LEARNINGS_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Update local cache
        srele_learnings = data["learnings"]
        print(f"Saved learning: \"{learning_text}\" (taught by {taught_by})")
        return True

    except Exception as e:
        print(f"Could not save learning: {e}")
        return False


def build_system_prompt():
    """Build the full system prompt including any learnings."""
    prompt = SRELE_SYSTEM_BASE

    if srele_learnings:
        prompt += "\n\nIMPORTANT — THINGS YOU HAVE LEARNED (always follow these):\n"
        for i, learning in enumerate(srele_learnings, 1):
            prompt += f"{i}. {learning}\n"

    return prompt


# ============================================================
# CLAUDE AI
# ============================================================

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def generate_item_name(raw_idea):
    """Use Claude to generate a short item name (max 15 words) from a raw idea."""
    message = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=100,
        system="Generate a short, clear item name (MAXIMUM 15 words) for a task/idea. Just return the name, nothing else. No quotes, no explanation.",
        messages=[{"role": "user", "content": raw_idea}],
    )

    name = message.content[0].text.strip().strip('"').strip("'")
    words = name.split()
    if len(words) > 15:
        name = " ".join(words[:15])
    return name


def fetch_image_as_base64(url):
    """Download an image and return it as base64 with its media type."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "image/png")
        # Normalize content type
        if "jpeg" in content_type or "jpg" in content_type:
            media_type = "image/jpeg"
        elif "png" in content_type:
            media_type = "image/png"
        elif "gif" in content_type:
            media_type = "image/gif"
        elif "webp" in content_type:
            media_type = "image/webp"
        else:
            media_type = "image/png"

        import base64
        image_data = base64.standard_b64encode(resp.content).decode("utf-8")
        return image_data, media_type

    except Exception as e:
        print(f"Could not fetch image {url}: {e}")
        return None, None


def chat_with_claude(channel_id, user_name, user_message, embed_context="", image_urls=None, channel_context=""):
    """Send a message to Claude with conversation history and return the response."""
    if channel_id not in conversation_history:
        conversation_history[channel_id] = []

    history = conversation_history[channel_id]

    # Build the text part of the message
    text_content = f"[{user_name}]: {user_message}"

    if embed_context:
        text_content += f"\n\n--- Link preview from Discord ---\n{embed_context}"
    else:
        url_context = extract_url_context(user_message)
        if url_context:
            text_content += f"\n\n--- Fetched link content ---\n{url_context}"

    # Add recent channel history so Srele knows what's been discussed
    if channel_context:
        text_content += f"\n\n--- Recent channel messages (for context, not directed at you) ---\n{channel_context}"

    # Build message content — text only or multimodal (text + images)
    if image_urls:
        content_blocks = []

        # Add images first
        for img_url in image_urls[:4]:  # Max 4 images
            img_data, media_type = fetch_image_as_base64(img_url)
            if img_data:
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": img_data,
                    },
                })

        # Add text after images
        content_blocks.append({"type": "text", "text": text_content})

        history.append({"role": "user", "content": content_blocks})
    else:
        history.append({"role": "user", "content": text_content})

    # Trim history if too long
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        conversation_history[channel_id] = history

    message = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=build_system_prompt(),
        messages=history,
    )

    assistant_reply = message.content[0].text.strip()

    history.append({
        "role": "assistant",
        "content": assistant_reply,
    })

    return assistant_reply


def parse_save_command(response_text):
    """Check if Claude's response contains a SAVE_IDEA command."""
    match = re.search(r'SAVE_IDEA:(\{.*?\})', response_text, re.DOTALL)
    if not match:
        return response_text, None

    try:
        idea_data = json.loads(match.group(1))
        clean_text = response_text[:match.start()].rstrip()
        return clean_text, idea_data
    except json.JSONDecodeError:
        return response_text, None


def parse_learning_command(response_text):
    """Check if Claude's response contains a SAVE_LEARNING command."""
    match = re.search(r'SAVE_LEARNING:(\{.*?\})', response_text, re.DOTALL)
    if not match:
        return response_text, None

    try:
        learning_data = json.loads(match.group(1))
        clean_text = response_text[:match.start()].rstrip()
        return clean_text, learning_data
    except json.JSONDecodeError:
        return response_text, None


def parse_push_command(response_text):
    """Check if Claude's response contains a PUSH_CODE command."""
    match = re.search(r'PUSH_CODE:(\{.*\})', response_text, re.DOTALL)
    if not match:
        return response_text, None

    try:
        push_data = json.loads(match.group(1))
        clean_text = response_text[:match.start()].rstrip()
        return clean_text, push_data
    except json.JSONDecodeError:
        return response_text, None


def parse_send_message_command(response_text):
    """Check if Claude's response contains a SEND_MESSAGE command."""
    match = re.search(r'SEND_MESSAGE:(\{.*?\})', response_text, re.DOTALL)
    if not match:
        return response_text, None

    try:
        send_data = json.loads(match.group(1))
        clean_text = response_text[:match.start()].rstrip()
        return clean_text, send_data
    except json.JSONDecodeError:
        return response_text, None


async def resolve_mentions(guild, message_text):
    """Replace {{username}} with actual Discord @mentions."""
    pattern = re.compile(r'\{\{(.+?)\}\}')
    matches = pattern.findall(message_text)

    for name in matches:
        # Search members by display name or username (case-insensitive)
        member = discord.utils.find(
            lambda m: m.display_name.lower() == name.lower() or m.name.lower() == name.lower(),
            guild.members
        )
        if member:
            message_text = message_text.replace(f"{{{{{name}}}}}", member.mention)
        else:
            # Leave the name as-is if not found
            message_text = message_text.replace(f"{{{{{name}}}}}", f"@{name}")

    return message_text


# Pending code pushes waiting for user confirmation (channel_id -> push_data)
pending_pushes = {}


# ============================================================
# CHANNEL HISTORY — read recent messages for context
# ============================================================

async def fetch_channel_history(channel, limit=CHANNEL_CONTEXT_LIMIT):
    """Fetch recent messages from a Discord channel and format them as context."""
    messages = []
    async for msg in channel.history(limit=limit, oldest_first=False):
        if msg.author.bot and msg.author.display_name == "Srele":
            messages.append(f"[Srele]: {msg.content[:300]}")
        else:
            content = msg.content[:300]
            # Note attachments
            if msg.attachments:
                att_names = ", ".join(a.filename for a in msg.attachments)
                content += f" [attachments: {att_names}]"
            if content.strip():
                messages.append(f"[{msg.author.display_name}]: {content}")

    messages.reverse()  # Oldest first
    return messages


async def fetch_channel_history_for_summary(channel, limit=200):
    """Fetch more messages for a full channel summary."""
    messages = []
    async for msg in channel.history(limit=limit, oldest_first=False):
        if msg.content.strip():
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
            content = msg.content[:500]
            author = msg.author.display_name
            messages.append(f"[{timestamp}] {author}: {content}")

    messages.reverse()
    return messages


def summarize_with_claude(messages_text):
    """Use Claude to summarize a channel's conversation."""
    message = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system="You are summarizing a Discord channel conversation. Give a clear, organized summary with:\n1. Main topics discussed\n2. Key decisions or conclusions\n3. Action items or ideas mentioned\n4. Notable disagreements or open questions\n\nBe concise but thorough. Use bullet points. Don't include every message — focus on what matters.",
        messages=[{"role": "user", "content": f"Summarize this conversation:\n\n{messages_text}"}],
    )
    return message.content[0].text.strip()


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!srele ", intents=intents)


@bot.event
async def on_ready():
    print(f"\n Srele is online as {bot.user} (ID: {bot.user.id})")
    print(f" Monday.com board: {MONDAY_BOARD_ID}")
    print(f" Listening for @Srele mentions...\n")

    # Load learnings from learnings.json on startup
    load_learnings()

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="@Srele"
        )
    )


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if bot.user not in message.mentions:
        await bot.process_commands(message)
        return

    # Check for push confirmation (user says "push it", "deploy it", etc.)
    channel_key = str(message.channel.id)
    msg_lower = message.content.lower()
    push_confirms = ["push it", "deploy it", "do it", "yes push", "yes deploy", "go ahead", "ship it"]
    push_cancels = ["cancel", "don't push", "nevermind", "nah", "no"]

    if channel_key in pending_pushes:
        push_data = pending_pushes[channel_key]

        if any(confirm in msg_lower for confirm in push_confirms):
            del pending_pushes[channel_key]
            async with message.channel.typing():
                file_name = push_data.get("file", "bot.py")
                content = push_data.get("content", "")
                commit_msg = push_data.get("commit_message", "Update from Srele")

                success, result_msg = github_push_file(file_name, content, commit_msg)
                if success:
                    await message.reply(f"Pushed `{file_name}` to GitHub. Railway will auto-deploy in ~30 seconds.")
                    await message.add_reaction("\U0001f680")  # rocket
                else:
                    await message.reply(f"Push failed: {result_msg}")
            return

        elif any(cancel in msg_lower for cancel in push_cancels):
            del pending_pushes[channel_key]
            await message.reply("Got it, cancelled the push.")
            return

    # Remove the bot mention to get the actual message
    raw_text = message.content
    for mention in message.mentions:
        raw_text = raw_text.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    raw_text = raw_text.strip()

    # Extract image URLs from attachments
    image_urls = []
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            image_urls.append(attachment.url)

    if not raw_text and not image_urls:
        await message.reply(
            "Hey! I'm **Srele** — your AI assistant for React Biome.\n\n"
            "Just tag me and chat about anything. If you want me to save an idea or task "
            "to Monday.com, just ask!\n\n"
            "You can also send me images/screenshots and I'll tell you what I see.\n"
            "Teach me things with *\"learn this: ...\"* and I'll remember forever."
        )
        return

    # If only images, no text — set a default prompt
    if not raw_text and image_urls:
        raw_text = "What's in this image?"

    # Check if user wants a channel summary
    summarize_keywords = ["summarize this channel", "summarize this thread", "summarize the chat",
                          "summarize the conversation", "what did i miss", "catch me up",
                          "what's been discussed", "summary of this channel", "recap this"]
    if any(kw in raw_text.lower() for kw in summarize_keywords):
        async with message.channel.typing():
            try:
                history_msgs = await fetch_channel_history_for_summary(message.channel, limit=200)
                if len(history_msgs) < 3:
                    await message.reply("Not enough messages in this channel to summarize.")
                    return

                messages_text = "\n".join(history_msgs)
                summary = summarize_with_claude(messages_text)

                # Split if too long for Discord
                if len(summary) <= 2000:
                    await message.reply(summary)
                else:
                    chunks = [summary[i:i+2000] for i in range(0, len(summary), 2000)]
                    await message.reply(chunks[0])
                    for chunk in chunks[1:]:
                        await message.channel.send(chunk)

                print(f"Summarized #{message.channel.name} for {message.author.name}")
            except Exception as e:
                await message.reply(f"Couldn't summarize: `{str(e)[:80]}`")
                print(f"Summarize error: {e}")
        return

    # Fetch recent channel history for background context (so Srele knows what's going on)
    channel_context = ""
    try:
        recent_msgs = await fetch_channel_history(message.channel, limit=CHANNEL_CONTEXT_LIMIT)
        if recent_msgs:
            channel_context = "\n".join(recent_msgs)
    except Exception as e:
        print(f"Could not fetch channel history: {e}")

    # If message has URLs but no embeds yet, wait briefly for Discord to generate them
    has_urls = URL_PATTERN.search(raw_text)
    if has_urls and not message.embeds:
        await asyncio.sleep(2)
        try:
            message = await message.channel.fetch_message(message.id)
        except Exception:
            pass

    # Extract info from Discord embeds (link previews that Discord auto-generates)
    embed_context = ""
    if message.embeds:
        embed_parts = []
        for emb in message.embeds:
            parts = []
            if emb.author and emb.author.name:
                parts.append(f"Author: {emb.author.name}")
            if emb.title:
                parts.append(f"Title: {emb.title}")
            if emb.description:
                parts.append(f"Description: {emb.description}")
            if emb.footer and emb.footer.text:
                parts.append(f"Footer: {emb.footer.text}")
            if parts:
                source = emb.url or "link preview"
                embed_parts.append(f"[Embed from {source}]\n" + "\n".join(parts))
        if embed_parts:
            embed_context = "\n\n".join(embed_parts)

    async with message.channel.typing():
        try:
            # Get Claude's response
            response = chat_with_claude(
                str(message.channel.id),
                message.author.display_name,
                raw_text,
                embed_context=embed_context,
                image_urls=image_urls if image_urls else None,
                channel_context=channel_context,
            )

            # Check for learning command first
            display_text, learning_data = parse_learning_command(response)
            if learning_data:
                learning_text = learning_data.get("learning", "")
                if learning_text:
                    saved = save_learning(learning_text, taught_by=message.author.display_name)
                    if saved:
                        if display_text:
                            await message.reply(display_text)
                        await message.add_reaction("\U0001f9e0")  # brain emoji
                    else:
                        await message.reply(display_text or "Got it, but I had trouble saving that to my memory. Try again?")
                else:
                    await message.reply(display_text or "I didn't catch what to learn. Could you rephrase?")
                return

            # Check for push code command
            display_text, push_data = parse_push_command(display_text)
            if push_data:
                # Store pending push and ask for confirmation
                pending_pushes[str(message.channel.id)] = push_data
                file_name = push_data.get("file", "bot.py")
                commit_msg = push_data.get("commit_message", "Update")

                # Send the conversational reply (which includes the code preview)
                if display_text:
                    if len(display_text) <= 2000:
                        await message.reply(display_text)
                    else:
                        chunks = [display_text[i:i+2000] for i in range(0, len(display_text), 2000)]
                        await message.reply(chunks[0])
                        for chunk in chunks[1:]:
                            await message.channel.send(chunk)

                await message.channel.send(
                    f"**Ready to push `{file_name}`** — \"{commit_msg}\"\n\n"
                    f"Say **\"push it\"** to deploy or **\"cancel\"** to abort."
                )
                return

            # Check for send message command
            display_text, send_data = parse_send_message_command(display_text)
            if send_data:
                target_channel_name = send_data.get("channel", "").strip().lstrip("#")
                msg_to_send = send_data.get("message", "")

                if target_channel_name and msg_to_send:
                    # Find the channel by name
                    target_channel = discord.utils.get(message.guild.text_channels, name=target_channel_name)
                    if target_channel:
                        # Resolve {{username}} mentions
                        msg_to_send = await resolve_mentions(message.guild, msg_to_send)
                        await target_channel.send(msg_to_send)

                        if display_text:
                            await message.reply(display_text)
                        else:
                            await message.reply(f"Sent to #{target_channel_name}")
                        await message.add_reaction("\u2709\ufe0f")  # envelope
                    else:
                        await message.reply(f"Couldn't find channel **#{target_channel_name}**. Check the name?")
                else:
                    await message.reply(display_text or "I need a channel name and a message to send.")
                return

            # Check for save idea command
            display_text, idea_data = parse_save_command(display_text)

            if idea_data:
                idea_text = idea_data.get("idea", raw_text)
                priority = idea_data.get("priority", "normal")
                priority_id = PRIORITY_WORKING_ON_IT if priority == "high" else PRIORITY_TO_BE_MADE

                item_name = generate_item_name(idea_text)
                item = create_monday_item(item_name, priority_id)
                item_id = item["id"]

                discord_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
                update_body = (
                    f"<strong>Original idea from Discord</strong>\n\n"
                    f"<strong>Posted by:</strong> {message.author.display_name} (@{message.author.name})\n"
                    f"<strong>Channel:</strong> #{message.channel.name}\n"
                    f"<strong>Date:</strong> {datetime.datetime.now().strftime('%B %d, %Y at %I:%M %p')}\n\n"
                    f"---\n\n"
                    f"{idea_text}\n\n"
                    f"---\n\n"
                    f"<a href=\"{discord_link}\">View original Discord message</a>"
                )
                add_item_update(item_id, update_body)

                monday_url = f"https://tryreact1s-team.monday.com/boards/{MONDAY_BOARD_ID}/pulses/{item_id}"

                priority_labels = {
                    PRIORITY_TO_BE_MADE: "To be made",
                    PRIORITY_WORKING_ON_IT: "Working on it",
                    PRIORITY_DONE: "Done",
                }

                if display_text:
                    await message.reply(display_text)

                embed = discord.Embed(
                    title="Idea Saved!",
                    description=f"**{item_name}**",
                    color=0x00c875,
                    url=monday_url,
                )
                embed.add_field(name="Board", value="To-Dos > SRELE IDEAS", inline=True)
                embed.add_field(name="Priority", value=priority_labels.get(priority_id, "To be made"), inline=True)
                embed.set_footer(text=f"Saved by {message.author.display_name}")

                await message.channel.send(embed=embed)
                await message.add_reaction("\u2705")

                print(f"Saved: \"{item_name}\" (ID: {item_id}) by {message.author.name}")
            else:
                # Normal conversation — just reply
                if len(display_text) <= 2000:
                    await message.reply(display_text)
                else:
                    chunks = [display_text[i:i+2000] for i in range(0, len(display_text), 2000)]
                    await message.reply(chunks[0])
                    for chunk in chunks[1:]:
                        await message.channel.send(chunk)

        except anthropic.APIError as e:
            await message.reply(f"Oops, brain glitch. Try again? (`{str(e)[:80]}`)")
            print(f"Anthropic API error: {e}")

        except requests.exceptions.RequestException as e:
            await message.reply(f"Couldn't reach Monday.com. Try again? (`{str(e)[:80]}`)")
            print(f"Monday.com API error: {e}")

        except Exception as e:
            await message.reply(f"Something went wrong. Try again? (`{str(e)[:80]}`)")
            print(f"Unexpected error: {e}")


# ============================================================
# SLASH COMMANDS
# ============================================================

@bot.tree.command(name="idea", description="Save an idea to Monday.com")
async def slash_idea(interaction: discord.Interaction, text: str):
    await interaction.response.defer(thinking=True)

    try:
        priority_id = PRIORITY_TO_BE_MADE
        text_lower = text.lower()
        for kw in ["urgent", "asap", "high priority", "important", "critical"]:
            if kw in text_lower:
                priority_id = PRIORITY_WORKING_ON_IT
                break

        item_name = generate_item_name(text)
        item = create_monday_item(item_name, priority_id)
        item_id = item["id"]

        discord_link = f"https://discord.com/channels/{interaction.guild.id}/{interaction.channel.id}/{interaction.id}"
        update_body = (
            f"<strong>Original idea from Discord</strong>\n\n"
            f"<strong>Posted by:</strong> {interaction.user.display_name} (@{interaction.user.name})\n"
            f"<strong>Channel:</strong> #{interaction.channel.name}\n"
            f"<strong>Date:</strong> {datetime.datetime.now().strftime('%B %d, %Y at %I:%M %p')}\n\n"
            f"---\n\n{text}\n\n---\n\n"
            f"<a href=\"{discord_link}\">View original Discord message</a>"
        )
        add_item_update(item_id, update_body)

        monday_url = f"https://tryreact1s-team.monday.com/boards/{MONDAY_BOARD_ID}/pulses/{item_id}"

        priority_labels = {
            PRIORITY_TO_BE_MADE: "To be made",
            PRIORITY_WORKING_ON_IT: "Working on it",
        }

        embed = discord.Embed(
            title="Idea Saved!",
            description=f"**{item_name}**",
            color=0x00c875,
            url=monday_url,
        )
        embed.add_field(name="Board", value="To-Dos > SRELE IDEAS", inline=True)
        embed.add_field(name="Priority", value=priority_labels.get(priority_id, "To be made"), inline=True)
        embed.set_footer(text=f"Saved by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed)
        print(f"Saved (slash): \"{item_name}\" (ID: {item_id}) by {interaction.user.name}")

    except Exception as e:
        await interaction.followup.send(f"Something went wrong: `{str(e)[:100]}`")
        print(f"Slash command error: {e}")


@bot.tree.command(name="idea-list", description="Show the last 5 ideas in SRELE IDEAS")
async def slash_idea_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        query = """
        query ($boardId: [ID!]!) {
            boards(ids: $boardId) {
                groups(ids: ["group_mm1m2py5"]) {
                    items_page(limit: 5) {
                        items {
                            id
                            name
                            column_values {
                                id
                                text
                            }
                        }
                    }
                }
            }
        }
        """

        variables = {"boardId": [str(MONDAY_BOARD_ID)]}
        result = monday_request(query, variables)
        items = result["data"]["boards"][0]["groups"][0]["items_page"]["items"]

        if not items:
            await interaction.followup.send("No ideas in SRELE IDEAS yet. Use `/idea` or tag `@Srele` to save one!")
            return

        embed = discord.Embed(title="Latest Ideas — SRELE IDEAS", color=0x579bfc)

        for item in items:
            priority = "—"
            date_added = "—"
            for col in item["column_values"]:
                if col["id"] == COL_PRIORITY and col["text"]:
                    priority = col["text"]
                if col["id"] == COL_DATE_ADDED and col["text"]:
                    date_added = col["text"]

            monday_url = f"https://tryreact1s-team.monday.com/boards/{MONDAY_BOARD_ID}/pulses/{item['id']}"
            embed.add_field(
                name=item["name"],
                value=f"Priority: {priority} | Added: {date_added} | [Open]({monday_url})",
                inline=False,
            )

        embed.set_footer(text="To-Dos board > SRELE IDEAS group")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"Couldn't fetch ideas: `{str(e)[:100]}`")
        print(f"idea-list error: {e}")


@bot.tree.command(name="srele-memory", description="Show what Srele has learned")
async def slash_memory(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        load_learnings()  # Refresh from learnings.json

        if not srele_learnings:
            await interaction.followup.send("I haven't learned anything yet! Teach me by saying `@Srele learn this: ...`")
            return

        embed = discord.Embed(title="Srele's Memory", color=0x9d50dd)

        for i, learning in enumerate(srele_learnings, 1):
            # Truncate long learnings for display
            display = learning if len(learning) <= 100 else learning[:97] + "..."
            embed.add_field(name=f"#{i}", value=display, inline=False)

        embed.set_footer(text=f"{len(srele_learnings)} learnings stored in learnings.json")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"Couldn't fetch memory: `{str(e)[:100]}`")
        print(f"memory error: {e}")


@bot.tree.command(name="summarize", description="Summarize the recent conversation in this channel")
async def slash_summarize(interaction: discord.Interaction, messages: int = 200):
    """Slash command: /summarize [messages]"""
    await interaction.response.defer(thinking=True)

    try:
        # Clamp between 10 and 500
        msg_count = max(10, min(500, messages))

        history_msgs = await fetch_channel_history_for_summary(interaction.channel, limit=msg_count)
        if len(history_msgs) < 3:
            await interaction.followup.send("Not enough messages in this channel to summarize.")
            return

        messages_text = "\n".join(history_msgs)
        summary = summarize_with_claude(messages_text)

        embed = discord.Embed(
            title=f"Channel Summary — #{interaction.channel.name}",
            description=summary[:4096],
            color=0x579bfc,
        )
        embed.set_footer(text=f"Based on last {len(history_msgs)} messages")

        await interaction.followup.send(embed=embed)
        print(f"Summarized #{interaction.channel.name} ({len(history_msgs)} msgs) for {interaction.user.name}")

    except Exception as e:
        await interaction.followup.send(f"Couldn't summarize: `{str(e)[:100]}`")
        print(f"Summarize error: {e}")


# ============================================================
# SYNC SLASH COMMANDS ON READY
# ============================================================

@bot.event
async def setup_hook():
    await bot.tree.sync()
    print("Slash commands synced with Discord")


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print("\nStarting Srele Bot...")
    print(f"Board: To-Dos (ID: {MONDAY_BOARD_ID})")
    print(f"Group: SRELE IDEAS ({MONDAY_GROUP_ID})")
    print("Connecting to Discord...\n")
    bot.run(DISCORD_TOKEN)
