// Voice chat, built into Paddock: microphone → 20 ms Opus frames → Paddock relays them → everyone else
// decodes and mixes. No outside servers; it runs over the same HTTPS connection as the rest of the app.
//
//   const v = new VoiceSession("party:ABC123"); await v.start(); v.setMuted(true); v.stop();
//   events: "roster" (detail: members), "speaking" (detail: {id, value}), "level" (detail: 0..1), "error"

const CODEC_OPUS = 1;
const CODEC_PCM16 = 2;

export const voiceSupported = () => !!(navigator.mediaDevices?.getUserMedia && window.WebSocket && window.AudioWorkletNode);

export class VoiceSession extends EventTarget {
  constructor(room) {
    super();
    this.room = room;
    this.members = [];
    this.me = null;
    this.muted = false;
    this.deafened = false;
    this.speaking = false;
    this.seq = 0;
    this.speakers = new Map();   // id -> { decoder, next, gain }
    this.closed = false;
  }

  async start() {
    this.ctx = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
    this.out = this.ctx.createGain();
    this.out.connect(this.ctx.destination);
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (e) {
      this.ctx.close();
      throw new Error(e.name === "NotAllowedError" ? "Microphone access was blocked. Allow it in the browser's site settings." : `No microphone: ${e.message}`);
    }
    this.codec = await this.pickCodec();
    await this.ctx.audioWorklet.addModule("/static/js/voice-worklet.js");
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.collector = new AudioWorkletNode(this.ctx, "paddock-frames");
    this.collector.port.onmessage = (e) => this.onFrame(e.data);
    src.connect(this.collector);
    this.connect();
  }

