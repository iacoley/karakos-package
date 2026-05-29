#!/usr/bin/env python3
"""
Memory Maintenance — Episodic consolidation and embedding generation.

Processes recent messages from JSONL files:
1. Reads previous day's messages from JSONL
2. Scores importance (using Claude Haiku for cheap importance scoring)
3. Creates episodes in SQLite episodes table
4. Decays existing episode scores (configurable decay rate)
5. Applies cutoff to prune low-importance episodes

Called by scheduler daily at 3 AM.
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
MEMORY_DIR = WORKSPACE / "data" / "memory"
MEMORY_DB = MEMORY_DIR / "memory.db"
MESSAGES_DIR = WORKSPACE / "data" / "messages"
HEALTH_FILE = WORKSPACE / "data" / "health" / "memory-maintenance.json"

DECAY_RATE = float(os.environ.get("MEMORY_DECAY_RATE", "0.25"))
IMPORTANCE_CUTOFF = float(os.environ.get("MEMORY_CUTOFF", "6.0"))
MAX_EPISODES = int(os.environ.get("MEMORY_MAX_EPISODES", "15"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] memory-maintenance: %(message)s",
)
log = logging.getLogger(__name__)


def init_db() -> sqlite3.Connection:
    """Initialize the memory database with required tables."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(MEMORY_DB))
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 5.0,
            channel TEXT,
            tags TEXT,
            agents TEXT,
            created_at TIMESTAMP,
            consolidated_at TIMESTAMP DEFAULT NULL,
            embedding BLOB
        );

        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL DEFAULT 0.8,
            domain TEXT DEFAULT 'general',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL DEFAULT 0.7,
            reinforcement_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_importance ON episodes(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);
        CREATE INDEX IF NOT EXISTS idx_facts_domain ON facts(domain);
    """)

    conn.commit()
    return conn


def read_previous_day_messages() -> list:
    """Read messages from previous day's JSONL files."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")

    messages_file = MESSAGES_DIR / f"messages-{date_str}.jsonl"
    if not messages_file.exists():
        log.info(f"No messages file for {date_str}")
        return []

    messages = []
    with open(messages_file, 'r') as f:
        for line in f:
            try:
                msg = json.loads(line.strip())
                messages.append(msg)
            except json.JSONDecodeError:
                continue

    log.info(f"Read {len(messages)} messages from {date_str}")
    return messages


def score_importance(summary: str) -> float:
    """Score episode importance using Claude Haiku (cheap)."""
    prompt = f"""Score the importance of this conversation excerpt on a scale of 1-10.

Consider:
- 9-10: Major decisions, critical events, important personal information
- 7-8: Meaningful conversations, useful information, preferences
- 5-6: Normal interactions, routine tasks
- 3-4: Minor updates, simple acknowledgments
- 1-2: Trivial chatter, noise

Excerpt: {summary}

Respond with ONLY a number 1-10."""

    try:
        if os.environ.get("AGENT_BACKEND", "claude") == "ollama":
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from ollama_runner import run_oneshot
            ok, score_str, _meta = run_oneshot(prompt)
            if not ok:
                raise RuntimeError("ollama oneshot failed")
        else:
            result = subprocess.run(
                ["claude", "-p", prompt, "--model", "haiku", "--max-turns", "1"],
                capture_output=True,
                text=True,
                timeout=20
            )
            score_str = result.stdout.strip()
        # Local models often wrap the number in prose; grab the first integer.
        match = re.search(r"\d+(?:\.\d+)?", score_str)
        score = float(match.group()) if match else 5.0
        return max(1.0, min(10.0, score))
    except Exception as e:
        log.warning(f"Failed to score importance: {e}, defaulting to 5.0")
        return 5.0


