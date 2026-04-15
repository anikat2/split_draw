import { useState, useEffect, useRef, useCallback } from "react";

const WS_BASE = "ws://https://split-draw-v9wv.vercel.app//ws";
const API_BASE = "https://split-draw-v9wv.vercel.app"

const ROUND_TIME = 60;

const palette = ["#1a1a1a","#ffffff","#e63946","#2a9d8f","#e9c46a","#457b9d","#f4a261","#8338ec","#06d6a0","#fb5607"];

function generateUserId() {
  return Math.random().toString(36).slice(2, 8);
}

export default function App() {
  const [userId] = useState(generateUserId);
  const [screen, setScreen] = useState("home");
  const [lobbyId, setLobbyId] = useState("");
  const [joinInput, setJoinInput] = useState("");
  const [gameState, setGameState] = useState(null);
  const [error, setError] = useState("");
  const wsRef = useRef(null);

  const connectWs = useCallback((lid, uid) => {
    const ws = new WebSocket(`${WS_BASE}/${lid}/${uid}`);
    wsRef.current = ws;

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === "round1") {
        setGameState({ phase: "round1", prompt: msg.prompt, instruction: msg.instruction });
        setScreen("round1");
      } else if (msg.type === "round2") {
        setGameState(prev => ({ ...prev, phase: "round2", partnerHalf: msg.partner_half, instruction: msg.instruction }));
        setScreen("round2");
      } else if (msg.type === "round3") {
        setGameState(prev => ({ ...prev, phase: "round3", optionA: msg.A, optionB: msg.B, target: msg.target }));
        setScreen("round3");
      }
    };

    ws.onerror = () => setError("Connection failed");
  }, []);

  async function createLobby() {
    try {
      const res = await fetch(`${API_BASE}/new_lobby_code`);
      const data = await res.json();
      setLobbyId(data.lobby_id);
      connectWs(data.lobby_id, userId);
      setScreen("lobby");
    } catch {
      setError("Could not reach server");
    }
  }

  function joinLobby() {
    if (!joinInput.trim()) return;
    const lid = joinInput.trim();
    setLobbyId(lid);
    connectWs(lid, userId);
    setScreen("lobby");
  }

  async function startGame() {
    await fetch(`${API_BASE}/begin_round/${lobbyId}`);
  }

  function sendWs(msg) {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }

  return (
    <div style={styles.root}>
      <div style={styles.grain} />
      {screen === "home" && <HomeScreen onCreate={createLobby} onJoin={joinLobby} joinInput={joinInput} setJoinInput={setJoinInput} error={error} />}
      {screen === "lobby" && <LobbyScreen lobbyId={lobbyId} userId={userId} onStart={startGame} />}
      {screen === "round1" && <Round1Screen gameState={gameState} sendWs={sendWs} />}
      {screen === "round2" && <Round2Screen gameState={gameState} sendWs={sendWs} />}
      {screen === "round3" && <Round3Screen gameState={gameState} sendWs={sendWs} />}
    </div>
  );
}

function HomeScreen({ onCreate, onJoin, joinInput, setJoinInput, error }) {
  return (
    <div style={styles.centerPage}>
      <div style={styles.homeCard}>
        <div style={styles.logoMark}>✦</div>
        <h1 style={styles.title}>Split Draw</h1>
        <p style={styles.subtitle}>draw half · trust a stranger · see what AI thinks you meant</p>
        <div style={styles.divider} />
        <button style={styles.btnPrimary} onClick={onCreate}>create lobby</button>
        <div style={styles.joinRow}>
          <input
            style={styles.input}
            placeholder="lobby code"
            value={joinInput}
            onChange={e => setJoinInput(e.target.value.toUpperCase())}
            maxLength={6}
            onKeyDown={e => e.key === "Enter" && onJoin()}
          />
          <button style={styles.btnSecondary} onClick={onJoin}>join</button>
        </div>
        {error && <p style={styles.errorText}>{error}</p>}
      </div>
    </div>
  );
}

