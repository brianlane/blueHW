#!/usr/bin/env python3
"""
NemoClaw v5 — 5-model voting ensemble with research-enriched prompts.

Tiers:
  Seat 1:   Ollama qwen3.5:2b (free, local, downweighted — no thinking)
  Seat 2:   openrouter/free with price ordering (cheapest free model)
  Seats 3-4: Free OpenRouter models (best effort)
  Paid fallbacks (in order of cost):
    - Nano ($0.10/M)
    - DeepSeek R1 Distill 32B ($0.29/M — chain-of-thought reasoning)
    - Gemini Flash ($0.15/M)

Rules:
  - 3/5+ YES → APPROVED
  - 2/2 unanimous YES → APPROVED (both agree)
  - Ollama alone can't tip a tie (downweighted, no reasoning)
  - <2 total votes → REJECTED
"""
import json, os, time, hashlib, requests
from concurrent.futures import ThreadPoolExecutor, as_completed

CACHE_FILE = "/tmp/nemoclaw_cache.json"
COOLDOWN_HOURS = 2
TARGET_VOTES = 5

FREE_MODELS = [
    ("AutoFree",    "openrouter/free"),
    ("Hermes405B",  "nousresearch/hermes-3-llama-3.1-405b:free"),
    ("Nemotron",    "nvidia/nemotron-3-super-120b-a12b:free"),
    ("Qwen80B",     "qwen/qwen3-next-80b-a3b-instruct:free"),
]

PAID_FALLBACKS = [
    ("Gemini",     "google/gemini-2.5-flash"),
    ("DSR1-32B",   "deepseek/deepseek-r1-distill-qwen-32b"),
    ("Nano",       "openai/gpt-5.4-nano"),
]

OLLAMA_MODEL = "qwen3.5:2b"
OLLAMA_URL   = "http://localhost:11434/api/chat"


def _opp_hash(opp):
    key = f"{opp['ticker']}|{opp['action']}|{opp['side']}|{opp['price']}"
    return hashlib.md5(key.encode()).hexdigest()[:12]

def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except:
        return {}

def _save_cache(cache):
    cutoff = time.time() - 86400
    cache = {k: v for k, v in cache.items() if v.get("ts", 0) > cutoff}
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)

def _cached_result(opp):
    h = _opp_hash(opp)
    entry = _load_cache().get(h)
    if entry and time.time() - entry.get("ts", 0) < COOLDOWN_HOURS * 3600:
        return entry
    return None

def _store_result(opp, result):
    cache = _load_cache()
    cache[_opp_hash(opp)] = {"ts": time.time(), **result}
    _save_cache(cache)


def _call_openrouter(model_id, prompt, api_key, is_free=False):
    for attempt in range(2):
        try:
            body = {
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150 if "distill" in model_id else 20,
                "temperature": 0.1,
            }
            if is_free:
                body["provider"] = {"order": ["price"]}
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "NemoClaw",
                },
                json=body,
                timeout=25,
            )
            if r.status_code in (429, 502, 503):
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            choices = r.json().get("choices", [])
            if not choices:
                return None
            msg = choices[0].get("message", {})
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning") or "").strip()
            # For thinking models, answer may be in reasoning field
            if content:
                return content
            if reasoning:
                return reasoning
            return None
        except:
            if attempt == 0:
                time.sleep(1)
                continue
    return None


def _call_ollama(prompt):
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_predict": 30, "temperature": 0.1},
                "think": False,
            },
            timeout=20,
        )
        if r.status_code == 200:
            text = (r.json().get("message", {}).get("content") or "").strip()
            return text if text else None
    except:
        pass
    return None


def _parse_vote(text):
    if not text:
        return None
    upper = text.upper()[:50]
    if "YES" in upper:
        return "YES"
    if "NO" in upper:
        return "NO"
    return None


