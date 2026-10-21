package app.paddock.desktop

import app.paddock.data.PaddockJson
import app.paddock.live.ChatLine
import app.paddock.live.PartyClock
import app.paddock.live.PartyInfo
import app.paddock.live.PartyMember
import app.paddock.live.PartyState
import app.paddock.live.QueueEntry
import app.paddock.live.Reaction
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
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
import kotlin.math.abs

/**
 * This computer's place in a watch party: follows the server's clock (server/paddock/party.py) with the
 * player, and sends what this person does with Paddock's controls. The steering matches the phone and web:
 * small drift is corrected by playing 5% faster or slower, big drift by seeking.
 */
class PartyClient(val id: String, private val player: VideoPlayer, private var itemId: Int) {
    val info = MutableStateFlow<PartyInfo?>(null)
    val members = MutableStateFlow<List<PartyMember>>(emptyList())
    val chat = MutableStateFlow<List<ChatLine>>(emptyList())
    val reactions = MutableStateFlow<List<Reaction>>(emptyList())
    val status = MutableStateFlow("Connecting…")
    val ended = MutableStateFlow<String?>(null)
    val switchTo = MutableStateFlow<Int?>(null)
    val canControl = MutableStateFlow(true)
    val queue = MutableStateFlow<List<QueueEntry>>(emptyList())
    private var fallback: Pair<Int, String>? = null

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val clock = PartyClock()
    private var ws: WebSocket? = null
    private var state: PartyState? = null
    private var myName = ""
    private var buffering = false
    private var closed = false

    fun start() {
        connect()
        scope.launch { while (!closed) { delay(500); correct(false); watchBuffering() } }
        scope.launch { while (!closed) { delay(10_000); ping() } }
    }

