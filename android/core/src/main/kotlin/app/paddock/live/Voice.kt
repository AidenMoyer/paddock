package app.paddock.live

import kotlinx.serialization.Serializable
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

// Voice chat wire format (server/paddock/voice.py), shared by the Android and desktop apps.
// Client → server: [codec u8][seq u16][payload]. Server → clients: [speaker u16][codec u8][seq u16][payload].

@Serializable data class VoiceMember(val id: Int, val user_id: Int, val username: String, val muted: Boolean = false,
    val deafened: Boolean = false, val speaking: Boolean = false, val codec: String = "opus")

object VoiceWire {
    const val RATE = 48_000
    const val FRAME = 960            // 20 ms
    const val CODEC_OPUS = 1
    const val CODEC_PCM16 = 2        // 16 kHz mono, for devices without an Opus encoder

    fun frame(codec: Int, seq: Int, payload: ByteArray): ByteArray {
        val out = ByteArray(3 + payload.size)
        out[0] = codec.toByte(); out[1] = (seq shr 8).toByte(); out[2] = seq.toByte()
        payload.copyInto(out, 3)
        return out
    }

    class Incoming(val speaker: Int, val codec: Int, val seq: Int, val payload: ByteArray)

    fun parse(d: ByteArray): Incoming? {
        if (d.size < 6) return null
        return Incoming(((d[0].toInt() and 255) shl 8) or (d[1].toInt() and 255), d[2].toInt(),
            ((d[3].toInt() and 255) shl 8) or (d[4].toInt() and 255), d.copyOfRange(5, d.size))
    }

    /** 48 kHz → 16 kHz little-endian PCM (average of each three samples). */
    fun toPcm16(frame: ShortArray): ByteArray {
        val out = ByteBuffer.allocate(frame.size / 3 * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until frame.size / 3) out.putShort(((frame[i * 3] + frame[i * 3 + 1] + frame[i * 3 + 2]) / 3).toShort())
        return out.array()
    }

    /** 16 kHz little-endian PCM → 48 kHz samples. */
    fun fromPcm16(payload: ByteArray): ShortArray {
        val src = ByteBuffer.wrap(payload).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
        val up = ShortArray(src.remaining() * 3)
        for (i in 0 until src.remaining()) { val s = src.get(i); up[i * 3] = s; up[i * 3 + 1] = s; up[i * 3 + 2] = s }
        return up
    }
}

/** Voice-activity gate: an adaptive noise floor, and 400 ms of hang time so words don't get clipped. */
class VoiceGate {
    private var floor = -1.0
    private var lastVoice = Long.MIN_VALUE / 2
    var level = 0f
        private set

    fun active(frame: ShortArray, muted: Boolean, now: Long = System.currentTimeMillis()): Boolean {
        var sum = 0.0
        for (s in frame) sum += (s / 32768.0) * (s / 32768.0)
        val rms = sqrt(sum / frame.size)
        level = min(1f, (rms * 8).toFloat())
        floor = if (floor < 0) rms else min(max(floor * 1.002, 0.0005), max(rms, floor * 0.995))
        if (rms > max(0.008, floor * 2.5)) lastVoice = now
        return !muted && now - lastVoice < 400
    }
}

/** Per-speaker jitter buffers mixed into one 20 ms frame at a time. */
class VoiceMixer {
    private val queues = HashMap<Int, ArrayDeque<ShortArray>>()
    private val started = HashMap<Int, Boolean>()
    private val acc = IntArray(VoiceWire.FRAME)

    @Synchronized fun add(speaker: Int, pcm: ShortArray) {
        val q = queues.getOrPut(speaker) { ArrayDeque() }
        q.addLast(pcm)
        while (q.size > 20) q.removeFirst()              // more than 400 ms behind: catch up
    }

    @Synchronized fun clear() { queues.clear(); started.clear() }

    @Synchronized fun mix(out: ShortArray) {
        acc.fill(0)
        for ((id, q) in queues) {
            if (started[id] != true && q.size >= 3) started[id] = true     // ~60 ms cushion before starting
            if (started[id] != true) continue
            var need = VoiceWire.FRAME
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
        for (i in 0 until VoiceWire.FRAME) out[i] = acc[i].coerceIn(-32768, 32767).toShort()
    }
}
