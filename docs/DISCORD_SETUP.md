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
3. Under **TOKEN**, click **Copy** — this goes in `config/.env` as
   `DISCORD_BOT_TOKEN_PRIMARY`
4. **Save this token** — you can only see it once (you can regenerate if lost)

### Bot Settings

Under the Bot section, configure:

- **Public Bot**: OFF (only you need to add it)
- **Message Content Intent**: ON (required — the bot needs to read messages)
- **Server Members Intent**: OFF (the relay never requests it; turning it on
  changes nothing)
- **Presence Intent**: OFF (not needed)

## 3. Get the Bot User ID

1. In your application, go to **General Information**
2. Copy the **Application ID** — this goes in `config/.env` as
   `DISCORD_BOT_ID_PRIMARY`

Or: In Discord, enable Developer Mode (Settings → Advanced → Developer Mode), then right-click your bot in the server and **Copy User ID**.

### The names these end up under

The setup wizard writes them for you; this is what it writes, in case you are
editing `config/.env` by hand later:

| Value | Variable |
|---|---|
| Bot token | `DISCORD_BOT_TOKEN_PRIMARY` |
| Application / bot user ID | `DISCORD_BOT_ID_PRIMARY` |
| Server ID | `DISCORD_SERVER_ID` |
| General channel ID | `DISCORD_CHANNEL_GENERAL` |
| Signals channel ID | `DISCORD_CHANNEL_SIGNALS` |
| Your own user ID | `OWNER_DISCORD_ID` |

Additional agents use the same pattern with their own suffix, e.g.
`DISCORD_BOT_TOKEN_BUILDER`. There are no unsuffixed `DISCORD_BOT_TOKEN` or
`DISCORD_BOT_ID` variables — nothing reads those names.

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
5. In `config/agents.yaml`, set each agent's `discord.token_env` and `discord.bot_id_env`
   (and `discord.role_id_env` if you want a Discord role to address the agent; see
   `mention_only` below). The variable it names may hold several role ids separated by commas

Without multi-bot setup, all agents post through the primary bot.

## Shared Channels (Optional)

By default a channel's `default_agent` answers every human message in it. That
is right for a channel that exists to talk to the bot, and wrong for one you
also use to talk to other people. Two per-channel keys in
`config/channels.json` change it:

```json
{
  "channels": {
    "general": { "id": "...", "default_agent": "main" },
    "kitchen": { "id": "...", "default_agent": "main", "reply_gate": true },
    "agent-chat": { "id": "...", "default_agent": "main", "guest_agents": true }
  }
}
```

**`reply_gate`** — for channels shared with more than one human or agent. The
agent answers when it is @mentioned, when the message is a reply to something
it said, or when the message addresses it by name. It stays quiet otherwise,
including when people are trading messages quickly. It is silence-biased on
purpose: staying quiet costs you one word to recover from, and interrupting
costs you the conversation.

*On by default.* A channel with no `reply_gate` key is gated when it has two or
more agents (distinct agents whose shards list the channel, plus one if the
channel sets `"guest_agents": true`). Set `"reply_gate": false` to opt a channel
out, or `true` / an object to gate it regardless of agent count. Single-agent
channels are not gated unless you ask. `"mention_only": true` wins over the gate.
To restore opt-in behaviour for the whole relay, set
`KARAKOS_REPLY_GATE_DEFAULT=off`.

*A name is a hint, not a trigger.* A message engages the agent by name only when
it is a vocative: the name (optionally after `hey`, `ok` or `okay`) followed by
`,` `:` `!` `?` or `-`, or the name as the whole message. "amos, add milk" and
"hey amos" engage; "amos's reply is slow" and "amos is investigating" do not.

*Optional classifier tier (opt-in, silence on any doubt).* `"reply_gate": true`
is heuristic only. Make it an object to let a small model (Haiku) decide the
messages the heuristics leave open, such as a plain question said to the room:

```json
"kitchen": { "id": "...", "default_agent": "main",
  "reply_gate": { "classifier": "haiku", "context_messages": 6,
                  "min_confidence": 0.7, "timeout_s": 8,
                  "max_per_minute": 4, "max_per_hour": 60 } }
```

An object without `classifier` is heuristic only. Out-of-range numbers warn
once and use the defaults shown. Mentions, replies, name-openers, replies to
other people, the fast-volley rule, bot authors and empty messages never reach
the model. Any error, timeout, unparseable answer, low confidence, rate cap
(per channel) or paused account keeps the silence. An engage routes exactly as
a heuristic engage does, to the channel's agent.

