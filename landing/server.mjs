import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url)).replace(/[\\/]+$/, "");
const port = Number(process.env.LANDING_PORT || 5180);
const host = process.env.LANDING_HOST || "127.0.0.1";
const types = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json", ".jpg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
};
// Only these top-level entries are public (keeps server.mjs / package.json private).
const publicRoots = new Set(["index.html", "config.js", "css", "js", "img", "data"]);

createServer(async (request, response) => {
  let pathname;
  try { pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname); }
  catch { response.writeHead(400).end("Bad request"); return; }
  const requested = pathname === "/" ? "index.html" : pathname.slice(1);
  const file = normalize(join(root, requested));
  if (!file.startsWith(root + sep) || !publicRoots.has(requested.split("/")[0])) {
    response.writeHead(file.startsWith(root + sep) ? 404 : 403).end(file.startsWith(root + sep) ? "Not found" : "Forbidden");
    return;
  }
  try {
    const content = await readFile(file);
    const immutable = /\.(jpg|png)$/.test(file);
    response.writeHead(200, {
      "Content-Type": types[extname(file)] || "application/octet-stream",
      "Cache-Control": immutable ? "public, max-age=86400" : "no-cache",
      "X-Content-Type-Options": "nosniff",
    });
    response.end(content);
  } catch {
    response.writeHead(404).end("Not found");
  }
}).listen(port, host, () => console.log(`Landing page at http://${host}:${port}`));
