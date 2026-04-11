"""
Srele — AI Assistant for the React Biome Discord Server

Srele is a conversational AI assistant powered by Claude.
When users explicitly ask to save an idea/task, Srele creates
an item on the Monday.com To-Dos board in the SRELE IDEAS group.
Otherwise, Srele just chats normally.
"""

import os
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

MONDAY_BOARD_ID = 5089081467
MONDAY_GROUP_ID = "group_mm1m2py5"
MONDAY_API_URL = "https://api.monday.com/v2"

COL_PRIORITY = "status"
COL_DATE_ADDED = "datum"
COL_WHO = "person"

PRIORITY_TO_BE_MADE = 2
PRIORITY_WORKING_ON_IT = 0
PRIORITY_DONE = 1

# Srele's system prompt — conversational AI + idea saving capability
SRELE_SYSTEM = """You are Srele, a friendly and helpful AI assistant for the React Biome Discord server.

You chat naturally about anything — content ideas, business strategy, tech questions, creative brainstorming, or casual conversation. You're witty, direct, and helpful.

IMPORTANT — SAVING IDEAS TO MONDAY.COM:
You have the ability to save ideas/tasks to the team's Monday.com board. But you should ONLY do this when the user EXPLICITLY asks you to save, add, or track something. Look for phrases like:
- "save this", "add this", "put this on the board"
- "add to to-do", "add to monday", "track this"
- "make a task for", "create a task", "save as idea"
- "put this as an idea", "log this", "note this down"
- "remember this as a task"

If the user is just chatting, brainstorming, or asking questions — DO NOT save anything. Just have a normal conversation.

When you DO need to save something, respond with a JSON block in this exact format on its own line:
SAVE_IDEA:{"idea": "<the full idea text>", "priority": "normal"}

Set priority to "high" only if the user says it's urgent/important/high priority/asap/critical.
Set priority to "normal" for everything else.

Place the SAVE_IDEA line at the END of your message, after your conversational response. You can still chat normally before it.

If the user just says something like "save this" without context, ask them what they want to save.

Keep your responses concise — you're in a Discord chat, not writing an essay. A few sentences is usually enough."""

# Per-channel conversation history (keeps last messages for context)
conversation_history = {}
MAX_HISTORY = 20

# ============================================================
# URL FETCHING — extract info from links
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

        # Extract Open Graph / meta info
        title = None
        description = None

        # Try og:title first, then <title>
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"]
        elif soup.title and soup.title.string:
            title = soup.title.string.strip()

        # Try og:description, then meta description
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
    for url in urls[:3]:  # Limit to 3 URLs max
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


def create_monday_item(item_name, priority_label_id=PRIORITY_TO_BE_MADE):
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
        "groupId": MONDAY_GROUP_ID,
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
# CLAUDE AI
# ============================================================

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def generate_item_name(raw_idea):
    """Use Claude to generate a short item name (max 15 words) from a raw idea."""
    message = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        system="Generate a short, clear item name (MAXIMUM 15 words) for a task/idea. Just return the name, nothing else. No quotes, no explanation.",
        messages=[{"role": "user", "content": raw_idea}],
    )

    name = message.content[0].text.strip().strip('"').strip("'")
    words = name.split()
    if len(words) > 15:
        name = " ".join(words[:15])
    return name


def chat_with_claude(channel_id, user_name, user_message):
    """Send a message to Claude with conversation history and return the response."""
    if channel_id not in conversation_history:
        conversation_history[channel_id] = []

    history = conversation_history[channel_id]

    # Fetch context from any URLs in the message
    url_context = extract_url_context(user_message)
    message_content = f"[{user_name}]: {user_message}"
    if url_context:
        message_content += f"\n\n--- Fetched link content ---\n{url_context}"

    history.append({
        "role": "user",
        "content": message_content,
    })

    # Trim history if too long
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        conversation_history[channel_id] = history

    message = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SRELE_SYSTEM,
        messages=history,
    )

    assistant_reply = message.content[0].text.strip()

    history.append({
        "role": "assistant",
        "content": assistant_reply,
    })

    return assistant_reply


def parse_save_command(response_text):
    """Check if Claude's response contains a SAVE_IDEA command. Returns (clean_text, idea_data) or (text, None)."""
    match = re.search(r'SAVE_IDEA:(\{.*?\})', response_text, re.DOTALL)
    if not match:
        return response_text, None

    try:
        idea_data = json.loads(match.group(1))
        # Remove the SAVE_IDEA line from the display text
        clean_text = response_text[:match.start()].rstrip()
        return clean_text, idea_data
    except json.JSONDecodeError:
        return response_text, None


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!srele ", intents=intents)


@bot.event
async def on_ready():
    print(f"\n✅ Srele is online as {bot.user} (ID: {bot.user.id})")
    print(f"📋 Monday.com board: {MONDAY_BOARD_ID}")
    print(f"🎯 Listening for @Srele mentions...\n")

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

    # Remove the bot mention to get the actual message
    raw_text = message.content
    for mention in message.mentions:
        raw_text = raw_text.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    raw_text = raw_text.strip()

    if not raw_text:
        await message.reply(
            "Hey! I'm **Srele** — your AI assistant for React Biome.\n\n"
            "Just tag me and chat about anything. If you want me to save an idea or task "
            "to Monday.com, just ask! (e.g. *\"@Srele save this idea: podcast about morning routines\"*)"
        )
        return

    async with message.channel.typing():
        try:
            # Get Claude's response (conversational + possible save command)
            response = chat_with_claude(
                str(message.channel.id),
                message.author.display_name,
                raw_text,
            )

            # Check if Claude wants to save an idea
            display_text, idea_data = parse_save_command(response)

            if idea_data:
                # Claude detected a save request — create the Monday.com item
                idea_text = idea_data.get("idea", raw_text)
                priority = idea_data.get("priority", "normal")
                priority_id = PRIORITY_WORKING_ON_IT if priority == "high" else PRIORITY_TO_BE_MADE

                item_name = generate_item_name(idea_text)
                item = create_monday_item(item_name, priority_id)
                item_id = item["id"]

                # Add full details as a comment on the Monday item
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

                # Send the conversational reply first
                if display_text:
                    await message.reply(display_text)

                # Then send the save confirmation as an embed
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
                await message.add_reaction("✅")

                print(f"Saved: \"{item_name}\" (ID: {item_id}) by {message.author.name}")
            else:
                # Normal conversation — just reply
                # Split long responses for Discord's 2000 char limit
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