def segment_messages_into_episodes(messages: list) -> list:
    """Segment messages into conversation episodes."""
    if not messages:
        return []

    episodes = []
    current_episode = []
    last_ts = None

    # Group messages with <5 minute gaps into episodes
    for msg in messages:
        try:
            ts = datetime.fromisoformat(msg["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            continue

        if last_ts and (ts - last_ts).total_seconds() > 300:  # 5 min gap
            if current_episode:
                episodes.append(current_episode)
                current_episode = []

        current_episode.append(msg)
        last_ts = ts

    if current_episode:
        episodes.append(current_episode)

    return episodes


def create_episode_summary(messages: list) -> str:
    """Create a 2-3 sentence summary of an episode."""
    # Simple implementation: just concatenate the messages
    texts = []
    for msg in messages[:10]:  # Limit to first 10 messages
        author = msg.get("author_name", "User")
        content = msg.get("content", "")
        if content and not msg.get("is_bot", False):
            texts.append(f"{author}: {content}")

    return " | ".join(texts)[:500]  # Cap at 500 chars


def decay_importance(conn: sqlite3.Connection) -> int:
    """Apply time-based decay to episode importance scores.

    Decay formula: effective_score = importance - (days_since_creation * DECAY_RATE / 4)
    Default DECAY_RATE=0.25 means 0.25 points lost per 4 days.
    """
    # Calculate decay for each episode based on age
    rows = conn.execute(
        "SELECT id, importance, created_at FROM episodes WHERE importance > ?"
        , (IMPORTANCE_CUTOFF,)
    ).fetchall()

    decayed = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        try:
            created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            days_old = (now - created_at).total_seconds() / 86400
            decay_amount = (days_old / 4.0) * DECAY_RATE
            new_importance = max(0, row["importance"] - decay_amount)

            if new_importance != row["importance"]:
                conn.execute(
                    "UPDATE episodes SET importance = ? WHERE id = ?",
                    (new_importance, row["id"])
                )
                decayed += 1
        except Exception as e:
            log.warning(f"Failed to decay episode {row['id']}: {e}")
            continue

    conn.commit()
    log.info(f"Decayed importance on {decayed} episodes (rate={DECAY_RATE} per 4 days)")
    return decayed


def prune_low_importance(conn: sqlite3.Connection) -> int:
    """Remove episodes below the importance cutoff."""
    cursor = conn.execute(
        "DELETE FROM episodes WHERE importance < ?",
        (IMPORTANCE_CUTOFF,)
    )
    pruned = cursor.rowcount
    conn.commit()
    if pruned:
        log.info(f"Pruned {pruned} low-importance episodes")
    return pruned


def consolidate_episodes(conn: sqlite3.Connection) -> int:
    """Mark short recent episodes for consolidation."""
    # Find episodes with short summaries that haven't been consolidated
    rows = conn.execute(
        "SELECT id, summary FROM episodes "
        "WHERE consolidated_at IS NULL AND LENGTH(summary) < 200 "
        "ORDER BY created_at DESC LIMIT ?",
        (MAX_EPISODES,)
    ).fetchall()

    if len(rows) < 3:
        return 0

    # Group short episodes and mark them consolidated
    consolidated = 0
    for row in rows:
        conn.execute(
            "UPDATE episodes SET consolidated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (row["id"],)
        )
        consolidated += 1

    conn.commit()
    log.info(f"Marked {consolidated} episodes as consolidated")
    return consolidated


def generate_embeddings(conn: sqlite3.Connection) -> int:
    """Generate embeddings for episodes that don't have them yet."""
    try:
        from fastembed import TextEmbedding
    except ImportError:
        log.warning("fastembed not installed — skipping embedding generation")
        return 0

    rows = conn.execute(
        "SELECT id, summary FROM episodes WHERE embedding IS NULL LIMIT 50"
    ).fetchall()

    if not rows:
        return 0

    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    texts = [row["summary"] for row in rows]
    embeddings = list(model.embed(texts))

    import numpy as np
    for row, emb in zip(rows, embeddings):
        emb_bytes = np.array(emb, dtype=np.float32).tobytes()
        conn.execute(
            "UPDATE episodes SET embedding = ? WHERE id = ?",
            (emb_bytes, row["id"])
        )

    conn.commit()
    log.info(f"Generated embeddings for {len(rows)} episodes")
    return len(rows)


def write_health(success: bool, stats: dict) -> None:
    """Write health heartbeat."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "healthy" if success else "error",
        "stats": stats,
    }))


def process_messages_to_episodes(conn: sqlite3.Connection) -> int:
    """Process yesterday's messages into episodes."""
    messages = read_previous_day_messages()
    if not messages:
        return 0

    episodes = segment_messages_into_episodes(messages)
    created = 0

    for episode_msgs in episodes:
        if not episode_msgs:
            continue

        summary = create_episode_summary(episode_msgs)
        importance = score_importance(summary)

        # Extract metadata
        channel = episode_msgs[0].get("channel_name", "unknown")
        created_at = episode_msgs[0].get("ts", datetime.now(timezone.utc).isoformat())

        conn.execute(
            """INSERT INTO episodes (summary, importance, channel, created_at)
               VALUES (?, ?, ?, ?)""",
            (summary, importance, channel, created_at)
        )
        created += 1

    conn.commit()
    log.info(f"Created {created} episodes from messages")
    return created


def main():
    log.info("Memory maintenance starting")
    start = time.time()

    try:
        conn = init_db()

        stats = {
            "episodes_created": process_messages_to_episodes(conn),
            "decayed": decay_importance(conn),
            "pruned": prune_low_importance(conn),
            "consolidated": consolidate_episodes(conn),
            "embedded": generate_embeddings(conn),
        }

        conn.close()
        duration = round(time.time() - start, 2)
        stats["duration_s"] = duration

        log.info(f"Maintenance complete in {duration}s: {json.dumps(stats)}")
        write_health(True, stats)

    except Exception as e:
        log.error(f"Maintenance failed: {e}")
        write_health(False, {"error": str(e)})
        sys.exit(1)


if __name__ == "__main__":
    main()
