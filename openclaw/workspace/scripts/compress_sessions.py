#!/usr/bin/env python3
"""
Session summarizer + archiver.
Uses Ollama (free, local) to summarize session content before compressing.
Summaries are stored as readable .md files so other agents can reference them.

Weekly: summarize + compress sessions >7 days old
Monthly: merge week summaries into month summary
Yearly: merge month summaries into year summary
"""
import os, sys, json, gzip, glob, time, requests
from datetime import datetime, timedelta

SESSIONS_DIR = os.path.expanduser("~/.openclaw/agents/main/sessions")
ARCHIVE_DIR = os.path.expanduser("~/.openclaw/archives")
SUMMARIES_DIR = os.path.expanduser("~/.openclaw/archives/summaries")
OLLAMA_URL = "http://localhost:11434/api/chat"

def ensure_dirs():
    for d in [ARCHIVE_DIR, f"{ARCHIVE_DIR}/weeks", f"{ARCHIVE_DIR}/months",
              f"{ARCHIVE_DIR}/years", SUMMARIES_DIR]:
        os.makedirs(d, exist_ok=True)


def summarize_with_ollama(text, context="session"):
    """Use local Ollama (free) to create a useful summary."""
    prompt = f"""Summarize this OpenClaw {context} data for a trading bot. Focus on:
1. Key decisions made (trades opened/closed, approvals/rejections)
2. Outcomes (wins, losses, P&L)
3. Lessons learned (what worked, what failed)
4. Any patterns or recurring issues

Keep the summary concise but preserve all important details that would help
future analysis. Use bullet points.

DATA:
{text[:6000]}"""

    try:
        r = requests.post(OLLAMA_URL, json={
            "model": "qwen3.5:2b",
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_predict": 500, "temperature": 0.1},
            "think": False,
        }, timeout=30)
        if r.status_code == 200:
            return r.json().get("message", {}).get("content", "").strip()
    except:
        pass
    return f"(Summary unavailable — raw data archived)\nFirst 500 chars: {text[:500]}"


def compress_weekly():
    """Summarize sessions older than 7 days, archive raw data, save summary."""
    ensure_dirs()
    cutoff = time.time() - 7 * 86400
    files = sorted(glob.glob(f"{SESSIONS_DIR}/*.jsonl"))
    old_files = [f for f in files if os.path.getmtime(f) < cutoff]

    if not old_files:
        print("No sessions to compress")
        return

    week_str = datetime.utcnow().strftime("week-%Y-W%W")
    archive_path = f"{ARCHIVE_DIR}/weeks/{week_str}.jsonl.gz"
    summary_path = f"{SUMMARIES_DIR}/{week_str}.md"

    all_content = []
    with gzip.open(archive_path, "at", encoding="utf-8") as gz:
        for f in old_files:
            try:
                with open(f) as src:
                    content = src.read()
                    gz.write(f"# FILE: {os.path.basename(f)}\n")
                    gz.write(content)
                    gz.write("\n")
                    all_content.append(content[:1000])
                os.remove(f)
            except:
                continue

    combined = "\n---\n".join(all_content)
    summary = summarize_with_ollama(combined, "weekly sessions")

    with open(summary_path, "w") as f:
        f.write(f"# Week Summary: {week_str}\n")
        f.write(f"Generated: {datetime.utcnow().isoformat()}Z\n")
        f.write(f"Sessions archived: {len(old_files)}\n")
        f.write(f"Raw archive: {archive_path}\n\n")
        f.write(summary)

    print(f"Compressed {len(old_files)} sessions -> {archive_path}")
    print(f"Summary -> {summary_path}")


def compress_monthly():
    """Merge week summaries into a month summary."""
    ensure_dirs()
    cutoff = time.time() - 30 * 86400
    week_files = sorted(glob.glob(f"{ARCHIVE_DIR}/weeks/*.jsonl.gz"))
    old_files = [f for f in week_files if os.path.getmtime(f) < cutoff]
    week_summaries = sorted(glob.glob(f"{SUMMARIES_DIR}/week-*.md"))
    old_summaries = [f for f in week_summaries if os.path.getmtime(f) < cutoff]

    if not old_files:
        return

    month_str = datetime.utcnow().strftime("month-%Y-%m")
    archive_path = f"{ARCHIVE_DIR}/months/{month_str}.jsonl.gz"
    summary_path = f"{SUMMARIES_DIR}/{month_str}.md"

    summary_texts = []
    for sf in old_summaries:
        try:
            with open(sf) as f:
                summary_texts.append(f.read())
            os.remove(sf)
        except:
            continue

    with gzip.open(archive_path, "ab") as out:
        for f in old_files:
            try:
                with open(f, "rb") as src:
                    out.write(src.read())
                os.remove(f)
            except:
                continue

    if summary_texts:
        combined = "\n\n---\n\n".join(summary_texts)
        month_summary = summarize_with_ollama(combined, "monthly trading activity")
    else:
        month_summary = "(No week summaries available)"

    with open(summary_path, "w") as f:
        f.write(f"# Month Summary: {month_str}\n")
        f.write(f"Generated: {datetime.utcnow().isoformat()}Z\n")
        f.write(f"Weeks archived: {len(old_files)}\n\n")
        f.write(month_summary)

    print(f"Compressed {len(old_files)} week archives -> {archive_path}")


def compress_yearly():
    """Merge month summaries into a year summary."""
    ensure_dirs()
    cutoff = time.time() - 365 * 86400
    month_files = sorted(glob.glob(f"{ARCHIVE_DIR}/months/*.jsonl.gz"))
    old_files = [f for f in month_files if os.path.getmtime(f) < cutoff]
    month_summaries = sorted(glob.glob(f"{SUMMARIES_DIR}/month-*.md"))
    old_summaries = [f for f in month_summaries if os.path.getmtime(f) < cutoff]

    if not old_files:
        return

    year_str = datetime.utcnow().strftime("year-%Y")
    archive_path = f"{ARCHIVE_DIR}/years/{year_str}.jsonl.gz"
    summary_path = f"{SUMMARIES_DIR}/{year_str}.md"

    summary_texts = []
    for sf in old_summaries:
        try:
            with open(sf) as f:
                summary_texts.append(f.read())
            os.remove(sf)
        except:
            continue

    with gzip.open(archive_path, "ab") as out:
        for f in old_files:
            try:
                with open(f, "rb") as src:
                    out.write(src.read())
                os.remove(f)
            except:
                continue

    if summary_texts:
        combined = "\n\n---\n\n".join(summary_texts)
        year_summary = summarize_with_ollama(combined, "yearly trading performance")
    else:
        year_summary = "(No month summaries available)"

    with open(summary_path, "w") as f:
        f.write(f"# Year Summary: {year_str}\n")
        f.write(f"Generated: {datetime.utcnow().isoformat()}Z\n")
        f.write(f"Months archived: {len(old_files)}\n\n")
        f.write(year_summary)

    print(f"Compressed {len(old_files)} month archives -> {archive_path}")


def list_summaries():
    """List all available summaries for other agents to reference."""
    ensure_dirs()
    summaries = sorted(glob.glob(f"{SUMMARIES_DIR}/*.md"))
    for s in summaries:
        size = os.path.getsize(s)
        print(f"  {os.path.basename(s)} ({size} bytes)")
    return summaries


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    if mode == "weekly":
        compress_weekly()
    elif mode == "monthly":
        compress_monthly()
    elif mode == "yearly":
        compress_yearly()
    elif mode == "list":
        list_summaries()
    elif mode == "all":
        compress_weekly()
        compress_monthly()
        compress_yearly()
