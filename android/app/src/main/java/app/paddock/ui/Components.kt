package app.paddock.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.fadeIn
import androidx.compose.animation.slideInVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.paddock.PaddockApp
import app.paddock.data.Group
import app.paddock.data.Item
import app.paddock.ui.theme.LocalPaddock
import app.paddock.ui.theme.Motion
import coil.compose.AsyncImage
import coil.request.ImageRequest
import java.text.DateFormat
import java.util.Date

// ---------- formatting ----------

/** A metadata value as display text, or null — never the literal "null" for missing/JSON-null values. */
fun kotlinx.serialization.json.JsonObject?.str(key: String): String? {
    val v = this?.get(key) as? kotlinx.serialization.json.JsonPrimitive ?: return null
    if (v is kotlinx.serialization.json.JsonNull) return null
    return v.content.takeIf { it.isNotBlank() && it != "null" }
}

fun fmtDuration(s: Double?): String {
    if (s == null || s.isNaN()) return ""
    val t = s.toLong()
    val h = t / 3600; val m = (t % 3600) / 60; val sec = t % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, sec) else "%d:%02d".format(m, sec)
}

fun fmtBytes(b: Long?): String {
    if (b == null) return ""
    var v = b.toDouble(); var i = 0
    val u = listOf("B", "KB", "MB", "GB", "TB")
    while (v >= 1000 && i < u.size - 1) { v /= 1000; i++ }
    return if (v < 10 && i > 0) "%.1f %s".format(v, u[i]) else "%d %s".format(v.toLong(), u[i])
}

fun fmtDate(ts: Double?): String = ts?.let { DateFormat.getDateInstance(DateFormat.MEDIUM).format(Date((it * 1000).toLong())) } ?: ""

fun timeAgo(ts: Double?): String {
    if (ts == null) return ""
    val s = System.currentTimeMillis() / 1000.0 - ts
    return when {
        s < 60 -> "just now"
        s < 3600 -> "${(s / 60).toInt()} min ago"
        s < 86400 -> "${(s / 3600).toInt()} h ago"
        s < 86400 * 30 -> "${(s / 86400).toInt()} days ago"
        s < 86400 * 365 -> "${(s / 86400 / 30).toInt()} months ago"
        else -> "${(s / 86400 / 365).toInt()} years ago"
    }
}

fun kindIcon(kind: String): ImageVector = when (kind) {
    "video" -> Icons.Filled.Videocam; "audio" -> Icons.Filled.MusicNote; "photo" -> Icons.Filled.Photo
    "red_clip", "braw_clip" -> Icons.Filled.Camera; "model3d" -> Icons.Filled.ViewInAr; "gcode" -> Icons.Filled.Print
    "cad" -> Icons.Filled.Architecture; "pcb" -> Icons.Filled.Memory; else -> Icons.Filled.Description
}

fun subtitle(it: Item): String = when (it.kind) {
    "audio" -> listOfNotNull(it.artist, it.album).joinToString(" · ")
    "red_clip", "braw_clip" -> listOfNotNull(it.camera_model ?: it.camera?.let { c -> "Cam $c" + (it.reel?.let { r -> " · Reel $r" } ?: "") },
        it.fps?.let { f -> "$f fps" }, fmtDate(it.taken_at).ifBlank { null }).joinToString(" · ")
    "model3d", "gcode", "cad", "pcb" -> listOfNotNull(it.format, it.revision?.let { r -> "rev $r" }, it.print_time, timeAgo(it.added_at).ifBlank { null }).joinToString(" · ")
    else -> when {
        it.show != null -> "${it.show} · S${it.season ?: 0}E${it.episode ?: 0}"
        it.library_kind == "movies" -> it.year?.toString() ?: fmtDate(it.taken_at)
        else -> listOfNotNull(it.library, fmtDate(it.taken_at).ifBlank { null }).joinToString(" · ")
    }
}

// ---------- images ----------

