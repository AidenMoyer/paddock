package app.paddock.desktop

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.paddock.data.CallInfo
import app.paddock.data.Comment
import app.paddock.data.CommentPlace
import app.paddock.data.CommentsResponse
import app.paddock.data.PaddockJson
import app.paddock.data.Person
import app.paddock.data.VoicePlaces
import app.paddock.live.PartyInfo
import app.paddock.ui.theme.LocalPaddock
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.Timer
import kotlin.concurrent.schedule

/** The events socket: calls ringing, parties starting, new comments (server/paddock/events.py). */
object Events {
    val incoming = MutableStateFlow<CallInfo?>(null)
    val notice = MutableStateFlow<String?>(null)
    val comments = MutableSharedFlow<Comment>(extraBufferCapacity = 32)
    private var ws: WebSocket? = null
    private var base: String? = null

    @Synchronized fun follow(baseUrl: String?) {
        if (baseUrl == base && ws != null) return
        ws?.close(1000, null); ws = null
        base = baseUrl
        if (baseUrl != null) connect()
    }

    private fun connect() {
        val client = Connection.client ?: return
        val url = base ?: return
        val socket = client.newWebSocket(Request.Builder().url("$url/api/events/ws").build(), object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) = handle(text)
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = retry(webSocket)
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = retry(webSocket)
        })
        ws = socket
    }

    private fun retry(socket: WebSocket) {
        Timer(true).schedule(3000L + (0..3000).random()) { if (ws === socket && base != null) connect() }
    }

    private fun handle(raw: String) {
        val msg = runCatching { PaddockJson.parseToJsonElement(raw).jsonObject }.getOrNull() ?: return
        when (msg["type"]?.jsonPrimitive?.content) {
            "hello" -> (msg["ringing"] as? JsonArray)?.firstOrNull()?.let { incoming.value = PaddockJson.decodeFromJsonElement(it) }
            "ring" -> PaddockJson.decodeFromJsonElement<CallInfo>(msg["call"]!!).let { if (Voice.room.value != it.room) incoming.value = it }
            "ring_stop", "call_ended" -> {
                val id = msg["call_id"]?.jsonPrimitive?.content
                if (incoming.value?.id == id) incoming.value = null
                if (Voice.room.value == "call:$id") { Voice.leave(); notice.value = "The call ended" }
            }
            "declined" -> notice.value = "${msg["name"]?.jsonPrimitive?.content} can't talk right now"
            "party" -> PaddockJson.decodeFromJsonElement<PartyInfo>(msg["party"]!!).let { p ->
                notice.value = "${p.host} started a watch party${if (p.community.isNotBlank()) " in ${p.community}" else ""}: ${p.title}"
            }
            "comment" -> comments.tryEmit(PaddockJson.decodeFromJsonElement(msg["comment"]!!))
        }
    }
}

@Composable
fun IncomingCallDialog() {
    val call by Events.incoming.collectAsState()
    val c = call ?: return
    val p = LocalPaddock.current
    val scope = rememberCoroutineScope()
    AlertDialog(onDismissRequest = {}, icon = { Avatar(c.host, 56, ring = true) }, title = { Text("${c.host} is calling") },
        text = { Text("Voice call") },
        confirmButton = { BrandButton("Answer", icon = Icons.Filled.Call, onClick = { Events.incoming.value = null; Voice.join(c.room, "Call with ${c.host}") }) },
        dismissButton = {
            TextButton(onClick = {
                Events.incoming.value = null
                scope.launch { runCatching { Connection.api.raw("POST", "/api/calls/${c.id}/decline") } }
            }) { Text("Decline", color = p.danger) }
        })
}

@Composable
fun NoticeBar() {
    val n by Events.notice.collectAsState()
    LaunchedEffect(n) { if (n != null) { delay(6000); Events.notice.value = null } }
    val text = n ?: return
    val p = LocalPaddock.current
    Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 4.dp).clip(RoundedCornerShape(10.dp)).background(p.surface)
        .clickable { Nav.root(Screen.Parties) }.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Filled.Groups, null, tint = p.link)
        Spacer(Modifier.width(10.dp))
        Text(text, color = p.text, modifier = Modifier.weight(1f))
    }
}

