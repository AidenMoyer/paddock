package app.paddock.desktop

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.Crossfade
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.gestures.Orientation
import androidx.compose.foundation.gestures.draggable
import androidx.compose.foundation.gestures.rememberDraggableState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.input.pointer.PointerEventType
import androidx.compose.ui.input.pointer.onPointerEvent
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.paddock.data.Group
import app.paddock.data.HomeFeed
import app.paddock.data.Item
import app.paddock.data.PaddockJson
import app.paddock.data.Playback
import app.paddock.live.PartyInfo
import app.paddock.ui.theme.LocalPaddock
import kotlinx.coroutines.launch
import kotlinx.serialization.json.decodeFromJsonElement
import java.awt.FileDialog
import java.awt.Frame
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

// ---- pairing ------------------------------------------------------------------------------------

@Composable
fun PairScreen() {
    val p = LocalPaddock.current
    var code by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    val clip = LocalClipboardManager.current
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(Modifier.widthIn(max = 520.dp).padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Image(painterResource("paddock.png"), null, Modifier.size(72.dp))
            Spacer(Modifier.height(20.dp))
            Text("Connect to your Paddock", style = MaterialTheme.typography.headlineMedium, color = p.text)
            Spacer(Modifier.height(10.dp))
            Text("On your Paddock in a browser, open your account → Connect a device, then copy the computer code and paste it here. " +
                "This computer must be on your home network for this step.", color = p.text2, textAlign = TextAlign.Center)
            Spacer(Modifier.height(24.dp))
            OutlinedTextField(code, { code = it; error = null }, label = { Text("Computer code") }, placeholder = { Text("PADDOCK-…") },
                minLines = 3, maxLines = 5, modifier = Modifier.fillMaxWidth())
            Spacer(Modifier.height(8.dp))
            Row(Modifier.fillMaxWidth()) {
                TextButton(onClick = { clip.getText()?.text?.let { code = it } }) { Text("Paste", color = p.link) }
                Spacer(Modifier.weight(1f))
                BrandButton(if (busy) "Connecting…" else "Connect", enabled = !busy && code.isNotBlank(), onClick = {
                    busy = true
                    scope.launch {
                        runCatching { Pairing.pair(code) }.onSuccess { Store.addServer(it) }.onFailure { error = it.message ?: "Pairing failed" }
                        busy = false
                    }
                })
            }
            if (error != null) Text(error!!, color = p.danger, modifier = Modifier.padding(top = 12.dp))
        }
    }
}

// ---- home and browsing -----------------------------------------------------------------------------

private fun open(item: Item) {
    when (item.kind) {
        "photo" -> PhotoViewer.show(listOf(item), 0)
        "audio" -> MusicPlayer.play(listOf(item), 0)
        "video", "red_clip", "braw_clip" -> Nav.go(Screen.Watch(item.id))
    }
}