function LobbyScreen({ lobbyId, userId, onStart }) {
  return (
    <div style={styles.centerPage}>
      <div style={styles.homeCard}>
        <div style={styles.logoMark}>◈</div>
        <p style={styles.labelSmall}>lobby code</p>
        <h2 style={styles.lobbyCode}>{lobbyId}</h2>
        <p style={styles.subtitle}>share this code · wait for others to join</p>
        <div style={styles.divider} />
        <p style={styles.labelSmall}>your id: <span style={{color:"#e9c46a"}}>{userId}</span></p>
        <button style={{...styles.btnPrimary, marginTop: "1.5rem"}} onClick={onStart}>start game →</button>
        <p style={{...styles.subtitle, marginTop: "0.75rem", fontSize: "12px", opacity: 0.5}}>only the host needs to click start</p>
      </div>
    </div>
  );
}

function Round1Screen({ gameState, sendWs }) {
  const canvasRef = useRef(null);
  const [drawing, setDrawing] = useState(false);
  const [color, setColor] = useState("#1a1a1a");
  const [lineWidth, setLineWidth] = useState(4);
  const [timeLeft, setTimeLeft] = useState(ROUND_TIME);
  const [submitted, setSubmitted] = useState(false);
  const lastPos = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#f9f5f0";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#ccc";
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(canvas.width / 2, 0);
    ctx.lineTo(canvas.width / 2, canvas.height);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(0,0,0,0.08)";
    ctx.font = "11px monospace";
    ctx.textAlign = "center";
    ctx.fillText("← draw your half here", canvas.width / 4, 24);
  }, []);

  useEffect(() => {
    if (submitted) return;
    const t = setInterval(() => {
      setTimeLeft(p => {
        if (p <= 1) { clearInterval(t); handleSubmit(); return 0; }
        return p - 1;
      });
    }, 1000);
    return () => clearInterval(t);
  }, [submitted]);

  function getPos(e, canvas) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    if (e.touches) {
      return { x: (e.touches[0].clientX - rect.left) * scaleX, y: (e.touches[0].clientY - rect.top) * scaleY };
    }
    return { x: (e.clientX - rect.left) * scaleX, y: (e.clientY - rect.top) * scaleY };
  }

  function startDraw(e) {
    e.preventDefault();
    setDrawing(true);
    lastPos.current = getPos(e, canvasRef.current);
  }

  function draw(e) {
    e.preventDefault();
    if (!drawing) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    const pos = getPos(e, canvas);
    ctx.strokeStyle = color;
    ctx.lineWidth = lineWidth;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(lastPos.current.x, lastPos.current.y);
    ctx.lineTo(pos.x, pos.y);
    ctx.stroke();
    lastPos.current = pos;
  }

  function stopDraw() { setDrawing(false); }

  function handleSubmit() {
    if (submitted) return;
    setSubmitted(true);
    const canvas = canvasRef.current;
    const dataUrl = canvas.toDataURL("image/png");
    const base64 = dataUrl.split(",")[1];
    sendWs({ type: "half_draw", image: base64 });
  }

  function clearCanvas() {
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#f9f5f0";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#ccc";
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(canvas.width / 2, 0);
    ctx.lineTo(canvas.width / 2, canvas.height);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(0,0,0,0.08)";
    ctx.font = "11px monospace";
    ctx.textAlign = "center";
    ctx.fillText("← draw your half here", canvas.width / 4, 24);
  }

  const timerColor = timeLeft < 10 ? "#e63946" : timeLeft < 20 ? "#f4a261" : "#2a9d8f";

  return (
    <div style={styles.gamePage}>
      <div style={styles.gameHeader}>
        <div style={styles.roundBadge}>ROUND 1</div>
        <div style={styles.prompt}>"{gameState?.prompt}"</div>
        <div style={{...styles.timer, color: timerColor}}>{timeLeft}s</div>
      </div>
      <p style={styles.instruction}>draw the LEFT half only — your partner draws the right</p>
      <div style={styles.canvasWrapper}>
        <canvas
          ref={canvasRef}
          width={600}
          height={400}
          style={styles.canvas}
          onMouseDown={startDraw}
          onMouseMove={draw}
          onMouseUp={stopDraw}
          onMouseLeave={stopDraw}
          onTouchStart={startDraw}
          onTouchMove={draw}
          onTouchEnd={stopDraw}
        />
      </div>
      <div style={styles.toolbar}>
        <div style={styles.paletteRow}>
          {palette.map(c => (
            <button key={c} onClick={() => setColor(c)} style={{
              ...styles.colorSwatch,
              background: c,
              outline: color === c ? `3px solid #e9c46a` : "none",
              outlineOffset: "2px"
            }} />
          ))}
        </div>
        <div style={styles.toolRow}>
          <label style={styles.labelSmall}>size</label>
          <input type="range" min="1" max="24" value={lineWidth} onChange={e => setLineWidth(+e.target.value)} style={{width: "100px"}} />
          <span style={{...styles.labelSmall, minWidth: "24px"}}>{lineWidth}</span>
          <button style={styles.btnTool} onClick={clearCanvas}>clear</button>
          {!submitted
            ? <button style={styles.btnPrimary} onClick={handleSubmit}>submit half →</button>
            : <div style={styles.waitBadge}>⏳ waiting for partner…</div>
          }
        </div>
      </div>
    </div>
  );
}

