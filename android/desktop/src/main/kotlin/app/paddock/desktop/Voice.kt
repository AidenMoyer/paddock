package app.paddock.desktop

import app.paddock.data.PaddockJson
import app.paddock.live.VoiceGate
import app.paddock.live.VoiceMember
import app.paddock.live.VoiceMixer
import app.paddock.live.VoiceWire
import app.paddock.live.VoiceWire.CODEC_OPUS
import app.paddock.live.VoiceWire.CODEC_PCM16
import app.paddock.live.VoiceWire.FRAME
import app.paddock.live.VoiceWire.RATE
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import io.github.jaredmdobson.concentus.OpusApplication
import io.github.jaredmdobson.concentus.OpusDecoder
import io.github.jaredmdobson.concentus.OpusEncoder
import java.util.Timer
import java.util.TimerTask
import javax.sound.sampled.AudioFormat
import javax.sound.sampled.AudioSystem
import javax.sound.sampled.DataLine
import javax.sound.sampled.SourceDataLine
import javax.sound.sampled.TargetDataLine

/**
 * Voice chat on the computer: the same rooms, frames and Opus as the phone and the web (server/paddock/voice.py).
 * One room at a time, kept while you move around the app. Computers have no built-in echo canceller here,
 * so headphones are best; the voice gate keeps the room quiet between words.
 */
object Voice {
    val room = MutableStateFlow<String?>(null)
    val title = MutableStateFlow("")
    val members = MutableStateFlow<List<VoiceMember>>(emptyList())
    val speaking = MutableStateFlow<Set<Int>>(emptySet())
    val muted = MutableStateFlow(false)
    val deafened = MutableStateFlow(false)
    val level = MutableStateFlow(0f)
    val error = MutableStateFlow<String?>(null)
    @Volatile var me: Int? = null
        private set

    private var ws: WebSocket? = null
    private var mic: TargetDataLine? = null
    private var out: SourceDataLine? = null
    private var encoder: OpusEncoder? = null
    private val decoders = HashMap<Int, OpusDecoder>()
    private val mixer = VoiceMixer()
    private val gate = VoiceGate()
    @Volatile private var running = false
    @Volatile private var talking = false
    private var seq = 0
    private var retry: Timer? = null

    fun othersSpeaking(): Boolean = speaking.value.any { it != me }

    fun join(roomId: String, label: String) {
        if (room.value == roomId) return
        leave()
        error.value = null
        val format = AudioFormat(RATE.toFloat(), 16, 1, true, false)
        try {
            out = (AudioSystem.getLine(DataLine.Info(SourceDataLine::class.java, format)) as SourceDataLine).apply { open(format, FRAME * 2 * 4); start() }
        } catch (e: Exception) { error.value = "No speakers or headphones are available."; return }
        mic = runCatching {
            (AudioSystem.getLine(DataLine.Info(TargetDataLine::class.java, format)) as TargetDataLine).apply { open(format, FRAME * 2 * 4); start() }
        }.getOrNull()
        if (mic == null) { muted.value = true; error.value = "No microphone found. You can listen, but others won't hear you." }
        encoder = runCatching { OpusEncoder(RATE, 1, OpusApplication.OPUS_APPLICATION_VOIP).apply { setBitrate(24_000) } }.getOrNull()
        running = true
        room.value = roomId
        title.value = label
        connect()
        Thread(::captureLoop, "paddock-voice-in").apply { isDaemon = true; priority = Thread.MAX_PRIORITY }.start()
        Thread(::mixLoop, "paddock-voice-out").apply { isDaemon = true; priority = Thread.MAX_PRIORITY }.start()
    }

