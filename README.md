# Claude Gateway — Render Deployment

## What this is
A minimal FastAPI service implementing the exact contract the Base44 Claude Connector expects:
`POST /v1/ask`, `POST /v1/task`, `GET /v1/task/{job_id}`, `GET /health` — Bearer-token auth.

## Deploy (5 minutes, manual)
1. Push the `claude-gateway/` folder to a GitHub repo (or fork).
2. Render Dashboard → **New → Web Service** → connect the repo.
3. Runtime: **Python 3** · Build: `pip install -r requirements.txt` · Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add Environment Variables:
   - `GATEWAY_TOKEN` = the shared bearer token (must equal `CLAUDE_GATEWAY_TOKEN` in Base44)
   - `ANTHROPIC_API_KEY` = `sk-ant-...`
   - `ANTHROPIC_MODEL` = `claude-sonnet-4-5` (optional)
5. Deploy → note the URL: `https://<service>.onrender.com`
6. In Base44: update secret `CLAUDE_GATEWAY_URL` to that URL, and `CLAUDE_GATEWAY_TOKEN` to the same `GATEWAY_TOKEN`.

## Test after deploy
```bash
curl https://<service>.onrender.com/health -H "Authorization: Bearer <GATEWAY_TOKEN>"
# → {"ok": true}

curl -X POST https://<service>.onrender.com/v1/ask \
  -H "Authorization: Bearer <GATEWAY_TOKEN>" -H "Content-Type: application/json" \
  -d '{"prompt": "Say hello in Hebrew"}'
```

## Notes
- Job state is in-memory; Render free tier restarts wipe queued/running jobs (finished answers already delivered are unaffected). For durability, upgrade to a paid instance or add a DB later.
- The task loop is a multi-turn reasoning loop (no code execution / internet access) — the agent works through the task step by step and can return files.
- All four endpoints require the Bearer token. 401 otherwise.
