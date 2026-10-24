// Mumble PWA: record, upload, read the morning digest, search memory.

const $ = (sel) => document.querySelector(sel);

// ---------- token ----------
const params = new URLSearchParams(location.search);
if (params.get("t")) {
  localStorage.setItem("mumble-token", params.get("t"));
  history.replaceState(null, "", "/");
}
const token = () => localStorage.getItem("mumble-token") || "";

const API = (window.MUMBLE_API || "").replace(/\/$/, "");
const MAX_SECONDS = 300;
const MAX_BYTES = 750 * 1024;

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    ...opts,
    headers: { ...(opts.headers || {}), Authorization: `Bearer ${token()}` },
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("Need a token");
  }
  if (!res.ok) {
    const text = await res.text();
    let msg = text;
    try { msg = JSON.parse(text).error || JSON.parse(text).detail || text; } catch {}
    throw new Error(msg || res.statusText);
  }
  return res.json();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
}

function toast(msg) {
  $("#toast").textContent = msg;
}

function showLogin() {
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = true));
  $("#tabs").hidden = true;
  $("#login").hidden = false;
}

$("#login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  localStorage.setItem("mumble-token", $("#token").value.trim());
  $("#login").hidden = true;
  $("#tabs").hidden = false;
  show("mumble");
});

// ---------- tabs ----------
function show(name) {
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== `tab-${name}`));
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.dataset.tab === name ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current")
  );
  if (name === "mumble") loadPile();
  if (name === "morning") loadDigest();
}
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));

// ---------- recording ----------
let recorder = null;
let chunks = [];
let startedAt = 0;
let timer = null;

function pickMime() {
  for (const m of ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"]) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
  }
  return "";
}

async function startRecording() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast("Mumble needs the microphone. (It also needs https.)");
    return;
  }
  const mimeType = pickMime();
  // Speech sounds fine at 16 kbps, and it keeps a 5-minute memo under the
  // serverless request limit.
  recorder = new MediaRecorder(stream, { ...(mimeType ? { mimeType } : {}), audioBitsPerSecond: 16000 });
  chunks = [];
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    const type = (recorder.mimeType || mimeType || "audio/webm").split(";")[0];
    await upload(new Blob(chunks, { type }), type);
  };
  recorder.start();
  startedAt = Date.now();
  $("#rec").classList.add("recording");
  $("#rec").setAttribute("aria-label", "Stop recording");
  $("#rec-label").textContent = "Tap when you're done";
  timer = setInterval(() => {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    $("#rec-time").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    if (s >= MAX_SECONDS) stopRecording();
  }, 250);
}

function stopRecording() {
  clearInterval(timer);
  $("#rec").classList.remove("recording");
  $("#rec").setAttribute("aria-label", "Start recording");
  $("#rec-label").textContent = "Tap and mumble";
  $("#rec-time").textContent = "";
  recorder?.stop();
  recorder = null;
}

$("#rec").addEventListener("click", () => (recorder ? stopRecording() : startRecording()));

$("#file").addEventListener("change", async (e) => {
  for (const f of e.target.files) await upload(f, f.type || guessType(f.name));
  e.target.value = "";
});

function guessType(name) {
  const ext = name.split(".").pop().toLowerCase();
  return { m4a: "audio/mp4", mp4: "audio/mp4", webm: "audio/webm", mp3: "audio/mpeg", ogg: "audio/ogg",
           opus: "audio/ogg", wav: "audio/wav", aac: "audio/aac" }[ext] || "audio/webm";
}

function toBase64(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result.split(",", 2)[1]);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });
}

async function upload(blob, type) {
  if (blob.size > MAX_BYTES) {
    toast("That one's too long for a mumble. Keep it under about 5 minutes.");
    return;
  }
  toast("Saving…");
  try {
    await api("/api/memos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mime: type || "audio/webm", audio: await toBase64(blob) }),
    });
    toast("Got it. Forget about it. 🦥");
    loadPile();
  } catch (err) {
    toast(`Couldn't save that one: ${err.message}`);
  }
}

// ---------- tonight's pile ----------
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const fmtDay = (iso) =>
  new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

