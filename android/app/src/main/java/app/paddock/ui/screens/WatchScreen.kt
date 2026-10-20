package app.paddock.ui.screens

import android.app.Activity
import android.content.Intent
import android.content.pm.ActivityInfo
import androidx.annotation.OptIn
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.activity.compose.BackHandler
import android.content.res.Configuration
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.draw.scale
import androidx.compose.ui.text.style.TextOverflow
import androidx.media3.common.C
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import app.paddock.PaddockApp
import app.paddock.data.Item
import app.paddock.data.LiveCaptions
import app.paddock.data.Playback
import app.paddock.data.Quality
import app.paddock.live.PartyInfo
import app.paddock.live.PartyPanel
import app.paddock.live.PartySession
import app.paddock.live.ReactionsOverlay
import app.paddock.live.VoiceSession
import app.paddock.live.WatchTogetherDialog
import android.Manifest
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import app.paddock.player.Players
import app.paddock.ui.*
import app.paddock.ui.theme.LocalPaddock
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/** Shared with MainActivity so Home/back gestures can drop into picture-in-picture while playing. */
object NowPlaying {
    var player: ExoPlayer? = null
    @Volatile var itemId: Int? = null
    val inPip = mutableStateOf(false)
    val fullscreen = mutableStateOf(false)
    /** Your queue ("Play next" / "Add to queue"), played before Up next — like YouTube. */
    val queue = mutableStateListOf<Item>()
    /** Item whose action sheet (play next / add to queue / share) is open, from anywhere in the app. */
    val actionsFor = mutableStateOf<Item?>(null)

    fun playNext(item: Item) { queue.removeAll { it.id == item.id }; queue.add(0, item) }
    fun addToQueue(item: Item) { if (queue.none { it.id == item.id }) queue.add(item) }
}

