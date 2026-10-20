package app.paddock.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import app.paddock.PaddockApp
import app.paddock.data.Group
import app.paddock.data.HomeFeed
import app.paddock.data.Item
import app.paddock.data.PaddockJson
import app.paddock.data.ProjectSummary
import app.paddock.ui.*
import app.paddock.ui.theme.LocalPaddock
import kotlinx.serialization.json.decodeFromJsonElement

@Composable
fun HomeScreen(nav: Nav) {
    val api = PaddockApp.instance.api
    val conn by PaddockApp.instance.conn.state.collectAsState()
    val (load, retry) = rememberLoad(conn.baseUrl) { api.get<HomeFeed>("/api/home") }
    LazyColumn(Modifier.fillMaxSize()) {
        item {
            ChipRow(listOf("all" to "All", "movies" to "Movies", "shows" to "TV", "music" to "Music", "photos" to "Photos",
                "videos" to "Home videos", "red" to "Footage", "engineering" to "Engineering", "favorites" to "Favorites"), "all") {
                when (it) { "all" -> {}; "photos" -> nav.go("photos"); else -> nav.go("browse/$it") }
            }
        }
        item {
            LoadView(load, retry) { feed ->
                Column {
                    if (feed.rows.isEmpty()) EmptyState(Icons.Filled.FolderOpen, "Nothing here yet", "An admin can add libraries in Manage server on the web.")
                    for (row in feed.rows) HomeShelf(row.key, row.title, row.type, row.items, nav)
                }
            }
        }
    }
}

@Composable
private fun HomeShelf(key: String, title: String, type: String, raw: List<kotlinx.serialization.json.JsonElement>, nav: Nav) {
    val more = when (key) { "new_movies" -> "browse/movies"; "shows" -> "browse/shows"; "albums" -> "browse/music"; "shoots" -> "browse/red"
        "home_videos" -> "browse/videos"; "engineering" -> "browse/engineering"; "photos" -> "photos"; "projects" -> "projects"; else -> null }
    when (type) {
        "groups" -> {
            val groups = raw.mapNotNull { runCatching { PaddockJson.decodeFromJsonElement<Group>(it) }.getOrNull() }
            Shelf(title, more?.let { { nav.go(it) } }) {
                items(groups) { g -> GroupCard(g, square = key == "albums", modifier = Modifier.width(if (key == "albums") 150.dp else 260.dp)) { nav.go("group/${java.net.URLEncoder.encode(g.group_key, "UTF-8")}") } }
            }
        }
        "projects" -> {
            val ps = raw.mapNotNull { runCatching { PaddockJson.decodeFromJsonElement<ProjectSummary>(it) }.getOrNull() }
            Shelf(title, { nav.go("projects") }) {
                items(ps) { p -> ProjectCard(p, Modifier.width(260.dp)) { nav.go("project/${p.id}") } }
            }
        }
        else -> {
            val items = raw.mapNotNull { runCatching { PaddockJson.decodeFromJsonElement<Item>(it) }.getOrNull() }
            Shelf(title, more?.let { { nav.go(it) } }) {
                items(items) { it -> VideoCard(it, Modifier.width(if (key == "photos" || key == "memories") 180.dp else 280.dp), compact = true) { nav.open(it) } }
            }
        }
    }
}

@Composable
fun ProjectCard(p: ProjectSummary, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val c = LocalPaddock.current
    Column(modifier.clickable { onClick() }) {
        Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(12.dp))) {
            Thumb(p.cover_id, Modifier.fillMaxSize(), fallback = Icons.Filled.Work)
            Text(p.status, color = Color.White, style = MaterialTheme.typography.labelMedium, modifier = Modifier.padding(8.dp)
                .background(Color(0xCC000000), RoundedCornerShape(4.dp)).padding(horizontal = 6.dp, vertical = 1.dp))
        }
        Text(p.name, style = MaterialTheme.typography.titleMedium, color = c.text, modifier = Modifier.padding(top = 8.dp))
        Text("${p.item_count} items · updated ${timeAgo(p.updated_at)}", style = MaterialTheme.typography.bodySmall, color = c.text2)
    }
}