def _build_prompt(opp):
    platform = "Kalshi prediction market"

    context_lines = []
    if opp.get("forecast_info"):
        context_lines.append(f"Weather forecast: {opp['forecast_info']}")
    if opp.get("news_context"):
        context_lines.append(f"Market context: {opp['news_context']}")
    if opp.get("news_headlines"):
        context_lines.append(f"Recent headlines:\n{opp['news_headlines']}")

    context = "\n".join(context_lines)
    if context:
        context = f"\n\nResearch context:\n{context}\n"

    return f"""You are a quantitative trading analyst. Evaluate this paper trade:

Platform: {platform}
Trade: {opp.get('label', opp['ticker'])}
Entry price: {opp['price']}
Stop loss: {opp.get('stop', 'N/A')}
Profit target: {opp.get('target', 'N/A')}
Quantity: {opp.get('qty', 1)}{context}
Based on the research context and risk/reward ratio, should we enter this trade?
Reply with ONLY one word: YES or NO"""


def vote(opp):
    cached = _cached_result(opp)
    if cached:
        return cached

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    prompt = _build_prompt(opp)
    votes = {}
    tiers_used = set()

    # All free models + Ollama in parallel
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {}
        futures[pool.submit(_call_ollama, prompt)] = ("Ollama", "local")
        for label, model_id in FREE_MODELS:
            futures[pool.submit(_call_openrouter, model_id, prompt, api_key, True)] = (label, "free")

        for f in as_completed(futures, timeout=35):
            label, tier = futures[f]
            try:
                raw = f.result()
                parsed = _parse_vote(raw)
                if parsed:
                    votes[label] = parsed
                    tiers_used.add(tier)
            except:
                pass

    # Paid fallbacks to reach target votes (Nano first, then DSR1, then Gemini)
    if len(votes) < TARGET_VOTES and api_key:
        for label, model_id in PAID_FALLBACKS:
            if label in votes or len(votes) >= TARGET_VOTES:
                break
            raw = _call_openrouter(model_id, prompt, api_key)
            parsed = _parse_vote(raw)
            if parsed:
                votes[label] = parsed
                tiers_used.add("paid")

    # Extra attempt if still < 3
    if len(votes) < 3 and api_key:
        for label, model_id in PAID_FALLBACKS:
            if label in votes or len(votes) >= 3:
                break
            raw = _call_openrouter(model_id, prompt, api_key)
            parsed = _parse_vote(raw)
            if parsed:
                votes[label] = parsed
                tiers_used.add("paid")

    yes_ct = sum(1 for v in votes.values() if v == "YES")
    no_ct  = sum(1 for v in votes.values() if v == "NO")
    total  = yes_ct + no_ct
    non_ollama_yes = sum(1 for k, v in votes.items() if v == "YES" and k != "Ollama")

    if total < 2:
        approved = False
        status = f"REJECTED (only {total} vote)"
    elif total == 2:
        # 2/2 unanimous: both must agree
        approved = yes_ct == 2
        status = f"{'APPROVED' if approved else 'REJECTED'} {yes_ct}/{total} (unanimous)"
    elif yes_ct >= 3 and non_ollama_yes >= 2:
        approved = True
        status = f"APPROVED {yes_ct}/{total}"
    elif yes_ct >= 3 and non_ollama_yes < 2:
        # Ollama can't be the deciding swing vote
        approved = False
        status = f"REJECTED {yes_ct}/{total} (Ollama swing)"
    else:
        approved = False
        status = f"REJECTED {yes_ct}/{total}"

    tier_str = "+".join({"local": "Ollama", "paid": "Paid", "free": "Free"}.get(t, t) for t in sorted(tiers_used))
    vote_str = " ".join(f"{k}={'Y' if v == 'YES' else 'N'}" for k, v in votes.items())
    summary = f"{status} [{tier_str}] {vote_str}"

    result = {
        "approved": approved, "yes": yes_ct, "no": no_ct,
        "total": total, "votes": votes, "tiers": sorted(tiers_used),
        "summary": summary,
    }
    _store_result(opp, result)
    return result


def vote_batch(opportunities):
    results = {}
    for opp in opportunities:
        cached = _cached_result(opp)
        if cached:
            results[opp["ticker"]] = cached
        else:
            results[opp["ticker"]] = vote(opp)
    return results


if __name__ == "__main__":
    test = {
        "ticker": "TEST-V5", "action": "buy", "side": "yes",
        "price": 16, "qty": 1, "stop": 10, "target": 25,
        "label": "BUY YES New York High >53°F on Mar 20 @ 16c (12h left)",
        "market": "kalshi",
        "forecast_info": "NWS forecast: NYC high 56°F on Mar 20 (supports YES >53°F)",
    }
    r = vote(test)
    print(json.dumps(r, indent=2))
