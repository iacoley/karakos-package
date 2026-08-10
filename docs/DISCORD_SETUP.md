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

## Shared Channels (Optional)

By default a channel's `default_agent` answers every human message in it. That
is right for a channel that exists to talk to the bot, and wrong for one you
also use to talk to other people. Two per-channel keys in
`config/channels.json` change it:

```json
{
  "channels": {
    "general": { "id": "...", "default_agent": "amos" },
    "kitchen": { "id": "...", "default_agent": "amos", "reply_gate": true },
    "agent-chat": { "id": "...", "default_agent": "amos", "guest_agents": true }
  }
}
```

**`reply_gate`** — for channels shared with more than one human. The agent
answers when it is @mentioned, when the message is a reply to something it
said, or when the message opens with its name. It stays quiet otherwise,
including when people are trading messages quickly. It is silence-biased on
purpose: staying quiet costs you one word to recover from, and interrupting
costs you the conversation. Omit the key and the channel behaves as before.

**`guest_agents`** — lets bots from *outside* this install address your agents
in that channel. Off by default, so a stranger's bot in a shared server is
ignored.

Two rules apply to every bot regardless of that key:

- A bot must @mention an agent to reach it. `default_agent` applies to humans
  only — without that rule, two installs sharing a channel answer each other
  until a rate limit or a cost cap intervenes.
- Bot-to-bot exchanges stop after `GUEST_TURN_LIMIT` turns (default 12) with no
  human in between, and the relay posts once to say why. Anyone speaking in the
  channel refills the budget.

Set `GUEST_TURN_LIMIT` in `config/.env` to change the cap.

## Operational Commands

These are real Discord application commands: type `/` in any channel the bot
can see and they appear in the picker with descriptions. They are registered
on every container start by `bin/register-discord-commands.py` and dispatched
by the relay itself, not by an agent — which is the point, because the case
you need them in is an agent too wedged to read its own messages.

**Every one of them is owner-only.** They are gated on `OWNER_DISCORD_ID`
(step 6 above); an install that never set it denies everyone, including you.

| Command | What it does |
| --- | --- |
| `/status` | Each agent's state, whether its subprocess is alive, queue depth |
| `/health` | The health monitor's verdict, component by component |
| `/usage` | Rate-limit headroom — the limit that actually stops a turn |
| `/cost [agent]` | Today's and this month's spend, same numbers as `bin/cost-report.sh` |
| `/logs <service> [lines]` | Tail a log from `logs/`, e.g. `/logs relay 40` |
| `/interrupt [agent]` | Stop the generation in flight; the session survives |
| `/reload [agent]` | Bounce the subprocess, keep the session |
| `/clear [agent]` | Clear the session and restart — destructive |
| `/kill [agent]` | Kill the subprocess and leave it down |
| `/flush [agent]` | Drop the agent's pending message queue |
| `/help` | List the commands |

`agent` is optional everywhere it appears: with one agent configured it is
inferred, and in a channel with a `default_agent` that agent is used. With
several agents and no default, the command refuses rather than guessing —
clearing the wrong agent's session is not recoverable and is invisible to the
person who typed it.

`/clear`, `/reload`, `/status` and `/usage` are additionally accepted as plain
message text (`/clear`, or the older `/sys clear`), for the case where the
picker itself is unavailable.

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

**The agent answered but nothing appeared in the channel:**

A reply that is generated and then fails to post is written to
`data/discord-dead-letter.jsonl` rather than discarded — the agent ran and the
tokens were spent, so the answer is worth keeping. `GET /health` reports the
count:

```json
{ "status": "healthy", "dead_letters": 3, "dead_letter_path": "..." }
```

A non-zero count means the delivery path is broken, not that the agents are
idle. Each record holds the agent, channel id, timestamp, failure reason and
the full reply text, so it can be re-sent by hand once the cause is fixed.

The usual cause is a revoked **Send Messages** permission in that channel;
Discord answers 403 and the relay does not retry, because a revoked permission
does not heal on its own. Transient failures (5xx, network) are retried
`DISCORD_POST_MAX_ATTEMPTS` times (default 3) before being queued.

The file only ever grows — prune it once the entries have been recovered.

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
