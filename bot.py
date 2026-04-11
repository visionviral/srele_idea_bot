"""
Srele — AI Idea Capture Bot for Discord + Monday.com

Tags @Srele in Discord with any idea, task, or random thought.
Srele uses Claude to generate a short name (max 15 words),
creates an item on the Monday.com To-Dos board in the SRELE IDEAS group,
and posts the full description as a comment on the item.
"""

import os
import datetime
import json
import re
import discord
from discord.ext import commands
import anthropic
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Monday.com board details (To-Dos board → SRELE IDEAS group)
MONDAY_BOARD_ID = 5089081467
MONDAY_GROUP_ID = "group_mm1m2py5"
MONDAY_API_URL = "https://api.monday.com/v2"

# Column IDs on the To-Dos board
COL_PRIORITY = "status"       # Labels: 0=Working on it, 1=Done, 2=To be made
COL_DATE_ADDED = "datum"      # Date column
COL_WHO = "person"            # People column

# Priority label IDs
PRIORITY_TO_BE_MADE = 2
PRIORITY_WORKING_ON_IT = 0
PRIORITY_DONE = 1

# Srele's personality
SRELE_PERSONA = """You are Srele, a creative assistant bot. You help capture ideas and tasks.
When given a raw idea or thought, you generate a clear, concise item name (MAXIMUM 15 words)
that captures the essence of the idea. Be specific and actionable. Don't use filler words."""

# ============================================================
# VALIDATE CONFIG
# ============================================================

missing = []
if not DISCORD_TOKEN:
    missing.append("DISCORD_BOT_TOKEN")
if not MONDAY_API_TOKEN:
    missing.append("MONDAY_API_TOKEN")
if not ANTHROPIC_API_KEY:
    missing.append("ANTHROPIC_API_KEY")

if missing:
    print(f"\n❌ Missing environment variables: {', '.join(missing)}")
    print("Copy .env.example to .env and fill in your values.")
    print("Then run: python bot.py\n")
    exit(1)

# ============================================================
# MONDAY.COM API
# ============================================================

def monday_request(query, variables=None):
    """Send a GraphQL request to Monday.com API."""
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
    """Create a new item in the SRELE IDEAS group on the To-Dos board."""
    today = datetime.date.today().isoformat()  # YYYY-MM-DD

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
    """Add an update/comment to a Monday.com item."""
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
# CLAUDE AI — GENERATE ITEM NAME
# ============================================================

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def generate_item_name(raw_idea):
    """Use Claude to generate a short item name (max 15 words) from a raw idea."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        system=SRELE_PERSONA,
        messages=[
            {
                "role": "user",
                "content": f"""Generate a short, clear item name (MAXIMUM 15 words) for this idea.
Just return the name, nothing else. No quotes, no explanation, no prefix.

