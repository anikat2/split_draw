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
            json={"inputs": "black and white pencil sketch of a cat in space, hand drawn, monochrome, simple line art, white background"},
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
                await try_broadcast_results(lobby_id)

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
    lobby["bye_player"] = None  # Track the odd-one-out player

    for i in range(0, len(players) - 1, 2):
        p1 = players[i]
        p2 = players[i + 1]
        lobby["pairs"].append((p1, p2))
        lobby["pair_state"][p1["id"]] = {"partner": p2["id"]}
        lobby["pair_state"][p2["id"]] = {"partner": p1["id"]}

    # Handle odd player: give them a "bye" — solo round where AI does both halves
    if len(players) % 2 == 1:
        bye = players[-1]
        lobby["bye_player"] = bye["id"]
        lobby["pair_state"][bye["id"]] = {"partner": None, "is_bye": True}
        print(f"[lobby] odd player count — {bye['id']} gets a bye (solo AI round)")

    prompt = random.choice(PROMPTS)
    for p in players:
        pid = p["id"]
        is_bye = lobby["pair_state"][pid].get("is_bye", False)
        await p["ws"].send_json({
            "type": "round1",
            "prompt": prompt,
            "instruction": "draw HALF of the image",
            "is_bye": is_bye,  # Client can show a note if desired
        })
        lobby["pair_state"][pid]["prompt"] = prompt

    return {"status": "round1_started"}


# ---------------- AUTO ADVANCE ENGINE ---------------- #
async def try_advance(lobby_id: str):
    lobby = lobbies[lobby_id]

    if lobby["lock"]:
        return

    lobby["lock"] = True

    try:
        if lobby["round"] == 1:
            # Bye player has no partner, so their "half" counts immediately.
            # We only wait for paired players to submit halves.
            paired_ids = set()
            for p1, p2 in lobby["pairs"]:
                paired_ids.add(p1["id"])
                paired_ids.add(p2["id"])

            paired_ready = all(
                "half" in lobby["pair_state"].get(pid, {})
                for pid in paired_ids
            )

            # Bye player: treat as ready once they submit their half (or skip waiting for them)
            bye_id = lobby.get("bye_player")
            bye_ready = (
                bye_id is None or
                "half" in lobby["pair_state"].get(bye_id, {})
            )

            if paired_ready and bye_ready:
                await start_round2(lobby_id)

        elif lobby["round"] == 2:
            # Only wait for paired players to submit completions.
            # Bye player skips round 2 (they get an AI-vs-AI vote in round 3).
            paired_ids = set()
            for p1, p2 in lobby["pairs"]:
                paired_ids.add(p1["id"])
                paired_ids.add(p2["id"])

            paired_ready = all(
                "human" in lobby["pair_state"].get(pid, {})
                for pid in paired_ids
            )

            if paired_ready:
                await start_round3(lobby_id)
                lobby["round"] = 3

    finally:
        lobby["lock"] = False


# ---------------- ROUND 2 ---------------- #
async def start_round2(lobby_id: str):
    lobby = lobbies[lobby_id]
    bye_id = lobby.get("bye_player")

    for p in lobby["players"]:
        pid = p["id"]
        state = lobby["pair_state"][pid]

        if state.get("is_bye"):
            # Bye player goes straight to a waiting screen — they skip round 2 drawing
            await p["ws"].send_json({
                "type": "round2_bye",
                "instruction": "Odd player out — sit tight while others complete their drawings!"
            })
            # Mark them as having submitted their "human" completion (empty placeholder)
            # so try_advance doesn't wait on them
            lobby["pair_state"][pid]["human"] = None
        else:
            partner = state["partner"]
            await p["ws"].send_json({
                "type": "round2",
                "partner_half": lobby["pair_state"][partner].get("half"),
                "instruction": "complete the drawing"
            })

    lobby["round"] = 2

    # Generate AI completions for all players (including bye player using their own half)
    for p in lobby["players"]:
        pid = p["id"]
        state = lobby["pair_state"][pid]
        base_prompt = state.get("prompt", "a simple scene")

        prompt = (
            f"black and white pencil sketch of {base_prompt}, "
            "hand drawn illustration, monochrome ink line art, "
            "sketchy rough style, white background, no color whatsoever, "
            "no shading, simple childlike doodle, pencil strokes visible"
        )

        if state.get("is_bye"):
            # For bye player: generate AI completion of their own half
            half = state.get("half")
            print(f"[AI] bye player {pid} — generating AI completion from their own half")
        else:
            half = state.get("half")

        print(f"[AI] generating for {pid}, prompt: {prompt[:80]}...")
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
                json={
                    "inputs": prompt,
                    "parameters": {
                        "negative_prompt": (
                            "color, colorful, photorealistic, photo, realistic, "
                            "3d render, painting, watercolor, oil painting, "
                            "detailed shading, gradient, vibrant, saturated"
                        ),
                        "num_inference_steps": 4,
                        "guidance_scale": 0.0,
                    }
                },
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