What is sent: the new message plus the last `context_messages` messages of that
channel (each cut to 300 characters, kept in relay memory only, never written
to disk or logged), to the same Claude account your agents already use, in a
tool-less single-turn call with no MCP servers or settings. Each call is a
fraction of a cent, recorded under the channel's agent in the cost table;
counters are in `data/health/relay.json` under `reply_gate`. Set
`KARAKOS_REPLY_CLASSIFIER=off` in `config/.env` to disable the tier install-wide
without editing channels.

**`mention_only`** — for channels where an agent should speak only when it is
addressed on purpose. Set `"mention_only": true` on the channel and the agent
answers only a direct @mention of its bot, or a mention of a Discord role mapped
to it. A plain message, a reply to the agent, and a message that opens with its
name all stay silent. Channel ownership and the primary fallback do not apply
either: a shard that owns the channel still does not answer an unmentioned
message. A channel that sets both `mention_only` and `reply_gate` logs an error
at load and is treated as `mention_only`.

**`context_messages`** — a channel-level key (not inside `reply_gate`; that one
sizes the classifier's window and is separate). An agent's turn normally carries
only the message that woke it, so in a `mention_only` channel "take this feedback"
has nothing to point at. With this key the relay prepends the messages that
arrived in the channel since the agent last posted there (other agents' posts
included, the triggering message excluded), oldest first, one line each:
`[context] Author (HH:MM): text`, times in UTC. The default is 8, the range 0 to
20, and `0` delivers the bare message as before. Each message is cut to 300
characters and the relay remembers the last 20 per channel, in memory only; when
older messages were left out, the block ends its first line with a note telling
the agent to call `discord` history for more.

```json
{ "channels": { "ops": { "mention_only": true, "context_messages": 12 } } }
```

**`unlisted_channels`** — a top-level key in `channels.json` for channels that are
not listed in it. `"ignore"` (the default, also used when the key is absent) never
routes an unlisted channel, even on an @mention. `"mention_only"` makes an unlisted
channel behave like `mention_only: true`: an @mention of an agent routes, a plain
message does not. It applies only inside the servers named in `server_id` /
`server_ids`, and a foreign bot is still refused (there is no `guest_agents` for an
unlisted channel). Any other value logs an error at load and acts as `"ignore"`.

```json
{ "server_id": "...", "unlisted_channels": "mention_only", "channels": { } }
```

Role mentions need one extra key per agent, naming the environment variable that
holds the role's id (right-click the role in Discord with developer mode on, then
Copy Role ID). In `config/agents.yaml`:

```yaml
agents:
  main:
    discord:
      token_env: DISCORD_BOT_TOKEN_MAIN
      bot_id_env: DISCORD_BOT_ID_MAIN
      role_id_env: DISCORD_ROLE_ID_MAIN
```

and in `config/.env`, `DISCORD_ROLE_ID_MAIN=<role id>`. A mention of a mapped
role routes to that agent the same way a mention of its bot does. A role mention
with no mapped agent is ignored.

```json
"front-desk": { "id": "...", "default_agent": "main", "mention_only": true }
```

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

## Optional behaviours