    private fun connect() {
        val client = Connection.client ?: return
        val base = Connection.state.value.baseUrl ?: return scheduleRetry()
        val id = room.value ?: return
        ws = client.newWebSocket(Request.Builder().url("$base/api/voice/$id/ws").build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                text { put("type", "state"); put("codec", if (encoder != null) "opus" else "pcm16"); put("muted", muted.value); put("deafened", deafened.value) }
                if (talking) text { put("type", "speaking"); put("value", true) }
            }
            override fun onMessage(webSocket: WebSocket, text: String) = onText(text)
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) = onAudio(bytes.toByteArray())
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = dropped(code)
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = dropped(response?.code ?: 0)
        })
    }

    private fun scheduleRetry() {
        retry?.cancel()
        retry = Timer(true).apply { schedule(object : TimerTask() { override fun run() { if (running) connect() } }, 1500) }
    }

    private fun dropped(code: Int) {
        if (!running) return
        if (code == 4403 || code == 4401) { error.value = "You can't join this voice chat."; leave(); return }
        scheduleRetry()
    }

    private fun text(build: JsonObjectBuilder.() -> Unit) { ws?.send(buildJsonObject(build).toString()) }

    private fun onText(raw: String) {
        val msg = runCatching { PaddockJson.parseToJsonElement(raw).jsonObject }.getOrNull() ?: return
        when (msg["type"]?.jsonPrimitive?.content) {
            "hello", "members" -> {
                if (msg.containsKey("you")) me = msg["you"]!!.jsonPrimitive.content.toInt()
                val list = PaddockJson.decodeFromJsonElement<List<VoiceMember>>(msg["members"]!!)
                members.value = list
                speaking.value = list.filter { it.speaking }.map { it.id }.toSet()
            }
            "speaking" -> {
                val id = msg["id"]!!.jsonPrimitive.content.toInt()
                val on = msg["value"]?.jsonPrimitive?.content == "true"
                speaking.value = if (on) speaking.value + id else speaking.value - id
            }
        }
    }

    private fun captureLoop() {
        val bytes = ByteArray(FRAME * 2)
        val frame = ShortArray(FRAME)
        val packet = ByteArray(1275)
        while (running) {
            val line = mic
            if (line == null) { Thread.sleep(200); continue }
            var n = 0
            while (n < bytes.size && running) { val r = line.read(bytes, n, bytes.size - n); if (r <= 0) break; n += r }
            if (n < bytes.size) continue
            for (i in 0 until FRAME) frame[i] = ((bytes[i * 2 + 1].toInt() shl 8) or (bytes[i * 2].toInt() and 255)).toShort()
            val active = gate.active(frame, muted.value)
            level.value = gate.level
            if (active != talking) { talking = active; text { put("type", "speaking"); put("value", active) } }
            if (!active) continue
            val enc = encoder
            if (enc != null) {
                val len = runCatching { enc.encode(frame, 0, FRAME, packet, 0, packet.size) }.getOrDefault(0)
                if (len > 0) send(CODEC_OPUS, packet.copyOf(len))
            } else send(CODEC_PCM16, VoiceWire.toPcm16(frame))
        }
    }

    private fun send(codec: Int, payload: ByteArray) {
        ws?.send(VoiceWire.frame(codec, seq, payload).toByteString())
        seq = (seq + 1) and 0xffff
    }

    private fun onAudio(d: ByteArray) {
        if (!running || deafened.value) return
        val f = VoiceWire.parse(d) ?: return
        when (f.codec) {
            CODEC_OPUS -> synchronized(decoders) {
                val dec = decoders.getOrPut(f.speaker) { OpusDecoder(RATE, 1) }
                val pcm = ShortArray(FRAME * 3)      // up to 60 ms in one packet
                val n = runCatching { dec.decode(f.payload, 0, f.payload.size, pcm, 0, pcm.size, false) }.getOrDefault(0)
                if (n > 0) mixer.add(f.speaker, pcm.copyOf(n))
            }
            CODEC_PCM16 -> mixer.add(f.speaker, VoiceWire.fromPcm16(f.payload))
        }
    }

    private fun mixLoop() {
        val mix = ShortArray(FRAME)
        val bytes = ByteArray(FRAME * 2)
        while (running) {
            mixer.mix(mix)
            if (deafened.value) mix.fill(0)
            for (i in 0 until FRAME) { bytes[i * 2] = mix[i].toByte(); bytes[i * 2 + 1] = (mix[i].toInt() shr 8).toByte() }
            out?.write(bytes, 0, bytes.size) ?: break                         // blocks: paces this loop in real time
        }
    }

    fun setMuted(v: Boolean) {
        if (!v && mic == null) return
        muted.value = v
        if (v && talking) { talking = false; text { put("type", "speaking"); put("value", false) } }
        text { put("type", "state"); put("muted", v) }
    }

    fun setDeafened(v: Boolean) {
        deafened.value = v
        if (v) setMuted(true)
        text { put("type", "state"); put("deafened", v); put("muted", muted.value) }
    }

    fun leave() {
        if (!running && room.value == null) return
        running = false
        retry?.cancel()
        ws?.close(1000, null); ws = null
        runCatching { mic?.stop(); mic?.close() }; mic = null
        runCatching { out?.stop(); out?.close() }; out = null
        synchronized(decoders) { decoders.clear() }
        mixer.clear()
        room.value = null; members.value = emptyList(); speaking.value = emptySet(); level.value = 0f; talking = false
    }
}
