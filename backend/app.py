from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import random
import json
import httpx
import base64
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://split-draw.vercel.app",
        "https://split-draw-v9wv.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HF_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
HF_MODEL = "black-forest-labs/FLUX.1-schnell"

PROMPTS = [
    "a dragon flying over a city",
    "a cat in space",
    "a robot cooking dinner",
    "a floating castle",
    "a superhero at night",
    "a haunted house"
]

lobbies = {}


# ---------------- LOBBY ---------------- #
@app.get("/new_lobby_code")
def new_lobby():
    lobby_id = f"{random.randint(0,999999):06d}"
    lobbies[lobby_id] = {
        "players": [],
        "pairs": [],
        "pair_state": {},
        "round": 0,
        "lock": False
    }
    print(f"[lobby] created {lobby_id}. Active lobbies: {list(lobbies.keys())}")
    return {"lobby_id": lobby_id}


@app.get("/test_ai")
async def test_ai():
    if not HF_API_KEY:
        return {"error": "HF_API_KEY not set"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://router.huggingface.co/hf-inference/models/{HF_MODEL}",
            headers={
                "Authorization": f"Bearer {HF_API_KEY}",
                "X-Wait-For-Model": "true",
                "X-Use-Cache": "0",
            },
            json={"inputs": "a cat in space"},
        )
        return {
            "status": r.status_code,
            "body": r.text[:1000],
            "model": HF_MODEL,
            "key_prefix": HF_API_KEY[:8] if HF_API_KEY else None,
        }


# ---------------- WEBSOCKET ---------------- #
@app.websocket("/ws/{lobby_id}/{user_id}")
async def ws(websocket: WebSocket, lobby_id: str, user_id: str):
    # FIX: auto-create lobby if it's missing (handles server restarts / joining before host)
    if lobby_id not in lobbies:
        print(f"[ws] lobby {lobby_id} not found — creating on connect for {user_id}")
        lobbies[lobby_id] = {
            "players": [],
            "pairs": [],
            "pair_state": {},
            "round": 0,
            "lock": False
        }

    await websocket.accept()

    lobby = lobbies[lobby_id]

    # FIX: don't add duplicate player if they reconnect
    existing = next((p for p in lobby["players"] if p["id"] == user_id), None)
    if existing:
        existing["ws"] = websocket
        print(f"[ws] {user_id} reconnected to {lobby_id}")
    else:
        lobby["players"].append({"id": user_id, "ws": websocket})
        print(f"[ws] {user_id} joined {lobby_id}. Players: {[p['id'] for p in lobby['players']]}")

    lobby["pair_state"].setdefault(user_id, {})

    try:
        while True:
            msg = json.loads(await websocket.receive_text())

            if msg["type"] == "half_draw":
                lobby["pair_state"][user_id]["half"] = msg["image"]
                await try_advance(lobby_id)

            elif msg["type"] == "completion":
                lobby["pair_state"][user_id]["human"] = msg["image"]
                await try_advance(lobby_id)

            elif msg["type"] == "vote":
                lobby.setdefault("votes", {})
                lobby["votes"].setdefault(msg["target"], {"A": 0, "B": 0})
                lobby["votes"][msg["target"]][msg["choice"]] += 1

    except WebSocketDisconnect:
        lobby["players"] = [p for p in lobby["players"] if p["id"] != user_id]
        print(f"[ws] {user_id} disconnected from {lobby_id}")