@OptIn(UnstableApi::class)
@kotlin.OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WatchScreen(id: Int, nav: Nav, partyId: String? = null) {
    val app = PaddockApp.instance
    val api = app.api
    val p = LocalPaddock.current
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val (load, retry) = rememberLoad(id) {
        val item = api.get<Item>("/api/items/$id")
        val plan = api.get<Playback>("/api/items/$id/playback?codecs=${Players.codecs()}")
        val upNext = runCatching { api.get<List<Item>>("/api/items/$id/up_next") }.getOrDefault(emptyList())
        Triple(item, plan, upNext)
    }
    val fullscreenButton by NowPlaying.fullscreen
    // turning the phone sideways goes full screen, just like YouTube
    val landscape = LocalConfiguration.current.orientation == Configuration.ORIENTATION_LANDSCAPE
    val fullscreen = fullscreenButton || landscape
    LaunchedEffect(fullscreen) { systemBars(ctx as Activity, hidden = fullscreen) }
    BackHandler(enabled = fullscreen) { NowPlaying.fullscreen.value = false; setFullscreen(ctx as Activity, false) }

    // black backdrop under the status bar while loading, so the layout doesn't jump
    if (load !is Load.Ok) {
        Column(Modifier.fillMaxSize()) {
            Box(Modifier.fillMaxWidth().background(Color.Black).statusBarsPadding().aspectRatio(16f / 9f))
            LoadView(load, retry) { }
        }
        return
    }
    val (item, plan, upNext) = (load as Load.Ok).data
    val client = app.conn.client ?: return
    val player = remember(id) { app.players.player(client) }
    var quality by remember(id) { mutableStateOf(pickQuality(plan, app.settings.quality)) }
    var showSheet by remember { mutableStateOf(false) }
    val hasFileSubs = plan.subtitles.isNotEmpty()
    val hasLive = plan.live_captions?.enabled == true || plan.live_captions?.complete == true
    var ccOn by remember { mutableStateOf(app.settings.captions != "off" && (hasFileSubs || hasLive)) }
    val liveOn = ccOn && !hasFileSubs && hasLive
    var cues by remember { mutableStateOf(LiveCaptions()) }
    var position by remember { mutableLongStateOf(0L) }
    var favorite by remember { mutableStateOf(item.favorite) }
    val inPip by NowPlaying.inPip
    val queue = NowPlaying.queue
    fun next(): Item? = queue.firstOrNull { it.id != id } ?: upNext.firstOrNull()

    // ---- watch party: the shared clock decides play/pause; voice is optional
    val party = remember(id, partyId) { partyId?.let { PartySession(it, player, id) } }
    var voice by remember(partyId) { mutableStateOf<VoiceSession?>(null) }
    var showTogether by remember { mutableStateOf(false) }
    val micPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted && partyId != null) voice = VoiceSession("party:$partyId").also { it.start() }
        else if (!granted) android.widget.Toast.makeText(ctx, "Voice needs the microphone permission", android.widget.Toast.LENGTH_SHORT).show()
    }
    DisposableEffect(party) { party?.start(); onDispose { party?.close() } }
    DisposableEffect(partyId) { onDispose { voice?.stop(); player.volume = 1f } }
    LaunchedEffect(voice) {          // lower the movie while friends talk
        while (isActive && voice != null) { player.volume = if (voice?.othersSpeaking() == true) 0.45f else 1f; delay(200) }
    }
    val switchTo = party?.switchTo?.value
    LaunchedEffect(switchTo) { if (switchTo != null && switchTo != id) { nav.back(); nav.go("watch/$switchTo?party=$partyId") } }
    val ended = party?.ended?.value
    LaunchedEffect(ended) {
        if (ended != null) {
            android.widget.Toast.makeText(ctx, ended, android.widget.Toast.LENGTH_SHORT).show()
            voice?.stop(); voice = null; nav.back(); nav.go("watch/$id")
        }
    }
    fun playNext() {
        val n = next() ?: return
        if (party != null) { if (party.canControl.value) party.switchItem(n.id, n.title) } else nav.replace(n)
    }

    fun applyCaptions() {
        player.trackSelectionParameters = player.trackSelectionParameters.buildUpon()
            .setTrackTypeDisabled(C.TRACK_TYPE_TEXT, !(ccOn && hasFileSubs)).build()
    }
    fun load(q: Quality?, at: Long) {
        if (q == null) return
        player.setMediaItem(app.players.mediaItem(api, q, plan, item.title), at)
        applyCaptions()
        player.prepare()
        player.playWhenReady = partyId == null          // in a watch party the shared clock starts playback
    }

    DisposableEffect(id) {
        NowPlaying.player = player
        NowPlaying.itemId = id
        queue.removeAll { it.id == id }          // it is playing now, so it leaves the queue
        player.stop()
        player.clearMediaItems()
        load(quality, (plan.resume_at * 1000).toLong())
        val listener = object : Player.Listener {
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED && NowPlaying.itemId == id) {
                    scope.launch { saveProgress(id, player.duration, player.duration, completed = true) }
                    if (app.settings.autoplay) playNext()
                }
            }
            override fun onPlayerError(error: androidx.media3.common.PlaybackException) {
                // direct play failed (e.g. the phone's decoder can't do 6K) -> fall back to the transcoded ladder once
                if (NowPlaying.itemId != id) return
                val auto = plan.qualities.firstOrNull { it.id == "auto" }
                if (quality?.id != "auto" && auto != null) { quality = auto; load(auto, player.currentPosition) }
            }
        }
        player.addListener(listener)
        onDispose {
            val pos = player.currentPosition
            val dur = player.duration
            scope.launch { saveProgress(id, pos, dur) }
            player.removeListener(listener)
            // the next video may already own the shared player — only stop it if it is still ours
            if (NowPlaying.itemId == id) {
                player.stop()
                player.clearMediaItems()
                NowPlaying.itemId = null
                NowPlaying.fullscreen.value = false
                setFullscreen(ctx as Activity, false, leaving = true)
            }
        }
    }
    LaunchedEffect(id) {
        while (isActive) {
            delay(500)
            position = player.currentPosition
            if ((position / 1000) % 10 == 0L && player.isPlaying && NowPlaying.itemId == id) saveProgress(id, player.currentPosition, player.duration)
        }
    }
    LaunchedEffect(id, liveOn) {
        while (isActive && liveOn) {
            runCatching { cues = api.get("/api/items/$id/captions/live?at=${player.currentPosition / 1000.0}") }
            delay(2500)
        }
    }

    val playerBox: @Composable (Modifier) -> Unit = { mod ->
        Box(mod.background(Color.Black)) {
            AndroidView(factory = { c ->
                PlayerView(c).apply {
                    this.player = player
                    useController = false            // our own YouTube-style controls are drawn in Compose
                    resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
                    setKeepContentOnPlayerReset(true)
                }
            }, update = { it.player = player }, onRelease = { it.player = null }, modifier = Modifier.fillMaxSize())
            val now = position / 1000.0
            val active = if (liveOn) cues.cues.filter { it.start <= now && it.end >= now } else emptyList()
            Column(Modifier.align(Alignment.BottomCenter).padding(bottom = if (inPip) 4.dp else 48.dp, start = 24.dp, end = 24.dp),
                horizontalAlignment = Alignment.CenterHorizontally) {
                if (liveOn && active.isEmpty() && cues.pending > 0 && !inPip) Text("Transcribing…", color = Color(0xFFCCCCCC), fontSize = 11.sp)
                for (c in active) Text(c.text, color = Color.White, fontSize = if (inPip) 10.sp else 16.sp, textAlign = TextAlign.Center,
                    modifier = Modifier.background(Color(0xC8080808), RoundedCornerShape(4.dp)).padding(horizontal = 8.dp, vertical = 2.dp))
            }
            if (party != null) ReactionsOverlay(party)
            if (plan.mode == "unavailable") Text(plan.reason ?: "Can't play this", color = Color.White,
                modifier = Modifier.align(Alignment.Center).padding(24.dp), textAlign = TextAlign.Center)
            if (!inPip) PlayerControls(
                player = player, title = item.title, fullscreen = fullscreen, captionsOn = ccOn, hasCaptions = hasFileSubs || hasLive,
                hasNext = next() != null,
                onMinimize = { nav.back() },
                onToggleFullscreen = { val fs = !fullscreen; NowPlaying.fullscreen.value = fs; setFullscreen(ctx as Activity, fs) },
                onCaptions = { ccOn = !ccOn; app.settings.captions = if (ccOn) "on" else "off"; applyCaptions() },
                onSettings = { showSheet = true },
                onNext = { playNext() },
                // in full screen, keep every control inside the safe area (cutouts, transient system bars)
                modifier = if (fullscreen) Modifier.windowInsetsPadding(WindowInsets.safeDrawing) else Modifier,
            )
        }
    }

    if (fullscreen || inPip) {
        playerBox(Modifier.fillMaxSize())
    } else {
        // YouTube layout: the player stays pinned at the top; details, queue and Up next scroll underneath
        Column(Modifier.fillMaxSize()) {
            Box(Modifier.fillMaxWidth().background(Color.Black).statusBarsPadding()) {
                playerBox(Modifier.fillMaxWidth().aspectRatio(16f / 9f))
            }
            LazyColumn(Modifier.weight(1f), contentPadding = WindowInsets.navigationBars.asPaddingValues()) {
                item {
                    Column(Modifier.padding(horizontal = 16.dp, vertical = 12.dp)) {
                        Text(item.title, style = MaterialTheme.typography.titleLarge, color = p.text, fontWeight = FontWeight.Bold)
                        Text(listOfNotNull(fmtDate(item.taken_at).ifBlank { null }, item.library, quality?.label).joinToString(" · "), color = p.text2,
                            style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 4.dp))
                    }
                    LazyRow(contentPadding = PaddingValues(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        item {
                            PillButton(if (favorite) "Favorited" else "Favorite", if (favorite) Icons.Filled.Favorite else Icons.Filled.FavoriteBorder) {
                                scope.launch { runCatching { api.patch<JsonObject>("/api/items/$id", buildJsonObject { put("favorite", !favorite) }); favorite = !favorite } }
                            }
                        }
                        if (party == null) item { PillButton("Watch together", Icons.Filled.Groups) { showTogether = true } }
                        item { PillButton("Share", Icons.Filled.Share) { scope.launch { shareLink(ctx, id) } } }
                        if (hasLive) item {
                            PillButton(if (liveOn) "Live captions on" else "Live captions", Icons.Filled.ClosedCaption) {
                                ccOn = !ccOn; app.settings.captions = if (ccOn) "on" else "off"; applyCaptions()
                            }
                        }
                    }
                    if (!item.tags.isNullOrEmpty()) {
                        LazyRow(contentPadding = PaddingValues(16.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            items(item.tags) { t -> Text("#$t", color = p.text2, style = MaterialTheme.typography.labelMedium,
                                modifier = Modifier.clip(RoundedCornerShape(14.dp)).background(p.surface2).clickable { nav.go("search?q=$t") }.padding(horizontal = 10.dp, vertical = 4.dp)) }
                        }
                    } else Spacer(Modifier.height(12.dp))
                    if (party != null) PartyPanel(party, voice,
                        onJoinVoice = { micPermission.launch(Manifest.permission.RECORD_AUDIO) },
                        onLeaveVoice = { voice?.stop(); voice = null; player.volume = 1f },
                        onLeave = { nav.back(); nav.go("watch/$id") },
                        onEnd = { scope.launch { runCatching { api.delete("/api/parties/$partyId") } } })
                    Details(item)
                }
                if (queue.any { it.id != id }) {
                    item { QueuePanel(queue.filter { it.id != id }, onOpen = { nav.replace(it) }, onRemove = { r -> queue.removeAll { it.id == r.id } }, onClear = { queue.clear() }) }
                }
                item {
                    var autoplay by remember { mutableStateOf(app.settings.autoplay) }
                    Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp, top = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                        Text("Up next", style = MaterialTheme.typography.titleMedium, color = p.text, modifier = Modifier.weight(1f))
                        Text("Autoplay", color = p.text2, style = MaterialTheme.typography.bodySmall)
                        Switch(checked = autoplay, onCheckedChange = { autoplay = it; app.settings.autoplay = it }, modifier = Modifier.scale(0.8f))
                    }
                }
                items(upNext, key = { it.id }) { r ->
                    Box(Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) { VideoCard(r) { nav.replace(r) } }
                }
                if (upNext.isEmpty()) item { Text("Nothing else to play yet.", color = p.text3, modifier = Modifier.padding(16.dp)) }
                item { Spacer(Modifier.height(24.dp)) }
            }
        }
    }

    if (showTogether) {
        WatchTogetherDialog(onDismiss = { showTogether = false }, onStart = { everyone, hostOnly, wait ->
            showTogether = false
            scope.launch {
                runCatching {
                    api.post<PartyInfo>("/api/parties", buildJsonObject {
                        put("item_id", id); put("everyone", everyone); put("control", if (hostOnly) "host" else "everyone")
                        put("wait_for_all", wait); put("position", player.currentPosition / 1000.0)
                    })
                }.onSuccess { p -> nav.back(); nav.go("watch/$id?party=${p.id}") }
                    .onFailure { android.widget.Toast.makeText(ctx, it.message ?: "Couldn't start the party", android.widget.Toast.LENGTH_SHORT).show() }
            }
        })
    }

    if (showSheet) {
        ModalBottomSheet(onDismissRequest = { showSheet = false }, containerColor = p.bgElev) {
            QualitySheet(plan, quality, player.playbackParameters.speed, ccOn, onQuality = { q ->
                quality = q
                // Auto/Original mean "best for this phone"; a specific resolution is remembered as a preference
                app.settings.quality = if (q.id == "auto" || q.id == "original") "smart" else q.id
                load(q, player.currentPosition); showSheet = false
            }, onSpeed = { s -> player.setPlaybackSpeed(s); showSheet = false },
                onLive = { on -> ccOn = on; app.settings.captions = if (on) "on" else "off"; applyCaptions(); showSheet = false })
        }
    }
}