    private fun connect() {
        val client = Connection.client ?: return
        val base = Connection.state.value.baseUrl ?: run { scope.launch { delay(2000); if (!closed) connect() }; return }
        ws = client.newWebSocket(Request.Builder().url("$base/api/parties/$id/ws").build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) { scope.launch { repeat(4) { ping(); delay(250) } } }
            override fun onMessage(webSocket: WebSocket, text: String) = onMessage(text)
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = dropped(code)
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = dropped(response?.code ?: 0)
        })
    }

    private fun dropped(code: Int) {
        if (closed) return
        if (code in setOf(4404, 4401, 403, 404)) { ended.value = "This watch party has ended."; return }
        status.value = "Reconnecting…"
        scope.launch { delay(1500); if (!closed) connect() }
    }

    private fun ping() = send("ping") { put("t0", System.currentTimeMillis() / 1000.0) }

    private fun send(type: String, extra: JsonObjectBuilder.() -> Unit = {}) {
        ws?.send(buildJsonObject { put("type", type); extra() }.toString())
    }

    @Synchronized private fun onMessage(text: String) {
        val msg = runCatching { PaddockJson.parseToJsonElement(text).jsonObject }.getOrNull() ?: return
        when (msg["type"]?.jsonPrimitive?.content) {
            "pong" -> clock.pong(msg["t0"]!!.jsonPrimitive.content.toDouble(), msg["server"]!!.jsonPrimitive.content.toDouble())
            "hello" -> {
                myName = msg["you"]!!.jsonObject["username"]!!.jsonPrimitive.content
                canControl.value = msg["can_control"]?.jsonPrimitive?.content == "true"
                clock.hello(msg["server"]!!.jsonPrimitive.content.toDouble())
                val p = PaddockJson.decodeFromJsonElement<PartyInfo>(msg["party"]!!)
                info.value = p
                members.value = p.members
                queue.value = p.queue
                chat.value = (msg["chat"] as? JsonArray)?.map { line(it.jsonObject) } ?: emptyList()
                status.value = ""
                apply(PaddockJson.decodeFromJsonElement(msg["state"]!!), null, null)
            }
            "state" -> apply(PaddockJson.decodeFromJsonElement(msg["state"]!!), msg["by"]?.jsonPrimitive?.content, msg["action"]?.jsonPrimitive?.content)
            "members" -> members.value = PaddockJson.decodeFromJsonElement(msg["members"]!!)
            "chat", "notice" -> chat.value = (chat.value + line(msg)).takeLast(60)
            "reaction" -> {
                val r = Reaction(msg["emoji"]!!.jsonPrimitive.content, msg["user"]?.jsonPrimitive?.content ?: "", System.nanoTime())
                reactions.value = reactions.value + r
                scope.launch { delay(2600); reactions.value = reactions.value - r }
            }
            "error" -> status.value = msg["error"]?.jsonPrimitive?.content ?: ""
            "queue" -> {
                queue.value = PaddockJson.decodeFromJsonElement(msg["queue"]!!)
                val by = msg["by"]?.jsonPrimitive?.content
                if (msg["action"]?.jsonPrimitive?.content == "add" && by != null && by != myName)
                    chat.value = (chat.value + ChatLine(null, "$by queued ${msg["title"]?.jsonPrimitive?.content ?: "a video"}")).takeLast(60)
            }
            "queue_empty" -> { fallback?.let { (item, title) -> switchItem(item, title) }; fallback = null }
            "ended" -> ended.value = "The host ended the watch party."
        }
    }

    private fun line(m: JsonObject) = ChatLine(if (m["type"]?.jsonPrimitive?.content == "notice") null else m["user"]?.jsonPrimitive?.content,
        m["text"]?.jsonPrimitive?.content ?: "")

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

    @Synchronized private fun correct(force: Boolean) {
        val s = state ?: return
        if (s.item_id != itemId || player.duration.value <= 0) return
        val target = clock.target(s)
        val diff = player.position.value - target
        if (s.playing) {
            if (!player.playing.value) {
                if (abs(diff) > 0.3) player.seek(target)
                val startsIn = s.at - clock.now()
                if (startsIn > 0.05) scope.launch { delay((startsIn * 1000).toLong()); if (state === s) player.play() } else player.play()
                return
            }
            val rate = when {
                abs(diff) > 1.0 || (force && abs(diff) > 0.5) -> { player.seek(target); s.rate }
                abs(diff) > 0.12 -> s.rate * (if (diff > 0) 0.95 else 1.05)
                else -> s.rate
            }
            if (abs(player.rate.value - rate) > 0.001) player.setRate(rate)
        } else {
            if (player.playing.value) player.pause()
            if (abs(diff) > 0.3) player.seek(target)
        }
    }

    private fun watchBuffering() {
        val now = player.buffering.value
        if (now != buffering) { buffering = now; send("buffering") { put("value", now) } }
    }

    // what this person did with the controls
    fun userPlay() = send("play") { put("position", player.position.value) }
    fun userPause() = send("pause") { put("position", player.position.value) }
    fun userSeek(to: Double) = send("seek") { put("position", to) }
    fun chat(text: String) = send("chat") { put("text", text.take(500)) }
    fun react(emoji: String) = send("reaction") { put("emoji", emoji) }
    fun switchItem(item: Int, title: String) = send("item") { put("item_id", item); put("title", title); put("play", true) }
    fun followItem(item: Int) { itemId = item; switchTo.value = null }
    /** Move on together: the party's queue first, or this computer's own up-next pick when it's empty. */
    fun next(fromItem: Int, fallbackItem: Int?, fallbackTitle: String) {
        fallback = fallbackItem?.let { it to fallbackTitle }
        send("next") { put("from", fromItem) }
    }
    fun queueAdd(item: Int) = send("queue_add") { put("item_id", item) }
    fun queueRemove(index: Int) = send("queue_remove") { put("index", index) }

    fun close() {
        closed = true
        player.setRate(1.0)
        ws?.close(1000, null)
        scope.cancel()
    }
}
