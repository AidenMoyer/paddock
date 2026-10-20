package app.paddock.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.paddock.PaddockApp
import app.paddock.data.Group
import app.paddock.data.Item
import app.paddock.ui.*
import app.paddock.ui.theme.LocalPaddock
import kotlinx.coroutines.delay
import java.net.URLEncoder

private data class BrowseSpec(val title: String, val groups: String? = null, val kind: String? = null, val libKind: String? = null,
                              val sort: String = "added", val square: Boolean = false, val extra: String = "")

private fun spec(kind: String) = when (kind) {
    "movies" -> BrowseSpec("Movies", kind = "video", libKind = "movies")
    "shows" -> BrowseSpec("TV Shows", groups = "show:")
    "music" -> BrowseSpec("Music", groups = "album:", square = true)
    "videos" -> BrowseSpec("Home Videos", kind = "video", libKind = "home_videos", sort = "newest")
    "red" -> BrowseSpec("Footage", groups = "shoot:")
    "engineering" -> BrowseSpec("Engineering", groups = "eng:")
    "favorites" -> BrowseSpec("Favorites", extra = "&favorite=true")
    else -> BrowseSpec(kind.replaceFirstChar { it.uppercase() })
}

@Composable
fun TopBar(title: String, nav: Nav, actions: @Composable RowScope.() -> Unit = {}) {
    val p = LocalPaddock.current
    Row(Modifier.fillMaxWidth().height(56.dp).padding(horizontal = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        IconButton(onClick = { nav.back() }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back", tint = p.text) }
        Text(title, style = MaterialTheme.typography.titleLarge, color = p.text, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        actions()
    }
}

@Composable
fun BrowseScreen(kind: String, nav: Nav) {
    val s = spec(kind)
    val api = PaddockApp.instance.api
    Column(Modifier.fillMaxSize()) {
        TopBar(s.title, nav)
        if (s.groups != null) {
            val (load, retry) = rememberLoad(kind) { api.get<List<Group>>("/api/groups?prefix=${URLEncoder.encode(s.groups, "UTF-8")}&limit=500") }
            LoadView(load, retry) { groups ->
                if (groups.isEmpty()) EmptyState(Icons.Filled.FolderOpen, "Nothing here yet")
                LazyVerticalGrid(GridCells.Adaptive(if (s.square) 150.dp else 280.dp), contentPadding = PaddingValues(16.dp),
                    horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(20.dp)) {
                    items(groups) { g -> GroupCard(g, square = s.square) { nav.go("group/${URLEncoder.encode(g.group_key, "UTF-8")}") } }
                }
            }
        } else {
            val libs = PaddockApp.instance.libraries.value.filter { s.libKind == null || it.kind == s.libKind }.map { it.id }
            val libParam = if (s.libKind != null && libs.size == 1) "&library_id=${libs[0]}" else ""
            var sort by remember { mutableStateOf(s.sort) }
            ChipRow(listOf("added" to "Recently added", "newest" to "Newest", "title" to "A–Z", "duration" to "Longest"), sort) { sort = it }
            ItemGrid("/api/items?${s.kind?.let { "kind=$it&" } ?: ""}sort=$sort$libParam${s.extra}", nav,
                filter = { s.libKind == null || it.library_kind == s.libKind || (s.libKind == "home_videos" && it.library_kind in setOf("photos", "mixed")) })
        }
    }
}

/** Paged grid over /api/items. */
@Composable
fun ItemGrid(query: String, nav: Nav, filter: (Item) -> Boolean = { true }, header: (@Composable () -> Unit)? = null) {
    val api = PaddockApp.instance.api
    val items = remember(query) { mutableStateListOf<Item>() }
    var offset by remember(query) { mutableIntStateOf(0) }
    var done by remember(query) { mutableStateOf(false) }
    var error by remember(query) { mutableStateOf<String?>(null) }
    var loading by remember(query) { mutableStateOf(false) }
    suspend fun more() {
        if (loading || done) return
        loading = true
        try {
            val page = api.get<List<Item>>("$query&limit=60&offset=$offset")
            items += page.filter(filter)
            offset += page.size
            done = page.size < 60
        } catch (e: Exception) { error = e.message }
        loading = false
    }
    LaunchedEffect(query) { more() }
    if (error != null && items.isEmpty()) { EmptyState(Icons.Filled.CloudOff, "Couldn't load", error); return }
    if (done && items.isEmpty()) { EmptyState(Icons.Filled.SearchOff, "Nothing here yet"); return }
    LazyVerticalGrid(GridCells.Adaptive(300.dp), contentPadding = PaddingValues(16.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(20.dp)) {
        if (header != null) item(span = { GridItemSpan(maxLineSpan) }) { header() }
        items(items, key = { it.id }) { it -> VideoCard(it) { nav.open(it) } }
        item(span = { GridItemSpan(maxLineSpan) }) {
            LaunchedEffect(items.size) { if (!done) more() }
            if (loading) Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator(Modifier.size(28.dp)) }
        }
    }
}

@Composable
fun GroupScreen(key: String, nav: Nav) {
    val api = PaddockApp.instance.api
    val p = LocalPaddock.current
    val (load, retry) = rememberLoad(key) { api.get<List<Item>>("/api/items?group=${URLEncoder.encode(key, "UTF-8")}&sort=group&limit=500") }
    val kind = key.substringBefore(":")
    Column(Modifier.fillMaxSize()) {
        val first = (load as? Load.Ok)?.data?.firstOrNull()
        TopBar(when (kind) { "album" -> first?.album ?: "Album"; "show" -> first?.show ?: "Show"; "shoot" -> "Shoot · ${key.substringAfter(":")}"
            "eng" -> key.substringAfterLast("/"); else -> key.substringAfter(":") }, nav)
        LoadView(load, retry) { items ->
            if (kind == "album" || kind == "show") {
                LazyColumn(contentPadding = PaddingValues(bottom = 24.dp)) {
                    if (kind == "album" && first != null) item {
                        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                            Thumb(if (first.has_thumb) first.id else null, Modifier.size(120.dp).clip(RoundedCornerShape(12.dp)), fallback = Icons.Filled.Album)
                            Spacer(Modifier.width(16.dp))
                            Column {
                                Text(first.album ?: "", style = MaterialTheme.typography.titleLarge, color = p.text)
                                Text("${first.artist ?: ""} · ${items.size} songs", color = p.text2)
                                Spacer(Modifier.height(8.dp))
                                PillButton("Play", Icons.Filled.PlayArrow, primary = true) { nav.open(items.first()) }
                            }
                        }
                    }
                    items(items) { it ->
                        Row(Modifier.fillMaxWidth().clickable { nav.open(it) }.padding(horizontal = 16.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            if (kind == "show") Thumb(if (it.has_thumb) it.id else null, Modifier.width(140.dp).aspectRatio(16f / 9f).clip(RoundedCornerShape(8.dp)))
                            else Text("${it.progress?.let { "" } ?: ""}", modifier = Modifier.width(0.dp))
                            Spacer(Modifier.width(12.dp))
                            Column(Modifier.weight(1f)) {
                                Text(if (kind == "show" && it.episode != null) "${it.episode}. ${it.title}" else it.title, color = p.text, maxLines = 2, overflow = TextOverflow.Ellipsis)
                                Text(fmtDuration(it.duration), color = p.text2, style = MaterialTheme.typography.bodySmall)
                            }
                        }
                    }
                }
            } else {
                LazyVerticalGrid(GridCells.Adaptive(260.dp), contentPadding = PaddingValues(16.dp),
                    horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(20.dp)) {
                    items(items, key = { it.id }) { it -> VideoCard(it, compact = true) { nav.open(it) } }
                }
            }
        }
    }
}

@Composable
fun SearchScreen(nav: Nav, initial: String = "") {
    val p = LocalPaddock.current
    var q by remember { mutableStateOf(initial) }
    var submitted by remember { mutableStateOf(initial) }
    LaunchedEffect(q) { delay(350); if (q.length >= 2) submitted = q }
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = { nav.back() }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back", tint = p.text) }
            TextField(q, { q = it }, singleLine = true, placeholder = { Text("Search titles, footage, parts, what was said…") },
                modifier = Modifier.weight(1f), shape = RoundedCornerShape(24.dp),
                colors = TextFieldDefaults.colors(focusedContainerColor = p.surface, unfocusedContainerColor = p.surface,
                    focusedIndicatorColor = androidx.compose.ui.graphics.Color.Transparent, unfocusedIndicatorColor = androidx.compose.ui.graphics.Color.Transparent))
        }
        if (submitted.length >= 2) ItemGrid("/api/items?query=${URLEncoder.encode(submitted, "UTF-8")}&sort=relevance", nav)
        else EmptyState(Icons.Filled.Search, "Search everything", "Titles, folders, tags, camera and print metadata — and words spoken in transcribed videos.")
    }
}