  async pickCodec() {
    const cfg = { codec: "opus", sampleRate: this.ctx.sampleRate, numberOfChannels: 1, bitrate: 24000,
      opus: { frameDuration: 20000, application: "voip", complexity: 5 } };
    try {
      if (window.AudioEncoder && (await AudioEncoder.isConfigSupported(cfg)).supported) {
        this.encoder = new AudioEncoder({
          output: (chunk) => {
            const data = new Uint8Array(chunk.byteLength);
            chunk.copyTo(data);
            this.send(CODEC_OPUS, data);
          },
          error: (e) => this.fail(`Audio encoder: ${e.message}`),
        });
        this.encoder.configure(cfg);
        return "opus";
      }
    } catch { /* fall through to PCM */ }
    return "pcm16";
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/api/voice/${encodeURIComponent(this.room)}/ws`);
    this.ws.binaryType = "arraybuffer";
    this.ws.onopen = () => {
      this.ws.send(JSON.stringify({ type: "state", codec: this.codec, muted: this.muted, deafened: this.deafened }));
      if (this.speaking) this.ws.send(JSON.stringify({ type: "speaking", value: true }));   // started talking while connecting
    };
    this.ws.onmessage = (e) => (typeof e.data === "string" ? this.onText(JSON.parse(e.data)) : this.onAudio(e.data));
    this.ws.onclose = (e) => {
      if (this.closed) return;
      if (e.code === 4403 || e.code === 4401) { this.fail("You can't join this voice chat."); return; }
      setTimeout(() => !this.closed && this.connect(), 1500);   // network blip: rejoin
    };
  }

  onText(msg) {
    if (msg.type === "hello") { this.me = msg.you; this.setRoster(msg.members); }
    else if (msg.type === "members") this.setRoster(msg.members);
    else if (msg.type === "speaking") {
      const m = this.members.find((x) => x.id === msg.id);
      if (m) m.speaking = msg.value;
      this.dispatchEvent(new CustomEvent("speaking", { detail: { id: msg.id, value: msg.value } }));
    }
  }

  setRoster(members) {
    this.members = members;
    for (const id of [...this.speakers.keys()]) if (!members.some((m) => m.id === id)) this.dropSpeaker(id);
    this.dispatchEvent(new CustomEvent("roster", { detail: members }));
  }

  // ---- sending ----
  onFrame(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    const rms = Math.sqrt(sum / samples.length);
    this.dispatchEvent(new CustomEvent("level", { detail: Math.min(1, rms * 8) }));
    // voice-activity gate: adaptive noise floor, 400 ms hang-over so word endings aren't clipped
    this.floor = this.floor === undefined ? rms : Math.min(Math.max(this.floor * 1.002, 0.0005), Math.max(rms, this.floor * 0.995));
    const now = performance.now();
    if (rms > Math.max(0.008, this.floor * 2.5)) this.lastVoice = now;
    const active = !this.muted && now - (this.lastVoice || 0) < 400;
    if (active !== this.speaking) {
      this.speaking = active;
      this.text({ type: "speaking", value: active });
    }
    if (!active || this.ws?.readyState !== 1) return;
    if (this.codec === "opus") {
      this.ts = (this.ts || 0) + 20000;
      this.encoder.encode(new AudioData({ format: "f32", sampleRate: this.ctx.sampleRate, numberOfFrames: samples.length,
        numberOfChannels: 1, timestamp: this.ts, data: samples }));
    } else {
      const step = this.ctx.sampleRate / 16000;
      const out = new Int16Array(Math.floor(samples.length / step));
      for (let i = 0; i < out.length; i++) {
        let s = 0;
        const a = Math.floor(i * step), b = Math.floor((i + 1) * step);
        for (let j = a; j < b; j++) s += samples[j];
        out[i] = Math.max(-1, Math.min(1, s / (b - a))) * 32767;
      }
      this.send(CODEC_PCM16, new Uint8Array(out.buffer));
    }
  }

  send(codec, payload) {
    if (this.ws?.readyState !== 1) return;
    const frame = new Uint8Array(3 + payload.length);
    frame[0] = codec;
    frame[1] = (this.seq >> 8) & 255;
    frame[2] = this.seq & 255;
    frame.set(payload, 3);
    this.seq = (this.seq + 1) & 0xffff;
    this.ws.send(frame);
  }

  text(msg) { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(msg)); }

  // ---- receiving: per-speaker decode + small jitter buffer, Web Audio mixes them ----
  onAudio(buf) {
    if (this.deafened) return;
    const d = new Uint8Array(buf);
    if (d.length < 6) return;
    const id = (d[0] << 8) | d[1], codec = d[2];
    const payload = d.subarray(5);
    const sp = this.speaker(id);
    if (codec === CODEC_OPUS) {
      if (!sp.decoder) {
        if (!window.AudioDecoder) { this.warnOnce("This browser can't play everyone's voices. Chrome, Edge or Firefox can."); return; }
        sp.decoder = new AudioDecoder({ output: (data) => this.play(sp, data), error: () => { sp.decoder = null; } });
        sp.decoder.configure({ codec: "opus", sampleRate: 48000, numberOfChannels: 1 });
      }
      sp.ts = (sp.ts || 0) + 20000;
      sp.decoder.decode(new EncodedAudioChunk({ type: "key", timestamp: sp.ts, data: payload }));
    } else if (codec === CODEC_PCM16) {
      const pcm = new Int16Array(payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.length));
      const ab = this.ctx.createBuffer(1, pcm.length, 16000);
      const ch = ab.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
      this.schedule(sp, ab);
    }
  }

  play(sp, data) {
    const ab = this.ctx.createBuffer(1, data.numberOfFrames, data.sampleRate);
    data.copyTo(ab.getChannelData(0), { planeIndex: 0, format: "f32-planar" });
    data.close();
    this.schedule(sp, ab);
  }

  schedule(sp, ab) {
    const now = this.ctx.currentTime;
    if (sp.next < now + 0.02) sp.next = now + 0.06;          // first frame, or after a gap: small cushion
    if (sp.next > now + 0.4) return;                          // too far behind: drop to catch up
    const src = this.ctx.createBufferSource();
    src.buffer = ab;
    src.connect(sp.gain);
    src.start(sp.next);
    sp.next += ab.duration;
  }

  speaker(id) {
    let sp = this.speakers.get(id);
    if (!sp) {
      const gain = this.ctx.createGain();
      gain.connect(this.out);
      sp = { decoder: null, next: 0, gain };
      this.speakers.set(id, sp);
    }
    return sp;
  }

  dropSpeaker(id) {
    const sp = this.speakers.get(id);
    try { sp?.decoder?.close(); } catch { /* already closed */ }
    sp?.gain.disconnect();
    this.speakers.delete(id);
  }

  warnOnce(msg) { if (!this.warned) { this.warned = true; this.fail(msg, false); } }

  fail(message, fatal = true) {
    this.dispatchEvent(new CustomEvent("error", { detail: message }));
    if (fatal) this.stop();
  }

  setMuted(v) {
    this.muted = !!v;
    if (this.muted && this.speaking) { this.speaking = false; this.text({ type: "speaking", value: false }); }
    this.stream?.getAudioTracks().forEach((t) => { t.enabled = !this.muted; });
    this.text({ type: "state", muted: this.muted });
  }

  setDeafened(v) {
    this.deafened = !!v;
    this.out.gain.value = this.deafened ? 0 : 1;
    if (this.deafened) this.setMuted(true);
    this.text({ type: "state", deafened: this.deafened, muted: this.muted });
  }

  stop() {
    if (this.closed) return;
    this.closed = true;
    try { this.ws?.close(); } catch { /* closing */ }
    this.stream?.getTracks().forEach((t) => t.stop());
    for (const id of [...this.speakers.keys()]) this.dropSpeaker(id);
    try { this.encoder?.close(); } catch { /* closed */ }
    this.ctx?.close();
  }
}
