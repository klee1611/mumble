// Mumble PWA: record, upload, read the morning digest, search memory.

// The static host can't send frame-ancestors, so refuse to run inside a frame
// (a framing page could trick taps on "Done" or "Dismiss").
if (window.top !== window.self) {
  document.documentElement.replaceChildren();
  throw new Error("Mumble won't run in a frame");
}

const $ = (sel) => document.querySelector(sel);

// ---------- sign-in ----------
// Auth0 (OAuth 2 + PKCE). The API only accepts short-lived access tokens; the
// SDK refreshes them with a rotating refresh token. Config is in config.js.
import { createAuth0Client } from "/vendor/auth0-spa-js-2.28.2.esm.js";

const CFG = window.MUMBLE || {};
const API = (CFG.api || "").replace(/\/$/, "");
const MAX_SECONDS = 300;
const MAX_BYTES = 750 * 1024;
let auth0 = null;

async function setupAuth() {
  if (CFG.dev) return true; // local dev server without Auth0
  auth0 = await createAuth0Client({
    domain: CFG.auth0.domain,
    clientId: CFG.auth0.client_id,
    authorizationParams: {
      redirect_uri: `${location.origin}/`, // must match Allowed Callback URLs exactly
      audience: CFG.auth0.audience,
      scope: "openid profile email offline_access",
    },
    // Remembered across app launches, so the home-screen app doesn't sign you
    // out every time. Refresh tokens rotate: a stolen one stops working once used.
    cacheLocation: "localstorage",
    useRefreshTokens: true,
    useRefreshTokensFallback: false,
  });
  const q = new URLSearchParams(location.search);
  if (q.has("code") && q.has("state")) {
    try {
      await auth0.handleRedirectCallback();
    } catch (err) {
      loginError(`Sign-in didn't work: ${err.message}`);
    }
    history.replaceState(null, "", "/");
  } else if (q.has("error")) {
    loginError(`Sign-in didn't work: ${q.get("error_description") || q.get("error")}`);
    history.replaceState(null, "", "/");
  }
  return auth0.isAuthenticated();
}

async function bearer() {
  if (!auth0) return {};
  try {
    return { Authorization: `Bearer ${await auth0.getTokenSilently()}` };
  } catch {
    showLogin();
    throw new Error("Sign in again");
  }
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    ...opts,
    headers: { ...(opts.headers || {}), ...(await bearer()) },
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("Sign in again");
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

// One toast for every screen, just above the tabs. An optional action (Undo).
let toastTimer = null;
function toast(msg, action = null, ms = 5000) {
  clearTimeout(toastTimer);
  $("#toast-text").textContent = msg;
  const btn = $("#toast-action");
  btn.hidden = !action;
  btn.onclick = null;
  if (action) {
    btn.textContent = action.label;
    btn.onclick = () => { $("#toast").hidden = true; action.run(); };
  }
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => ($("#toast").hidden = true), action ? ms * 1.6 : ms);
}

// A screen that couldn't load says so and offers a retry, instead of staying blank.
async function guard(target, load) {
  try {
    await load();
  } catch (err) {
    if (err.message === "Sign in again") return;
    target.replaceChildren(el("div", { class: "card trouble" },
      el("p", {}, `Couldn't load this: ${err.message}`),
      el("button", { class: "ghost", onclick: () => guard(target, load) }, "Try again")));
  }
}

// Shown on the sign-in screen, where the toast (on the Mumble tab) is hidden.
function loginError(msg) {
  $("#login-error").textContent = msg;
  $("#login-error").hidden = false;
}

function signOut() {
  if (auth0) auth0.logout({ logoutParams: { returnTo: `${location.origin}/` } });
}

function showWho(me) {
  $("#whoami-text").textContent = CFG.dev ? "Dev mode · local account" : me.email;
  $("#whoami").hidden = false;
}