@Composable
fun Thumb(id: Int?, modifier: Modifier = Modifier, contain: Boolean = false, fallback: ImageVector = Icons.Filled.Image, preview: Boolean = false) {
    val p = LocalPaddock.current
    Box(modifier.background(p.surface), contentAlignment = Alignment.Center) {
        if (id != null) {
            val api = PaddockApp.instance.api
            val url = if (preview) api.preview(id) else api.thumb(id)
            val key = if (preview) "preview-$id" else "thumb-$id"
            AsyncImage(
                model = ImageRequest.Builder(LocalContext.current).data(url).diskCacheKey(key).memoryCacheKey(key).crossfade(true).build(),
                contentDescription = null,
                contentScale = if (contain) ContentScale.Fit else ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            Icon(fallback, null, tint = p.text3, modifier = Modifier.size(40.dp))
        }
    }
}

// ---------- cards ----------

@OptIn(androidx.compose.foundation.ExperimentalFoundationApi::class)
@Composable
fun VideoCard(item: Item, modifier: Modifier = Modifier, compact: Boolean = false, onClick: () -> Unit) {
    val p = LocalPaddock.current
    var pressed by remember { mutableStateOf(false) }
    val scale by animateFloatAsState(if (pressed) 0.98f else 1f, Motion.fast(), label = "press")
    val actionable = item.playable
    Column(modifier.scale(scale).combinedClickable(onClick = onClick,
        onLongClick = if (actionable) ({ app.paddock.ui.screens.NowPlaying.actionsFor.value = item }) else null)) {
        Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(if (compact) 10.dp else 12.dp))) {
            Thumb(if (item.has_thumb) item.id else null, Modifier.fillMaxSize(), contain = item.kind in setOf("model3d", "gcode", "cad", "pcb"),
                fallback = kindIcon(item.kind))
            if (item.duration != null && item.playable) {
                Text(fmtDuration(item.duration), color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Medium,
                    modifier = Modifier.align(Alignment.BottomEnd).padding(8.dp).background(Color(0xCC000000), RoundedCornerShape(4.dp)).padding(horizontal = 4.dp, vertical = 1.dp))
            }
            val pos = item.progress?.position ?: 0.0
            if (pos > 0 && (item.duration ?: 0.0) > 0) {
                Box(Modifier.align(Alignment.BottomStart).fillMaxWidth().height(4.dp).background(Color(0x55FFFFFF))) {
                    Box(Modifier.fillMaxWidth((pos / item.duration!!).toFloat().coerceIn(0f, 1f)).height(4.dp).background(p.accent))
                }
            }
            if (item.favorite) Icon(Icons.Filled.Favorite, null, tint = Color.White, modifier = Modifier.padding(8.dp).size(18.dp))
        }
        Row(Modifier.padding(top = 10.dp, bottom = 4.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            if (!compact) {
                Box(Modifier.size(36.dp).clip(CircleShape).background(p.surface2), contentAlignment = Alignment.Center) {
                    Icon(kindIcon(item.kind), null, tint = p.text2, modifier = Modifier.size(20.dp))
                }
            }
            Column(Modifier.weight(1f)) {
                Text(item.title, style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Medium,
                    fontSize = if (compact) 14.sp else 16.sp), maxLines = 2, overflow = TextOverflow.Ellipsis, color = p.text)
                Text(subtitle(item), style = MaterialTheme.typography.bodySmall, color = p.text2, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            if (actionable) Icon(Icons.Filled.MoreVert, "More", tint = p.text2, modifier = Modifier.size(20.dp)
                .clickable { app.paddock.ui.screens.NowPlaying.actionsFor.value = item })
        }
    }
}

@Composable
fun GroupCard(g: Group, square: Boolean = false, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val p = LocalPaddock.current
    val kind = g.group_key.substringBefore(":")
    Column(modifier.clickable { onClick() }) {
        Box(Modifier.fillMaxWidth().aspectRatio(if (square) 1f else 16f / 9f).clip(RoundedCornerShape(12.dp))) {
            Thumb(g.cover_id, Modifier.fillMaxSize(), fallback = when (kind) {
                "show" -> Icons.Filled.Tv; "album" -> Icons.Filled.Album; "shoot" -> Icons.Filled.Camera; "eng" -> Icons.Filled.ViewInAr
                else -> Icons.Filled.Folder })
            Text("${g.count}", color = Color.White, fontSize = 12.sp, modifier = Modifier.align(Alignment.BottomEnd).padding(8.dp)
                .background(Color(0xCC000000), RoundedCornerShape(4.dp)).padding(horizontal = 6.dp, vertical = 1.dp))
        }
        Text(g.name, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 8.dp), color = p.text)
        Text(when (kind) {
            "shoot" -> "${g.count} clips · ${fmtDuration(g.duration)}"
            "album" -> "${g.count} tracks"
            "show" -> "${g.count} episodes"
            else -> timeAgo(g.added ?: g.last)
        }, style = MaterialTheme.typography.bodySmall, color = p.text2)
    }
}