function Round2Screen({ gameState, sendWs }) {
  const canvasRef = useRef(null);
  const [drawing, setDrawing] = useState(false);
  const [color, setColor] = useState("#1a1a1a");
  const [lineWidth, setLineWidth] = useState(4);
  const [timeLeft, setTimeLeft] = useState(ROUND_TIME);
  const [submitted, setSubmitted] = useState(false);
  const lastPos = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#f9f5f0";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    if (gameState?.partnerHalf) {
      const img = new Image();
      img.onload = () => {
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        ctx.strokeStyle = "#ccc";
        ctx.setLineDash([6, 4]);
        ctx.beginPath();
        ctx.moveTo(canvas.width / 2, 0);
        ctx.lineTo(canvas.width / 2, canvas.height);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "rgba(0,0,0,0.08)";
        ctx.font = "11px monospace";
        ctx.textAlign = "center";
        ctx.fillText("→ complete here", (canvas.width * 3) / 4, 24);
      };
      img.src = `data:image/png;base64,${gameState.partnerHalf}`;
    } else {
      ctx.strokeStyle = "#ccc";
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(canvas.width / 2, 0);
      ctx.lineTo(canvas.width / 2, canvas.height);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }, [gameState?.partnerHalf]);

  useEffect(() => {
    if (submitted) return;
    const t = setInterval(() => {
      setTimeLeft(p => {
        if (p <= 1) { clearInterval(t); handleSubmit(); return 0; }
        return p - 1;
      });
    }, 1000);
    return () => clearInterval(t);
  }, [submitted]);

  function getPos(e, canvas) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    if (e.touches) {
      return { x: (e.touches[0].clientX - rect.left) * scaleX, y: (e.touches[0].clientY - rect.top) * scaleY };
    }
    return { x: (e.clientX - rect.left) * scaleX, y: (e.clientY - rect.top) * scaleY };
  }

  function startDraw(e) {
    e.preventDefault();
    setDrawing(true);
    lastPos.current = getPos(e, canvasRef.current);
  }

  function draw(e) {
    e.preventDefault();
    if (!drawing) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    const pos = getPos(e, canvas);
    ctx.strokeStyle = color;
    ctx.lineWidth = lineWidth;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(lastPos.current.x, lastPos.current.y);
    ctx.lineTo(pos.x, pos.y);
    ctx.stroke();
    lastPos.current = pos;
  }

  function stopDraw() { setDrawing(false); }

  function handleSubmit() {
    if (submitted) return;
    setSubmitted(true);
    const canvas = canvasRef.current;
    const dataUrl = canvas.toDataURL("image/png");
    const base64 = dataUrl.split(",")[1];
    sendWs({ type: "completion", image: base64 });
  }

  const timerColor = timeLeft < 10 ? "#e63946" : timeLeft < 20 ? "#f4a261" : "#2a9d8f";

  return (
    <div style={styles.gamePage}>
      <div style={styles.gameHeader}>
        <div style={{...styles.roundBadge, background: "#457b9d"}}>ROUND 2</div>
        <div style={styles.prompt}>complete the drawing</div>
        <div style={{...styles.timer, color: timerColor}}>{timeLeft}s</div>
      </div>
      <p style={styles.instruction}>your partner drew the left — finish the right side · AI is also completing it simultaneously</p>
      <div style={styles.canvasWrapper}>
        <canvas
          ref={canvasRef}
          width={600}
          height={400}
          style={styles.canvas}
          onMouseDown={startDraw}
          onMouseMove={draw}
          onMouseUp={stopDraw}
          onMouseLeave={stopDraw}
          onTouchStart={startDraw}
          onTouchMove={draw}
          onTouchEnd={stopDraw}
        />
      </div>
      <div style={styles.toolbar}>
        <div style={styles.paletteRow}>
          {palette.map(c => (
            <button key={c} onClick={() => setColor(c)} style={{
              ...styles.colorSwatch,
              background: c,
              outline: color === c ? `3px solid #e9c46a` : "none",
              outlineOffset: "2px"
            }} />
          ))}
        </div>
        <div style={styles.toolRow}>
          <label style={styles.labelSmall}>size</label>
          <input type="range" min="1" max="24" value={lineWidth} onChange={e => setLineWidth(+e.target.value)} style={{width: "100px"}} />
          <span style={{...styles.labelSmall, minWidth: "24px"}}>{lineWidth}</span>
          {!submitted
            ? <button style={styles.btnPrimary} onClick={handleSubmit}>submit →</button>
            : <div style={styles.waitBadge}>⏳ waiting for AI + results…</div>
          }
        </div>
      </div>
    </div>
  );
}