async function loadPile() {
  const [memos, nap] = await Promise.all([api("/api/memos?limit=20"), api("/api/nap")]);
  const pending = memos.filter((m) => m.status === "pending");
  const recent = memos.filter((m) => m.status !== "pending").slice(0, 5);

  const napBtn = el(
    "button",
    {
      class: "ghost",
      onclick: async () => {
        const r = await api("/api/nap", { method: "POST" });
        toast(r.started ? "Woke the night shift. Check Morning in a few minutes." : "Already napping.");
        setTimeout(loadPile, 3000);
      },
    },
    nap.napping ? "Napping…" : "Can't wait? Nap now"
  );
  if (nap.napping) napBtn.disabled = true;

  $("#pile").replaceChildren(...[
    el("h2", {}, pending.length ? `Tonight's pile · ${pending.length}` : "Tonight's pile"),
    pending.length
      ? el("ul", {}, pending.map((m) => el("li", {}, `Memo at ${fmtTime(m.created_at)}`, el("span", { class: "pill" }, "sleeping"))))
      : el("p", { class: "muted" }, "Empty. Mumble something."),
    el("p", { class: "muted" }, "Sorted overnight at 3 AM, slowly and steadily."),
    recent.length
      ? el("ul", {}, recent.map((m) =>
          el("li", {}, `Memo at ${fmtTime(m.created_at)}`,
            el("span", { class: `pill ${m.status === "failed" ? "failed" : ""}` }, m.status === "failed" ? "failed" : "sorted"))))
      : null,
    napBtn,
  ].filter(Boolean));
  if (nap.napping) setTimeout(loadPile, 4000);
}

// ---------- morning digest ----------
function minutes(s) {
  if (s < 60) return `${s} s`;
  return `${Math.round(s / 60)} min`;
}

async function setStatus(id, status) {
  await api(`/api/items/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  loadDigest();
}

function itemCard(it) {
  const actions = el("div", { class: "actions" });
  if (it.kind === "event" && it.due_at) {
    actions.append(el("a", { href: `${API}/api/items/${it.id}/event.ics?t=${encodeURIComponent(token())}` }, "Add to calendar"));
  }
  if (it.kind === "todo") actions.append(el("button", { onclick: () => setStatus(it.id, "done") }, "Done"));
  if (it.kind === "note") actions.append(el("button", { onclick: () => setStatus(it.id, "approved") }, "Keep"));
  actions.append(el("button", { class: "no", onclick: () => setStatus(it.id, "dismissed") }, "Nah"));

  return el(
    "div",
    { class: "card item" },
    el("div", { class: "title" }, it.title),
    actions,
    it.detail ? el("div", { class: "detail" }, it.detail) : null,
    it.due_at
      ? el("div", { class: "when" }, `${fmtDay(it.due_at)} · you said “${it.when_text}”`)
      : it.when_text
        ? el("div", { class: "when" }, `you said “${it.when_text}”`)
        : null
  );
}

async function loadDigest() {
  const d = await api("/api/digest");
  const p = d.last_nap?.payoff;
  $("#payoff").replaceChildren(
    p && d.last_nap.memos
      ? el("span", {},
          el("span", { class: "payoff-big" }, `${minutes(p.saved_s)} saved`),
          el("br"),
          `Last night: ${d.last_nap.memos} memos, ${minutes(p.spoken_s)} of mumbling instead of ${minutes(p.by_hand_s)} of typing and sorting.`)
      : "Nothing sorted yet. Mumble today, read this tomorrow."
  );

  const groups = [
    ["event", "Happening"],
    ["todo", "To do"],
    ["note", "Worth remembering"],
  ];
  const parts = groups
    .map(([kind, label]) => {
      const items = d.items.filter((i) => i.kind === kind);
      return items.length ? el("div", { class: "group" }, el("h2", {}, label), items.map(itemCard)) : null;
    })
    .filter(Boolean);
  $("#digest").replaceChildren(...(parts.length ? parts : [el("p", { class: "empty" }, "All clear. Go back to bed. 🦥")]));
}

// ---------- remember ----------
$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#results").replaceChildren(el("p", { class: "muted" }, "Thinking slowly…"));
  try {
    const rows = await api(`/api/search?q=${encodeURIComponent($("#q").value)}`);
    $("#results").replaceChildren(
      ...(rows.length
        ? rows.map((r) =>
            el("div", { class: "card" },
              el("div", { class: "title" }, r.title),
              r.detail ? el("div", { class: "muted" }, r.detail) : null,
              el("div", { class: "said" }, `You said this ${fmtDay(r.said_at)}`)))
        : [el("p", { class: "empty" }, "You never mumbled about that.")])
    );
  } catch (err) {
    $("#results").replaceChildren(el("p", { class: "muted" }, err.message));
  }
});

// ---------- boot ----------
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
if (!token()) showLogin();
else show(new URLSearchParams(location.hash.slice(1)).get("tab") || "mumble");