Four small behaviours that make a busy channel and a long-running agent easier
to live with. Each of those four is **off by default**, so an upgraded install behaves
exactly as before. `tool_streaming` is not one of these: it
overrides the agent's own setting (see the table). `reactions` is **on** by default (see the table). They are set in `config/channels.json`: a top-level `"ux"`
object applies to every channel, and a channel's own `"ux"` object overrides it
key by key. A key set in neither place is off (`tool_streaming` is the exception:
unset, it inherits the agent's setting; `reactions` is the other: unset, it is on). A wrong type or an unknown key is
logged once and treated as off.

```json
{
  "ux": {"suppress_embeds": true},
  "channels": {
    "general": {
      "id": "...",
      "ux": {"threads": {"after_s": 60}, "edit_reroute": true, "reaction_notices": "owner"}
    }
  }
}
```

| Key | Values (default off) | What it does | Discord permissions |
|---|---|---|---|
| `threads` | `false`, `true`, or `{"after_s": 60, "max_lines": 40}` | Once a turn has run `after_s` seconds, its tool-activity lines move into a public thread opened on the first tool line. The final reply stays in the channel. The per-turn line cap rises from 12 to `max_lines`. If a thread cannot be created, the lines stay in the channel. Replies inside the thread route as the parent channel. | Create Public Threads, Send Messages in Threads |
| `reaction_notices` | `false`, `"owner"` (or `true`), `"humans"` | Tells the agent when a person reacts to one of its messages. The agent normally answers `PASS`, which is not posted. One notice per user and message per minute, at most 10 per channel per minute. | Read Message History |
| `edit_reroute` | `false`, `true`, or `{"window_s": 900, "max_followups": 3}` | When a person edits a message an agent already received: a still-queued message is rewritten in place, otherwise the agent gets a follow-up with the old and new text. An edit after a `PASS` or empty reply is ignored. | Read Message History |
| `suppress_embeds` | `false`, `true` | Agent text replies and tool lines are posted with link previews suppressed. Ask prompts keep their embeds. Messages already posted are not changed. | none |
| `tool_streaming` | `false`, `true`; unset inherits the agent | Turns the `-# ⚙ Tool — …` activity lines on or off for this channel. Precedence: channel `ux.tool_streaming` > agent `tool_streaming` in `agents.yaml` > default `true`. A top-level `ux.tool_streaming` applies to every channel that does not set its own, so it also overrides the agent setting. | none |
| `reactions` | `true` (default), `false` | Lets an agent react with an emoji. A final reply of exactly `REACT: <emoji>` adds that reaction to the message that started the turn instead of posting text, and the `discord` tool's `react` action does the same for any message (`channel`, `message_id`, `emoji`; a custom emoji is `name:id`). When off, `REACT: ...` posts as plain text and the tool action returns an error. A reaction on a message that is not a real Discord message (an edit, a reaction notice, an internal row) is skipped. | Add Reactions |

`true` for `threads` means `after_s` 60 and `max_lines` 40; `true` for `edit_reroute`
means `window_s` 900 and `max_followups` 3.

Reaction notices and edits need the relay to be able to read the message, so the
bot must be able to see the channel's history. Changes to `channels.json` are
picked up by the relay within seconds and by the server on its next agent reload.

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
- Check that the container is running: `make ps` or
  `docker compose -f config/docker-compose.yml --env-file config/.env ps`
- Check the logs: `make logs`. There is one compose service, `karakos`, and
  the relay is a process inside it — `docker compose logs relay` will not work
- Verify `DISCORD_BOT_TOKEN_PRIMARY` in `config/.env`

**Bot can't read messages:**
- Ensure **Message Content Intent** is enabled in the developer portal
- Check the bot has Read Messages permission in the channel
- Test it with an @mention. A mention still reaches the bot without the intent. If the bot answers tags but ignores everything else, the intent is off. If it ignores tags too, the connection is dead or the code threw.
- The intent must be on in two places: the portal toggle (Bot -> Privileged Gateway Intents -> Message Content Intent) and in code. The code part is already set in `bin/relay.py`.

**"Missing Access" error:**
- The bot isn't in the server or doesn't have channel permissions
- Re-invite using the URL generator with correct permissions

**The agent answered but nothing appeared in the channel:**

A reply that is generated goes into a durable outbox (`data/outbox/outbox.db`)
before it is sent, so a Discord outage or restart does not lose it. Delivery is
retried with backoff (5 s, 15 s, 45 s, ... capped at 1 h) until
`DISCORD_OUTBOX_MAX_ATTEMPTS` (default 12) or `DISCORD_OUTBOX_MAX_AGE_S`
(default 24 h); a 400, 401, 403 or 404 is not retried. A row that gives up is
`dead` and keeps its text. `GET /health` reports it:

```json
{ "status": "healthy", "dead_letters": 3,
  "outbox": { "pending": 0, "sending": 0, "dead": 3, "oldest_pending_age_s": null } }
```

A non-zero `dead` count means the delivery path is broken, not that the agents
are idle. Inspect and revive rows with `GET /outbox`, `GET /outbox/{id}`,
`POST /outbox/{id}/retry` and `POST /outbox/{id}/discard`, or, with the server
down, `python3 lib/outbox.py {stats,list,show,retry,discard}`. Every attempt is
recorded in `outbox_events` (never the message text).

The usual cause is a revoked **Send Messages** permission in that channel;
Discord answers 403 and the outbox does not retry, because a revoked permission
does not heal on its own. A reply that is empty or exactly `PASS` is never
posted. Delivery is at-least-once, narrowed by a per-chunk nonce. A 1.x
`data/discord-dead-letter.jsonl` is imported as `dead` rows by `karakos migrate`.

**Slash commands don't show up when you type `/`:**
- Registration runs automatically on container start
  (`bin/register-discord-commands.py`, via `bin/entrypoint.sh`) and logs a
  `WARNING` on failure — check `make logs`
- To see what is currently registered, or to wipe it and let the next start
  re-register from scratch: `bin/register-discord-commands.py --list` and
  `--clear`
- A 403 in that log means the bot was invited without the
  `applications.commands` scope. Re-invite it through **OAuth2 → URL
  Generator** with that scope checked (step 4 above) — a token or
  permission change alone will not fix it

**Rate limited:**
- Discord rate limits are handled automatically with exponential backoff
- If persistent, reduce message volume or check for loops