@Composable
fun Shelf(title: String, onMore: (() -> Unit)? = null, content: androidx.compose.foundation.lazy.LazyListScope.() -> Unit) {
    val p = LocalPaddock.current
    Column(Modifier.padding(bottom = 28.dp)) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(title, style = MaterialTheme.typography.titleLarge, color = p.text, modifier = Modifier.weight(1f))
            if (onMore != null) TextButton(onClick = onMore) { Text("View all", color = p.text) }
        }
        LazyRow(contentPadding = PaddingValues(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(12.dp), content = content)
    }
}

@Composable
fun ChipRow(options: List<Pair<String, String>>, selected: String, onSelect: (String) -> Unit) {
    val p = LocalPaddock.current
    LazyRow(contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        items(options) { (value, label) ->
            FilterChip(
                selected = value == selected, onClick = { onSelect(value) }, label = { Text(label) },
                shape = RoundedCornerShape(8.dp), border = null,
                colors = FilterChipDefaults.filterChipColors(containerColor = p.chip, labelColor = p.text,
                    selectedContainerColor = p.chipActive, selectedLabelColor = p.chipActiveText),
            )
        }
    }
}

// ---------- states ----------

sealed interface Load<out T> {
    data object Loading : Load<Nothing>
    data class Ok<T>(val data: T) : Load<T>
    data class Err(val message: String) : Load<Nothing>
}

/** Load data on first composition (and whenever [keys] change), with a retry hook. */
@Composable
fun <T> rememberLoad(vararg keys: Any?, block: suspend () -> T): Pair<Load<T>, () -> Unit> {
    var state by remember(*keys) { mutableStateOf<Load<T>>(Load.Loading) }
    var nonce by remember { mutableIntStateOf(0) }
    LaunchedEffect(*keys, nonce) {
        state = Load.Loading
        state = try { Load.Ok(block()) } catch (e: Exception) { Load.Err(e.message ?: "Something went wrong") }
    }
    return state to { nonce++ }
}

@Composable
fun <T> LoadView(load: Load<T>, retry: () -> Unit, content: @Composable (T) -> Unit) {
    when (load) {
        is Load.Loading -> Box(Modifier.fillMaxSize().padding(48.dp), contentAlignment = Alignment.Center) {
            CircularProgressIndicator(color = LocalPaddock.current.text2, strokeWidth = 3.dp, modifier = Modifier.size(32.dp))
        }
        is Load.Err -> EmptyState(Icons.Filled.CloudOff, "Couldn't load this", load.message, "Try again", retry)
        is Load.Ok -> {
            var shown by remember { mutableStateOf(false) }
            LaunchedEffect(Unit) { shown = true }
            AnimatedVisibility(shown, enter = fadeIn(Motion.enter()) + slideInVertically(Motion.enter()) { it / 40 }) { content(load.data) }
        }
    }
}

@Composable
fun EmptyState(icon: ImageVector, title: String, text: String? = null, action: String? = null, onAction: (() -> Unit)? = null) {
    val p = LocalPaddock.current
    Column(Modifier.fillMaxWidth().padding(horizontal = 24.dp, vertical = 56.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Icon(icon, null, tint = p.text3, modifier = Modifier.size(64.dp))
        Spacer(Modifier.height(16.dp))
        Text(title, style = MaterialTheme.typography.titleLarge, color = p.text)
        if (text != null) Text(text, style = MaterialTheme.typography.bodyMedium, color = p.text2, modifier = Modifier.padding(top = 6.dp),
            textAlign = androidx.compose.ui.text.style.TextAlign.Center)
        if (action != null && onAction != null) {
            Spacer(Modifier.height(16.dp))
            Button(onClick = onAction, colors = ButtonDefaults.buttonColors(containerColor = p.accent)) { Text(action) }
        }
    }
}

@Composable
fun PillButton(text: String, icon: ImageVector? = null, primary: Boolean = false, onClick: () -> Unit) {
    val p = LocalPaddock.current
    Button(onClick = onClick, shape = RoundedCornerShape(50), contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
        colors = ButtonDefaults.buttonColors(containerColor = if (primary) p.text else p.surface2, contentColor = if (primary) p.bg else p.text)) {
        if (icon != null) { Icon(icon, null, Modifier.size(18.dp)); Spacer(Modifier.width(6.dp)) }
        Text(text, fontWeight = FontWeight.Medium)
    }
}
