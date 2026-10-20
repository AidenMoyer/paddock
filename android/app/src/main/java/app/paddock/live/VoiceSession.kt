package app.paddock.live

import android.annotation.SuppressLint
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import app.paddock.PaddockApp
import app.paddock.data.PaddockJson
import kotlinx.serialization.Serializable
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
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

@Serializable data class VoiceMember(val id: Int, val user_id: Int, val username: String, val muted: Boolean = false,
    val deafened: Boolean = false, val speaking: Boolean = false, val codec: String = "opus")

/**
 * Voice chat built into Paddock (the server relays frames, see server/paddock/voice.py).
 *
 * Capture: the microphone in voice-communication mode (with the phone's echo canceller, noise suppressor and
 * gain control), 20 ms frames, a voice-activity gate, then Opus from the phone's own codec (Android 10+) or
 * 16 kHz PCM. Playback: one decoder and a small jitter buffer per speaker, mixed into a single AudioTrack.
 */
class VoiceSession(val room: String) {
    val members = mutableStateListOf<VoiceMember>()
    val speaking = mutableStateMapOf<Int, Boolean>()
    val muted = mutableStateOf(false)
    val deafened = mutableStateOf(false)
    val level = mutableFloatStateOf(0f)
    val error = mutableStateOf<String?>(null)
    var me: Int? = null
        private set

    private val main = Handler(Looper.getMainLooper())
    private var ws: WebSocket? = null
    private var record: AudioRecord? = null
    private var track: AudioTrack? = null
    private var encoder: MediaCodec? = null
    private val decoders = HashMap<Int, MediaCodec>()
    private val queues = HashMap<Int, ArrayDeque<ShortArray>>()
    private val started = HashMap<Int, Boolean>()
    @Volatile private var running = false
    @Volatile private var talking = false
    private var codec = "pcm16"
    private var seq = 0
    private var encPts = 0L
    private val decPts = HashMap<Int, Long>()

    fun othersSpeaking(): Boolean = speaking.any { (id, v) -> v && id != me }

