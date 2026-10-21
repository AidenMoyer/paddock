package app.paddock.live

import kotlinx.serialization.Serializable

// The watch-party protocol (server/paddock/party.py), shared by the Android and desktop apps.

@Serializable data class PartyState(val item_id: Int = 0, val title: String = "", val playing: Boolean = false,
    val intent_playing: Boolean = false, val position: Double = 0.0, val at: Double = 0.0, val rate: Double = 1.0,
    val hold: Boolean = false, val waiting_for: List<String> = emptyList())
@Serializable data class PartyMember(val user_id: Int, val username: String, val buffering: Boolean = false,
    val host: Boolean = false, val devices: Int = 1)
@Serializable data class PartyInfo(val id: String, val host: String = "", val host_id: Int = 0, val item_id: Int = 0,
    val title: String = "", val everyone: Boolean = true, val control: String = "everyone",
    val members: List<PartyMember> = emptyList(), val state: PartyState? = null)
data class ChatLine(val user: String?, val text: String)
data class Reaction(val emoji: String, val user: String, val key: Long)

val REACTIONS = listOf("😂", "❤️", "😮", "👏", "😢", "🔥", "👀", "🎉")

/**
 * The server's playback clock as seen from this device: ping/pong offsets (median of the last seven, so one slow
 * round trip doesn't jolt everyone) and where the shared playhead is right now.
 */
class PartyClock {
    private val offsets = ArrayDeque<Double>()

    private fun local() = System.currentTimeMillis() / 1000.0

    fun hello(server: Double) { offsets.clear(); offsets.addLast(server - local()) }

    fun pong(t0: Double, server: Double) {
        val t1 = local()
        offsets.addLast(server - (t0 + (t1 - t0) / 2))
        while (offsets.size > 7) offsets.removeFirst()
    }

    fun now(): Double = if (offsets.isEmpty()) local() else local() + offsets.sorted()[offsets.size / 2]

    fun target(s: PartyState): Double {
        val now = now()
        return if (s.playing && now > s.at) s.position + (now - s.at) * s.rate else s.position
    }
}