@Composable
private fun QueuePanel(items: List<Item>, onOpen: (Item) -> Unit, onRemove: (Item) -> Unit, onClear: () -> Unit) {
    val p = LocalPaddock.current
    var open by remember { mutableStateOf(true) }
    Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp).fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface)) {
        Row(Modifier.fillMaxWidth().clickable { open = !open }.padding(start = 12.dp, end = 4.dp, top = 8.dp, bottom = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Filled.QueueMusic, null, tint = p.text)
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text("Queue", color = p.text, fontWeight = FontWeight.Medium)
                Text("${items.size} video${if (items.size > 1) "s" else ""} · next: ${items.first().title}", color = p.text2,
                    style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            TextButton(onClick = onClear) { Text("Clear", color = p.text2) }
            Icon(if (open) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore, null, tint = p.text2)
        }
        AnimatedVisibility(open) {
            Column {
                items.forEachIndexed { i, it ->
                    Row(Modifier.fillMaxWidth().clickable { onOpen(it) }.padding(horizontal = 8.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                        Text("${i + 1}", color = p.text3, modifier = Modifier.width(22.dp), textAlign = TextAlign.Center, style = MaterialTheme.typography.bodySmall)
                        Thumb(if (it.has_thumb) it.id else null, Modifier.width(96.dp).aspectRatio(16f / 9f).clip(RoundedCornerShape(6.dp)), fallback = kindIcon(it.kind))
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Text(it.title, color = p.text, maxLines = 2, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodyMedium)
                            Text(fmtDuration(it.duration), color = p.text2, style = MaterialTheme.typography.bodySmall)
                        }
                        IconButton(onClick = { onRemove(it) }) { Icon(Icons.Filled.Close, "Remove from queue", tint = p.text2) }
                    }
                }
            }
        }
    }
}