function Round3Screen({ gameState, sendWs }) {
  const [voted, setVoted] = useState(null);

  function vote(choice) {
    if (voted) return;
    setVoted(choice);
    sendWs({ type: "vote", target: gameState.target, choice });
  }

  return (
    <div style={styles.gamePage}>
      <div style={styles.gameHeader}>
        <div style={{...styles.roundBadge, background: "#8338ec"}}>ROUND 3</div>
        <div style={styles.prompt}>which completion is human-made?</div>
        <div style={styles.timer} />
      </div>
      <p style={styles.instruction}>one was drawn by your partner · one was generated by AI · can you tell the difference?</p>
      <div style={styles.voteGrid}>
        <VoteCard
          label="A"
          image={gameState?.optionA}
          chosen={voted === "A"}
          disabled={!!voted}
          onVote={() => vote("A")}
        />
        <VoteCard
          label="B"
          image={gameState?.optionB}
          chosen={voted === "B"}
          disabled={!!voted}
          onVote={() => vote("B")}
        />
      </div>
      {voted && (
        <div style={styles.votedBanner}>
          voted {voted} — waiting for others…
        </div>
      )}
    </div>
  );
}

function VoteCard({ label, image, chosen, disabled, onVote }) {
  return (
    <div style={{
      ...styles.voteCard,
      outline: chosen ? "3px solid #e9c46a" : "3px solid transparent",
      opacity: disabled && !chosen ? 0.6 : 1,
      cursor: disabled ? "default" : "pointer",
    }} onClick={!disabled ? onVote : undefined}>
      {image
        ? <img src={`data:image/png;base64,${image}`} alt={`Option ${label}`} style={styles.voteImg} />
        : <div style={styles.voteImgPlaceholder}>loading…</div>
      }
      <div style={{
        ...styles.voteLabel,
        background: chosen ? "#e9c46a" : "rgba(0,0,0,0.75)",
        color: chosen ? "#1a1a1a" : "#fff",
      }}>
        {chosen ? "✓ " : ""}{label}
      </div>
    </div>
  );
}

