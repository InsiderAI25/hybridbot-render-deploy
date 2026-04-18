import httpx
import os
from datetime import datetime, timezone
from fastapi import FastAPI, Request

app = FastAPI()

OPEN_BRAIN_URL = "https://genesis-open-brain-865073155062.us-central1.run.app"
NEWTON_URL = "https://newton-ai-663026475135.us-central1.run.app"
CORE_PROJECT = "insider-core-project"
PROD_PROJECT = "genesis-ai-prod-7x2k"
CHANNEL_ID = "-1003947467895"


@app.get("/")
def index():
    return {"message": "HybridBot Render Deployment Live!"}


@app.post("/sync-agent")
async def sync_agent(request: Request):
    body = await request.json()
    agent_name = body.get("agent_name", "")
    action = body.get("action", "sync")
    confidence = body.get("confidence", 0.85)

    if not agent_name:
        return {"error": "agent_name required"}

    if confidence < 0.77:
        return {
            "approved": False,
            "reason": f"VIE confidence {confidence} below threshold 0.77",
            "agent": agent_name
        }

    brain_response = {}
    newton_response = {}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            brain_resp = await client.post(
                f"{OPEN_BRAIN_URL}/query",
                json={
                    "agent": agent_name,
                    "context": f"sync_request:{action}",
                    "project": PROD_PROJECT,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat()
                }
            )
            brain_response = brain_resp.json() if brain_resp.status_code == 200 else {"status": "unavailable"}
    except Exception:
        brain_response = {"status": "unreachable"}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            newton_resp = await client.post(
                f"{NEWTON_URL}/query",
                json={
                    "agent": agent_name,
                    "context": f"validate_sync:{action}"
                }
            )
            newton_response = newton_resp.json() if newton_resp.status_code == 200 else {"validated": False}
    except Exception:
        newton_response = {"validated": False}

    return {
        "approved": True,
        "agent": agent_name,
        "action": action,
        "vie_confidence": confidence,
        "monique_ceo_authority": "51%",
        "open_brain": brain_response,
        "newton_validated": newton_response.get("validated", False),
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "empire": {
            "core_project": CORE_PROJECT,
            "prod_project": PROD_PROJECT,
            "channel_id": CHANNEL_ID
        }
    }


@app.get("/empire-status")
async def empire_status():
    agents = [
        "sharp-og", "sharp-og-bot", "newton-ai",
        "monique-controller", "technical-master-ai",
        "vaultcast-engine-prod", "coordinator-v2",
        "vaultcast-landing", "aegis-ai-link"
    ]
    results = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for agent in agents:
            try:
                url = f"https://{agent}-663026475135.us-central1.run.app/health"
                resp = await client.get(url)
                results[agent] = {"status": resp.status_code, "project": "insider-core-project"}
            except Exception:
                try:
                    url = f"https://{agent}-865073155062.us-central1.run.app/health"
                    resp = await client.get(url)
                    results[agent] = {"status": resp.status_code, "project": "genesis-ai-prod-7x2k"}
                except Exception:
                    results[agent] = {"status": "unreachable"}
    return {
        "empire": "Genesis AI Empire",
        "monique_ceo": "51% VIE authority",
        "agents_checked": len(agents),
        "results": results,
        "timestamp": datetime.now(tz=timezone.utc).isoformat()
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
