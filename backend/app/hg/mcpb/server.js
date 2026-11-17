// Hedgehog for Claude Desktop — a tiny stdio ⇄ HTTP bridge to the Hedgehog MCP server.
//
// Claude Desktop runs this with its bundled Node.js. It reads MCP JSON-RPC messages from stdin
// (one per line), POSTs each to HG_URL with the user's key, and writes the server's replies to
// stdout. Node built-ins only: no dependencies to bundle, nothing for the user to install.
//
// HG_URL and HG_KEY come from manifest.json (set per user when the file is downloaded).
"use strict";

const https = require("https");
const http = require("http");
const readline = require("readline");

const URL_ = process.env.HG_URL || "https://hg.voitta.ai/mcp/";
const KEY = process.env.HG_KEY || "";
const TIMEOUT_MS = 330000; // a StockTrak call can take a while; MCP clients wait too

let sessionId = null;
let protocolVersion = null;

function log(...a) {
  process.stderr.write("[hedgehog] " + a.join(" ") + "\n");
}

function write(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

function errorReply(id, message) {
  if (id === undefined || id === null) return; // notifications get no reply
  write({ jsonrpc: "2.0", id, error: { code: -32000, message } });
}

// Parse a Streamable-HTTP response: either JSON, or SSE with one "data:" line per message.
function parseBody(contentType, body) {
  if ((contentType || "").includes("text/event-stream")) {
    const out = [];
    for (const block of body.split(/\r?\n\r?\n/)) {
      const data = block
        .split(/\r?\n/)
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (data) {
        try {
          out.push(JSON.parse(data));
        } catch (_) {
          /* skip non-JSON events */
        }
      }
    }
    return out;
  }
  if (!body.trim()) return [];
  const parsed = JSON.parse(body);
  return Array.isArray(parsed) ? parsed : [parsed];
}

function post(msg) {
  return new Promise((resolve, reject) => {
    const u = new URL(URL_);
    const payload = JSON.stringify(msg);
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      Authorization: "Bearer " + KEY,
      "Content-Length": Buffer.byteLength(payload),
      "User-Agent": "hedgehog-claude-desktop/1",
    };
    if (sessionId) headers["Mcp-Session-Id"] = sessionId;
    if (protocolVersion) headers["Mcp-Protocol-Version"] = protocolVersion;
    const lib = u.protocol === "http:" ? http : https;
    const req = lib.request(
      { method: "POST", hostname: u.hostname, port: u.port || undefined, path: u.pathname + u.search, headers },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const sid = res.headers["mcp-session-id"];
          if (sid) sessionId = String(sid);
          const body = Buffer.concat(chunks).toString("utf8");
          if (res.statusCode === 401) return reject(new Error(
            "Hedgehog rejected the key (it was regenerated or revoked). Download the Claude Desktop " +
            "extension again from the Hedgehog panel on app.stocktrak.com and install it."));
          if (res.statusCode >= 400 && !(res.headers["content-type"] || "").includes("json")) {
            return reject(new Error(`Hedgehog server error ${res.statusCode}`));
          }
          try {
            resolve(parseBody(res.headers["content-type"], body));
          } catch (e) {
            reject(new Error(`Bad reply from Hedgehog (${res.statusCode}): ${body.slice(0, 200)}`));
          }
        });
      },
    );
    req.setTimeout(TIMEOUT_MS, () => req.destroy(new Error("Hedgehog server didn't answer in time")));
    req.on("error", reject);
    req.end(payload);
  });
}

async function handle(line) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (_) {
    return;
  }
  if (msg && msg.method === "initialize" && msg.params && msg.params.protocolVersion) {
    protocolVersion = msg.params.protocolVersion;
  }
  try {
    const replies = await post(msg);
    for (const r of replies) {
      if (r && r.result && r.result.protocolVersion && msg.method === "initialize") {
        protocolVersion = r.result.protocolVersion;
      }
      write(r);
    }
  } catch (e) {
    log(e.message);
    errorReply(msg && msg.id, e.message);
  }
}

if (!KEY) log("no key configured: download the extension again from the Hedgehog panel");
const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", (line) => {
  if (line.trim()) void handle(line);
});
rl.on("close", () => process.exit(0));
