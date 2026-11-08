# Asks

Two ways for an agent to put a question to its owner in Discord.

- `ask_user` blocks the turn until a button is clicked.
- `ask` does not block. It posts the question, the turn ends, and the answer wakes the agent later. The ask is stored in `data/asks/<id>.json`, so it survives a restart.

Use `ask_user` when the answer is needed inside this turn. Use `ask` when the turn can end and resume on the answer.

## Spec

| Key | Meaning |
|---|---|
| `channel` | channel name from `config/channels.json`, or a numeric id |
| `title`, `problem` | embed title and description |
| `options` | 2 to 9 objects `{label, rationale?}` |
| `recommendation` | 0-based index of the recommended option, or `null` |
| `no_recommendation` | required reason when `recommendation` is `null` |
| `mention_owner` | `true` pings the owner in the post |
| `on_answer` | optional; default `{"type":"poke","agent":<you>}`, so the answer turn replies in the ask's channel. Or `{"type":"buzz","shard":"main"}` (wakes you in the internal hive channel, no channel reply) or `{"type":"poke","agent":"main"}` |
| `expires_s` | seconds until the ask expires (default 14 days) |

```bash
cat > /tmp/spec.json <<'JSON'
{"channel": "general", "title": "Pick a host", "problem": "Where should the build run?",
 "options": [{"label": "local", "rationale": "free"}, {"label": "remote"}],
 "recommendation": 0, "on_answer": {"type": "poke", "agent": "main"}}
JSON
python3 bin/ask.py post --spec /tmp/spec.json
python3 bin/ask.py list
```

## Answering

Each option is a button (`kask2:<id>:<index>`, distinct from the blocking `kask:` buttons). Number reactions 1 to 9 on the same message also work; the `ask-poll` job checks them every minute. Only the owner can answer: `OWNER_DISCORD_ID` (read from the environment, then `config/.env`), plus any ids in the optional comma list `ASK_EXTRA_OWNER_IDS`. Other presses and reactions are ignored without a reply. The first answer wins.

The answer is written before the agent is woken. If the wake fails, the ask is marked `wake_failed` and `ask-poll` retries it.

## Waking the agent

The wake message is `ask <id> answered: <label>`. A `buzz` route posts to the agent server `/hive/buzz` and falls back to `bin/poke.sh` when the buzz is refused; a `poke` route calls `bin/poke.sh` directly. Discord calls use `DISCORD_BOT_TOKEN_PRIMARY` (or `DISCORD_BOT_TOKEN`).

## Commands

`bin/ask.py` has `post`, `poll`, `answer`, `list`, `cancel ID [--note T]` and `resolve ID --note T`. Events go to `logs/ask.jsonl`.
