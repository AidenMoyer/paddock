package app.paddock.desktop

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ExitToApp
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.automirrored.filled.VolumeOff
import androidx.compose.material.icons.automirrored.filled.VolumeUp
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.paddock.data.Item
import app.paddock.data.Playback
import app.paddock.data.Quality
import app.paddock.live.REACTIONS
import app.paddock.ui.theme.LocalPaddock
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.JsonObject

@Composable
fun WatchScreen(id: Int, partyId: String?) {
    val p = LocalPaddock.current
    val player = remember { VideoPlayer() }
    var itemId by remember { mutableStateOf(id) }
    var item by remember { mutableStateOf<Item?>(null) }
    var plan by remember { mutableStateOf<Playback?>(null) }
    var quality by remember { mutableStateOf("auto") }
    var err by remember { mutableStateOf<String?>(null) }
    var upNext by remember { mutableStateOf<List<Item>>(emptyList()) }
    var party by remember { mutableStateOf<PartyClient?>(null) }
    val scope = rememberCoroutineScope()
    val full by Nav.fullscreen
    val settings by Store.state.collectAsState()

    fun source(pl: Playback, q: String): String? {
        val pick = pl.qualities.firstOrNull { it.id == q } ?: pl.qualities.firstOrNull { it.id == "auto" } ?: pl.qualities.firstOrNull()
        val direct = pick?.type == "direct" && (pick.mime ?: "").let { it.contains("mp4") || it.contains("mpeg") }
        return when {
            pick == null -> pl.url
            pick.type == "hls" || direct -> pick.url
            else -> pl.hls ?: pl.url
        }
    }

    LaunchedEffect(itemId) {
        err = null; plan = null
        val i = runCatching { Connection.api.get<Item>("/api/items/$itemId") }.getOrElse { err = it.message; return@LaunchedEffect }
        item = i
        val pl = runCatching { Connection.api.get<Playback>("/api/items/$itemId/playback?codecs=h264,aac,mp3,mp4") }.getOrElse { err = it.message; return@LaunchedEffect }
        if (pl.mode == "unavailable") { err = pl.reason ?: "This can't be played"; return@LaunchedEffect }
        plan = pl
        quality = if (pl.qualities.any { it.id == "auto" }) "auto" else pl.default_quality
        Connection.ensure()
        source(pl, quality)?.let { player.open(MediaProxy.url(it), if (partyId == null) pl.resume_at else 0.0) }
        upNext = runCatching { Connection.api.get<List<Item>>("/api/items/$itemId/up_next?limit=20") }.getOrDefault(emptyList())
    }
    // join (or keep) the watch party
    DisposableEffect(partyId) {
        val c = partyId?.let { PartyClient(it, player, id).also { pc -> pc.start() } }
        party = c
        onDispose { c?.close() }
    }
    val switchTo by (party?.switchTo ?: remember { kotlinx.coroutines.flow.MutableStateFlow<Int?>(null) }).collectAsState()
    LaunchedEffect(switchTo) { switchTo?.let { party?.followItem(it); itemId = it } }
    // remember where this person got to, and roll on to the next video
    LaunchedEffect(itemId) {
        while (true) {
            delay(10_000)
            if (player.playing.value) runCatching {
                Connection.api.post<JsonObject>("/api/items/$itemId/progress", buildJsonObject { put("position", player.position.value); put("duration", player.duration.value) })
            }
        }
    }
    player.onEnded = {
        val next = upNext.firstOrNull()
        if (next != null && settings.settings.autoplay) {
            if (party != null) party?.switchItem(next.id, next.title) else itemId = next.id
        }
    }
    DisposableEffect(Unit) { onDispose { player.dispose(); if (Nav.fullscreen.value) Nav.fullscreen.value = false } }

    Row(Modifier.fillMaxSize().padding(if (full) 0.dp else 24.dp)) {
        Column(Modifier.weight(1f)) {
            Box(Modifier.fillMaxWidth().then(if (full) Modifier.weight(1f) else Modifier.aspectRatio(16f / 9f)).clip(RoundedCornerShape(if (full) 0.dp else 12.dp)).background(Color.Black)) {
                VideoView(player, Modifier.fillMaxSize())
                party?.let { Reactions(it, Modifier.align(Alignment.BottomStart).padding(16.dp)) }
            }
            Controls(player, plan, quality, onQuality = { q ->
                quality = q
                val pos = player.position.value
                plan?.let { pl -> source(pl, q)?.let { player.open(MediaProxy.url(it), pos) } }
            }, party = party)
            if (!full) {
                val e = err
                if (e != null) Text(e, color = p.danger, modifier = Modifier.padding(top = 8.dp))
                item?.let { i -> Details(i, player, party, partyId, scope) }
            }
        }
        if (!full) {
            Spacer(Modifier.width(24.dp))
            Column(Modifier.width(380.dp)) {
                val pc = party
                if (pc != null) PartyPanel(pc, Modifier.weight(1f)) else UpNext(upNext)
            }
        }
    }
}