@Composable
fun HomeScreen() {
    val conn by Connection.state.collectAsState()
    var feed by remember { mutableStateOf<HomeFeed?>(null) }
    var err by remember { mutableStateOf<String?>(null) }
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(conn.baseUrl, tick) {
        if (conn.baseUrl == null) return@LaunchedEffect
        runCatching { Connection.api.get<HomeFeed>("/api/home") }.onSuccess { feed = it; err = null }.onFailure { err = it.message }
    }
    val f = feed
    when {
        f == null && err != null -> Problem(err!!) { tick++ }
        f == null -> Loading()
        else -> LazyColumn(contentPadding = PaddingValues(start = 24.dp, end = 24.dp, bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(20.dp)) {
            item { PartiesStrip() }
            for (row in f.rows) {
                if (row.type == "projects") continue
                item(row.key) {
                    SectionTitle(row.title)
                    LazyRow(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        if (row.type == "groups") {
                            val groups = row.items.mapNotNull { runCatching { PaddockJson.decodeFromJsonElement<Group>(it) }.getOrNull() }
                            items(groups) { g -> GroupCard(g, square = g.group_key.startsWith("album:")) { Nav.go(Screen.Group(g.group_key, g.name, g.group_key.startsWith("album:"))) } }
                        } else {
                            val list = row.items.mapNotNull { runCatching { PaddockJson.decodeFromJsonElement<Item>(it) }.getOrNull() }
                            items(list) { it2 -> ItemCard(it2) { if (it2.kind == "photo") PhotoViewer.show(list.filter { x -> x.kind == "photo" }, list.filter { x -> x.kind == "photo" }.indexOf(it2)) else open(it2) } }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun PartiesStrip() {
    val p = LocalPaddock.current
    val parties by produceState<List<PartyInfo>>(emptyList()) { value = runCatching { Connection.api.get<List<PartyInfo>>("/api/parties") }.getOrDefault(emptyList()) }
    if (parties.isEmpty()) return
    SectionTitle("Happening now")
    LazyRow(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        items(parties) { party ->
            Row(Modifier.clip(RoundedCornerShape(14.dp)).background(p.surface).clickable { Nav.go(Screen.Watch(party.item_id, party.id)) }.padding(12.dp),
                verticalAlignment = Alignment.CenterVertically) {
                RemoteImage("/api/items/${party.item_id}/thumb", Modifier.size(96.dp, 54.dp).clip(RoundedCornerShape(8.dp)))
                Spacer(Modifier.width(12.dp))
                Column {
                    Text(party.title, color = p.text, fontWeight = FontWeight.Medium, maxLines = 1)
                    Text("${party.host}'s watch party · ${party.members.size} watching", color = p.text2, fontSize = 13.sp)
                }
                Spacer(Modifier.width(12.dp))
                BrandButton("Join", onClick = { Nav.go(Screen.Watch(party.item_id, party.id)) })
            }
        }
    }
}

/** A paged grid of items for any listing endpoint (offset/limit). */
@Composable
private fun ItemGrid(query: String, title: String, emptyText: String, cell: Int = 240) {
    val items = remember(query) { mutableStateListOf<Item>() }
    var done by remember(query) { mutableStateOf(false) }
    var loading by remember(query) { mutableStateOf(false) }
    var err by remember(query) { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    val more: () -> Unit = {
        if (!loading && !done) {
            loading = true
            scope.launch {
                runCatching { Connection.api.get<List<Item>>("$query&limit=120&offset=${items.size}") }
                    .onSuccess { items.addAll(it); done = it.size < 120; err = null }.onFailure { err = it.message }
                loading = false
            }
        }
    }
    LaunchedEffect(query) { more() }
    val grid = rememberLazyGridState()
    val nearEnd by remember { derivedStateOf { grid.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0 >= items.size - 12 } }
    LaunchedEffect(nearEnd) { if (nearEnd) more() }
    if (items.isEmpty() && err != null) return Problem(err!!) { more() }
    LazyVerticalGrid(GridCells.Adaptive(cell.dp), state = grid, contentPadding = PaddingValues(24.dp),
        horizontalArrangement = Arrangement.spacedBy(16.dp), verticalArrangement = Arrangement.spacedBy(20.dp)) {
        item(span = { GridItemSpan(maxLineSpan) }) { SectionTitle(title) }
        items(items, key = { it.id }) { it2 -> ItemCard(it2, cell) { open(it2) } }
        if (loading) item(span = { GridItemSpan(maxLineSpan) }) { Loading() }
        else if (items.isEmpty() && done) item(span = { GridItemSpan(maxLineSpan) }) { Problem(emptyText) }
    }
}

@Composable fun VideosScreen() = ItemGrid("/api/items?kind=video,red_clip&sort=added", "Videos", "No videos yet")
@Composable fun LibraryScreen(id: Int, name: String) = ItemGrid("/api/items?library_id=$id&sort=newest", name, "This library is empty")
@Composable fun SearchScreen(q: String) = ItemGrid("/api/items?query=${java.net.URLEncoder.encode(q, "UTF-8")}", "Results for \"$q\"", "Nothing matched")

@Composable
fun GroupScreen(key: String, name: String, music: Boolean) {
    if (!music) return ItemGrid("/api/items?group=${java.net.URLEncoder.encode(key, "UTF-8")}&sort=group", name, "Nothing here")
    val p = LocalPaddock.current
    val tracks by produceState<List<Item>?>(null, key) {
        value = runCatching { Connection.api.get<List<Item>>("/api/items?group=${java.net.URLEncoder.encode(key, "UTF-8")}&sort=group&limit=500") }.getOrDefault(emptyList())
    }
    val list = tracks ?: return Loading()
    LazyColumn(contentPadding = PaddingValues(24.dp)) {
        item {
            Row(verticalAlignment = Alignment.Bottom) {
                RemoteImage(list.firstOrNull()?.let { "/api/items/${it.id}/thumb" }, Modifier.size(180.dp).clip(RoundedCornerShape(12.dp)))
                Spacer(Modifier.width(24.dp))
                Column {
                    Text(name, style = MaterialTheme.typography.headlineMedium, color = p.text)
                    Text(listOfNotNull(list.firstOrNull()?.artist, "${list.size} songs").joinToString(" · "), color = p.text2)
                    Spacer(Modifier.height(12.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        BrandButton("Play", icon = Icons.Filled.PlayArrow, onClick = { MusicPlayer.play(list, 0) })
                        SoftButton("Shuffle", icon = Icons.Filled.Shuffle, onClick = { MusicPlayer.play(list.shuffled(), 0) })
                    }
                }
            }
            Spacer(Modifier.height(20.dp))
        }
        items(list.size) { i ->
            val t = list[i]
            val now by MusicPlayer.current.collectAsState()
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(if (now?.id == t.id) p.surface else Color.Transparent)
                .clickable { MusicPlayer.play(list, i) }.padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("${i + 1}", color = p.text3, modifier = Modifier.width(32.dp))
                Text(t.title, color = if (now?.id == t.id) p.accent else p.text, modifier = Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(fmtDuration(t.duration), color = p.text2, fontSize = 13.sp)
            }
        }
    }
}

@Composable
fun MusicScreen() {
    val albums by produceState<List<Group>?>(null) { value = runCatching { Connection.api.get<List<Group>>("/api/groups?prefix=album:&limit=500") }.getOrDefault(emptyList()) }
    val list = albums ?: return Loading()
    LazyVerticalGrid(GridCells.Adaptive(180.dp), contentPadding = PaddingValues(24.dp), horizontalArrangement = Arrangement.spacedBy(16.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp)) {
        item(span = { GridItemSpan(maxLineSpan) }) { SectionTitle("Music") }
        items(list, key = { it.group_key }) { g -> GroupCard(g, 180, square = true) { Nav.go(Screen.Group(g.group_key, g.name, true)) } }
        if (list.isEmpty()) item(span = { GridItemSpan(maxLineSpan) }) { Problem("No music yet") }
    }
}

// ---- photos: a grid by day, and a viewer you scroll or swipe through ---------------------------------

private val dayFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("EEEE, d MMMM yyyy")

@Composable
fun PhotosScreen() {
    val p = LocalPaddock.current
    val photos = remember { mutableStateListOf<Item>() }
    var done by remember { mutableStateOf(false) }
    var loading by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val more: () -> Unit = {
        if (!loading && !done) {
            loading = true
            scope.launch {
                runCatching { Connection.api.get<List<Item>>("/api/items?kind=photo,video&library_kind=photos&sort=newest&limit=200&offset=${photos.size}") }
                    .onSuccess { got -> photos.addAll(got.filter { it.kind == "photo" || it.library_kind == "photos" }); done = got.size < 200 }
                loading = false
            }
        }
    }
    LaunchedEffect(Unit) { more() }
    val grid = rememberLazyGridState()
    val nearEnd by remember { derivedStateOf { (grid.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0) >= grid.layoutInfo.totalItemsCount - 20 } }
    LaunchedEffect(nearEnd) { if (nearEnd) more() }
    val days = remember(photos.size) {
        photos.groupBy { it.taken_at?.let { t -> Instant.ofEpochSecond(t.toLong()).atZone(ZoneId.systemDefault()).toLocalDate() } }.toList()
    }
    LazyVerticalGrid(GridCells.Adaptive(170.dp), state = grid, contentPadding = PaddingValues(24.dp),
        horizontalArrangement = Arrangement.spacedBy(4.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        item(span = { GridItemSpan(maxLineSpan) }) { SectionTitle("Photos") }
        for ((day, list) in days) {
            item(span = { GridItemSpan(maxLineSpan) }) {
                Text(day?.format(dayFmt) ?: "No date", color = p.text, fontWeight = FontWeight.Medium, modifier = Modifier.padding(top = 16.dp, bottom = 6.dp))
            }
            items(list, key = { it.id }) { ph ->
                Box(Modifier.aspectRatio(1f).clip(RoundedCornerShape(4.dp)).background(p.surface2).clickable { PhotoViewer.show(photos.toList(), photos.indexOf(ph)) }) {
                    RemoteImage("/api/items/${ph.id}/thumb", Modifier.fillMaxSize())
                    if (ph.kind != "photo") Icon(Icons.Filled.PlayCircle, null, tint = Color.White, modifier = Modifier.align(Alignment.Center).size(36.dp))
                }
            }
        }
        if (loading) item(span = { GridItemSpan(maxLineSpan) }) { Loading() }
        else if (photos.isEmpty() && done) item(span = { GridItemSpan(maxLineSpan) }) { Problem("No photos yet. Phones back up here from the Paddock app.") }
    }
}

object PhotoViewer {
    val list = mutableStateOf<List<Item>>(emptyList())
    val index = mutableStateOf(-1)
    fun show(items: List<Item>, at: Int) { list.value = items; index.value = at.coerceAtLeast(0) }
    fun close() { index.value = -1 }
    fun step(d: Int) { val n = list.value.size; if (n > 0) index.value = (index.value + d).coerceIn(0, n - 1) }
}

/** Full-window photo viewer: arrow keys, the mouse wheel, or a drag move between photos; Esc closes. */
@OptIn(ExperimentalComposeUiApi::class)
@Composable
fun PhotoViewerOverlay() {
    val i by PhotoViewer.index
    val list by PhotoViewer.list
    if (i < 0 || i >= list.size) return
    val item = list[i]
    val focus = remember { FocusRequester() }
    var drag by remember { mutableStateOf(0f) }
    var wheelAt by remember { mutableStateOf(0L) }
    LaunchedEffect(Unit) { focus.requestFocus() }
    // keep the neighbours ready so moving feels instant
    LaunchedEffect(i) { for (d in listOf(1, -1, 2)) list.getOrNull(i + d)?.let { if (it.kind == "photo") Images.load("/api/items/${it.id}/preview", big = true) } }
    Box(Modifier.fillMaxSize().background(Color(0xF2000000)).focusRequester(focus).focusable()
        .onKeyEvent { e ->
            if (e.type != KeyEventType.KeyDown) return@onKeyEvent false
            when (e.key) { Key.DirectionRight, Key.DirectionDown, Key.Spacebar -> PhotoViewer.step(1); Key.DirectionLeft, Key.DirectionUp -> PhotoViewer.step(-1)
                Key.Escape -> PhotoViewer.close(); else -> return@onKeyEvent false }
            true
        }
        .onPointerEvent(PointerEventType.Scroll) { e ->
            val dy = e.changes.firstOrNull()?.scrollDelta?.let { it.y + it.x } ?: 0f
            val now = System.currentTimeMillis()
            if (dy != 0f && now - wheelAt > 180) { wheelAt = now; PhotoViewer.step(if (dy > 0) 1 else -1) }
        }
        .draggable(rememberDraggableState { drag += it }, Orientation.Horizontal, onDragStopped = {
            if (drag < -80) PhotoViewer.step(1) else if (drag > 80) PhotoViewer.step(-1)
            drag = 0f
        })) {
        Crossfade(item.id, label = "photo") { id ->
            val it2 = list.firstOrNull { x -> x.id == id } ?: item
            if (it2.kind == "photo") RemoteImage("/api/items/$id/preview", Modifier.fillMaxSize().padding(48.dp), ContentScale.Fit, big = true)
            else Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                RemoteImage("/api/items/$id/thumb", Modifier.fillMaxSize().padding(48.dp), ContentScale.Fit)
                BrandButton("Play video", icon = Icons.Filled.PlayArrow, onClick = { PhotoViewer.close(); Nav.go(Screen.Watch(id)) })
            }
        }
        IconButton(onClick = { PhotoViewer.close() }, Modifier.align(Alignment.TopEnd).padding(12.dp)) { Icon(Icons.Filled.Close, "Close", tint = Color.White) }
        if (i > 0) IconButton(onClick = { PhotoViewer.step(-1) }, Modifier.align(Alignment.CenterStart).padding(12.dp)) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Previous", tint = Color.White) }
        if (i < list.size - 1) IconButton(onClick = { PhotoViewer.step(1) }, Modifier.align(Alignment.CenterEnd).padding(12.dp)) { Icon(Icons.AutoMirrored.Filled.ArrowForward, "Next", tint = Color.White) }
        Text(listOfNotNull(item.date, item.camera_model, "${i + 1} of ${list.size}").joinToString(" · "), color = Color(0xCCFFFFFF), fontSize = 13.sp,
            modifier = Modifier.align(Alignment.BottomCenter).padding(16.dp))
    }
}

// ---- music --------------------------------------------------------------------------------------

object MusicPlayer {
    val player = VideoPlayer(video = false)
    val queue = mutableStateOf<List<Item>>(emptyList())
    val current = kotlinx.coroutines.flow.MutableStateFlow<Item?>(null)
    private var index = 0
    private val scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.SupervisorJob() + kotlinx.coroutines.Dispatchers.Default)

    init { player.onEnded = { next() } }

    fun play(items: List<Item>, at: Int) { queue.value = items; index = at; start() }
    fun next() { if (index < queue.value.size - 1) { index++; start() } }
    fun prev() { if (player.position.value > 4) player.seek(0.0) else if (index > 0) { index--; start() } }
    fun stop() { player.pause(); current.value = null }

    private fun start() {
        val item = queue.value.getOrNull(index) ?: return
        current.value = item
        scope.launch {
            val plan = runCatching { Connection.api.get<Playback>("/api/items/${item.id}/playback?codecs=aac,mp3,mp4") }.getOrNull() ?: return@launch
            Connection.ensure() ?: return@launch
            val url = if (plan.mode == "direct" && plan.url != null && item.ext in setOf(".mp3", ".m4a", ".aac", ".wav")) plan.url!!
            else "/api/items/${item.id}/stream.mp4"
            player.open(MediaProxy.url(url))
        }
    }
}

@Composable
fun MusicBar() {
    val p = LocalPaddock.current
    val item by MusicPlayer.current.collectAsState()
    val playing by MusicPlayer.player.playing.collectAsState()
    val pos by MusicPlayer.player.position.collectAsState()
    val dur by MusicPlayer.player.duration.collectAsState()
    val t = item ?: return
    Row(Modifier.fillMaxWidth().height(72.dp).background(p.bgElev).padding(horizontal = 20.dp), verticalAlignment = Alignment.CenterVertically) {
        RemoteImage("/api/items/${t.id}/thumb", Modifier.size(48.dp).clip(RoundedCornerShape(6.dp)))
        Spacer(Modifier.width(12.dp))
        Column(Modifier.width(240.dp)) {
            Text(t.title, color = p.text, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(t.artist ?: t.album ?: "", color = p.text2, fontSize = 13.sp, maxLines = 1)
        }
        IconButton(onClick = { MusicPlayer.prev() }) { Icon(Icons.Filled.SkipPrevious, "Previous", tint = p.text) }
        IconButton(onClick = { MusicPlayer.player.toggle() }) { Icon(if (playing) Icons.Filled.Pause else Icons.Filled.PlayArrow, if (playing) "Pause" else "Play", tint = p.text) }
        IconButton(onClick = { MusicPlayer.next() }) { Icon(Icons.Filled.SkipNext, "Next", tint = p.text) }
        Text(fmtDuration(pos), color = p.text2, fontSize = 12.sp)
        Slider(value = if (dur > 0) (pos / dur).toFloat().coerceIn(0f, 1f) else 0f, onValueChange = { MusicPlayer.player.seek(it * dur) },
            modifier = Modifier.weight(1f).padding(horizontal = 12.dp))
        Text(fmtDuration(dur), color = p.text2, fontSize = 12.sp)
        IconButton(onClick = { MusicPlayer.stop() }) { Icon(Icons.Filled.Close, "Close player", tint = p.text2) }
    }
}

// ---- watch parties -------------------------------------------------------------------------------

@Composable
fun PartiesScreen() {
    val p = LocalPaddock.current
    var tick by remember { mutableStateOf(0) }
    val parties by produceState<List<PartyInfo>?>(null, tick) { value = runCatching { Connection.api.get<List<PartyInfo>>("/api/parties") }.getOrDefault(emptyList()) }
    val list = parties ?: return Loading()
    LazyColumn(contentPadding = PaddingValues(24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            SectionTitle("Watch parties")
            Text("Watch together in sync, with chat and voice. Start one from any video with Watch together.", color = p.text2)
            TextButton(onClick = { tick++ }) { Text("Refresh", color = p.link) }
        }
        if (list.isEmpty()) item { Problem("No watch parties right now") }
        items(list) { party ->
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(p.surface).padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                RemoteImage("/api/items/${party.item_id}/thumb", Modifier.size(160.dp, 90.dp).clip(RoundedCornerShape(8.dp)))
                Spacer(Modifier.width(16.dp))
                Column(Modifier.weight(1f)) {
                    Text(party.title, color = p.text, fontWeight = FontWeight.Medium, fontSize = 17.sp)
                    Text("Hosted by ${party.host} · ${party.members.joinToString { it.username }}", color = p.text2)
                }
                BrandButton("Join", onClick = { Nav.go(Screen.Watch(party.item_id, party.id)) })
            }
        }
    }
}

// ---- settings ------------------------------------------------------------------------------------

@Composable
fun SettingsScreen() {
    val p = LocalPaddock.current
    val store by Store.state.collectAsState()
    var note by remember { mutableStateOf<String?>(null) }
    var adding by remember { mutableStateOf(false) }
    if (adding) return Column { TextButton(onClick = { adding = false }) { Text("← Back to settings", color = p.link) }; PairScreen() }
    LazyColumn(contentPadding = PaddingValues(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp), modifier = Modifier.widthIn(max = 820.dp)) {
        item { Text("Settings", style = MaterialTheme.typography.headlineMedium, color = p.text) }
        item {
            Card("Servers") {
                for (s in store.servers) Row(Modifier.fillMaxWidth().padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(selected = s.id == Store.active?.id, onClick = { Store.setActive(s.id) })
                    Column(Modifier.weight(1f)) {
                        Text(s.name, color = p.text, fontWeight = FontWeight.Medium)
                        Text("${s.user.display_name ?: s.user.username} · ${s.lanUrls.firstOrNull() ?: ""}", color = p.text2, fontSize = 13.sp)
                    }
                    if (Pairing.tunnelConfig(s) != null) TextButton(onClick = {
                        val dlg = FileDialog(null as Frame?, "Save the Paddock tunnel for WireGuard", FileDialog.SAVE).apply { file = "paddock-${s.name.lowercase().replace(Regex("[^a-z0-9]+"), "-")}.conf"; isVisible = true }
                        val f = dlg.file?.let { File(dlg.directory, it) }
                        if (f != null && Pairing.saveTunnelConfig(s, f)) note = "Saved. Import ${f.name} in the WireGuard app (Import tunnel from file) to reach ${s.name} away from home."
                    }) { Text("Save tunnel file", color = p.link) }
                    TextButton(onClick = { Store.removeServer(s.id) }) { Text("Forget", color = p.danger) }
                }
                note?.let { Text(it, color = p.text2, fontSize = 13.sp, modifier = Modifier.padding(top = 6.dp)) }
                TextButton(onClick = { adding = true }) { Text("+ Add another server", color = p.link) }
            }
        }
        item {
            Card("Appearance") {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    for ((v, l) in listOf("system" to "System", "dark" to "Dark", "light" to "Light"))
                        SoftButton(l, onClick = { Store.setSettings { it.copy(theme = v) } }, active = store.settings.theme == v)
                }
                Row(Modifier.padding(top = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text("Play the next video automatically", color = p.text, modifier = Modifier.weight(1f))
                    Switch(store.settings.autoplay, { v -> Store.setSettings { it.copy(autoplay = v) } })
                }
            }
        }
        item {
            Card("Away from home") {
                Text("At home, Paddock connects directly. Elsewhere, save the tunnel file above and turn the tunnel on in the WireGuard app " +
                    "(wireguard.com/install). Its only route is your Paddock server, so the rest of this computer's traffic is untouched.", color = p.text2)
            }
        }
        item { Text("Paddock for ${System.getProperty("os.name")} $APP_VERSION · private, self-hosted", color = p.text3, fontSize = 12.sp) }
    }
}

@Composable
private fun Card(title: String, content: @Composable ColumnScope.() -> Unit) {
    val p = LocalPaddock.current
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface).padding(18.dp)) {
        Text(title, style = MaterialTheme.typography.titleMedium, color = p.text, modifier = Modifier.padding(bottom = 10.dp))
        content()
    }
}
