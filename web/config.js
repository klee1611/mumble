// Public settings for the phone app (none of these are secrets).
// The local dev server serves its own /config.js from your environment instead.
window.MUMBLE = {
  api: "", // same origin: Vercel rewrites /api to api/index.py
  dev: false,
  auth0: {
    domain: "AUTH0_DOMAIN",
    client_id: "AUTH0_CLIENT_ID",
    audience: "https://mumble-azure.vercel.app/api",
  },
};
