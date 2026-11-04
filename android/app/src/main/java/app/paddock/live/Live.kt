package app.paddock.live

import android.Manifest
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ProcessLifecycleOwner
import app.paddock.MainActivity
import app.paddock.PaddockApp
import app.paddock.R
import app.paddock.data.CallInfo
import app.paddock.data.Comment
import app.paddock.data.PaddockJson
import app.paddock.ui.theme.LocalPaddock
import app.paddock.ui.theme.Tokens
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import android.os.Handler
import android.os.Looper

/**
 * Live things that follow you around the app: one events socket (calls ringing, watch parties starting, new
 * comments) for as long as Paddock is connected, and [VoiceHub], so voice chat keeps going while you browse.
 */
object Live {
    const val CHANNEL = "calls"
    private const val RING_ID = 7

    val incoming = mutableStateOf<CallInfo?>(null)
    private val _notices = MutableSharedFlow<String>(extraBufferCapacity = 8)
    val notices: SharedFlow<String> = _notices
    private val _comments = MutableSharedFlow<Comment>(extraBufferCapacity = 32)
    val comments: SharedFlow<Comment> = _comments

    private val main = Handler(Looper.getMainLooper())
    private var ws: WebSocket? = null
    private var base: String? = null

    /** Called whenever the connection changes: (re)connect to the server we're on, or let go. */
    fun follow(baseUrl: String?) { main.post { followNow(baseUrl) } }

    private fun followNow(baseUrl: String?) {
        if (baseUrl == base && ws != null) return
        ws?.close(1000, null); ws = null
        base = baseUrl
        if (baseUrl != null) connect()
    }

    private fun connect() {
        val app = PaddockApp.instance
        val client = app.conn.client ?: return
        val url = base ?: return
        ws = client.newWebSocket(Request.Builder().url("$url/api/events/ws").build(), object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) { main.post { handle(text) } }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = retry(webSocket)
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = retry(webSocket)
        })
    }

    private fun retry(socket: WebSocket) {
        main.postDelayed({ if (ws === socket && base != null) connect() }, 3000L + (0..3000).random())
    }

    private fun handle(raw: String) {
        val msg = runCatching { PaddockJson.parseToJsonElement(raw).jsonObject }.getOrNull() ?: return
        val app = PaddockApp.instance
        when (msg["type"]?.jsonPrimitive?.content) {
            "hello" -> (msg["ringing"] as? kotlinx.serialization.json.JsonArray)?.firstOrNull()?.let { ring(PaddockJson.decodeFromJsonElement(it)) }
            "ring" -> ring(PaddockJson.decodeFromJsonElement(msg["call"]!!))
            "ring_stop", "call_ended" -> {
                val id = msg["call_id"]?.jsonPrimitive?.content
                if (incoming.value?.id == id) stopRinging()
                if (VoiceHub.room.value == "call:$id") { VoiceHub.leave(); _notices.tryEmit("The call ended") }
            }
            "declined" -> _notices.tryEmit("${msg["name"]?.jsonPrimitive?.content} can't talk right now")
            "party" -> {
                val p = PaddockJson.decodeFromJsonElement<PartyInfo>(msg["party"]!!)
                val text = "${p.host} started a watch party${if (p.community.isNotBlank()) " in ${p.community}" else ""}: ${p.title}"
                _notices.tryEmit(text)
                if (!foreground()) notify(app, 8, "Watch party", text, "party/${p.id}")
            }
            "comment" -> _comments.tryEmit(PaddockJson.decodeFromJsonElement(msg["comment"]!!))
        }
    }

    private fun foreground() = ProcessLifecycleOwner.get().lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)

    private fun ring(call: CallInfo) {
        if (!call.ringing || VoiceHub.room.value == call.room) return
        incoming.value = call
        if (!foreground()) notify(PaddockApp.instance, RING_ID, "${call.host} is calling", "Tap to answer in Paddock", "call")
        main.postDelayed({ if (incoming.value?.id == call.id) stopRinging() }, 45_000)
    }

    fun stopRinging() {
        incoming.value = null
        PaddockApp.instance.getSystemService(NotificationManager::class.java).cancel(RING_ID)
    }

    private fun notify(ctx: Context, id: Int, title: String, text: String, open: String) {
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val intent = Intent(ctx, MainActivity::class.java).putExtra("open", open).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val pi = PendingIntent.getActivity(ctx, id, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(ctx, CHANNEL).setSmallIcon(R.drawable.ic_notification).setContentTitle(title).setContentText(text)
            .setPriority(NotificationCompat.PRIORITY_HIGH).setCategory(NotificationCompat.CATEGORY_CALL).setAutoCancel(true)
            .setContentIntent(pi).build()
        ctx.getSystemService(NotificationManager::class.java).notify(id, n)
    }
}