/** Original when the phone can play the file as-is (instant, full quality, no server work); otherwise Auto.
 *  A resolution the user explicitly picked is remembered and used when available. */
private fun pickQuality(plan: Playback, preferred: String): Quality? {
    fun byId(id: String) = plan.qualities.firstOrNull { it.id == id }
    val smart = (if (plan.mode == "direct") byId("original") else byId("auto")) ?: byId(plan.default_quality) ?: plan.qualities.firstOrNull()
    return if (preferred == "smart") smart else byId(preferred) ?: smart
}


private suspend fun saveProgress(id: Int, positionMs: Long, durationMs: Long, completed: Boolean = false) {
    val dur = durationMs.takeIf { it > 0 }?.div(1000.0)
    val pos = if (completed && dur != null) dur else positionMs / 1000.0
    runCatching { PaddockApp.instance.api.post<JsonObject>("/api/items/$id/progress", buildJsonObject { put("position", pos); if (dur != null) put("duration", dur) }) }
}

private suspend fun shareLink(ctx: android.content.Context, id: Int) {
    val r = runCatching { PaddockApp.instance.api.post<JsonObject>("/api/share", buildJsonObject { put("item_id", id); put("hours", 72) }) }.getOrNull() ?: return
    val url = r["url"]?.jsonPrimitive?.content ?: return
    ctx.startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, url), "Share link (works on your network / VPN)"))
}

