#!/usr/bin/env python3
"""
Model Router — Central cost-tier routing for all agents.
Debate team bypasses this (uses direct model calls per role).
This serves: Guardian, Research Agent, Evolutionary Agent, Main Trader scan prompts.

Tiers (sorted by price):
  free     → Ollama local, then OpenRouter auto-free          ($0)
  cheap    → Gemini Flash                                     ($0.15/M)
  mid      → Gemini Flash → DSR1 Distill 32B                 ($0.15-0.29/M)
  reasoning→ DSR1 Distill 32B → R1 full                      ($0.29-0.70/M)
  deep     → DeepSeek R1 full (chain-of-thought reasoning)   ($0.70/M)
  premium  → R1 full → DSR1 Distill → Gemini                 ($0.70/M)
"""
import os, requests, time

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL = "http://localhost:11434/api/chat"

TIERS = {
    "free": [
        ("Ollama", "local", "qwen3.5:2b"),
        ("AutoFree", "openrouter", "openrouter/free"),
    ],
    "cheap": [
        ("Gemini", "openrouter", "google/gemini-2.5-flash"),
    ],
    "mid": [
        ("Gemini", "openrouter", "google/gemini-2.5-flash"),
        ("DSR1-32B", "openrouter", "deepseek/deepseek-r1-distill-qwen-32b"),
    ],
    "reasoning": [
        ("DSR1-32B", "openrouter", "deepseek/deepseek-r1-distill-qwen-32b"),
        ("R1", "openrouter", "deepseek/deepseek-r1"),
    ],
    "deep": [
        ("R1", "openrouter", "deepseek/deepseek-r1"),
        ("DSR1-32B", "openrouter", "deepseek/deepseek-r1-distill-qwen-32b"),
    ],
    "premium": [
        ("R1", "openrouter", "deepseek/deepseek-r1"),
        ("DSR1-32B", "openrouter", "deepseek/deepseek-r1-distill-qwen-32b"),
        ("Gemini", "openrouter", "google/gemini-2.5-flash"),
    ],
    "digest": [
        ("GPT5Nano", "openrouter", "openai/gpt-5.4-nano"),
        ("Gemini", "openrouter", "google/gemini-2.5-flash"),
    ],
}


def call_model(prompt, tier="cheap", system=None, max_tokens=100):
    """Route a prompt to the best available model in the given tier.
    Returns (label, response_text) or (None, None) on failure."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    models = TIERS.get(tier, TIERS["cheap"])

    for label, provider, model_id in models:
        text = None
        if provider == "local":
            text = _call_ollama(prompt, system, max_tokens)
        elif provider == "openrouter":
            text = _call_openrouter(model_id, prompt, system, max_tokens, api_key)
        if text:
            return label, text

    return None, None


def _call_ollama(prompt, system, max_tokens):
    try:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        r = requests.post(OLLAMA_URL, json={
            "model": "qwen3.5:2b", "messages": msgs, "stream": False,
            "options": {"num_predict": max_tokens, "temperature": 0.1}, "think": False,
        }, timeout=20)
        if r.status_code == 200:
            text = (r.json().get("message", {}).get("content") or "").strip()
            return text if text else None
    except:
        pass
    return None


def _call_openrouter(model_id, prompt, system, max_tokens, api_key):
    is_r1_full = model_id == "deepseek/deepseek-r1"
    is_thinking = "distill" in model_id or is_r1_full

    for attempt in range(2):
        try:
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})

            is_free = model_id.endswith(":free") or model_id == "openrouter/free"
            body = {
                "model": model_id, "messages": msgs,
                "max_tokens": max(max_tokens, 200) if is_thinking else max_tokens,
                "temperature": 0.1,
            }
            if is_free:
                body["provider"] = {"order": ["price"]}

            timeout = 50 if is_r1_full else 30

            r = requests.post(OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "NemoClaw",
                },
                json=body, timeout=timeout)

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
            return content or reasoning or None
        except:
            if attempt == 0:
                time.sleep(1)
                continue
    return None