# ---------------- RESULTS ---------------- #
async def try_broadcast_results(lobby_id: str):
    lobby = lobbies[lobby_id]
    players = lobby["players"]
    votes = lobby.get("votes", {})
    pair_state = lobby["pair_state"]

    # Count voting players: bye player votes on a special AI-vs-AI card (target = bye_id),
    # all others vote on their own pair. Every player casts exactly one vote.
    total_votes = sum(v["A"] + v["B"] for v in votes.values())
    if total_votes < len(players):
        return

    results = []
    for pid, state in pair_state.items():
        target_votes = votes.get(pid, {"A": 0, "B": 0})
        human_img = state.get("human")
        ai_img = state.get("ai")
        human_is_A = state.get("human_is_A")

        if state.get("is_bye"):
            # Bye player: both options were AI — just show the result as informational
            results.append({
                "target": pid,
                "human_votes": target_votes["A"],
                "ai_votes": target_votes["B"],
                "humans_fooled": False,
                "human_img": None,
                "ai_img": ai_img,
                "is_bye": True,
            })
            continue

        if human_is_A is None:
            continue

        human_votes = target_votes["A"] if human_is_A else target_votes["B"]
        ai_votes    = target_votes["B"] if human_is_A else target_votes["A"]
        humans_fooled = ai_votes > human_votes

        results.append({
            "target": pid,
            "human_votes": human_votes,
            "ai_votes": ai_votes,
            "humans_fooled": humans_fooled,
            "human_img": human_img,
            "ai_img": ai_img,
            "is_bye": False,
        })

    for p in players:
        await p["ws"].send_json({
            "type": "results",
            "results": results,
        })


# ---------------- ROUND 3 ---------------- #
async def start_round3(lobby_id: str):
    print("starting round 3")
    lobby = lobbies[lobby_id]

    for p in lobby["players"]:
        pid = p["id"]
        state = lobby["pair_state"][pid]

        if state.get("is_bye"):
            # Bye player votes on two AI images (their own AI + a second AI generation)
            # Use the AI image twice as a placeholder — or generate a second one for fun
            ai_img = state.get("ai")

            # Generate a second distinct AI image for them to compare against
            base_prompt = state.get("prompt", "a simple scene")
            prompt2 = (
                f"black and white pencil sketch of {base_prompt}, "
                "hand drawn illustration, monochrome ink line art, simple childlike doodle"
            )
            print(f"[round3] bye player {pid}: generating second AI image for voting")
            ai_img2 = await inpaint(state.get("half"), prompt2)

            options = [ai_img, ai_img2 or ai_img]
            random.shuffle(options)
            # human_is_A is meaningless for bye, but set it so results math doesn't crash
            state["human_is_A"] = False

            await p["ws"].send_json({
                "type": "round3",
                "A": options[0],
                "B": options[1],
                "target": pid,
                "is_bye": True,
                "bye_prompt": "Which AI drawing do you prefer? (You were the odd one out this round)",
            })
            continue

        human = state.get("human")
        ai = state.get("ai")

        print(f"[round3] {pid}: human={bool(human)} ai={bool(ai)}")

        if not ai:
            partner_id = state.get("partner")
            ai = lobby["pair_state"].get(partner_id, {}).get("human")
            print(f"[round3] {pid}: AI was None, falling back to partner's drawing")

        if not human or not ai:
            print(f"[round3] {pid}: missing data after fallback, skipping")
            continue

        options = [human, ai]
        random.shuffle(options)
        human_is_A = options[0] is human
        state["human_is_A"] = human_is_A

        await p["ws"].send_json({
            "type": "round3",
            "A": options[0],
            "B": options[1],
            "target": pid,
            "is_bye": False,
        })