@Composable
fun VideoView(player: VideoPlayer, modifier: Modifier = Modifier) {
    val img by player.frame
    Box(modifier.background(Color.Black), contentAlignment = Alignment.Center) {
        img?.let { androidx.compose.foundation.Image(it, null, Modifier.fillMaxSize(), contentScale = androidx.compose.ui.layout.ContentScale.Fit) }
        if (!Vlc.available) Text(Vlc.MISSING, color = Color.White, modifier = Modifier.padding(24.dp))
    }
}

/** Emoji reactions float up over the picture for everyone in the party. */
@Composable
private fun Reactions(pc: PartyClient, modifier: Modifier) {
    val reactions by pc.reactions.collectAsState()
    Column(modifier) {
        for (r in reactions.takeLast(6)) {
            val rise = remember(r.key) { androidx.compose.animation.core.Animatable(0f) }
            LaunchedEffect(r.key) { rise.animateTo(1f, androidx.compose.animation.core.tween(2400)) }
            Text("${r.emoji} ${r.user}", fontSize = 22.sp, color = Color.White,
                modifier = Modifier.padding(vertical = 2.dp).graphicsLayer { translationY = -rise.value * 120f; alpha = 1f - rise.value })
        }
    }
}

@Composable
private fun Controls(player: VideoPlayer, plan: Playback?, quality: String, onQuality: (String) -> Unit, party: PartyClient?) {
    val p = LocalPaddock.current
    val playing by player.playing.collectAsState()
    val pos by player.position.collectAsState()
    val dur by player.duration.collectAsState()
    val buffering by player.buffering.collectAsState()
    val error by player.error.collectAsState()
    var dragging by remember { mutableStateOf<Float?>(null) }
    var volume by remember { mutableStateOf(Store.settings.volume) }
    var menu by remember { mutableStateOf(false) }
    val canControl by (party?.canControl ?: remember { kotlinx.coroutines.flow.MutableStateFlow(true) }).collectAsState()
    Column(Modifier.fillMaxWidth().background(p.bg).padding(horizontal = 8.dp)) {
        Slider(value = dragging ?: if (dur > 0) (pos / dur).toFloat().coerceIn(0f, 1f) else 0f, enabled = canControl && dur > 0,
            onValueChange = { dragging = it }, onValueChangeFinished = {
                val to = (dragging ?: 0f) * dur
                dragging = null
                player.seek(to); party?.userSeek(to)
            })
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(enabled = canControl, onClick = {
                if (playing) { player.pause(); party?.userPause() } else { player.play(); party?.userPlay() }
            }) { Icon(if (playing) Icons.Filled.Pause else Icons.Filled.PlayArrow, if (playing) "Pause" else "Play", tint = p.text) }
            IconButton(enabled = canControl, onClick = { val to = (pos - 10).coerceAtLeast(0.0); player.seek(to); party?.userSeek(to) }) { Icon(Icons.Filled.Replay10, "Back 10 seconds", tint = p.text) }
            IconButton(enabled = canControl, onClick = { val to = (pos + 10).coerceAtMost(dur); player.seek(to); party?.userSeek(to) }) { Icon(Icons.Filled.Forward10, "Forward 10 seconds", tint = p.text) }
            Text("${fmtDuration(pos)} / ${fmtDuration(dur)}", color = p.text2, fontSize = 13.sp)
            if (buffering) { Spacer(Modifier.width(10.dp)); CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp) }
            if (error != null) Text("  $error", color = p.danger, fontSize = 13.sp, maxLines = 1)
            Spacer(Modifier.weight(1f))
            Icon(if (volume == 0f) Icons.AutoMirrored.Filled.VolumeOff else Icons.AutoMirrored.Filled.VolumeUp, null, tint = p.text2)
            Slider(volume, { volume = it; player.volume = it.toDouble() }, onValueChangeFinished = { Store.setSettings { s -> s.copy(volume = volume) } },
                modifier = Modifier.width(110.dp))
            Box {
                TextButton(onClick = { menu = true }) { Text(plan?.qualities?.firstOrNull { it.id == quality }?.label ?: "Quality", color = p.text) }
                DropdownMenu(menu, { menu = false }) {
                    for (q: Quality in plan?.qualities.orEmpty()) DropdownMenuItem(text = { Text(q.label) }, onClick = { menu = false; onQuality(q.id) },
                        leadingIcon = { if (q.id == quality) Icon(Icons.Filled.Check, null) })
                }
            }
            IconButton(onClick = { Nav.fullscreen.value = !Nav.fullscreen.value }) {
                Icon(if (Nav.fullscreen.value) Icons.Filled.FullscreenExit else Icons.Filled.Fullscreen, "Full screen (F11)", tint = p.text)
            }
        }
    }
    LaunchedEffect(Unit) { player.volume = volume.toDouble() }
}