    @SuppressLint("MissingPermission")   // the caller asks for RECORD_AUDIO first
    fun start() {
        running = true
        codec = if (Build.VERSION.SDK_INT >= 29 && hasCodec(MediaFormat.MIMETYPE_AUDIO_OPUS, encoder = true)) "opus" else "pcm16"
        val minIn = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, RATE, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, max(minIn, FRAME * 2 * 4))
        if (rec.state != AudioRecord.STATE_INITIALIZED) { error.value = "The microphone isn't available."; running = false; return }
        record = rec
        if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(rec.audioSessionId)?.enabled = true
        if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(rec.audioSessionId)?.enabled = true
        if (AutomaticGainControl.isAvailable()) AutomaticGainControl.create(rec.audioSessionId)?.enabled = true
        val minOut = AudioTrack.getMinBufferSize(RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
            .setBufferSizeInBytes(max(minOut, FRAME * 2 * 3)).setTransferMode(AudioTrack.MODE_STREAM).build()
            .also { it.play() }
        if (codec == "opus") encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS).apply {
            configure(MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, RATE, 1).apply {
                setInteger(MediaFormat.KEY_BIT_RATE, 24_000)
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, FRAME * 2)
            }, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            start()
        }
        connect()
        rec.startRecording()
        Thread(::captureLoop, "paddock-voice-in").apply { priority = Thread.MAX_PRIORITY }.start()
        Thread(::mixLoop, "paddock-voice-out").apply { priority = Thread.MAX_PRIORITY }.start()
    }

    private fun connect() {
        val app = PaddockApp.instance
        val client = app.conn.client ?: return
        val base = app.conn.state.value.baseUrl ?: return
        ws = client.newWebSocket(Request.Builder().url("$base/api/voice/$room/ws").build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                text { put("type", "state"); put("codec", codec); put("muted", muted.value); put("deafened", deafened.value) }
                if (talking) text { put("type", "speaking"); put("value", true) }
            }
            override fun onMessage(webSocket: WebSocket, text: String) { onText(text) }
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) { onAudio(bytes.toByteArray()) }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = dropped(code)
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = dropped(response?.code ?: 0)
        })
    }

    private fun dropped(code: Int) {
        if (!running) return
        if (code == 4403 || code == 4401) { main.post { error.value = "You can't join this voice chat." }; stop(); return }
        main.postDelayed({ if (running) connect() }, 1500)
    }

    private fun text(build: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit) { ws?.send(buildJsonObject(build).toString()) }

    private fun onText(raw: String) {
        val msg = runCatching { PaddockJson.parseToJsonElement(raw).jsonObject }.getOrNull() ?: return
        main.post {
            when (msg["type"]?.jsonPrimitive?.content) {
                "hello", "members" -> {
                    if (msg.containsKey("you")) me = msg["you"]!!.jsonPrimitive.content.toInt()
                    val list = PaddockJson.decodeFromJsonElement<List<VoiceMember>>(msg["members"]!!)
                    members.clear(); members.addAll(list)
                    speaking.keys.retainAll(list.map { it.id }.toSet())
                    list.forEach { if (it.speaking) speaking[it.id] = true }
                }
                "speaking" -> speaking[msg["id"]!!.jsonPrimitive.content.toInt()] = msg["value"]?.jsonPrimitive?.content == "true"
            }
        }
    }

    // ---- capture ------------------------------------------------------------------------------------
    private fun captureLoop() {
        val frame = ShortArray(FRAME)
        var floor = -1.0
        var lastVoice = 0L
        while (running) {
            var n = 0
            while (n < FRAME && running) {
                val r = record?.read(frame, n, FRAME - n) ?: -1
                if (r <= 0) break
                n += r
            }
            if (n < FRAME) continue
            var sum = 0.0
            for (s in frame) sum += (s / 32768.0) * (s / 32768.0)
            val rms = sqrt(sum / FRAME)
            main.post { level.floatValue = min(1f, (rms * 8).toFloat()) }
            floor = if (floor < 0) rms else min(max(floor * 1.002, 0.0005), max(rms, floor * 0.995))
            val now = System.currentTimeMillis()
            if (rms > max(0.008, floor * 2.5)) lastVoice = now
            val active = !muted.value && now - lastVoice < 400
            if (active != talking) {
                talking = active
                text { put("type", "speaking"); put("value", active) }
            }
            if (!active) continue
            if (codec == "opus") encodeOpus(frame) else sendPcm16(frame)
        }
    }

    private fun encodeOpus(frame: ShortArray) {
        val enc = encoder ?: return
        val inIdx = enc.dequeueInputBuffer(5_000)
        if (inIdx >= 0) {
            val buf = enc.getInputBuffer(inIdx)!!
            buf.clear(); buf.order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().put(frame)
            enc.queueInputBuffer(inIdx, 0, FRAME * 2, encPts, 0)
            encPts += 20_000
        }
        val info = MediaCodec.BufferInfo()
        while (true) {
            val outIdx = enc.dequeueOutputBuffer(info, 0)
            if (outIdx < 0) break
            if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0 && info.size > 0) {
                val out = enc.getOutputBuffer(outIdx)!!
                val packet = ByteArray(info.size)
                out.position(info.offset); out.get(packet)
                sendFrame(CODEC_OPUS, packet)
            }
            enc.releaseOutputBuffer(outIdx, false)
        }
    }

    private fun sendPcm16(frame: ShortArray) {
        val out = ByteBuffer.allocate(FRAME / 3 * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until FRAME / 3) out.putShort(((frame[i * 3] + frame[i * 3 + 1] + frame[i * 3 + 2]) / 3).toShort())
        sendFrame(CODEC_PCM16, out.array())
    }

    private fun sendFrame(c: Int, payload: ByteArray) {
        val frame = ByteArray(3 + payload.size)
        frame[0] = c.toByte(); frame[1] = (seq shr 8).toByte(); frame[2] = seq.toByte()
        payload.copyInto(frame, 3)
        seq = (seq + 1) and 0xffff
        ws?.send(frame.toByteString())
    }

    // ---- playback ----------------------------------------------------------------------------------
    private fun onAudio(d: ByteArray) { synchronized(queues) { onAudioLocked(d) } }

    private fun onAudioLocked(d: ByteArray) {
        if (!running || d.size < 6 || deafened.value) return
        val id = ((d[0].toInt() and 255) shl 8) or (d[1].toInt() and 255)
        val c = d[2].toInt()
        val payload = d.copyOfRange(5, d.size)
        val pcm: List<ShortArray> = when (c) {
            CODEC_OPUS -> decodeOpus(id, payload)
            CODEC_PCM16 -> {
                val src = ByteBuffer.wrap(payload).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
                val up = ShortArray(src.remaining() * 3)
                for (i in 0 until src.remaining()) { val s = src.get(i); up[i * 3] = s; up[i * 3 + 1] = s; up[i * 3 + 2] = s }
                listOf(up)
            }
            else -> emptyList()
        }
        if (pcm.isEmpty()) return
        val q = queues.getOrPut(id) { ArrayDeque() }
        pcm.forEach { q.addLast(it) }
        while (q.size > 20) q.removeFirst()              // more than 400 ms behind: catch up
    }

    private fun decodeOpus(id: Int, packet: ByteArray): List<ShortArray> {
        val dec = decoders.getOrPut(id) {
            MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS).apply {
                configure(MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, RATE, 1).apply {
                    setByteBuffer("csd-0", opusHead())
                    setByteBuffer("csd-1", nanos(0))
                    setByteBuffer("csd-2", nanos(80_000_000))
                }, null, null, 0)
                start()
            }
        }
        val inIdx = dec.dequeueInputBuffer(5_000)
        if (inIdx >= 0) {
            dec.getInputBuffer(inIdx)!!.apply { clear(); put(packet) }
            val pts = decPts.getOrDefault(id, 0L)
            dec.queueInputBuffer(inIdx, 0, packet.size, pts, 0)
            decPts[id] = pts + 20_000
        }
        val out = mutableListOf<ShortArray>()
        val info = MediaCodec.BufferInfo()
        while (true) {
            val outIdx = dec.dequeueOutputBuffer(info, 2_000)
            if (outIdx < 0) break
            if (info.size > 0) {
                val sb = dec.getOutputBuffer(outIdx)!!.order(ByteOrder.LITTLE_ENDIAN)
                sb.position(info.offset); sb.limit(info.offset + info.size)
                val shorts = ShortArray(info.size / 2)
                sb.asShortBuffer().get(shorts)
                out.add(shorts)
            }
            dec.releaseOutputBuffer(outIdx, false)
        }
        return out
    }

    private fun mixLoop() {
        val acc = IntArray(FRAME)
        val mix = ShortArray(FRAME)
        while (running) {
            acc.fill(0)
            synchronized(queues) {
                for ((id, q) in queues) {
                    if (started[id] != true && q.size >= 3) started[id] = true     // ~60 ms cushion before starting
                    if (started[id] != true) continue
                    var need = FRAME
                    var at = 0
                    while (need > 0) {
                        val f = q.firstOrNull() ?: break
                        val take = min(need, f.size)
                        for (i in 0 until take) acc[at + i] += f[i].toInt()
                        if (take == f.size) q.removeFirst() else q[0] = f.copyOfRange(take, f.size)
                        need -= take; at += take
                    }
                    if (need > 0) started[id] = false                              // ran dry: rebuffer
                }
            }
            for (i in 0 until FRAME) mix[i] = acc[i].coerceIn(-32768, 32767).toShort()
            if (deafened.value) mix.fill(0)
            track?.write(mix, 0, FRAME)                                             // blocks: paces this loop in real time
        }
    }

    fun setMuted(v: Boolean) {
        muted.value = v
        if (v && talking) { talking = false; text { put("type", "speaking"); put("value", false) } }
        text { put("type", "state"); put("muted", v) }
    }

    fun setDeafened(v: Boolean) {
        deafened.value = v
        if (v) setMuted(true)
        text { put("type", "state"); put("deafened", v); put("muted", muted.value) }
    }

    fun stop() {
        if (!running) return
        running = false
        ws?.close(1000, null)
        runCatching { record?.stop(); record?.release() }
        runCatching { track?.stop(); track?.release() }
        runCatching { encoder?.stop(); encoder?.release() }
        synchronized(queues) { decoders.values.forEach { runCatching { it.stop(); it.release() } }; decoders.clear(); queues.clear() }
    }

    companion object {
        const val RATE = 48_000
        const val FRAME = 960            // 20 ms
        const val CODEC_OPUS = 1
        const val CODEC_PCM16 = 2

        fun hasCodec(mime: String, encoder: Boolean) = MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos.any { info ->
            info.isEncoder == encoder && info.supportedTypes.any { it.equals(mime, ignoreCase = true) }
        }

        /** The Opus identification header decoders need: mono, 48 kHz, no pre-skip (raw 20 ms packets). */
        private fun opusHead(): ByteBuffer = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("OpusHead".toByteArray()); put(1); put(1); putShort(0); putInt(RATE); putShort(0); put(0); flip()
        }

        private fun nanos(v: Long): ByteBuffer = ByteBuffer.allocate(8).order(ByteOrder.nativeOrder()).apply { putLong(v); flip() }
    }
}
