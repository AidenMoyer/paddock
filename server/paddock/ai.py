"""The built-in assistant, AI auto-tagging and video planning.

Providers:
  * "ollama"  — an Ollama server on your LAN (e.g. the Ollama app on the same TrueNAS
                box). Nothing leaves your network. Recommended model: qwen3.8:27b
                (dense 27B, vision + tools + thinking in one download).
  * "openai"  — any OpenAI-compatible *local* server: vLLM, llama.cpp server,
                LM Studio. Use this for AWQ/INT4/NVFP4 quants of Qwen3.8-27B on vLLM,
                which is much faster than Ollama on multi-GPU or Blackwell cards.
  * "claude"  — Anthropic's Claude API. Opt-in only and labelled in the UI, because
                prompts, catalog excerpts and (for tagging) thumbnails leave your network.

All providers run the same tool loop over paddock.tools as the signed-in user, so
the assistant can never do more than that user could — and write tools are only
offered when the user has turned on "Let the assistant make changes".

Making a 27B model pleasant on a home server:
  * One model does chat, planning AND image tagging (Qwen3.8 is natively
    multimodal), so a second vision model never swaps into VRAM.
  * Requests are serialized (one GPU, one job); interactive chat jumps ahead of
    background tagging, which politely waits.
  * num_ctx is always explicit (Ollama's default silently truncates) and
    keep_alive keeps the model resident between messages.
  * Thinking is on where it pays (planning, multi-step chat) and off where it
    doesn't (bulk tagging).
  * The system prompt and tool list are byte-stable across turns so the server's
    prompt cache (llama.cpp / vLLM prefix caching) is reused; tool results are
    trimmed; images are downscaled to 512 px before encoding.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from . import db, projects, thumbs, tools
from .actor import Actor

log = logging.getLogger("paddock.ai")

CLAUDE_MODEL = "claude-opus-5-5"
MAX_STEPS = 12
TOOL_RESULT_CHARS = 16_000
IMAGE_MAX_SIDE = 512

DEFAULTS = {
    "provider": "none",                       # none | ollama | openai | claude
    "ollama_url": "http://ollama:11434",
    "openai_url": "http://vllm:8000/v1",
    "openai_api_key": "",
    "model": "qwen3.8:27b",
    "vision_model": "",                       # empty = same as model (recommended for Qwen3.8)
    "num_ctx": 32768,
    "keep_alive": "30m",
    "think_chat": True,
    "think_plan": True,
    "think_tag": False,
    "temperature": 0.6,
    "claude_effort": "medium",
    "autotag_on_upload": False,
}

# Starting points shown in Settings → Assistant. Users can type any tag their server has.
PRESETS = [
    {"id": "qwen38-24gb", "label": "Qwen3.8 27B · 24 GB GPU (recommended)", "provider": "ollama",
     "model": "qwen3.8:27b", "num_ctx": 32768,
     "notes": "Q4_K_M, 18 GB download. Fits fully on a 24 GB card (RTX 3090/4090) with ~32K context. "
              "Set OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0 on the Ollama app to fit more context."},
    {"id": "qwen38-q8", "label": "Qwen3.8 27B Q8 · 40 GB+ VRAM", "provider": "ollama",
     "model": "qwen3.8:27b-q8_0", "num_ctx": 65536,
     "notes": "Best quality on Ollama; needs ~30 GB for weights plus KV cache (two 24 GB cards or a 48 GB card)."},
    {"id": "qwen38-vllm", "label": "Qwen3.8 27B on vLLM (AWQ/INT4, fastest)", "provider": "openai",
     "model": "Qwen3.8-27B-AWQ", "num_ctx": 65536,
     "notes": "Run vLLM with an INT4/AWQ quant (fits 2×RTX 3090 with long context, or one 32 GB Blackwell with NVFP4). "
              "Point the URL at http://<host>:8000/v1 and use the served model name."},
    {"id": "small-16gb", "label": "Smaller model · 8–16 GB GPU or CPU", "provider": "ollama",
     "model": "qwen3:8b", "num_ctx": 16384,
     "notes": "Runs on modest hardware. Less capable at long multi-step tasks; set a separate vision model if needed."},
]


def config() -> dict:
    cfg = dict(DEFAULTS)
    stored = db.get_setting("ai", {})
    if stored.get("provider") == "local":  # pre-release name
        stored["provider"] = "ollama"
    cfg.update(stored)
    return cfg


def public_config() -> dict:
    cfg = config()
    key = cfg.pop("claude_api_key", None)
    cfg["claude_key_set"] = bool(key)
    cfg["openai_key_set"] = bool(cfg.pop("openai_api_key", ""))
    cfg["sends_data_offsite"] = cfg["provider"] == "claude"
    cfg["presets"] = PRESETS
    return cfg


EDITABLE = ("provider", "ollama_url", "openai_url", "model", "vision_model", "num_ctx", "keep_alive", "think_chat",
            "think_plan", "think_tag", "temperature", "claude_effort", "autotag_on_upload")


def save_config(update: dict) -> dict:
    cfg = db.get_setting("ai", {})
    for k in EDITABLE:
        if k in update and update[k] is not None:
            cfg[k] = update[k]
    for secret in ("claude_api_key", "openai_api_key"):
        if update.get(secret):
            cfg[secret] = str(update[secret]).strip()
        if update.get(f"clear_{secret}"):
            cfg.pop(secret, None)
    if cfg.get("provider") not in ("none", "ollama", "openai", "claude"):
        raise ValueError("provider must be none, ollama, openai or claude")
    if cfg.get("claude_effort", "medium") not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("invalid effort")
    cfg["num_ctx"] = int(min(max(int(cfg.get("num_ctx", 32768)), 2048), 262144))
    cfg["temperature"] = float(min(max(float(cfg.get("temperature", 0.6)), 0.0), 2.0))
    for url_key in ("ollama_url", "openai_url"):
        if cfg.get(url_key) and not re.match(r"^https?://", cfg[url_key]):
            raise ValueError(f"{url_key} must start with http:// or https://")
    db.set_setting("ai", cfg)
    return public_config()


# ---- GPU scheduling ----------------------------------------------------------------

_gpu = threading.Lock()
_interactive_waiting = 0
_waiting_lock = threading.Lock()


@contextmanager
def gpu_slot(interactive: bool):
    """One model request at a time. Background work yields to people."""
    global _interactive_waiting
    if interactive:
        with _waiting_lock:
            _interactive_waiting += 1
        try:
            _gpu.acquire()
        finally:
            with _waiting_lock:
                _interactive_waiting -= 1
    else:
        while True:
            while _interactive_waiting > 0:
                time.sleep(0.5)
            if _gpu.acquire(timeout=1):
                if _interactive_waiting > 0:  # someone arrived; let them go first
                    _gpu.release()
                    continue
                break
    try:
        yield
    finally:
        _gpu.release()


# ---- prompts --------------------------------------------------------------------------

def system_prompt(actor: Actor, can_write: bool) -> str:
    # Keep this byte-stable for a given user/mode so prompt caches can be reused.
    write_rule = (
        "You may make changes (tags, collections, projects, notes, plans, rescans, moving items to Paddock's trash). "
        "Before touching more than 25 items or trashing anything, state exactly what you will change and ask for "
        "confirmation unless the user already clearly asked for it."
        if can_write else
        "You are in read-only mode. If the user asks for changes, explain what you would do and tell them they can "
        "enable 'Let the assistant make changes' in Settings → Assistant.")
    return f"""You are the assistant built into Paddock, {actor.username}'s private media and engineering vault running on their own home server (TrueNAS). It holds movies, TV, music, photos and home videos, RED/cinema camera footage, CAD files, 3D-print files (STL/3MF/G-code) and electronics designs, organised into libraries and projects.