/** One voice room at a time, kept while you move around the app (calls, lounges and watch parties all use it). */
object VoiceHub {
    val session = mutableStateOf<VoiceSession?>(null)
    val room = mutableStateOf<String?>(null)
    val title = mutableStateOf("")

    fun join(roomId: String, label: String) {
        if (room.value == roomId && session.value != null) return
        leave()
        val s = VoiceSession(roomId)
        s.start()
        session.value = s; room.value = roomId; title.value = label
    }

    fun leave() {
        session.value?.stop()
        session.value = null; room.value = null; title.value = ""
    }

    fun inRoom(roomId: String) = room.value == roomId
}

/** Returns "join this room": asks for the microphone first if needed. */
@Composable
fun rememberJoinVoice(): (String, String) -> Unit {
    val ctx = LocalContext.current
    var pending by remember { mutableStateOf<Pair<String, String>?>(null) }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val p = pending
        pending = null
        if (granted && p != null) VoiceHub.join(p.first, p.second)
        else if (!granted) android.widget.Toast.makeText(ctx, "Voice chat needs the microphone", android.widget.Toast.LENGTH_SHORT).show()
    }
    return { room, label ->
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) VoiceHub.join(room, label)
        else { pending = room to label; ask.launch(Manifest.permission.RECORD_AUDIO) }
    }
}

/** The ringing screen for an incoming call. */
@Composable
fun IncomingCall() {
    val call = Live.incoming.value ?: return
    val join = rememberJoinVoice()
    val scope = rememberCoroutineScope()
    AlertDialog(
        onDismissRequest = {},
        icon = { BrandAvatar(call.host, 56) },
        title = { Text("${call.host} is calling") },
        text = {
            val others = call.people.filter { it.user_id != call.host_id && it.user_id != PaddockApp.instance.me.value?.id }.map { it.name }
            Text(if (others.isEmpty()) "Voice call" else "with ${others.joinToString()}")
        },
        confirmButton = {
            Button(onClick = { Live.stopRinging(); join(call.room, "Call with ${call.host}") }, colors = ButtonDefaults.buttonColors(containerColor = LocalPaddock.current.ok)) {
                Icon(Icons.Filled.Call, null); Spacer(Modifier.width(6.dp)); Text("Answer")
            }
        },
        dismissButton = {
            TextButton(onClick = {
                Live.stopRinging()
                scope.launch { runCatching { PaddockApp.instance.api.raw("POST", "/api/calls/${call.id}/decline") } }
            }) { Text("Decline", color = LocalPaddock.current.danger) }
        },
    )
}

/** While you're in a voice room: who's here and who's talking, mute and leave. Sits above the bottom bar. */
@Composable
fun VoiceDock(onOpen: () -> Unit) {
    val p = LocalPaddock.current
    val s = VoiceHub.session.value
    AnimatedVisibility(s != null) {
        if (s == null) return@AnimatedVisibility
        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp).clip(RoundedCornerShape(14.dp)).background(p.surface)
            .clickable { onOpen() }.padding(horizontal = 12.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(8.dp).clip(CircleShape).background(p.ok))
            Spacer(Modifier.width(8.dp))
            Column(Modifier.weight(1f)) {
                Text(VoiceHub.title.value.ifBlank { "Voice chat" }, color = p.text, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    for (m in s.members.take(6)) {
                        val talking = s.speaking[m.id] == true && !m.muted
                        Box(Modifier.size(20.dp).then(if (talking) Modifier.border(2.dp, p.ok, CircleShape) else Modifier).padding(2.dp)
                            .clip(CircleShape).background(Brush.linearGradient(Tokens.Brand.gradient)), contentAlignment = Alignment.Center) {
                            Text(m.username.take(1).uppercase(), color = Color.White, fontSize = 9.sp, fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }
            IconButton(onClick = { s.setMuted(!s.muted.value) }) {
                Icon(if (s.muted.value) Icons.Filled.MicOff else Icons.Filled.Mic, if (s.muted.value) "Unmute" else "Mute", tint = if (s.muted.value) p.danger else p.text)
            }
            IconButton(onClick = { VoiceHub.leave() }) { Icon(Icons.Filled.CallEnd, "Leave", tint = p.danger) }
        }
    }
}

@Composable
fun BrandAvatar(name: String, size: Int = 32) {
    Box(Modifier.size(size.dp).clip(CircleShape).background(Brush.linearGradient(Tokens.Brand.gradient)), contentAlignment = Alignment.Center) {
        Text(name.take(1).uppercase(), color = Color.White, fontWeight = FontWeight.Bold, fontSize = (size * 0.42).sp)
    }
}
