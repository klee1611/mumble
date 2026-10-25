// Public settings for the phone app (none of these are secrets).
// The local dev server serves its own /config.js from your environment instead.
window.MUMBLE = {
  api: "", // same origin: Vercel rewrites /api to api/index.py
  dev: false,
  auth0: {
    domain: "dev-l1bc0yw26epgfj00.us.auth0.com",
    client_id: "xyWBWbw0UPN6MN6nAqf1tKDIZje29o5j",
    audience: "https://mumble-azure.vercel.app/api",
  },
};