You help with: finding things, organising and tagging, keeping track of footage from shoots, sorting engineering files, checking on the server, and planning videos about projects (outline, shot list from existing clips, missing shots, voice-over, music).

How to work:
- Use the tools to look things up; never guess ids, titles, dates or counts. Refer to items as #<id> so the app can link them.
- When the request is ambiguous, search first, then summarise what you found and propose next steps.
- Keep answers short and practical; use Markdown lists and headings for plans.
- {write_rule}
- Paddock cannot permanently delete files; trash is reversible.
- The current date is given at the start of each user message."""


def _stamp(text: str) -> str:
    return f"[{datetime.now():%A %Y-%m-%d %H:%M}]\n{text}"


# ---- conversations (in memory, per user) ------------------------------------------

@dataclass
class Conversation:
    id: str
    user_id: int
    provider: str
    messages: list = field(default_factory=list)
    updated: float = field(default_factory=time.time)


_convos: "OrderedDict[str, Conversation]" = OrderedDict()
_convo_lock = threading.Lock()


def _get_convo(conv_id: str | None, actor: Actor, provider: str) -> Conversation:
    with _convo_lock:
        if conv_id and conv_id in _convos:
            c = _convos[conv_id]
            if c.user_id == actor.user_id and c.provider == provider:
                _convos.move_to_end(conv_id)
                return c
        c = Conversation(id=secrets.token_urlsafe(12), user_id=actor.user_id, provider=provider)
        _convos[c.id] = c
        while len(_convos) > 100:
            _convos.popitem(last=False)
        return c


def _assistant_actor(actor: Actor) -> tuple[Actor, bool]:
    user = db.q1("SELECT allow_ai_changes FROM users WHERE id=?", (actor.user_id,))
    can_write = bool(user and user["allow_ai_changes"]) and actor.can("write")
    scopes = {"read"} | ({"write"} if can_write else set())
    return Actor(user_id=actor.user_id, username=actor.username, is_admin=actor.is_admin, kind="ai",
                 scopes=scopes, label=f"assistant:{actor.username}"), can_write


def _compact(obj: Any) -> Any:
    """Drop empty fields so tool results cost fewer tokens."""
    if isinstance(obj, dict):
        return {k: _compact(v) for k, v in obj.items() if v not in (None, "", [], {}) and k not in ("has_thumb",)}
    if isinstance(obj, list):
        return [_compact(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 3)
    return obj


def _run_tool(ai_actor: Actor, name: str, args: dict) -> tuple[str, bool]:
    try:
        result = tools.call(ai_actor, name, args)
        text = json.dumps(_compact(result), default=str, separators=(",", ":"))
        if len(text) > TOOL_RESULT_CHARS:
            text = text[:TOOL_RESULT_CHARS] + '... (truncated — narrow the query or lower "limit")'
        return text, True
    except tools.ToolError as e:
        return f"Error: {e}", False
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s failed", name)
        return f"Error: {type(e).__name__}: {e}", False


def chat(actor: Actor, message: str, conversation_id: str | None = None, project_id: int | None = None) -> dict:
    cfg = config()
    provider = cfg["provider"]
    if provider == "none":
        return {"error": "No assistant model is configured. An admin can set one up in Settings → Assistant "
                         "(a local model keeps everything on your network)."}
    ai_actor, can_write = _assistant_actor(actor)
    convo = _get_convo(conversation_id, actor, provider)
    text = message.strip()
    if project_id and not convo.messages:
        outline = projects.outline_for_ai(project_id, max_items=150)
        if outline:
            text = f"(Context: I'm looking at project #{project_id}.)\n\n{outline}\n\n---\n{text}"
    steps: list[dict] = []
    started = time.time()
    with gpu_slot(interactive=True):
        if provider == "claude":
            reply = _claude_loop(cfg, ai_actor, can_write, convo, _stamp(text), steps)
        elif provider == "openai":
            reply = _openai_loop(cfg, ai_actor, can_write, convo, _stamp(text), steps)
        else:
            reply = _ollama_loop(cfg, ai_actor, can_write, convo, _stamp(text), steps)
    convo.updated = time.time()
    return {"conversation_id": convo.id, "reply": reply, "steps": steps, "can_write": can_write,
            "provider": provider, "model": CLAUDE_MODEL if provider == "claude" else cfg["model"],
            "seconds": round(time.time() - started, 1)}


# ---- Claude ---------------------------------------------------------------------------

def _claude_client(cfg: dict):
    import anthropic

    key = cfg.get("claude_api_key")
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def _claude_create(client, cfg: dict, **kwargs):
    # Server-side fallback: if Claude Opus 5.5 declines, the API retries on a fallback model in the same call.
    return client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        output_config={"effort": cfg.get("claude_effort", "medium")},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        **kwargs,
    )


def _claude_error(e: Exception) -> str:
    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return "Claude rejected the API key. Check Settings → Assistant."
    if isinstance(e, anthropic.RateLimitError):
        return "Claude is rate-limiting requests right now — try again in a minute."
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API (is the server allowed to reach the internet?)."
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude API error {e.status_code}: {e.message}"
    return f"Assistant error: {e}"


def _claude_loop(cfg: dict, ai_actor: Actor, can_write: bool, convo: Conversation, text: str, steps: list) -> str:
    import anthropic

    client = _claude_client(cfg)
    tool_specs = [t.spec() for t in tools.available(ai_actor)]
    convo.messages.append({"role": "user", "content": text})
    for _ in range(MAX_STEPS):
        try:
            resp = _claude_create(client, cfg, system=system_prompt(ai_actor, can_write), tools=tool_specs,
                                  messages=convo.messages)
        except anthropic.APIError as e:
            if convo.messages and convo.messages[-1]["role"] == "user" and isinstance(convo.messages[-1]["content"], str):
                convo.messages.pop()  # keep history append-only and valid for a retry
            return _claude_error(e)
        # append the full content unchanged (thinking blocks must round-trip as-is)
        convo.messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "refusal":
            return "The model declined to help with that request."
        if resp.stop_reason == "max_tokens":
            return _text(resp.content) + "\n\n_(reply was cut off)_"
        uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            return _text(resp.content)
        results = []
        for b in uses:
            out, ok = _run_tool(ai_actor, b.name, dict(b.input or {}))
            steps.append({"tool": b.name, "args": b.input, "ok": ok})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, **({} if ok else {"is_error": True})})
        convo.messages.append({"role": "user", "content": results})
    return "I stopped after several steps without finishing — try a narrower request."


def _text(content) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


# ---- Ollama -------------------------------------------------------------------------------

_tool_spec_cache: dict[tuple, list] = {}


def _function_tools(ai_actor: Actor) -> list[dict]:
    key = tuple(sorted(ai_actor.scopes))
    if key not in _tool_spec_cache:  # identical bytes every turn → prompt-cache friendly
        _tool_spec_cache[key] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                   "parameters": t.schema}}
                                 for t in tools.available(ai_actor)]
    return _tool_spec_cache[key]


def _ollama_chat(cfg: dict, messages: list, tool_specs: list | None = None, model: str | None = None,
                 think: bool | None = None, timeout: float = 600, num_ctx: int | None = None) -> dict:
    body: dict[str, Any] = {
        "model": model or cfg["model"],
        "messages": messages,
        "stream": False,
        "keep_alive": cfg.get("keep_alive", "30m"),
        "options": {"num_ctx": num_ctx or cfg["num_ctx"], "temperature": cfg.get("temperature", 0.6)},
    }
    if think is not None:
        body["think"] = think
    if tool_specs:
        body["tools"] = tool_specs
    r = httpx.post(f"{cfg['ollama_url'].rstrip('/')}/api/chat", json=body, timeout=timeout)
    if r.status_code == 400 and "think" in body and "think" in r.text.lower():
        body.pop("think")  # model without a thinking switch
        r = httpx.post(f"{cfg['ollama_url'].rstrip('/')}/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _ollama_loop(cfg: dict, ai_actor: Actor, can_write: bool, convo: Conversation, text: str, steps: list) -> str:
    if not convo.messages:
        convo.messages.append({"role": "system", "content": system_prompt(ai_actor, can_write)})
    convo.messages.append({"role": "user", "content": text})
    specs = _function_tools(ai_actor)
    for _ in range(MAX_STEPS):
        try:
            data = _ollama_chat(cfg, convo.messages, specs, think=cfg.get("think_chat", True))
        except httpx.HTTPStatusError as e:
            convo.messages.pop()
            return f"The local model returned an error ({e.response.status_code}): {e.response.text[:300]}"
        except httpx.HTTPError as e:
            convo.messages.pop()
            return f"Couldn't reach the local model at {cfg['ollama_url']} ({type(e).__name__}). Is Ollama running?"
        msg = data.get("message", {})
        # keep the model's own reasoning in history (Qwen3.8 benefits from preserved thinking)
        convo.messages.append({k: v for k, v in msg.items() if k in ("role", "content", "thinking", "tool_calls")})
        calls = msg.get("tool_calls") or []
        if not calls:
            return _strip_think(msg.get("content", ""))
        for c in calls:
            fn = c.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            out, ok = _run_tool(ai_actor, fn.get("name", ""), args)
            steps.append({"tool": fn.get("name"), "args": args, "ok": ok})
            convo.messages.append({"role": "tool", "tool_name": fn.get("name"), "content": out})
    return "I stopped after several steps without finishing — try a narrower request."


def _strip_think(s: str) -> str:
    return re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()


# ---- OpenAI-compatible local servers (vLLM, llama.cpp, LM Studio) --------------------------

def _openai_chat(cfg: dict, messages: list, tool_specs: list | None = None, model: str | None = None,
                 think: bool | None = None, timeout: float = 600) -> dict:
    body: dict[str, Any] = {"model": model or cfg["model"], "messages": messages,
                            "temperature": cfg.get("temperature", 0.6), "max_tokens": 8192}
    if tool_specs:
        body["tools"] = tool_specs
        body["tool_choice"] = "auto"
    if think is not None:
        # vLLM / SGLang pass this to the Qwen chat template
        body["chat_template_kwargs"] = {"enable_thinking": bool(think)}
    headers = {"Authorization": f"Bearer {cfg['openai_api_key']}"} if cfg.get("openai_api_key") else {}
    r = httpx.post(f"{cfg['openai_url'].rstrip('/')}/chat/completions", json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _openai_loop(cfg: dict, ai_actor: Actor, can_write: bool, convo: Conversation, text: str, steps: list) -> str:
    if not convo.messages:
        convo.messages.append({"role": "system", "content": system_prompt(ai_actor, can_write)})
    convo.messages.append({"role": "user", "content": text})
    specs = _function_tools(ai_actor)
    for _ in range(MAX_STEPS):
        try:
            data = _openai_chat(cfg, convo.messages, specs, think=cfg.get("think_chat", True))
        except httpx.HTTPStatusError as e:
            convo.messages.pop()
            return f"The model server returned an error ({e.response.status_code}): {e.response.text[:300]}"
        except httpx.HTTPError as e:
            convo.messages.pop()
            return f"Couldn't reach the model server at {cfg['openai_url']} ({type(e).__name__})."
        msg = (data.get("choices") or [{}])[0].get("message", {})
        keep = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            keep["tool_calls"] = msg["tool_calls"]
        convo.messages.append(keep)
        calls = msg.get("tool_calls") or []
        if not calls:
            return _strip_think(msg.get("content", ""))
        for c in calls:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            out, ok = _run_tool(ai_actor, fn.get("name", ""), args)
            steps.append({"tool": fn.get("name"), "args": args, "ok": ok})
            convo.messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
    return "I stopped after several steps without finishing — try a narrower request."


# ---- model server management (admin UI) -----------------------------------------------------

def server_status(cfg: dict | None = None) -> dict:
    """Reachability, installed models, and what is loaded in VRAM right now."""
    cfg = cfg or config()
    out: dict = {"provider": cfg["provider"], "model": cfg["model"], "reachable": False}
    try:
        if cfg["provider"] == "ollama":
            base = cfg["ollama_url"].rstrip("/")
            tags = httpx.get(f"{base}/api/tags", timeout=5).json()
            out["installed"] = [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
                                 "family": (m.get("details") or {}).get("family"),
                                 "quant": (m.get("details") or {}).get("quantization_level"),
                                 "params": (m.get("details") or {}).get("parameter_size")}
                                for m in tags.get("models", [])]
            ps = httpx.get(f"{base}/api/ps", timeout=5).json()
            out["loaded"] = [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
                              "vram_gb": round(m.get("size_vram", 0) / 1e9, 1),
                              "context": m.get("context_length"),
                              "on_gpu_pct": round(100 * m.get("size_vram", 0) / m["size"]) if m.get("size") else None,
                              "expires_at": m.get("expires_at")}
                             for m in ps.get("models", [])]
            try:
                out["version"] = httpx.get(f"{base}/api/version", timeout=5).json().get("version")
            except (httpx.HTTPError, ValueError):
                pass
            out["reachable"] = True
            out["model_installed"] = any(m["name"] == cfg["model"] or m["name"] == f"{cfg['model']}:latest"
                                         for m in out["installed"])
            for m in out["loaded"]:
                if m["on_gpu_pct"] is not None and m["on_gpu_pct"] < 100:
                    out.setdefault("warnings", []).append(
                        f"{m['name']} is only {m['on_gpu_pct']}% on the GPU — the rest runs on CPU and will be slow. "
                        "Lower the context size, use a smaller quant, or enable OLLAMA_KV_CACHE_TYPE=q8_0.")
        elif cfg["provider"] == "openai":
            headers = {"Authorization": f"Bearer {cfg['openai_api_key']}"} if cfg.get("openai_api_key") else {}
            r = httpx.get(f"{cfg['openai_url'].rstrip('/')}/models", headers=headers, timeout=5)
            out["installed"] = [{"name": m["id"], "context": m.get("max_model_len")} for m in r.json().get("data", [])]
            out["reachable"] = True
            out["model_installed"] = any(m["name"] == cfg["model"] for m in out["installed"])
        elif cfg["provider"] == "claude":
            out["reachable"] = bool(cfg.get("claude_api_key"))
            out["model"] = CLAUDE_MODEL
    except (httpx.HTTPError, ValueError, KeyError) as e:
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return out


def pull_model(name: str, ctx=None) -> str:
    """Download a model into Ollama (runs as a background job with progress)."""
    cfg = config()
    if not re.match(r"^[\w.\-/:]+$", name):
        raise ValueError("Invalid model name")
    with httpx.stream("POST", f"{cfg['ollama_url'].rstrip('/')}/api/pull", json={"model": name, "stream": True},
                      timeout=httpx.Timeout(30, read=None)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("error"):
                raise RuntimeError(ev["error"])
            if ctx and ev.get("total"):
                ctx.progress(ev.get("completed", 0) / ev["total"], f"{name}: {ev.get('status', '')}")
            elif ctx and ev.get("status"):
                ctx.progress(0, f"{name}: {ev['status']}")
    return f"Pulled {name}"


def unload_model(name: str | None = None) -> None:
    """Free VRAM now (e.g. before a heavy transcode)."""
    cfg = config()
    if cfg["provider"] != "ollama":
        return
    httpx.post(f"{cfg['ollama_url'].rstrip('/')}/api/generate", json={"model": name or cfg["model"], "keep_alive": 0},
               timeout=30)


def warm_up() -> None:
    """Load the model into VRAM ahead of time."""
    cfg = config()
    if cfg["provider"] == "ollama":
        httpx.post(f"{cfg['ollama_url'].rstrip('/')}/api/generate",
                   json={"model": cfg["model"], "keep_alive": cfg.get("keep_alive", "30m"),
                         "options": {"num_ctx": cfg["num_ctx"]}}, timeout=600)


# ---- video planning -------------------------------------------------------------------------

PLAN_INSTRUCTIONS = """Write a practical video plan for this project as Markdown with these sections:

