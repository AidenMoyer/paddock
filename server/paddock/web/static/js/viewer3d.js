// Minimal WebGL viewer for STL meshes: orbit, zoom, pan, auto-fit. No dependencies.

import { h, iconBtn } from "./ui.js";

function parseSTL(buf) {
  const dv = new DataView(buf);
  if (buf.byteLength >= 84) {
    const n = dv.getUint32(80, true);
    if (84 + n * 50 === buf.byteLength) {
      const pos = new Float32Array(n * 9);
      for (let i = 0; i < n; i++) {
        const o = 84 + i * 50 + 12;
        for (let k = 0; k < 9; k++) pos[i * 9 + k] = dv.getFloat32(o + k * 4, true);
      }
      return pos;
    }
  }
  const text = new TextDecoder().decode(buf);
  const nums = [...text.matchAll(/vertex\s+([-+\deE.]+)\s+([-+\deE.]+)\s+([-+\deE.]+)/g)].flatMap((m) => [+m[1], +m[2], +m[3]]);
  return new Float32Array(nums);
}

function normals(pos) {
  const n = new Float32Array(pos.length);
  for (let i = 0; i < pos.length; i += 9) {
    const ax = pos[i + 3] - pos[i], ay = pos[i + 4] - pos[i + 1], az = pos[i + 5] - pos[i + 2];
    const bx = pos[i + 6] - pos[i], by = pos[i + 7] - pos[i + 1], bz = pos[i + 8] - pos[i + 2];
    let nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
    const l = Math.hypot(nx, ny, nz) || 1;
    nx /= l; ny /= l; nz /= l;
    for (let k = 0; k < 3; k++) { n[i + k * 3] = nx; n[i + k * 3 + 1] = ny; n[i + k * 3 + 2] = nz; }
  }
  return n;
}

// column-major mat4 helpers
const M = {
  persp(fov, a, n, f) { const t = 1 / Math.tan(fov / 2); return [t / a, 0, 0, 0, 0, t, 0, 0, 0, 0, (f + n) / (n - f), -1, 0, 0, (2 * f * n) / (n - f), 0]; },
  mul(a, b) {
    const o = new Array(16).fill(0);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) for (let k = 0; k < 4; k++) o[c * 4 + r] += a[k * 4 + r] * b[c * 4 + k];
    return o;
  },
  rx(t) { const c = Math.cos(t), s = Math.sin(t); return [1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]; },
  rz(t) { const c = Math.cos(t), s = Math.sin(t); return [c, s, 0, 0, -s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]; },
  tr(x, y, z) { return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]; },
  sc(s) { return [s, 0, 0, 0, 0, s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1]; },
};

const VS = `attribute vec3 p; attribute vec3 n; uniform mat4 mvp; uniform mat4 model; varying vec3 vn; varying vec3 vp;
void main(){ vn = mat3(model) * n; vp = (model * vec4(p,1.)).xyz; gl_Position = mvp * vec4(p,1.); }`;
const FS = `precision mediump float; varying vec3 vn; varying vec3 vp; uniform vec3 color;
void main(){ vec3 N = normalize(vn); if(!gl_FrontFacing) N = -N;
  vec3 L = normalize(vec3(.4,.6,.8)); vec3 L2 = normalize(vec3(-.6,-.3,.4));
  float d = max(dot(N,L),0.)*.75 + max(dot(N,L2),0.)*.25; float hemi = .5 + .5*N.z;
  float rim = pow(1. - abs(N.z), 3.) * .15;
  vec3 c = color * (.22 + .9*d) + vec3(.05,.06,.08)*hemi + rim;
  gl_FragColor = vec4(c,1.); }`;