/** Together: watch parties happening now, community lounges, and calling people. */
@Composable
fun TogetherScreen() {
    val p = LocalPaddock.current
    val scope = rememberCoroutineScope()
    var tick by remember { mutableStateOf(0) }
    var parties by remember { mutableStateOf<List<PartyInfo>>(emptyList()) }
    var places by remember { mutableStateOf(VoicePlaces()) }
    var people by remember { mutableStateOf<List<Person>>(emptyList()) }
    val room by Voice.room.collectAsState()
    LaunchedEffect(tick, room) {
        runCatching { parties = Connection.api.get("/api/parties") }
        runCatching { places = Connection.api.get("/api/voice/places") }
        runCatching { people = Connection.api.get("/api/people") }
    }
    LaunchedEffect(Unit) { while (true) { delay(8000); tick++ } }
    fun call(ids: List<Int>, label: String) = scope.launch {
        runCatching { Connection.api.post<CallInfo>("/api/calls", buildJsonObject { put("user_ids", JsonArray(ids.map { JsonPrimitive(it) })) }) }
            .onSuccess { Voice.join(it.room, label); tick++ }.onFailure { Events.notice.value = it.message }
    }
    LazyColumn(contentPadding = PaddingValues(24.dp), verticalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.widthIn(max = 900.dp)) {
        item {
            Text("Together", style = MaterialTheme.typography.headlineMedium, color = p.text)
            Text("Watch in sync and talk, wherever everyone is. Voice keeps going while you browse.", color = p.text2)
            SectionTitle("Happening now", Modifier.padding(top = 12.dp))
        }
        if (parties.isEmpty()) item { Text("Nobody's watching together right now. Start a party from any video with Watch together.", color = p.text3) }
        items(parties, key = { "p" + it.id }) { party ->
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(p.surface).padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                RemoteImage("/api/items/${party.item_id}/thumb", Modifier.size(160.dp, 90.dp).clip(RoundedCornerShape(8.dp)))
                Spacer(Modifier.width(16.dp))
                Column(Modifier.weight(1f)) {
                    Text(party.title, color = p.text, fontWeight = FontWeight.Medium, fontSize = 17.sp)
                    Text(listOfNotNull("${party.host}'s party", party.community.ifBlank { null }, "${party.members.size} watching",
                        if (party.queue.isNotEmpty()) "${party.queue.size} queued" else null).joinToString(" · "), color = p.text2)
                }
                BrandButton("Join", onClick = { Nav.go(Screen.Watch(party.item_id, party.id)) })
            }
        }
        item { SectionTitle("Lounges") }
        if (places.lounges.isEmpty()) item { Text("You're not in a community with a voice lounge.", color = p.text3) }
        items(places.lounges, key = { it.room }) { l ->
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(p.surface).padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(44.dp).clip(RoundedCornerShape(12.dp)).background(BrandGradient), contentAlignment = Alignment.Center) { Icon(Icons.Filled.Groups, null, tint = Color.White) }
                Spacer(Modifier.width(14.dp))
                Column(Modifier.weight(1f)) {
                    Text(l.name, color = p.text, fontWeight = FontWeight.Medium)
                    Text(if (l.members.isEmpty()) "Empty" else l.members.joinToString { it.username }, color = p.text2, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                if (room == l.room) SoftButton("Leave", onClick = { Voice.leave() }) else BrandButton("Join", onClick = { Voice.join(l.room, "${l.name} lounge") })
            }
        }
        if (places.calls.isNotEmpty()) {
            item { SectionTitle("Your calls") }
            items(places.calls, key = { "c" + it.id }) { c ->
                Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(p.surface).padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Filled.Call, null, tint = p.ok)
                    Spacer(Modifier.width(14.dp))
                    Text(c.people.joinToString { it.name }, color = p.text, modifier = Modifier.weight(1f))
                    if (room == c.room) Text("You're in it", color = p.ok) else BrandButton("Join", onClick = { Voice.join(c.room, "Call with ${c.host}") })
                }
            }
        }
        item { SectionTitle("Call someone") }
        if (people.isEmpty()) item { Text("People in your communities show up here.", color = p.text3) }
        items(people, key = { "u" + it.id }) { person ->
            val online = person.id in places.online
            Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Avatar(person.name, 34)
                Spacer(Modifier.width(12.dp))
                Text(person.name, color = p.text, modifier = Modifier.weight(1f))
                Box(Modifier.size(8.dp).clip(CircleShape).background(if (online) p.ok else p.text3))
                Text(if (online) "  Online" else "  Away", color = if (online) p.ok else p.text3, fontSize = 13.sp)
                Spacer(Modifier.width(16.dp))
                SoftButton("Call", icon = Icons.Filled.Call, onClick = { call(listOf(person.id), "Call with ${person.name}") })
            }
        }
    }
}

