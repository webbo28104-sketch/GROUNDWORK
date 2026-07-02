// This deployment is Cloudflare Workers Static Assets (not classic Pages) —
// the wrangler-generated config only serves files from frontend/ by default.
// A plain `_redirects` file can't proxy to an external absolute URL on this
// platform ("Proxy (200) redirects can only point to relative paths"), so
// Flask/Railway routes that don't exist as static files here (magic-link
// verification, the account area, admin) need an actual Worker script to
// forward them to the Railway origin. Presence of this file switches
// wrangler into "Advanced Mode", where it — not the default asset router —
// handles every request; we explicitly fall back to env.ASSETS.fetch() for
// anything that isn't one of these backend paths.

const RAILWAY_ORIGIN = "https://web-production-748b1.up.railway.app";

// Prefixes with a trailing slash only match paths under them (there's no
// bare /api or /verify route). /account and /admin have no trailing slash
// since both have a route at the bare path too (/account, /admin/login, ...)
// and no static file of that name exists in frontend/ to collide with.
const BACKEND_PREFIXES = ["/api/", "/verify/", "/account", "/admin"];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const needsBackend = BACKEND_PREFIXES.some((p) => url.pathname === p || url.pathname.startsWith(p));

    if (needsBackend) {
      const target = new URL(url.pathname + url.search, RAILWAY_ORIGIN);
      const proxied = new Request(target, request);
      return fetch(proxied);
    }

    return env.ASSETS.fetch(request);
  },
};