# <working title>
## Hook (first 15 seconds) — which existing clips/photos (#ids) open the video and why
## Story outline — 4–8 beats, each with a target length and the #ids that cover it
## Shot list — table: Beat | Shot | Have it? (#id or "MISSING") | Notes (lens, angle, b-roll)
## Missing shots to capture — checklist, most important first, with where/when to get them
## Voice-over / talking points — a short draft script in the user's voice
## Music & sound — suggestions (mention any music/audio items in the project by #id)
## Edit notes — pacing, color, total runtime target, chapters

Only cite #ids that appear in the project listing. Be concrete and brief."""


def generate_plan(actor: Actor, project_id: int, extra: str = "") -> dict:
    cfg = config()
    outline = projects.outline_for_ai(project_id)
    if not outline:
        raise ValueError("Project not found")
    if cfg["provider"] == "none":
        plan = projects.template_plan(project_id)
        projects.save_plan(project_id, plan)
        return {"plan": plan, "provider": "template"}
    prompt = f"{outline}\n\n---\n{PLAN_INSTRUCTIONS}\n{('Extra direction from the user: ' + extra) if extra else ''}"
    with gpu_slot(interactive=True):
        if cfg["provider"] == "claude":
            import anthropic

            try:
                resp = _claude_create(_claude_client(cfg), cfg, messages=[{"role": "user", "content": prompt}])
            except anthropic.APIError as e:
                raise ValueError(_claude_error(e))
            if resp.stop_reason == "refusal":
                raise ValueError("The model declined to write this plan.")
            plan = _text(resp.content)
        else:
            try:
                if cfg["provider"] == "openai":
                    data = _openai_chat(cfg, [{"role": "user", "content": prompt}], think=cfg.get("think_plan", True))
                    plan = _strip_think((data.get("choices") or [{}])[0].get("message", {}).get("content", ""))
                else:
                    data = _ollama_chat(cfg, [{"role": "user", "content": prompt}], think=cfg.get("think_plan", True))
                    plan = _strip_think(data.get("message", {}).get("content", ""))
            except httpx.HTTPError as e:
                raise ValueError(f"Model server unavailable: {e}")
    if not plan:
        raise ValueError("The model returned an empty plan.")
    projects.save_plan(project_id, plan)
    db.audit(actor.audit_name, "project.plan_generated", project=project_id, provider=cfg["provider"])
    return {"plan": plan, "provider": cfg["provider"]}