/** Comments under a video or photo, by community, with jump-to-moment. */
@Composable
fun CommentsPanel(itemId: Int, position: (() -> Double)? = null, onSeek: ((Double) -> Unit)? = null) {
    val p = LocalPaddock.current
    val scope = rememberCoroutineScope()
    var comments by remember(itemId) { mutableStateOf<List<Comment>>(emptyList()) }
    var places by remember(itemId) { mutableStateOf<List<CommentPlace>>(emptyList()) }
    var place by remember(itemId) { mutableStateOf<CommentPlace?>(null) }
    var text by remember(itemId) { mutableStateOf("") }
    var atMoment by remember(itemId) { mutableStateOf(false) }
    var menu by remember { mutableStateOf(false) }
    LaunchedEffect(itemId) {
        runCatching { Connection.api.get<CommentsResponse>("/api/items/$itemId/comments") }.onSuccess { comments = it.comments; places = it.places; place = it.places.firstOrNull() }
    }
    LaunchedEffect(itemId) { Events.comments.collect { c -> if (c.item_id == itemId && comments.none { it.id == c.id }) comments = comments + c } }
    Column(Modifier.fillMaxWidth().padding(top = 16.dp)) {
        Text(if (comments.isEmpty()) "Comments" else "Comments · ${comments.size}", style = MaterialTheme.typography.titleMedium, color = p.text)
        if (places.isNotEmpty()) Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(text, { text = it }, placeholder = { Text("Add a comment…") }, modifier = Modifier.weight(1f), maxLines = 4)
            if (places.size > 1) Box {
                TextButton(onClick = { menu = true }) { Text(place?.name ?: "Everyone", color = p.text2) }
                DropdownMenu(menu, { menu = false }) { for (pl in places) DropdownMenuItem(text = { Text(pl.name) }, onClick = { place = pl; menu = false }) }
            }
            if (position != null) Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(atMoment, { atMoment = it }); Text("At this moment", color = p.text2, fontSize = 13.sp) }
            IconButton(enabled = text.isNotBlank(), onClick = {
                val body = text.trim()
                scope.launch {
                    runCatching {
                        Connection.api.post<Comment>("/api/items/$itemId/comments", buildJsonObject {
                            put("body", body); put("community_id", place?.id?.let { JsonPrimitive(it) } ?: JsonNull)
                            if (atMoment && position != null) put("at", position().toLong().toDouble())
                        })
                    }.onSuccess { c -> text = ""; if (comments.none { it.id == c.id }) comments = comments + c }.onFailure { Events.notice.value = it.message }
                }
            }) { Icon(Icons.AutoMirrored.Filled.Send, "Comment", tint = p.link) }
        }
        for (c in comments) Row(Modifier.padding(vertical = 8.dp)) {
            Avatar(c.display_name.ifBlank { c.username }, 32)
            Spacer(Modifier.width(10.dp))
            Column {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(c.display_name.ifBlank { c.username }, color = p.text, fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
                    if (places.size > 1 || c.community_id != null) Text(c.community, color = p.text3, fontSize = 13.sp)
                    c.at?.let { at -> Text(fmtDuration(at), color = p.link, fontSize = 13.sp, modifier = Modifier.clickable(enabled = onSeek != null) { onSeek?.invoke(at) }) }
                }
                Text(c.body, color = p.text)
            }
        }
        if (comments.isEmpty()) Text("No comments yet.", color = p.text3, fontSize = 13.sp)
    }
}