function showLogin() {
  stopPlaying();
  for (const id of ["boot", "whoami", "account", "tabs", "toast"]) $(`#${id}`).hidden = true;
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = true));
  $("#login").hidden = false;
}

$("#sign-in").addEventListener("click", () => auth0.loginWithRedirect());

// ---------- screens ----------
// Three tabs, plus the account screen, which sits on top and goes back.
let currentTab = "mumble";

function show(name) {
  currentTab = name;
  stopPlaying();
  $("#account").hidden = true;
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== `tab-${name}`));
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.dataset.tab === name ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current")
  );
  if (name === "mumble") guard($("#pile"), loadPile);
  if (name === "morning") guard($("#digest"), loadDigest);
  if (name === "remember") guard($("#kept"), loadKept);
  window.scrollTo(0, 0);
}
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));

function showAccount() {
  stopPlaying();
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = true));
  document.querySelectorAll("#tabs button").forEach((b) => b.removeAttribute("aria-current"));
  $("#account").hidden = false;
  window.scrollTo(0, 0);
  $("#me").replaceChildren(el("p", { class: "muted" }, "Loading…"));
  guard($("#me"), loadMe);
}
$("#open-account").addEventListener("click", showAccount);
$("#back").addEventListener("click", () => show(currentTab));

// ---------- recording ----------
let recorder = null;
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
  // A local name, not the global: stopRecording() clears `recorder` before
  // the browser fires onstop, and onstop still needs it.
  const rec = new MediaRecorder(stream, { ...(mimeType ? { mimeType } : {}), audioBitsPerSecond: 16000 });
  const parts = [];
  rec.ondataavailable = (e) => e.data.size && parts.push(e.data);
  rec.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    const type = (rec.mimeType || mimeType || "audio/webm").split(";")[0];
    await upload(new Blob(parts, { type }), type, Math.round((Date.now() - startedAt) / 1000));
  };
  rec.onerror = (e) => toast(`Recording failed: ${e.error?.message || "unknown error"}`);
  recorder = rec;
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

// Recordings made in this tab play from memory; older ones are fetched.
const localAudio = new Map(); // memo id -> blob URL
const lengths = new Map(); // memo id -> seconds
let freshId = null;

async function upload(blob, type, seconds) {
  if (blob.size > MAX_BYTES) {
    toast("That one's too long for a mumble. Keep it under about 5 minutes.");
    return;
  }
  toast("Saving…");
  try {
    const memo = await api("/api/memos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mime: type || "audio/webm", audio: await toBase64(blob) }),
    });
    localAudio.set(memo.id, URL.createObjectURL(blob));
    if (seconds) lengths.set(memo.id, seconds);
    freshId = memo.id;
    toast("Got it. Forget about it. 🦥");
    loadPile();
  } catch (err) {
    toast(`Couldn't save that one: ${err.message}`);
  }
}

// ---------- listening back ----------
const player = new Audio();
let playingBtn = null;

function stopPlaying() {
  player.pause();
  if (playingBtn) {
    playingBtn.textContent = "▶";
    playingBtn.setAttribute("aria-label", playingBtn.dataset.label);
  }
  playingBtn = null;
}
player.addEventListener("ended", stopPlaying);

async function play(memoId, btn) {
  if (playingBtn === btn) return stopPlaying();
  stopPlaying();
  try {
    if (!localAudio.has(memoId)) {
      const r = await api(`/api/memos/${memoId}/audio`);
      const bytes = Uint8Array.from(atob(r.audio), (c) => c.charCodeAt(0));
      localAudio.set(memoId, URL.createObjectURL(new Blob([bytes], { type: r.mime })));
    }
    player.src = localAudio.get(memoId);
    await player.play();
    playingBtn = btn;
    btn.textContent = "■";
    btn.setAttribute("aria-label", "Stop");
  } catch (err) {
    toast(`Couldn't play that one: ${err.message}`);
  }
}