# ---------------- START GAME ---------------- #
@app.get("/begin_round/{lobby_id}")
async def begin(lobby_id: str):
    if lobby_id not in lobbies:
        raise HTTPException(status_code=404, detail="Lobby not found")

    lobby = lobbies[lobby_id]
    players = lobby["players"]

    if len(players) < 2:
        return {"error": "not enough players"}

    lobby["round"] = 1
    random.shuffle(players)
    lobby["pairs"] = []
    lobby["pair_state"] = {}

    for i in range(0, len(players) - 1, 2):
        p1 = players[i]
        p2 = players[i + 1]
        lobby["pairs"].append((p1, p2))
        lobby["pair_state"][p1["id"]] = {"partner": p2["id"]}
        lobby["pair_state"][p2["id"]] = {"partner": p1["id"]}

    prompt = random.choice(PROMPTS)
    for p in players:
        await p["ws"].send_json({
            "type": "round1",
            "prompt": prompt,
            "instruction": "draw HALF of the image"
        })
        # store prompt per player so AI can use it later
        lobby["pair_state"][p["id"]]["prompt"] = prompt

    return {"status": "round1_started"}


# ---------------- AUTO ADVANCE ENGINE ---------------- #
async def try_advance(lobby_id: str):
    lobby = lobbies[lobby_id]

    if lobby["lock"]:
        return

    lobby["lock"] = True

    try:
        if lobby["round"] == 1:
            if all("half" in p for p in lobby["pair_state"].values()):
                await start_round2(lobby_id)

        elif lobby["round"] == 2:
            if all("human" in p for p in lobby["pair_state"].values()):
                await start_round3(lobby_id)
                lobby["round"] = 3

    finally:
        lobby["lock"] = False


# ---------------- ROUND 2 ---------------- #
async def start_round2(lobby_id: str):
    lobby = lobbies[lobby_id]

    for p in lobby["players"]:
        pid = p["id"]
        partner = lobby["pair_state"][pid]["partner"]
        await p["ws"].send_json({
            "type": "round2",
            "partner_half": lobby["pair_state"][partner].get("half"),
            "instruction": "complete the drawing"
        })

    lobby["round"] = 2

    for p in lobby["players"]:
        pid = p["id"]
        half = lobby["pair_state"][pid].get("half")
        prompt = lobby["pair_state"][pid].get("prompt", "complete this drawing in the same hand-drawn style, only use black and white")

        print(f"[AI] generating for {pid}, image present: {bool(half)}")
        ai = await inpaint(half, prompt)
        print(f"[AI] result for {pid}: {'OK' if ai else 'FAILED'}")
        lobby["pair_state"][pid]["ai"] = ai

    await try_advance(lobby_id)


# ---------------- AI ---------------- #
async def inpaint(image_b64: str, prompt: str):
    if not HF_API_KEY:
        print("[AI] HF_API_KEY not set")
        return None

    url = f"https://router.huggingface.co/hf-inference/models/{HF_MODEL}"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {HF_API_KEY}",
                    "X-Wait-For-Model": "true",
                    "X-Use-Cache": "0",
                },
                json={"inputs": prompt},
            )
            print(f"[AI] HF status: {r.status_code}")
            if r.status_code != 200:
                print(f"[AI] HF error: {r.text[:500]}")
                return None
            return base64.b64encode(r.content).decode()

    except httpx.TimeoutException:
        print("[AI] timed out after 120s")
        return None
    except Exception as e:
        print(f"[AI] unexpected error: {e}")
        return None


# ---------------- ROUND 3 ---------------- #
async def start_round3(lobby_id: str):
    print("starting round 3")
    lobby = lobbies[lobby_id]

    for p in lobby["players"]:
        pid = p["id"]
        human = lobby["pair_state"][pid].get("human")
        ai = lobby["pair_state"][pid].get("ai")

        print(f"[round3] {pid}: human={bool(human)} ai={bool(ai)}")

        if not ai:
            partner_id = lobby["pair_state"][pid].get("partner")
            ai = lobby["pair_state"].get(partner_id, {}).get("human")
            print(f"[round3] {pid}: AI was None, falling back to partner's drawing")

        if not human or not ai:
            print(f"[round3] {pid}: missing data after fallback, skipping")
            continue

        options = [human, ai]
        random.shuffle(options)

        await p["ws"].send_json({
            "type": "round3",
            "A": options[0],
            "B": options[1],
            "target": pid
        })