Idea: {raw_idea}"""
            }
        ],
    )

    name = message.content[0].text.strip()
    # Remove any quotes the AI might add
    name = name.strip('"').strip("'")
    # Enforce 15 word limit
    words = name.split()
    if len(words) > 15:
        name = " ".join(words[:15])

    return name


def detect_priority(text):
    """Try to detect priority from the message text."""
    text_lower = text.lower()

    high_keywords = ["high priority", "urgent", "asap", "important", "critical"]
    for keyword in high_keywords:
        if keyword in text_lower:
            return PRIORITY_WORKING_ON_IT, keyword

    low_keywords = ["low priority", "whenever", "no rush", "someday", "maybe"]
    for keyword in low_keywords:
        if keyword in text_lower:
            return PRIORITY_TO_BE_MADE, keyword

    # Default
    return PRIORITY_TO_BE_MADE, None


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!srele ", intents=intents)


@bot.event
async def on_ready():
    print(f"\n✅ Srele is online as {bot.user} (ID: {bot.user.id})")
    print(f"📋 Connected to Monday.com board: {MONDAY_BOARD_ID}")
    print(f"📁 Saving ideas to group: SRELE IDEAS ({MONDAY_GROUP_ID})")
    print(f"🔗 Invite URL: https://discord.com/oauth2/authorize?client_id={bot.user.id}&permissions=8&scope=bot")
    print(f"\n🎯 Listening for @Srele mentions...\n")

    # Set the bot's status
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="your ideas | @Srele"
        )
    )


@bot.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return

    # Check if the bot was mentioned
    if bot.user not in message.mentions:
        # Also process commands (like !srele)
        await bot.process_commands(message)
        return

    # Remove the bot mention from the message to get the raw idea
    raw_text = message.content
    for mention in message.mentions:
        raw_text = raw_text.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    raw_text = raw_text.strip()

    # If the message is empty after removing the mention
    if not raw_text:
        await message.reply(
            "👋 Hey! I'm **Srele** — your idea capture bot.\n\n"
            "Tag me with any idea, task, or thought and I'll save it to Monday.com.\n\n"
            "**Example:** `@Srele We should do a collab with that fitness influencer`\n\n"
            "I'll give it a short name, save it to the board, and post the full details as a comment."
        )
        return

    # Check for greetings
    greetings = ["hello", "hi", "hey", "sup", "yo", "what's up", "whatsup"]
    if raw_text.lower().strip() in greetings:
        await message.reply(
            "👋 Hey! I'm **Srele** — ready to capture your ideas.\n\n"
            "Just tag me with your idea and I'll save it to Monday.com!\n"
            "**Example:** `@Srele Podcast episode about morning routines`"
        )
        return

    # Show typing indicator while processing
    async with message.channel.typing():
        try:
            # Detect priority from the text
            priority_id, priority_keyword = detect_priority(raw_text)

            # Clean priority keywords from the text before sending to AI
            clean_text = raw_text
            if priority_keyword:
                clean_text = re.sub(re.escape(priority_keyword), "", clean_text, flags=re.IGNORECASE).strip()
                clean_text = re.sub(r"^[:\-,.\s]+", "", clean_text).strip()  # Clean up leading punctuation

            # Generate a short item name using Claude
            item_name = generate_item_name(clean_text if clean_text else raw_text)

            # Create the item on Monday.com
            item = create_monday_item(item_name, priority_id)
            item_id = item["id"]

            # Build the full comment/update body
            discord_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"

            update_body = (
                f"💡 <strong>Original idea from Discord</strong>\n\n"
                f"<strong>Posted by:</strong> {message.author.display_name} (@{message.author.name})\n"
                f"<strong>Channel:</strong> #{message.channel.name}\n"
                f"<strong>Date:</strong> {datetime.datetime.now().strftime('%B %d, %Y at %I:%M %p')}\n\n"
                f"---\n\n"
                f"{raw_text}\n\n"
                f"---\n\n"
                f"<a href=\"{discord_link}\">View original Discord message</a>"
            )

            # Add the comment to the item
            add_item_update(item_id, update_body)

            # Build Monday.com item URL
            monday_url = f"https://tryreact1s-team.monday.com/boards/{MONDAY_BOARD_ID}/pulses/{item_id}"

            # Map priority ID to label
            priority_labels = {
                PRIORITY_TO_BE_MADE: "To be made",
                PRIORITY_WORKING_ON_IT: "Working on it",
                PRIORITY_DONE: "Done",
            }
            priority_label = priority_labels.get(priority_id, "To be made")

            # Reply with confirmation
            embed = discord.Embed(
                title="✅ Idea Saved!",
                description=f"**{item_name}**",
                color=0x00c875,  # Monday.com green
                url=monday_url,
            )
            embed.add_field(name="📋 Board", value="To-Dos → SRELE IDEAS", inline=True)
            embed.add_field(name="🔖 Priority", value=priority_label, inline=True)
            embed.add_field(name="📅 Date Added", value=datetime.date.today().strftime("%B %d, %Y"), inline=True)
            embed.set_footer(text=f"Saved by {message.author.display_name} • Full details in item comment")

            await message.reply(embed=embed)

            # React to the original message with a checkmark
            await message.add_reaction("✅")

            print(f"💾 Saved: \"{item_name}\" (ID: {item_id}) by {message.author.name}")

        except anthropic.APIError as e:
            await message.reply(f"❌ AI error — couldn't process your idea. Try again?\n`{str(e)[:100]}`")
            print(f"❌ Anthropic API error: {e}")

        except requests.exceptions.RequestException as e:
            await message.reply(f"❌ Monday.com error — couldn't save to the board. Try again?\n`{str(e)[:100]}`")
            print(f"❌ Monday.com API error: {e}")

        except Exception as e:
            await message.reply(f"❌ Something went wrong. Try again?\n`{str(e)[:100]}`")
            print(f"❌ Unexpected error: {e}")


# ============================================================
# SLASH COMMANDS
# ============================================================

@bot.tree.command(name="idea", description="Save an idea to Monday.com")
async def slash_idea(interaction: discord.Interaction, text: str):
    """Slash command: /idea <text>"""
    await interaction.response.defer(thinking=True)

    try:
        priority_id, priority_keyword = detect_priority(text)

        clean_text = text
        if priority_keyword:
            clean_text = re.sub(re.escape(priority_keyword), "", clean_text, flags=re.IGNORECASE).strip()
            clean_text = re.sub(r"^[:\-,.\s]+", "", clean_text).strip()

        item_name = generate_item_name(clean_text if clean_text else text)
        item = create_monday_item(item_name, priority_id)
        item_id = item["id"]

        discord_link = f"https://discord.com/channels/{interaction.guild.id}/{interaction.channel.id}/{interaction.id}"

        update_body = (
            f"💡 <strong>Original idea from Discord</strong>\n\n"
            f"<strong>Posted by:</strong> {interaction.user.display_name} (@{interaction.user.name})\n"
            f"<strong>Channel:</strong> #{interaction.channel.name}\n"
            f"<strong>Date:</strong> {datetime.datetime.now().strftime('%B %d, %Y at %I:%M %p')}\n\n"
            f"---\n\n"
            f"{text}\n\n"
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

        embed = discord.Embed(
            title="✅ Idea Saved!",
            description=f"**{item_name}**",
            color=0x00c875,
            url=monday_url,
        )
        embed.add_field(name="📋 Board", value="To-Dos → SRELE IDEAS", inline=True)
        embed.add_field(name="🔖 Priority", value=priority_labels.get(priority_id, "To be made"), inline=True)
        embed.add_field(name="📅 Date Added", value=datetime.date.today().strftime("%B %d, %Y"), inline=True)
        embed.set_footer(text=f"Saved by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed)
        print(f"💾 Saved (slash): \"{item_name}\" (ID: {item_id}) by {interaction.user.name}")

    except Exception as e:
        await interaction.followup.send(f"❌ Something went wrong: `{str(e)[:100]}`")
        print(f"❌ Slash command error: {e}")


@bot.tree.command(name="idea-list", description="Show the last 5 ideas in SRELE IDEAS")
async def slash_idea_list(interaction: discord.Interaction):
    """Slash command: /idea-list"""
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
            await interaction.followup.send("📭 No ideas in SRELE IDEAS yet. Be the first — use `/idea` or tag `@Srele`!")
            return

        embed = discord.Embed(
            title="📋 Latest Ideas — SRELE IDEAS",
            color=0x579bfc,
        )

        for item in items:
            # Get priority and date from column values
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

        embed.set_footer(text="To-Dos board → SRELE IDEAS group")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't fetch ideas: `{str(e)[:100]}`")
        print(f"❌ idea-list error: {e}")


# ============================================================
# SYNC SLASH COMMANDS ON READY
# ============================================================

@bot.event
async def setup_hook():
    """Sync slash commands with Discord when the bot starts."""
    await bot.tree.sync()
    print("🔄 Slash commands synced with Discord")


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print("\n🚀 Starting Srele Bot...")
    print(f"📋 Board: To-Dos (ID: {MONDAY_BOARD_ID})")
    print(f"📁 Group: SRELE IDEAS ({MONDAY_GROUP_ID})")
    print("⏳ Connecting to Discord...\n")
    bot.run(DISCORD_TOKEN)