@Composable
private fun Details(item: Item, player: VideoPlayer, party: PartyClient?, partyId: String?, scope: kotlinx.coroutines.CoroutineScope) {
    val p = LocalPaddock.current
    var msg by remember { mutableStateOf<String?>(null) }
    Column(Modifier.padding(top = 12.dp)) {
        Text(item.title, style = MaterialTheme.typography.titleLarge, color = p.text, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Text(listOfNotNull(item.show?.let { "$it · Season ${item.season}" }, item.year?.toString() ?: item.date, item.library).joinToString(" · "), color = p.text2)
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (partyId == null) BrandButton("Watch together", icon = Icons.Filled.Groups, onClick = {
                scope.launch {
                    runCatching {
                        Connection.api.post<app.paddock.live.PartyInfo>("/api/parties", buildJsonObject { put("item_id", item.id); put("position", player.position.value) })
                    }.onSuccess { pi ->
                        Nav.back(); Nav.go(Screen.Watch(item.id, pi.id))
                        Voice.join("party:${pi.id}", "Watch party")
                    }.onFailure { msg = it.message }
                }
            })
            else {
                val room by Voice.room.collectAsState()
                if (room != "party:$partyId") SoftButton("Join voice", icon = Icons.Filled.Mic, onClick = { Voice.join("party:$partyId", "Watch party") })
                SoftButton("Leave party", icon = Icons.AutoMirrored.Filled.ExitToApp, onClick = { if (Voice.room.value == "party:$partyId") Voice.leave(); Nav.back() })
            }
        }
        msg?.let { Text(it, color = p.danger, modifier = Modifier.padding(top = 6.dp)) }
    }
}

@Composable
private fun UpNext(list: List<Item>) {
    val p = LocalPaddock.current
    Text("Up next", style = MaterialTheme.typography.titleMedium, color = p.text, modifier = Modifier.padding(bottom = 8.dp))
    LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        items(list, key = { it.id }) { i ->
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).clickable { Nav.back(); Nav.go(Screen.Watch(i.id)) }, verticalAlignment = Alignment.Top) {
                Box(Modifier.width(168.dp).aspectRatio(16f / 9f).clip(RoundedCornerShape(8.dp)).background(p.surface2)) {
                    RemoteImage("/api/items/${i.id}/thumb", Modifier.fillMaxSize())
                    if ((i.duration ?: 0.0) > 0) Text(fmtDuration(i.duration), color = Color.White, fontSize = 11.sp,
                        modifier = Modifier.align(Alignment.BottomEnd).padding(4.dp).background(Color(0xCC000000)).padding(horizontal = 3.dp))
                }
                Spacer(Modifier.width(10.dp))
                Column {
                    Text(i.title, color = p.text, fontWeight = FontWeight.Medium, maxLines = 2, overflow = TextOverflow.Ellipsis, fontSize = 14.sp)
                    Text(i.library ?: "", color = p.text2, fontSize = 12.sp, maxLines = 1)
                }
            }
        }
    }
}

@Composable
private fun PartyPanel(pc: PartyClient, modifier: Modifier) {
    val p = LocalPaddock.current
    val info by pc.info.collectAsState()
    val members by pc.members.collectAsState()
    val chat by pc.chat.collectAsState()
    val status by pc.status.collectAsState()
    val ended by pc.ended.collectAsState()
    val speaking by Voice.speaking.collectAsState()
    val voiceMembers by Voice.members.collectAsState()
    var text by remember { mutableStateOf("") }
    val listState = rememberLazyListState()
    LaunchedEffect(chat.size) { if (chat.isNotEmpty()) listState.animateScrollToItem(chat.size - 1) }
    Column(modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface).padding(14.dp)) {
        Text("Watch party", style = MaterialTheme.typography.titleMedium, color = p.text)
        Text(ended ?: status.ifBlank { "Hosted by ${info?.host ?: "…"} · in sync" }, color = if (ended != null) p.danger else p.text2, fontSize = 13.sp)
        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            for (m in members) {
                val talking = voiceMembers.any { it.user_id == m.user_id && it.id in speaking }
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Avatar(m.username, 34, ring = talking)
                    Text(m.username + if (m.buffering) " ⏳" else "", color = p.text2, fontSize = 11.sp, maxLines = 1)
                }
            }
        }
        HorizontalDivider(Modifier.padding(vertical = 10.dp), color = p.border)
        LazyColumn(state = listState, modifier = Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            items(chat) { c ->
                if (c.user == null) Text(c.text, color = p.text3, fontSize = 12.sp)
                else Text("${c.user}: ${c.text}", color = p.text, fontSize = 14.sp)
            }
        }
        Row(Modifier.padding(vertical = 6.dp), horizontalArrangement = Arrangement.spacedBy(2.dp)) {
            for (e in REACTIONS) Text(e, fontSize = 20.sp, modifier = Modifier.clip(RoundedCornerShape(8.dp)).clickable { pc.react(e) }.padding(4.dp))
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(text, { text = it }, placeholder = { Text("Say something") }, singleLine = true, modifier = Modifier.weight(1f))
            IconButton(onClick = { if (text.isNotBlank()) { pc.chat(text.trim()); text = "" } }) { Icon(Icons.AutoMirrored.Filled.Send, "Send", tint = p.link) }
        }
    }
}
