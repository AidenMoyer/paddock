// Thin fetch wrapper. Cookie auth + an anti-forgery header on every request.

import { toast } from "./ui.js";

export class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}

async function request(method, path, body, opts = {}) {
  const headers = { "x-paddock": "1" };
  let payload;
  if (body instanceof Blob || body instanceof ArrayBuffer) payload = body;
  else if (body !== undefined) { headers["content-type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(path, { method, headers, body: payload, credentials: "same-origin", signal: opts.signal });
  if (res.status === 401 && !opts.allow401) {
    window.dispatchEvent(new CustomEvent("paddock:signed-out"));
    throw new ApiError(401, "Signed out");
  }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    let detail = typeof data === "object" ? data.detail : data;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    const err = new ApiError(res.status, detail || res.statusText);
    if (!opts.quiet) toast(err.message, { error: true });
    throw err;
  }
  return data;
}

export const api = {
  get: (p, opts) => request("GET", p, undefined, opts),
  post: (p, b, opts) => request("POST", p, b ?? {}, opts),
  put: (p, b, opts) => request("PUT", p, b ?? {}, opts),
  patch: (p, b, opts) => request("PATCH", p, b ?? {}, opts),
  del: (p, opts) => request("DELETE", p, undefined, opts),
  raw: request,
};

export function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.set(k, v);
  const s = u.toString();
  return s ? `?${s}` : "";
}

export const thumbUrl = (id) => `/api/items/${id}/thumb`;

// ---- resumable uploads (same protocol the Android app uses) ----

async function sha256(file, onProgress) {
  // WebCrypto has no streaming digest; files up to ~2 GB are hashed in memory, larger ones in a worker-free chunk loop.
  if (file.size < 1.5e9) {
    const buf = await file.arrayBuffer();
    const d = await crypto.subtle.digest("SHA-256", buf);
    return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  throw new Error("Files over 1.5 GB: use the Paddock app or copy to the NAS share");
}

export async function uploadFile(file, { libraryId, subfolder, onProgress } = {}) {
  onProgress?.(0, "Checking…");
  const hash = await sha256(file);
  const start = await api.post("/api/uploads", {
    filename: file.name, size: file.size, sha256: hash, taken_at: file.lastModified / 1000,
    library_id: libraryId || null, subfolder: subfolder || null,
  });
  if (start.status === "duplicate") { onProgress?.(1, "Already in Paddock"); return { duplicate: true, item_id: start.item_id }; }
  let offset = start.offset;
  const chunk = start.chunk_size || 8 * 1024 * 1024;
  while (offset < file.size) {
    const part = file.slice(offset, offset + chunk);
    let tries = 0;
    for (;;) {
      try {
        const r = await fetch(`/api/uploads/${start.upload_id}?offset=${offset}`, {
          method: "PUT", body: part, headers: { "x-paddock": "1", "content-type": "application/octet-stream" },
        });
        if (r.status === 409) { offset = Number(r.headers.get("upload-offset") || offset); break; }
        if (!r.ok) throw new Error(`upload failed (${r.status})`);
        offset = (await r.json()).offset;
        break;
      } catch (e) {
        if (++tries > 5) throw e;
        await new Promise((res) => setTimeout(res, 1000 * tries));
      }
    }
    onProgress?.(offset / file.size, "Uploading…");
  }
  onProgress?.(1, "Processing…");
  return api.post(`/api/uploads/${start.upload_id}/complete`);
}