/** Full screen like YouTube: landscape + hidden system bars; leaving restores normal rotation. */
fun setFullscreen(activity: Activity, on: Boolean, leaving: Boolean = false) {
    activity.requestedOrientation = when {
        leaving -> ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        on -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
        else -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
    }
    systemBars(activity, hidden = on && !leaving)
}

fun systemBars(activity: Activity, hidden: Boolean) {
    val c = WindowCompat.getInsetsController(activity.window, activity.window.decorView)
    if (hidden) { c.hide(WindowInsetsCompat.Type.systemBars()); c.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE }
    else c.show(WindowInsetsCompat.Type.systemBars())
}

@Composable
private fun QualitySheet(plan: Playback, current: Quality?, speed: Float, liveOn: Boolean,
                         onQuality: (Quality) -> Unit, onSpeed: (Float) -> Unit, onLive: (Boolean) -> Unit) {
    val p = LocalPaddock.current
    Column(Modifier.padding(bottom = 24.dp)) {
        Text("Quality", style = MaterialTheme.typography.titleMedium, color = p.text, modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
        for (q in plan.qualities) {
            Row(Modifier.fillMaxWidth().clickable { onQuality(q) }.padding(horizontal = 24.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.width(32.dp)) { if (q.id == current?.id) Icon(Icons.Filled.Check, null, tint = p.text) }
                Column(Modifier.weight(1f)) {
                    Text(q.label, color = p.text)
                    val hint = when (q.id) { "auto" -> "Adjusts to your connection"; "original" -> if (q.type == "direct") "Full quality · original file" else "Full quality · remuxed, video untouched"
                        else -> "Converted on the server${if (q.id in setOf("2160p", "1440p", "1080p")) " · high quality" else " · saves data"}" }
                    Text(hint, color = p.text2, style = MaterialTheme.typography.bodySmall)
                }
            }
        }
        HorizontalDivider(color = p.border, modifier = Modifier.padding(vertical = 8.dp))
        Text("Playback speed", style = MaterialTheme.typography.titleMedium, color = p.text, modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
        LazyRow(contentPadding = PaddingValues(horizontal = 20.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(listOf(0.5f, 0.75f, 1f, 1.25f, 1.5f, 2f)) { s ->
                FilterChip(selected = s == speed, onClick = { onSpeed(s) }, label = { Text(if (s == 1f) "Normal" else "${s}×") })
            }
        }
        if (plan.live_captions?.enabled == true || plan.live_captions?.complete == true) {
            HorizontalDivider(color = p.border, modifier = Modifier.padding(vertical = 8.dp))
            Row(Modifier.fillMaxWidth().clickable { onLive(!liveOn) }.padding(horizontal = 24.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Filled.ClosedCaption, null, tint = p.text)
                Spacer(Modifier.width(16.dp))
                Column(Modifier.weight(1f)) {
                    Text(if (plan.live_captions?.complete == true) "Transcript captions (AI)" else "Live transcription (AI)", color = p.text)
                    Text("Generated on your server", color = p.text2, style = MaterialTheme.typography.bodySmall)
                }
                Switch(checked = liveOn, onCheckedChange = { onLive(it) })
            }
        }
        if (plan.subtitles.isNotEmpty()) Text("File subtitles: use the CC button on the player.", color = p.text3,
            style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
    }
}

@Composable
private fun Details(item: Item) {
    val p = LocalPaddock.current
    var open by remember { mutableStateOf(false) }
    val m = item.meta ?: JsonObject(emptyMap())
    fun s(k: String) = m.str(k)
    val rows = listOfNotNull(
        "Resolution" to (if (item.width != null && item.height != null) "${item.width}×${item.height}" else null), "Video" to s("vcodec"), "Audio" to s("acodec"),
        "Camera" to (s("camera_model") ?: s("model")), "ISO" to s("iso"), "Lens" to s("lens"), "Timecode" to s("timecode"),
        "Size" to fmtBytes(item.size).ifBlank { null }, "File" to item.path,
    ).filter { it.second != null }
    Column(Modifier.padding(horizontal = 16.dp).fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface).clickable { open = !open }.padding(12.dp)) {
        Text(if (open) "Details" else "Details …more", color = p.text, fontWeight = FontWeight.Medium)
        AnimatedVisibility(open, enter = fadeIn(), exit = fadeOut()) {
            Column(Modifier.padding(top = 8.dp)) {
                for ((k, v) in rows) Row(Modifier.padding(vertical = 2.dp)) {
                    Text(k, color = p.text2, modifier = Modifier.width(100.dp), style = MaterialTheme.typography.bodySmall)
                    Text(v!!, color = p.text, style = MaterialTheme.typography.bodySmall)
                }
            }
        }
    }
}
