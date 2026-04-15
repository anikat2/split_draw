from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

HF_API_KEY = os.getenv("HUGGINGFACE_API_KEY")

# image-to-image model — takes an image + prompt, returns a completion
# no mask_image required unlike inpainting models
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
# -- websocket -- #
@app.websocket("/ws/{lobby_id}/{user_id}")
async def ws(websocket: WebSocket, lobby_id: str, user_id: str):
    await websocket.accept()

    lobby = lobbies[lobby_id]
    lobby["players"].append({"id": user_id, "ws": websocket})
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


# ---------------- START GAME ---------------- #
@app.get("/begin_round/{lobby_id}")
async def begin(lobby_id: str):
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

    for p in players:
        await p["ws"].send_json({
            "type": "round1",
            "prompt": random.choice(PROMPTS),
            "instruction": "draw HALF of the image"
        })

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

    # Generate AI completions for all players
    for p in lobby["players"]:
        pid = p["id"]
        half = lobby["pair_state"][pid].get("half")
        prompt = lobby["pair_state"][pid].get("prompt", "draw the other half of this in the same style only in black white background make it look hand drawn")

        print(f"[AI] generating for {pid}, image present: {bool(half)}")
        ai = await inpaint(half, prompt)
        print(f"[AI] result for {pid}: {'OK' if ai else 'FAILED'}")

        lobby["pair_state"][pid]["ai"] = ai

    # AI done — check if humans also submitted, fire round 3 if so
    await try_advance(lobby_id)


# ---------------- AI ---------------- #
async def inpaint(image_b64: str, prompt: str):
    """
    Text-to-image via HF serverless inference API.
    The API expects JSON: {"inputs": "prompt text"}
    and returns raw image bytes on 200.
    """
    if not HF_API_KEY:
        print("[AI] HF_API_KEY not set — check your .env file")
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
        print("[AI] timed out after 120s — model may be cold, try again")
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

        # Graceful fallback: if AI failed, use the partner's human completion
        # so the vote can still happen
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