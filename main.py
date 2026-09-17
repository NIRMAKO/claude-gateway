"""
Claude Gateway — NIRMAKO
Implements the exact contract consumed by the Base44 Claude Connector:
  POST /v1/ask          { prompt, system?, messages?, model?, max_tokens? } -> { answer, model, usage }
  POST /v1/task         { task, context?, files?, max_turns? }             -> { job_id, status: "queued" }
  GET  /v1/task/{id}    -> { status, result?, logs?, files?, error? }
  GET  /health          -> { ok: true }

Auth: every request must send  Authorization: Bearer <GATEWAY_TOKEN>
Env vars (set in Render dashboard):
  GATEWAY_TOKEN     — shared bearer token (must equal CLAUDE_GATEWAY_TOKEN in Base44)
  ANTHROPIC_API_KEY — Anthropic API key (sk-ant-...)
  ANTHROPIC_MODEL   — optional, default "claude-sonnet-4-5"

Note: job state is in-memory. On Render free tier the service sleeps and
restarts, which wipes running jobs — finished answers that were already
fetched by the client are unaffected.
"""
import os
import time
import uuid
import threading
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MAX_PROMPT_CHARS = 100_000

app = FastAPI(title="Claude Gateway", version="1.0.0")
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


# ---------- auth ----------

def check_auth(request: Request):
    auth = request.headers.get("authorization", "")
    if not GATEWAY_TOKEN or auth != f"Bearer {GATEWAY_TOKEN}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


# ---------- anthropic ----------

def call_anthropic(messages: List[Dict[str, str]], system: Optional[str],
                   model: str, max_tokens: int) -> Dict[str, Any]:
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: Dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system
    with httpx.Client(timeout=300) as client:
        r = client.post(ANTHROPIC_URL, headers=headers, json=body)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"Anthropic error: {r.text[:500]}")
    data = r.json()
    return {
        "answer": "".join(b.get("text", "") for b in data.get("content", [])),
        "model": data.get("model", model),
        "usage": {
            "input_tokens": data.get("usage", {}).get("input_tokens"),
            "output_tokens": data.get("usage", {}).get("output_tokens"),
        },
    }


# ---------- schemas ----------

class AskBody(BaseModel):
    prompt: Optional[str] = None
    system: Optional[str] = None
    messages: Optional[List[Dict[str, str]]] = None
    model: Optional[str] = None
    max_tokens: Optional[int] = None


class FileIn(BaseModel):
    name: str
    content: str


class TaskBody(BaseModel):
    task: str
    context: Optional[str] = None
    files: Optional[List[FileIn]] = None
    max_turns: Optional[int] = 20


# ---------- /v1/ask ----------

@app.post("/v1/ask")
def ask(body: AskBody, request: Request):
    check_auth(request)
    if not API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    if not body.prompt and not body.messages:
        raise HTTPException(status_code=400, detail="'prompt' or 'messages' is required")
    text = body.prompt or str(body.messages)
    if len(text) > MAX_PROMPT_CHARS:
        raise HTTPException(status_code=400, detail=f"Prompt too large: {len(text)} chars (max {MAX_PROMPT_CHARS})")

    messages = body.messages or [{"role": "user", "content": body.prompt}]
    result = call_anthropic(
        messages,
        body.system,
        body.model or DEFAULT_MODEL,
        min(body.max_tokens or 4096, 8192),
    )
    return result


# ---------- /v1/task ----------

TASK_SYSTEM = """You are Claude running as an autonomous task agent.
Work through the user's task step by step across multiple turns.
Each turn: report progress in one short line, then continue.
When the task is complete, output exactly:
TASK_COMPLETE
followed by the final result.
If the task requires producing files, output them at the end as a fenced JSON array:
<<<FILES>>>
[{"name": "filename", "content": "file content"}]
<<<END FILES>>>
You cannot execute code or access the internet — you reason, write, transform and analyze text and provided files."""


def run_task(job_id: str, body: TaskBody):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    logs: List[str] = []
    try:
        parts: List[str] = [f"TASK:\n{body.task}"]
        if body.context:
            parts.append(f"CONTEXT:\n{body.context}")
        if body.files:
            parts.append("FILES:\n" + "\n".join(f"--- {f.name} ---\n{f.content}" for f in body.files))
        messages: List[Dict[str, str]] = [{"role": "user", "content": "\n\n".join(parts)}]
        result_text = ""
        for turn in range(1, body.max_turns + 1):
            logs.append(f"turn {turn}: thinking...")
            resp = call_anthropic(messages, TASK_SYSTEM, DEFAULT_MODEL, 4096)
            answer = resp["answer"]
            result_text = answer
            logs.append(f"turn {turn}: {len(answer)} chars")
            messages.append({"role": "assistant", "content": answer})
            if "TASK_COMPLETE" in answer:
                break
            messages.append({"role": "user", "content": "Continue. Finish with TASK_COMPLETE when done."})

        # extract files if present
        files = []
        if "<<<FILES>>>" in result_text:
            try:
                raw = result_text.split("<<<FILES>>>")[1].split("<<<END FILES>>>")[0].strip()
                if raw.startswith("```"):
                    raw = raw.strip("`").lstrip("json").strip()
                import json
                files = [{"name": f["name"], "content": f["content"]} for f in json.loads(raw)]
                result_text = result_text.split("<<<FILES>>>")[0].strip()
            except Exception:
                pass

        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "done",
                "result": result_text.replace("TASK_COMPLETE", "").strip(),
                "logs": logs,
                "files": files,
            })
    except Exception as e:  # noqa: BLE001
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": str(e)[:500], "logs": logs})


@app.post("/v1/task")
def create_task(body: TaskBody, request: Request):
    check_auth(request)
    if not API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    if not body.task:
        raise HTTPException(status_code=400, detail="'task' is required")
    if len(body.task) > MAX_PROMPT_CHARS:
        raise HTTPException(status_code=400, detail=f"Task too large: {len(body.task)} chars")

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "logs": [], "files": [], "result": None,
                        "error": None, "created": time.time()}
    threading.Thread(target=run_task, args=(job_id, body), daemon=True).start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/v1/task/{job_id}")
def task_status(job_id: str, request: Request):
    check_auth(request)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found (may have been restarted)")
    out = {"status": job["status"]}
    if job["status"] == "done":
        out["result"] = job["result"]
    if job.get("logs"):
        out["logs"] = job["logs"]
    if job.get("files"):
        out["files"] = job["files"]
    if job["status"] == "error":
        out["error"] = job["error"]
    return out


@app.get("/health")
def health(request: Request):
    check_auth(request)
    return {"ok": True}


@app.get("/")
def root(request: Request):
    return {"service": "claude-gateway", "ok": True}
