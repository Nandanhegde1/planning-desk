// A pass-through for the four Open-Meteo endpoints planning-desk calls, plus a
// keep-alive for the free Render instance.
//
// Why it exists: Open-Meteo's free API caps requests per IP per day, and on
// Render's free plan the outbound address is shared, so other tenants spent the
// allowance before the app made a call ("Daily API request limit exceeded").
// Requests sent from here leave from Cloudflare instead. HOSTING.md has the rest.

const ROUTES = {
  "/v1/forecast": { upstream: "https://api.open-meteo.com/v1/forecast", ttl: 1800 },
  "/v1/air-quality": { upstream: "https://air-quality-api.open-meteo.com/v1/air-quality", ttl: 1800 },
  // History does not change once published.
  "/v1/archive": { upstream: "https://archive-api.open-meteo.com/v1/archive", ttl: 7 * 86400 },
  "/v1/search": { upstream: "https://geocoding-api.open-meteo.com/v1/search", ttl: 30 * 86400 },
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const route = ROUTES[url.pathname];
    // Only these four GET routes. Anything else is refused, so this cannot be
    // used as a general proxy.
    if (!route || request.method !== "GET") {
      return new Response("not found", { status: 404 });
    }

    const upstream = route.upstream + url.search;
    const cacheKey = new Request(upstream, { method: "GET" });
    const cache = caches.default;
    const hit = await cache.match(cacheKey);
    if (hit) return hit;

    const response = await fetch(upstream, {
      headers: { "User-Agent": request.headers.get("User-Agent") || "planning-desk-edge" },
    });
    // Failures pass through untouched, so the app's own retry and fallback still
    // see a 429 as a 429. Only successes are cached.
    if (!response.ok) return response;

    const cached = new Response(response.body, response);
    cached.headers.set("Cache-Control", `public, max-age=${route.ttl}`);
    ctx.waitUntil(cache.put(cacheKey, cached.clone()));
    return cached;
  },

  // Render's free plan sleeps after 15 minutes without a request, and waking takes
  // about a minute. A ping every 10 minutes keeps the first visitor from waiting.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(fetch(env.KEEPALIVE_URL, { headers: { "User-Agent": "planning-desk-edge keepalive" } }));
  },
};