// ---------- tonight's pile ----------
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const fmtDay = (iso) =>
  new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const fmtLength = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function pendingRow(m) {
  const label = `Play memo from ${fmtTime(m.created_at)}`;
  const btn = el("button", { class: "play", "aria-label": label, "data-label": label }, "▶");
  btn.addEventListener("click", () => play(m.id, btn));
  const seconds = lengths.get(m.id);
  return el("li", { class: `memo${m.id === freshId ? " fresh" : ""}` },
    el("div", { class: "memo-head" },
      btn,
      el("span", { class: "memo-when" }, Date.now() - new Date(m.created_at) < 60_000 ? "Just now" : fmtTime(m.created_at),
        seconds ? el("span", { class: "muted" }, ` · ${fmtLength(seconds)}`) : null),
      el("span", { class: "pill" }, "waits for 3 AM")));
}

function sortedRow(m) {
  const failed = m.status === "failed";
  const made = [[m.todos, "to-do"], [m.events, "event"], [m.notes, "note"]]
    .filter(([n]) => n > 0).map(([n, w]) => plural(n, w)).join(" · ");
  const words = (m.transcript || "").trim();
  return el("li", { class: "memo" },
    el("div", { class: "memo-head" },
      el("span", { class: "memo-when" }, fmtDay(m.created_at)),
      el("span", { class: `pill ${failed ? "failed" : ""}` }, failed ? "couldn't sort" : "sorted")),
    failed
      ? el("p", { class: "muted" }, "Mumble couldn't make this one out.")
      : el("p", { class: "said" }, words ? `“${words}”` : "Sounded silent, so nothing to sort."),
    made
      ? el("p", { class: "made" }, `Became ${made} · `,
          el("button", { class: "link", onclick: () => show("morning") }, "see them in Morning"))
      : null);
}

async function loadPile() {
  const [memos, nap, me] = await Promise.all([api("/api/memos?limit=20"), api("/api/nap"), api("/api/me")]);
  const pending = memos.filter((m) => m.status === "pending");
  const recent = memos.filter((m) => m.status !== "pending").slice(0, 3);

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
  if (!nap.can_nap_now) napBtn.hidden = true;

  stopPlaying();
  $("#pile").replaceChildren(...[
    el("h2", {}, pending.length ? `Tonight's pile · ${pending.length}` : "Tonight's pile"),
    el("p", { class: "muted explain" },
      pending.length
        ? "Mumble listens to these at 3 AM and puts what you said in Morning: to-dos, events and notes."
        : "Empty. Mumble something. At 3 AM it becomes to-dos, events and notes in Morning."),
    pending.length ? el("ul", {}, pending.map(pendingRow)) : null,
    el("p", { class: "muted quota" },
      `Today: ${me.usage.memos_24h} of ${me.quota.memos_per_day} memos · ` +
      `${me.usage.audio_mb_24h} of ${me.quota.audio_mb_per_day} MB`),
    recent.length ? el("h2", { class: "sub" }, "Already sorted") : null,
    recent.length ? el("ul", {}, recent.map(sortedRow)) : null,
    napBtn,
  ].filter(Boolean));
  if (nap.napping) setTimeout(loadPile, 4000);
}

// ---------- morning digest ----------
function minutes(s) {
  if (s < 60) return `${s} s`;
  return `${Math.round(s / 60)} min`;
}

function patchStatus(id, status) {
  return api(`/api/items/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}

const DONE_SAYS = { done: "Done", approved: "Kept in Remember", dismissed: "Dismissed", new: "Back in Morning" };

// Done, Keep and Dismiss take the item away at once; Undo brings it back.
async function setStatus(it, status, reload) {
  try {
    await patchStatus(it.id, status);
  } catch (err) {
    return toast(`Couldn't update that: ${err.message}`);
  }
  toast(`${DONE_SAYS[status]}: ${it.title}`, {
    label: "Undo",
    run: async () => {
      await patchStatus(it.id, it.status).catch((err) => toast(`Couldn't undo: ${err.message}`));
      reload();
    },
  });
  reload();
}

