package app.paddock.live

import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ProcessLifecycleOwner
import androidx.media3.common.PlaybackParameters
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import app.paddock.PaddockApp
import app.paddock.data.PaddockJson
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import kotlin.math.abs

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
 * One phone's place in a watch party: follows the server's playback clock with the shared ExoPlayer and
 * sends this person's own play / pause / seek. See server/paddock/party.py for the protocol.
 */
class PartySession(val id: String, private val player: ExoPlayer, private var itemId: Int) {
    val info = mutableStateOf<PartyInfo?>(null)
    val members = mutableStateListOf<PartyMember>()
    val chat = mutableStateListOf<ChatLine>()
    val reactions = mutableStateListOf<Reaction>()
    val status = mutableStateOf("Connecting…")
    val ended = mutableStateOf<String?>(null)
    val switchTo = mutableStateOf<Int?>(null)
    val canControl = mutableStateOf(true)
    var myUserId = 0
        private set
    private var myName = ""

    private val main = Handler(Looper.getMainLooper())
    private var ws: WebSocket? = null
    private var state: PartyState? = null
    private val offsets = ArrayDeque<Double>()
    private var suppressUntil = 0L
    private var buffering = false
    private var closed = false

    private val visible get() = ProcessLifecycleOwner.get().lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)

    private val listener = object : Player.Listener {
        override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) {
            if (!mine() || reason != Player.PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST) return
            send(if (playWhenReady) "play" else "pause") { put("position", pos()) }
        }
        override fun onPositionDiscontinuity(old: Player.PositionInfo, new: Player.PositionInfo, reason: Int) {
            if (reason == Player.DISCONTINUITY_REASON_SEEK && mine()) send("seek") { put("position", pos()) }
        }
        override fun onPlaybackStateChanged(s: Int) {
            main.removeCallbacks(bufferingStarted)
            if (s == Player.STATE_BUFFERING && visible) main.postDelayed(bufferingStarted, 400)
            else if (s == Player.STATE_READY && buffering) { buffering = false; send("buffering") { put("value", false) } }
        }
    }
    private val bufferingStarted = Runnable { buffering = true; send("buffering") { put("value", true) } }
    private val tick = object : Runnable {
        override fun run() { if (!closed) { correct(false); main.postDelayed(this, 500) } }
    }
    private val pinger = object : Runnable {
        override fun run() { if (!closed) { ping(); main.postDelayed(this, 10_000) } }
    }

    // Only deliberate actions count: Android pausing playback because the app went to the background
    // must never pause everyone else.
    private fun mine() = System.currentTimeMillis() > suppressUntil && visible
    private fun quiet(ms: Long = 800) { suppressUntil = System.currentTimeMillis() + ms }
    private fun pos() = player.currentPosition / 1000.0

    fun start() {
        player.addListener(listener)
        connect()
        main.post(tick)
        main.postDelayed(pinger, 10_000)
    }

    private fun connect() {
        val app = PaddockApp.instance
        val client = app.conn.client ?: return
        val base = app.conn.state.value.baseUrl ?: run { main.postDelayed({ if (!closed) connect() }, 2000); return }
        ws = client.newWebSocket(Request.Builder().url("$base/api/parties/$id/ws").build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) { repeat(4) { i -> main.postDelayed({ ping() }, i * 250L) } }
            override fun onMessage(webSocket: WebSocket, text: String) { main.post { onMessage(text) } }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) { main.post { dropped(code) } }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { main.post { dropped(response?.code ?: 0) } }
        })
    }

    private fun dropped(code: Int) {
        if (closed) return
        if (code == 4404 || code == 4401 || code == 403 || code == 404) { ended.value = "This watch party has ended."; return }
        status.value = "Reconnecting…"
        main.postDelayed({ if (!closed) connect() }, 1500)
    }

    private fun serverNow(): Double {
        val now = System.currentTimeMillis() / 1000.0
        if (offsets.isEmpty()) return now
        return now + offsets.sorted()[offsets.size / 2]
    }

    private fun ping() = send("ping") { put("t0", System.currentTimeMillis() / 1000.0) }

    private fun send(type: String, extra: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit = {}) {
        ws?.send(buildJsonObject { put("type", type); extra() }.toString())
    }

    private fun onMessage(text: String) {
        val msg = runCatching { PaddockJson.parseToJsonElement(text).jsonObject }.getOrNull() ?: return
        when (msg["type"]?.jsonPrimitive?.content) {
            "pong" -> {
                val t0 = msg["t0"]!!.jsonPrimitive.content.toDouble()
                val t1 = System.currentTimeMillis() / 1000.0
                offsets.addLast(msg["server"]!!.jsonPrimitive.content.toDouble() - (t0 + (t1 - t0) / 2))
                while (offsets.size > 7) offsets.removeFirst()
            }
            "hello" -> {
                myUserId = msg["you"]!!.jsonObject["user_id"]!!.jsonPrimitive.content.toInt()
                myName = msg["you"]!!.jsonObject["username"]!!.jsonPrimitive.content
                canControl.value = msg["can_control"]?.jsonPrimitive?.content == "true"
                offsets.clear(); offsets.addLast(msg["server"]!!.jsonPrimitive.content.toDouble() - System.currentTimeMillis() / 1000.0)
                val p = PaddockJson.decodeFromJsonElement<PartyInfo>(msg["party"]!!)
                info.value = p
                members.clear(); members.addAll(p.members)
                chat.clear()
                (msg["chat"] as? kotlinx.serialization.json.JsonArray)?.forEach { c -> chatLine(c.jsonObject) }
                status.value = ""
                apply(PaddockJson.decodeFromJsonElement(msg["state"]!!), null, null)
            }
            "state" -> apply(PaddockJson.decodeFromJsonElement(msg["state"]!!), msg["by"]?.jsonPrimitive?.content, msg["action"]?.jsonPrimitive?.content)
            "members" -> { members.clear(); members.addAll(PaddockJson.decodeFromJsonElement<List<PartyMember>>(msg["members"]!!)) }
            "chat", "notice" -> chatLine(msg)
            "reaction" -> {
                val r = Reaction(msg["emoji"]!!.jsonPrimitive.content, msg["user"]?.jsonPrimitive?.content ?: "", System.nanoTime())
                reactions.add(r)
                main.postDelayed({ reactions.remove(r) }, 2600)
            }
            "error" -> status.value = msg["error"]?.jsonPrimitive?.content ?: ""
            "ended" -> ended.value = "The host ended the watch party."
        }
    }

    private fun chatLine(m: JsonObject) {
        val user = if (m["type"]?.jsonPrimitive?.content == "notice") null else m["user"]?.jsonPrimitive?.content
        chat.add(ChatLine(user, m["text"]?.jsonPrimitive?.content ?: ""))
        while (chat.size > 60) chat.removeAt(0)
    }

    private fun apply(s: PartyState, by: String?, action: String?) {
        state = s
        if (s.item_id != itemId) { switchTo.value = s.item_id; return }
        status.value = when {
            s.hold -> "Waiting for ${s.waiting_for.joinToString()}…"
            by != null && by != myName && action in setOf("play", "pause", "seek") ->
                "$by ${mapOf("play" to "pressed play", "pause" to "paused", "seek" to "skipped")[action]}"
            else -> ""
        }
        correct(true)
    }

    private fun target(s: PartyState): Double {
        val now = serverNow()
        return if (s.playing && now > s.at) s.position + (now - s.at) * s.rate else s.position
    }

    /** Steer the local player toward the shared clock: small drift by speed, big drift by seeking. */
    private fun correct(force: Boolean) {
        val s = state ?: return
        if (s.item_id != itemId || player.playbackState == Player.STATE_IDLE) return
        val target = target(s)
        val diff = pos() - target
        if (s.playing) {
            if (!player.playWhenReady) {
                val startsIn = s.at - serverNow()
                if (abs(diff) > 0.3) { quiet(); player.seekTo((target * 1000).toLong()) }
                if (startsIn > 0.05) main.postDelayed({ if (state === s) { quiet(); player.play() } }, (startsIn * 1000).toLong())
                else { quiet(); player.play() }
                return
            }
            val rate = when {
                abs(diff) > 1.0 || (force && abs(diff) > 0.5) -> { quiet(); player.seekTo((target * 1000).toLong()); s.rate }
                abs(diff) > 0.12 -> s.rate * (if (diff > 0) 0.95 else 1.05)
                else -> s.rate
            }
            if (abs(player.playbackParameters.speed - rate) > 0.001) player.playbackParameters = PlaybackParameters(rate.toFloat())
        } else {
            if (player.playWhenReady) { quiet(); player.pause() }
            if (abs(diff) > 0.3) { quiet(); player.seekTo((target * 1000).toLong()) }
        }
    }

    fun chat(text: String) = send("chat") { put("text", text.take(500)) }
    fun react(emoji: String) = send("reaction") { put("emoji", emoji) }
    fun switchItem(item: Int, title: String) = send("item") { put("item_id", item); put("title", title); put("play", true) }

    fun close() {
        closed = true
        main.removeCallbacksAndMessages(null)
        player.removeListener(listener)
        player.playbackParameters = PlaybackParameters(1f)
        ws?.close(1000, null)
    }
}
