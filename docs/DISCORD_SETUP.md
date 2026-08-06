# Discord Bot Setup

Step-by-step guide to creating a Discord bot for Karakos.

## 1. Create a Discord Application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application**
3. Name it after your system (e.g., "Athena" or whatever you chose in setup)
4. Click **Create**

## 2. Create the Bot

1. In your application, go to the **Bot** section (left sidebar)
2. Click **Add Bot** → **Yes, do it!**
3. Under **TOKEN**, click **Copy** — this is your `DISCORD_BOT_TOKEN`
4. **Save this token** — you can only see it once (you can regenerate if lost)

### Bot Settings

Under the Bot section, configure:

- **Public Bot**: OFF (only you need to add it)
- **Message Content Intent**: ON (required — the bot needs to read messages)
- **Server Members Intent**: ON (optional — enables online member list)
- **Presence Intent**: OFF (not needed)

## 3. Get the Bot User ID

1. In your application, go to **General Information**
2. Copy the **Application ID** — this is your `DISCORD_BOT_ID`

Or: In Discord, enable Developer Mode (Settings → Advanced → Developer Mode), then right-click your bot in the server and **Copy User ID**.

## 4. Invite the Bot

1. Go to **OAuth2 → URL Generator** in the developer portal
2. Select scopes:
   - `bot`
   - `applications.commands` (**required** — Karakos registers slash
     commands on startup; without this scope registration returns 403,
     and the only fix is to redo this invite step. Adding the scope
     later requires a human with Manage Server to re-authorise the bot
     through this same URL generator, so check it now.)
3. Select bot permissions:
   - Read Messages/View Channels
   - Send Messages
   - Send Messages in Threads
   - Read Message History
   - Add Reactions
   - Embed Links
   - Attach Files
4. Copy the generated URL and open it in your browser
5. Select your server and authorize

## 5. Get Channel IDs

In Discord, enable Developer Mode if you haven't:
- User Settings → Advanced → Developer Mode → ON

Then right-click each channel and **Copy Channel ID**:

| Channel | Purpose | Required |
|---------|---------|----------|
| #general | Main conversation channel | Yes |
| #signals | System alerts and health updates | Yes |
| #staff-comms | Agent-to-agent backchannel | Optional |

## 6. Get Your User ID

Right-click your own username in Discord → **Copy User ID**. This is your `OWNER_DISCORD_ID`.

## 7. Get Server ID

Right-click your server name → **Copy Server ID**. This is your `DISCORD_SERVER_ID`.

## Multi-Bot Setup (Optional)

If you want each agent to post under its own identity:

1. Create additional bot applications (one per agent)
2. Copy each bot's token and user ID
3. Invite all bots to your server
4. In `config/.env`, add:
   ```
   DISCORD_BOT_TOKEN_BUILDER=<token>
   DISCORD_BOT_ID_BUILDER=<id>
   ```
5. In `config/agents.json`, set each agent's `discord_bot_token_env` and `discord_bot_id_env`

Without multi-bot setup, all agents post through the primary bot.

## Troubleshooting

**Bot appears offline:**
- Check that the container is running: `docker compose ps`
- Check relay logs: `docker compose logs relay`
- Verify the token in `config/.env`

**Bot can't read messages:**
- Ensure **Message Content Intent** is enabled in the developer portal
- Check the bot has Read Messages permission in the channel

**"Missing Access" error:**
- The bot isn't in the server or doesn't have channel permissions
- Re-invite using the URL generator with correct permissions

**Slash commands don't show up when you type `/`:**
- Registration runs automatically on container start
  (`bin/register-discord-commands.py`, via `bin/entrypoint.sh`) and logs a
  `WARNING` on failure — check `docker compose logs`
- A 403 in that log means the bot was invited without the
  `applications.commands` scope. Re-invite it through **OAuth2 → URL
  Generator** with that scope checked (step 4 above) — a token or
  permission change alone will not fix it

**Rate limited:**
- Discord rate limits are handled automatically with exponential backoff
- If persistent, reduce message volume or check for loops
