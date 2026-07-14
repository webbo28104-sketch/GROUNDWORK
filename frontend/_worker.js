// This deployment is Cloudflare Workers Static Assets (not classic Pages).
// A plain `_redirects` file can't proxy to an external absolute URL on this
// platform ("Proxy (200) redirects can only point to relative paths"), so
// Flask/Railway routes that don't exist as static files here (magic-link
// verification, the account area, admin) need an actual Worker script to
// forward them to the Railway origin. Wired up explicitly as `main` in
// /wrangler.jsonc (relying on auto-detecting this file's presence did not
// work in practice — the build kept using assets-only mode regardless), so
// this script sees every request first; we explicitly fall back to
// env.ASSETS.fetch() (the "ASSETS" binding, also named explicitly in
// wrangler.jsonc) for anything that isn't one of these backend paths.

const RAILWAY_ORIGIN = "https://web-production-748b1.up.railway.app";

// Prefixes with a trailing slash only match paths under them (there's no
// bare /api or /verify route). /account and /admin have no trailing slash
// since both have a route at the bare path too (/account, /admin/login, ...)
// and no static file of that name exists in frontend/ to collide with.
const BACKEND_PREFIXES = ["/api/", "/verify/", "/account", "/admin", "/webhook/"];

// ── Inbound email (Section 11a's reply-capture kill-switch) ──────────────
//
// Cloudflare Email Routing, once a routing rule on the outreach sending
// domain is pointed at this same Worker, invokes email() (a separate
// handler from fetch() — one Worker script can export both) for every
// inbound message. There's no separate Wrangler project for this: it's
// the existing `groundwork` worker, already git-linked/auto-deployed, with
// a second handler added — no new deploy pipeline to stand up.
//
// Deliberately dependency-free (no postal-mime or similar): this project
// has zero npm dependencies today (no package.json), and introducing one
// purely to parse MIME would add real risk to a deploy pipeline this
// script has no way to test against directly. Handles the common case —
// a single-part message, or two-part multipart/alternative (text/plain +
// text/html) from a mainstream client (Gmail, Outlook, Apple Mail all send
// this shape) — including quoted-printable and base64 decoding. It is NOT
// a fully spec-compliant MIME parser (nested multipart/mixed with
// attachments, unusual charsets, etc. aren't handled) — good enough for
// detecting a short human reply, not a general-purpose mail client.
function decodeContentTransfer(headers, body) {
  const cte = ((headers.match(/^Content-Transfer-Encoding:\s*(.+)$/im) || [, ""])[1] || "").trim().toLowerCase();
  if (cte === "base64") {
    try {
      return atob(body.replace(/\s+/g, ""));
    } catch {
      return body;
    }
  }
  if (cte === "quoted-printable") {
    return body
      .replace(/=\r?\n/g, "")
      .replace(/=([0-9A-Fa-f]{2})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
  }
  return body;
}

function splitHeadersAndBody(chunk) {
  const idx = chunk.indexOf("\r\n\r\n");
  if (idx === -1) return { headers: chunk, body: "" };
  return { headers: chunk.slice(0, idx), body: chunk.slice(idx + 4) };
}

async function extractPlainText(rawStream) {
  const raw = await new Response(rawStream).text();
  const { headers, body } = splitHeadersAndBody(raw);

  const contentType = (headers.match(/^Content-Type:\s*(.+)$/im) || [, ""])[1] || "";
  const boundaryMatch = contentType.match(/boundary="?([^";]+)"?/i);

  if (!boundaryMatch) {
    return decodeContentTransfer(headers, body).trim();
  }

  const boundary = boundaryMatch[1];
  const parts = body.split(`--${boundary}`).slice(1, -1);
  let plainPart = null;
  let htmlPart = null;

  for (const part of parts) {
    const { headers: pHeaders, body: pBody } = splitHeadersAndBody(part);
    if (/text\/plain/i.test(pHeaders) && plainPart === null) {
      plainPart = decodeContentTransfer(pHeaders, pBody);
    } else if (/text\/html/i.test(pHeaders) && htmlPart === null) {
      htmlPart = decodeContentTransfer(pHeaders, pBody);
    }
  }

  if (plainPart !== null) return plainPart.trim();
  if (htmlPart !== null) return htmlPart.replace(/<[^>]+>/g, " ").trim();
  return "";
}

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

  async email(message, env, ctx) {
    let text = "";
    try {
      text = await extractPlainText(message.raw);
    } catch (err) {
      text = "";
    }

    try {
      await fetch(`${RAILWAY_ORIGIN}/api/webhooks/email-inbound`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Groundwork-Email-Secret": env.EMAIL_INBOUND_SHARED_SECRET || "",
        },
        body: JSON.stringify({ from: message.from, text }),
      });
    } catch (err) {
      // Swallow — nothing useful to retry against from here. Doesn't
      // reject the message, so Cloudflare still accepts it normally.
    }

    // Additive, human-visible copy — independent of the webhook call above,
    // so a failure/success on either side never affects the other.
    // message.forward() re-delivers the original raw message as-is (headers
    // included), so the From header stays the prospect's real address:
    // replying to the forwarded copy in Outlook goes straight back to them,
    // not through any Groundwork address. Requires
    // groundwork-build@outlook.com to be a verified destination address in
    // Cloudflare Email Routing, same as any other forwarding target.
    try {
      await message.forward("groundwork-build@outlook.com");
    } catch (err) {
      // Swallow for the same reason as above — a failed forward must never
      // block/reject the message or affect the webhook post.
    }
  },
};