function itemCard(it) {
  const reload = () => guard($("#digest"), loadDigest);
  const actions = el("div", { class: "actions" });
  if (it.ics) {
    actions.append(el("a", { href: `${API}/api/items/${it.id}/event.ics?${it.ics}` }, "Add to calendar"));
  }
  if (it.kind === "todo") actions.append(el("button", { onclick: () => setStatus(it, "done", reload) }, "Done"));
  if (it.kind === "note") actions.append(el("button", { onclick: () => setStatus(it, "approved", reload) }, "Keep"));
  actions.append(el("button", { class: "no", onclick: () => setStatus(it, "dismissed", reload) }, "Dismiss"));

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

function reviewCard(r) {
  if (!r) return null;
  const b = r.body;
  const claim = (c) =>
    el("li", {}, c.text, c.items?.length ? el("span", { class: "cites" }, ` · from “${c.items.map((i) => i.title).join("”, “")}”`) : null);
  const section = (label, list) => (list?.length ? [el("h3", {}, label), el("ul", {}, list.map(claim))] : []);
  const cents = (Number(r.cost_usd) * 100).toFixed(2);
  return el(
    "div",
    { class: "card review" },
    el("h2", {}, "Your week, read by Ultramarine"),
    b.headline ? el("p", { class: "headline" }, b.headline) : null,
    b.one_thing ? el("p", { class: "one-thing" }, el("strong", {}, "One small thing: "), b.one_thing.text) : null,
    ...section("You keep saying", b.keep_saying),
    ...section("Slipping", b.slipping),
    ...section("These go together", b.connections),
    el("p", { class: "said" }, `Read ${r.items_read} items on ${fmtDay(r.created_at)} · cost ${cents}¢`)
  );
}

async function loadDigest() {
  const [d, review] = await Promise.all([api("/api/digest"), api("/api/review")]);
  $("#review").replaceChildren(...[reviewCard(review)].filter(Boolean));
  // Morning is what still needs you. Kept notes live in Remember.
  const open = d.items.filter((i) => i.status === "new");
  const p = d.last_nap?.payoff;
  $("#payoff").replaceChildren(
    p && d.last_nap.memos
      ? el("span", {},
          el("span", { class: "payoff-big" }, `${minutes(p.saved_s)} saved`),
          el("br"),
          `Last night: ${plural(d.last_nap.memos, "memo")}, ${minutes(p.spoken_s)} of mumbling instead of ${minutes(p.by_hand_s)} of typing and sorting.`)
      : open.length
        ? `${plural(open.length, "thing")} from your mumbles, waiting for you.`
        : d.all_time.memos
          ? "Nothing new since your last mumbles."
          : "Nothing sorted yet. Mumble today, read this tomorrow."
  );

  const groups = [
    ["event", "Happening"],
    ["todo", "To do"],
    ["note", "New notes"],
  ];
  const parts = groups
    .map(([kind, label]) => {
      const items = open.filter((i) => i.kind === kind);
      return items.length ? el("div", { class: "group" }, el("h2", {}, label), items.map(itemCard)) : null;
    })
    .filter(Boolean);
  $("#digest").replaceChildren(...(parts.length ? parts : [
    el("div", { class: "empty" },
      el("p", {}, "All clear. Go back to bed. 🦥"),
      el("p", { class: "small" },
        el("button", { class: "link", onclick: () => show("mumble") }, "Mumble something"), " or ",
        el("button", { class: "link", onclick: () => show("remember") }, "see what you kept"))),
  ]));
}

// ---------- remember ----------
const KIND = { event: "Event", todo: "To-do", note: "Note" };
const STATUS = { new: "in Morning", approved: "kept", done: "done", dismissed: "dismissed" };

function memoryCard(r) {
  return el("div", { class: "card memory" },
    el("div", { class: "memory-head" },
      el("span", { class: "title" }, r.title),
      el("span", { class: "pill" }, `${KIND[r.kind] || r.kind} · ${STATUS[r.status] || r.status}`)),
    r.detail ? el("div", { class: "muted" }, r.detail) : null,
    r.due_at ? el("div", { class: "when" }, fmtDay(r.due_at)) : null,
    r.said_at ? el("div", { class: "said" }, `You said this ${fmtDay(r.said_at)}`) : null);
}

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#q").value.trim();
  $("#results").replaceChildren(el("p", { class: "muted" }, "Thinking slowly…"));
  $("#kept").hidden = true;
  try {
    const rows = await api(`/api/search?q=${encodeURIComponent(q)}`);
    $("#results").replaceChildren(
      el("p", { class: "results-head" },
        rows.length ? `${rows.length} ${rows.length === 1 ? "match" : "matches"} for “${q}”`
          : `You never mumbled about “${q}”.`, " ",
        el("button", { class: "link", onclick: clearSearch }, "Clear")),
      ...rows.map(memoryCard));
  } catch (err) {
    $("#results").replaceChildren(el("p", { class: "muted" }, `Couldn't search: ${err.message}`));
  }
});