export function viewer3d(url, { hud = "" } = {}) {
  const canvas = h("canvas");
  const hudEl = h("div", { class: "hud" }, hud);
  let reset;
  const wrap = h("div", { class: "viewer3d" }, canvas, hudEl,
    h("div", { class: "tools" }, iconBtn("refresh", "Reset view", () => reset?.())));
  const gl = canvas.getContext("webgl", { antialias: true });
  if (!gl) { hudEl.textContent = "WebGL isn't available in this browser."; return wrap; }

  const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS));
  gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog);
  gl.useProgram(prog);
  const loc = { p: gl.getAttribLocation(prog, "p"), n: gl.getAttribLocation(prog, "n"), mvp: gl.getUniformLocation(prog, "mvp"),
    model: gl.getUniformLocation(prog, "model"), color: gl.getUniformLocation(prog, "color") };

  let count = 0, center = [0, 0, 0], radius = 1;
  let yaw = -0.6, pitch = -1.0, dist = 2.6, panX = 0, panY = 0;
  reset = () => { yaw = -0.6; pitch = -1.0; dist = 2.6; panX = panY = 0; draw(); };

  fetch(url, { credentials: "same-origin" }).then((r) => r.arrayBuffer()).then((buf) => {
    const pos = parseSTL(buf);
    count = pos.length / 3;
    const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < pos.length; i += 3) for (let k = 0; k < 3; k++) { min[k] = Math.min(min[k], pos[i + k]); max[k] = Math.max(max[k], pos[i + k]); }
    center = min.map((v, k) => (v + max[k]) / 2);
    radius = Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2 || 1;
    const bind = (data, attr) => {
      const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(attr); gl.vertexAttribPointer(attr, 3, gl.FLOAT, false, 0, 0);
    };
    bind(pos, loc.p);
    bind(normals(pos), loc.n);
    if (!hud) hudEl.textContent = `${(count / 3).toLocaleString()} triangles · ${(max[0] - min[0]).toFixed(1)} × ${(max[1] - min[1]).toFixed(1)} × ${(max[2] - min[2]).toFixed(1)} mm`;
    draw();
  }).catch(() => { hudEl.textContent = "Couldn't load the model."; });

  function draw() {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const w = canvas.clientWidth * dpr, hgt = canvas.clientHeight * dpr;
    if (canvas.width !== w || canvas.height !== hgt) { canvas.width = w; canvas.height = hgt; }
    gl.viewport(0, 0, w, hgt);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    if (!count) return;
    const model = M.mul(M.rx(pitch), M.mul(M.rz(yaw), M.mul(M.sc(1 / radius), M.tr(-center[0], -center[1], -center[2]))));
    const view = M.tr(panX, panY, -dist);
    const mvp = M.mul(M.persp(0.7, w / hgt, 0.05, 50), M.mul(view, model));
    gl.uniformMatrix4fv(loc.mvp, false, new Float32Array(mvp));
    gl.uniformMatrix4fv(loc.model, false, new Float32Array(model));
    gl.uniform3fv(loc.color, [1.0, 0.48, 0.27]);
    gl.drawArrays(gl.TRIANGLES, 0, count);
  }

  // interaction: drag = orbit, right/shift-drag = pan, wheel/pinch = zoom
  const pointers = new Map();
  let lastPinch = 0;
  canvas.addEventListener("pointerdown", (e) => { canvas.setPointerCapture(e.pointerId); pointers.set(e.pointerId, [e.clientX, e.clientY]); });
  canvas.addEventListener("pointerup", (e) => { pointers.delete(e.pointerId); lastPinch = 0; });
  canvas.addEventListener("pointercancel", (e) => pointers.delete(e.pointerId));
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("pointermove", (e) => {
    if (!pointers.has(e.pointerId)) return;
    const [px, py] = pointers.get(e.pointerId);
    const dx = e.clientX - px, dy = e.clientY - py;
    pointers.set(e.pointerId, [e.clientX, e.clientY]);
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      if (lastPinch) dist = Math.min(10, Math.max(0.6, dist * (lastPinch / d)));
      lastPinch = d;
    } else if (e.buttons === 2 || e.shiftKey) {
      panX += (dx / canvas.clientWidth) * dist * 0.8; panY -= (dy / canvas.clientHeight) * dist * 0.8;
    } else {
      yaw += dx * 0.01; pitch = Math.min(0.2, Math.max(-3.0, pitch + dy * 0.01));
    }
    draw();
  });
  canvas.addEventListener("wheel", (e) => { e.preventDefault(); dist = Math.min(10, Math.max(0.6, dist * (1 + Math.sign(e.deltaY) * 0.1))); draw(); }, { passive: false });
  canvas.addEventListener("dblclick", () => reset());
  new ResizeObserver(() => draw()).observe(wrap);
  return wrap;
}

// ---------- G-code toolpath preview (isometric, layer scrubber) ----------