# ---- auto-tagging --------------------------------------------------------------------------

TAG_PROMPT = ("Look at this image from a personal media library. Reply with JSON only: "
              '{"caption": "<one short sentence>", "tags": ["3-8 lowercase tags: subjects, setting, shot type '
              '(wide/close-up/aerial/interview), time of day, activity"]}')


def _small_jpeg_b64(item_id: int) -> str | None:
    tp = thumbs.thumb_path(item_id)
    if not tp.exists():
        return None
    from PIL import Image

    with Image.open(tp) as im:
        im = im.convert("RGB")
        im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def describe_image(item: dict) -> dict | None:
    """Caption + tags for one item from its thumbnail. Returns None if unavailable."""
    cfg = config()
    if cfg["provider"] == "none":
        return None
    b64 = _small_jpeg_b64(item["id"])
    if not b64:
        return None
    raw = ""
    with gpu_slot(interactive=False):
        if cfg["provider"] == "claude":
            import anthropic

            try:
                resp = _claude_create(_claude_client(cfg), cfg, messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": TAG_PROMPT}]}])
            except anthropic.APIError:
                return None
            if resp.stop_reason == "refusal":
                return None
            raw = _text(resp.content)
        elif cfg["provider"] == "openai":
            try:
                data = _openai_chat(cfg, [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": TAG_PROMPT}]}], model=cfg.get("vision_model") or None,
                    think=cfg.get("think_tag", False), timeout=180)
                raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            except httpx.HTTPError:
                return None
        else:
            try:
                data = _ollama_chat(cfg, [{"role": "user", "content": TAG_PROMPT, "images": [b64]}],
                                    model=cfg.get("vision_model") or None, think=cfg.get("think_tag", False),
                                    timeout=180, num_ctx=4096)
            except httpx.HTTPError:
                return None
            raw = data.get("message", {}).get("content", "")
    raw = _strip_think(raw)
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        parsed = json.loads(raw[start:end])
        tags = [str(t).strip().lower()[:40] for t in parsed.get("tags", []) if str(t).strip()][:8]
        return {"caption": str(parsed.get("caption", ""))[:300], "tags": tags}
    except (ValueError, json.JSONDecodeError):
        return None


def autotag(item_ids: list[int], ctx=None) -> str:
    done = 0
    for n, iid in enumerate(item_ids):
        item = db.q1("SELECT * FROM items WHERE id=?", (iid,))
        if not item or item["thumb_state"] != "ok":
            continue
        if ctx:
            ctx.progress(n / max(len(item_ids), 1), f"Tagging {item['title']}")
        res = describe_image(item)
        if not res:
            continue
        meta = json.loads(item["meta"] or "{}")
        meta["caption"] = res["caption"]
        with db.tx():
            db.x("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta, default=str), iid))
            for t in res["tags"]:
                db.x("INSERT OR IGNORE INTO tags(item_id, tag, source) VALUES(?,?, 'ai')", (iid, t))
            db.fts_index(iid)
        done += 1
    return f"Tagged {done} of {len(item_ids)} items"