function clearSearch() {
  $("#q").value = "";
  $("#results").replaceChildren();
  $("#kept").hidden = false;
}
$("#q").addEventListener("input", () => { if (!$("#q").value) clearSearch(); });

// With no search, Remember shows the notes you kept.
async function loadKept() {
  const d = await api("/api/digest");
  const kept = d.items.filter((i) => i.status === "approved");
  $("#kept").replaceChildren(
    el("h2", { class: "section" }, "Kept"),
    ...(kept.length
      ? kept.map(memoryCard)
      : [el("p", { class: "muted" }, "Notes you Keep in Morning land here. Search finds everything else.")]));
}

// ---------- you ----------
function meter(label, used, most, unit = "") {
  return el("div", { class: "meter" },
    el("span", {}, label),
    el("progress", { max: String(most), value: String(Math.min(used, most)) }),
    el("span", { class: "muted" }, `${used}${unit} / ${most}${unit}`));
}

async function loadMe() {
  const me = await api("/api/me");
  let armed = false;
  const del = el("button", {
    class: "no",
    onclick: async () => {
      // Two taps instead of a pop-up: the first one arms it.
      if (!armed) {
        armed = true;
        del.textContent = "Tap again to delete every memo, item and review";
        setTimeout(() => { armed = false; del.textContent = "Delete my account"; }, 5000);
        return;
      }
      await api("/api/account", { method: "DELETE" });
      signOut();
    },
  }, "Delete my account");
  $("#account-email").textContent = CFG.dev ? "Dev mode · local account" : me.email;
  $("#me").replaceChildren(
    el("h2", {}, "Today's allowance"),
    meter("Memos today", me.usage.memos_24h, me.quota.memos_per_day),
    meter("Audio today", me.usage.audio_mb_24h, me.quota.audio_mb_per_day, " MB"),
    meter("Unsorted", me.usage.pending, me.quota.pending_max),
    el("p", { class: "muted small" }, "Allowances reset over the day. Unsorted memos clear at 3 AM."),
    el("div", { class: "actions" }, auth0 ? el("button", { onclick: signOut }, "Sign out") : null, del)
  );
}

// ---------- boot ----------
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
setupAuth().then(async (signedIn) => {
  if (!signedIn) return showLogin();
  $("#boot").hidden = true;
  $("#tabs").hidden = false;
  const tab = new URLSearchParams(location.hash.slice(1)).get("tab");
  show(["mumble", "morning", "remember"].includes(tab) ? tab : "mumble");
  showWho(await api("/api/me"));
}).catch((err) => {
  showLogin();
  loginError(`Couldn't reach sign-in: ${err.message}`);
});