export function gcodeView(url) {
  const canvas = h("canvas");
  const range = h("input", { type: "range", min: 0, max: 0, value: 0, "aria-label": "Layer" });
  const label = h("span", null, "Loading toolpath…");
  const wrap = h("div", { class: "gcode-view" }, canvas, h("div", { class: "layer-ctl" }, label, range));
  let layers = [], zs = [], bounds = null;
  fetch(url, { credentials: "same-origin" }).then((r) => r.text()).then((text) => {
    let x = 0, y = 0, z = 0, e = 0, rel = false, relE = false, cur = null;
    const minB = [Infinity, Infinity], maxB = [-Infinity, -Infinity];
    for (const raw of text.split("\n")) {
      const line = raw.split(";")[0].trim();
      if (!line) continue;
      const cmd = line.split(/\s+/)[0].toUpperCase();
      if (cmd === "G90") { rel = false; continue; } if (cmd === "G91") { rel = true; continue; }
      if (cmd === "M82") { relE = false; continue; } if (cmd === "M83") { relE = true; continue; }
      if (cmd === "G92") { const m = line.match(/E([-\d.]+)/i); if (m) e = +m[1]; continue; }
      if (cmd !== "G0" && cmd !== "G1") continue;
      const get = (k) => { const m = line.match(new RegExp(`${k}([-\\d.]+)`, "i")); return m ? +m[1] : null; };
      const nx = get("X"), ny = get("Y"), nz = get("Z"), ne = get("E");
      const tx = nx === null ? x : rel ? x + nx : nx, ty = ny === null ? y : rel ? y + ny : ny, tz = nz === null ? z : rel ? z + nz : nz;
      let extrude = false;
      if (ne !== null) { const de = relE || rel ? ne : ne - e; extrude = de > 0; e = relE || rel ? e + ne : ne; }
      if (tz !== z) z = tz;
      if (extrude && (tx !== x || ty !== y)) {
        if (!cur || cur.z !== z) { cur = { z, segs: [] }; layers.push(cur); }
        cur.segs.push(x, y, tx, ty);
        minB[0] = Math.min(minB[0], x, tx); minB[1] = Math.min(minB[1], y, ty);
        maxB[0] = Math.max(maxB[0], x, tx); maxB[1] = Math.max(maxB[1], y, ty);
      }
      x = tx; y = ty;
    }
    layers.sort((a, b) => a.z - b.z);
    zs = layers.map((l) => l.z);
    bounds = { minB, maxB, maxZ: zs[zs.length - 1] || 1 };
    range.max = Math.max(0, layers.length - 1);
    range.value = range.max;
    draw();
  }).catch(() => { label.textContent = "Couldn't load G-code."; });
  range.addEventListener("input", draw);
  new ResizeObserver(draw).observe(wrap);

  function draw() {
    if (!bounds) return;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const W = canvas.clientWidth * dpr, H = canvas.clientHeight * dpr;
    canvas.width = W; canvas.height = H;
    const c = canvas.getContext("2d");
    c.clearRect(0, 0, W, H);
    const upTo = +range.value;
    label.textContent = layers.length ? `Layer ${upTo + 1} / ${layers.length} · Z ${zs[upTo]?.toFixed(2)} mm` : "No extrusion moves found";
    const cx = (bounds.minB[0] + bounds.maxB[0]) / 2, cy = (bounds.minB[1] + bounds.maxB[1]) / 2;
    const span = Math.max(bounds.maxB[0] - bounds.minB[0], bounds.maxB[1] - bounds.minB[1], 1);
    const s = Math.min(W, H) / (span * 1.6);
    const cos = Math.cos(-0.6), sin = Math.sin(-0.6);
    const P = (px, py, pz) => {
      const rx = (px - cx) * cos - (py - cy) * sin, ry = (px - cx) * sin + (py - cy) * cos;
      return [W / 2 + rx * s, H * 0.62 + ry * s * 0.5 - pz * s * 0.9];
    };
    // keep big files responsive: draw at most ~400k segments, skipping evenly through lower layers
    let total = 0;
    for (let i = 0; i <= upTo; i++) total += layers[i].segs.length / 4;
    const step = Math.max(1, Math.ceil(total / 400000));
    c.lineCap = "round";
    for (let i = 0; i <= upTo; i++) {
      const L = layers[i];
      const t = L.z / bounds.maxZ;
      c.strokeStyle = i === upTo ? "#ffd166" : `hsl(${20 + 200 * t}, 85%, ${45 + 10 * t}%)`;
      c.lineWidth = i === upTo ? 1.6 * dpr : 1 * dpr;
      c.beginPath();
      const segs = L.segs;
      for (let k = 0; k < segs.length; k += 4 * (i === upTo ? 1 : step)) {
        const a = P(segs[k], segs[k + 1], L.z), b = P(segs[k + 2], segs[k + 3], L.z);
        c.moveTo(a[0], a[1]); c.lineTo(b[0], b[1]);
      }
      c.stroke();
    }
  }
  return wrap;
}