const styles = {
  root: {
    minHeight: "100vh",
    background: "#0f0e0d",
    color: "#f9f5f0",
    fontFamily: "'Courier New', Courier, monospace",
    position: "relative",
    overflow: "hidden",
  },
  grain: {
    position: "fixed",
    inset: 0,
    backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E")`,
    backgroundSize: "256px 256px",
    pointerEvents: "none",
    zIndex: 0,
  },
  centerPage: {
    minHeight: "100vh",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: "2rem",
    position: "relative",
    zIndex: 1,
  },
  homeCard: {
    background: "#1a1916",
    border: "1px solid #333",
    borderRadius: "4px",
    padding: "2.5rem 2rem",
    maxWidth: "380px",
    width: "100%",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: "0.75rem",
  },
  logoMark: {
    fontSize: "28px",
    color: "#e9c46a",
    lineHeight: 1,
  },
  title: {
    fontSize: "32px",
    fontWeight: "700",
    letterSpacing: "0.2em",
    color: "#f9f5f0",
    margin: 0,
    fontFamily: "'Courier New', Courier, monospace",
  },
  subtitle: {
    fontSize: "12px",
    color: "#888",
    textAlign: "center",
    lineHeight: 1.6,
    margin: 0,
    fontFamily: "'Courier New', Courier, monospace",
  },
  divider: {
    width: "100%",
    height: "1px",
    background: "#333",
    margin: "0.5rem 0",
  },
  btnPrimary: {
    background: "#e9c46a",
    color: "#1a1a1a",
    border: "none",
    borderRadius: "2px",
    padding: "10px 24px",
    fontFamily: "'Courier New', Courier, monospace",
    fontWeight: "700",
    fontSize: "13px",
    letterSpacing: "0.05em",
    cursor: "pointer",
    width: "100%",
    transition: "opacity 0.15s",
  },
  btnSecondary: {
    background: "transparent",
    color: "#f9f5f0",
    border: "1px solid #555",
    borderRadius: "2px",
    padding: "10px 16px",
    fontFamily: "'Courier New', Courier, monospace",
    fontSize: "13px",
    cursor: "pointer",
    flexShrink: 0,
  },
  btnTool: {
    background: "transparent",
    color: "#aaa",
    border: "1px solid #444",
    borderRadius: "2px",
    padding: "6px 12px",
    fontFamily: "'Courier New', Courier, monospace",
    fontSize: "12px",
    cursor: "pointer",
  },
  input: {
    background: "#111",
    color: "#f9f5f0",
    border: "1px solid #444",
    borderRadius: "2px",
    padding: "10px 12px",
    fontFamily: "'Courier New', Courier, monospace",
    fontSize: "16px",
    letterSpacing: "0.15em",
    flex: 1,
    outline: "none",
  },
  joinRow: {
    display: "flex",
    gap: "8px",
    width: "100%",
  },
  errorText: {
    color: "#e63946",
    fontSize: "12px",
    margin: 0,
  },
  labelSmall: {
    fontSize: "11px",
    color: "#888",
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    margin: 0,
    fontFamily: "'Courier New', Courier, monospace",
  },
  lobbyCode: {
    fontSize: "42px",
    fontWeight: "700",
    letterSpacing: "0.3em",
    color: "#e9c46a",
    margin: "0.25rem 0",
    fontFamily: "'Courier New', Courier, monospace",
  },
  gamePage: {
    minHeight: "100vh",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    padding: "1.5rem 1rem",
    position: "relative",
    zIndex: 1,
    gap: "0.75rem",
  },
  gameHeader: {
    display: "flex",
    alignItems: "center",
    gap: "1rem",
    width: "100%",
    maxWidth: "660px",
    flexWrap: "wrap",
  },
  roundBadge: {
    background: "#2a9d8f",
    color: "#fff",
    fontSize: "11px",
    fontWeight: "700",
    letterSpacing: "0.1em",
    padding: "4px 10px",
    borderRadius: "2px",
    flexShrink: 0,
    fontFamily: "'Courier New', Courier, monospace",
  },
  prompt: {
    flex: 1,
    fontSize: "15px",
    color: "#f9f5f0",
    fontStyle: "italic",
    fontFamily: "'Courier New', Courier, monospace",
  },
  timer: {
    fontSize: "22px",
    fontWeight: "700",
    fontFamily: "'Courier New', Courier, monospace",
    minWidth: "44px",
    textAlign: "right",
  },
  instruction: {
    fontSize: "12px",
    color: "#777",
    textAlign: "center",
    margin: 0,
    maxWidth: "500px",
    fontFamily: "'Courier New', Courier, monospace",
  },
  canvasWrapper: {
    width: "100%",
    maxWidth: "660px",
    border: "1px solid #333",
    borderRadius: "2px",
    overflow: "hidden",
    lineHeight: 0,
  },
  canvas: {
    width: "100%",
    height: "auto",
    display: "block",
    touchAction: "none",
    cursor: "crosshair",
    background: "#f9f5f0",
  },
  toolbar: {
    width: "100%",
    maxWidth: "660px",
    background: "#1a1916",
    border: "1px solid #333",
    borderRadius: "2px",
    padding: "0.75rem 1rem",
    display: "flex",
    flexDirection: "column",
    gap: "0.5rem",
  },
  paletteRow: {
    display: "flex",
    gap: "6px",
    flexWrap: "wrap",
  },
  colorSwatch: {
    width: "24px",
    height: "24px",
    border: "1px solid #555",
    borderRadius: "2px",
    cursor: "pointer",
    padding: 0,
    flexShrink: 0,
  },
  toolRow: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    flexWrap: "wrap",
  },
  waitBadge: {
    fontSize: "12px",
    color: "#e9c46a",
    fontFamily: "'Courier New', Courier, monospace",
    padding: "6px 12px",
    border: "1px solid #e9c46a55",
    borderRadius: "2px",
  },
  voteGrid: {
    display: "flex",
    gap: "1.5rem",
    width: "100%",
    maxWidth: "780px",
    flexWrap: "wrap",
    justifyContent: "center",
  },
  voteCard: {
    position: "relative",
    background: "#1a1916",
    border: "1px solid #333",
    borderRadius: "4px",
    overflow: "hidden",
    cursor: "pointer",
    transition: "outline 0.1s, opacity 0.2s",
    flex: "1 1 300px",
    maxWidth: "360px",
  },
  voteImg: {
    width: "100%",
    height: "auto",
    display: "block",
  },
  voteImgPlaceholder: {
    height: "240px",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    color: "#555",
    fontSize: "13px",
    fontFamily: "'Courier New', Courier, monospace",
  },
  voteLabel: {
    position: "absolute",
    bottom: 0,
    left: 0,
    right: 0,
    padding: "8px",
    textAlign: "center",
    fontWeight: "700",
    fontSize: "14px",
    letterSpacing: "0.15em",
    fontFamily: "'Courier New', Courier, monospace",
    transition: "background 0.15s, color 0.15s",
  },
  votedBanner: {
    fontSize: "13px",
    color: "#e9c46a",
    border: "1px solid #e9c46a44",
    borderRadius: "2px",
    padding: "8px 20px",
    fontFamily: "'Courier New', Courier, monospace",
    letterSpacing: "0.05em",
  },
};