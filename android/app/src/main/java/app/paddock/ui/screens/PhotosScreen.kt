package app.paddock.ui.screens

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.paddock.PaddockApp
import app.paddock.data.Item
import app.paddock.ui.*
import app.paddock.ui.theme.LocalPaddock
import java.text.DateFormat
import java.util.Date

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun PhotosScreen(nav: Nav) {
    val api = PaddockApp.instance.api
    val p = LocalPaddock.current
    val photos = remember { mutableStateListOf<Item>() }
    var offset by remember { mutableIntStateOf(0) }
    var done by remember { mutableStateOf(false) }
    var loading by remember { mutableStateOf(false) }
    var viewer by remember { mutableStateOf<Int?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    suspend fun more() {
        if (loading || done) return
        loading = true
        try {
            val page = api.get<List<Item>>("/api/items?kind=photo,video&sort=newest&limit=200&offset=$offset")
            photos += page.filter { it.kind == "photo" || it.library_kind in setOf("photos", "home_videos", "mixed") }
            offset += page.size
            done = page.size < 200
        } catch (e: Exception) { error = e.message }
        loading = false
    }
    LaunchedEffect(Unit) { more() }

    Box(Modifier.fillMaxSize()) {
        Column {
            Row(Modifier.fillMaxWidth().height(56.dp).padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("Photos", style = MaterialTheme.typography.titleLarge, color = p.text, modifier = Modifier.weight(1f))
                IconButton(onClick = { nav.go("you") }) { Icon(Icons.Filled.CloudUpload, "Backup settings", tint = p.text) }
            }
            if (error != null && photos.isEmpty()) EmptyState(Icons.Filled.CloudOff, "Couldn't load photos", error)
            else if (done && photos.isEmpty()) EmptyState(Icons.Filled.Photo, "No photos yet", "Turn on backup in You → Photo backup.", "Set up backup") { nav.go("you") }
            // group by day
            val days = photos.withIndex().groupBy { it.value.date?.take(10) ?: "Undated" }
            LazyVerticalGrid(GridCells.Adaptive(110.dp), horizontalArrangement = Arrangement.spacedBy(2.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                for ((day, list) in days) {
                    item(span = { GridItemSpan(maxLineSpan) }, key = "h$day") {
                        val ts = list.first().value.taken_at
                        Text(ts?.let { DateFormat.getDateInstance(DateFormat.FULL).format(Date((it * 1000).toLong())) } ?: day,
                            style = MaterialTheme.typography.titleMedium, color = p.text, modifier = Modifier.padding(start = 12.dp, top = 16.dp, bottom = 8.dp))
                    }
                    for ((index, ph) in list) {
                        item(key = ph.id) {
                            Box(Modifier.aspectRatio(1f).clickable { viewer = index }) {
                                Thumb(if (ph.has_thumb) ph.id else null, Modifier.fillMaxSize())
                                if (ph.kind == "video") Text("▶ ${fmtDuration(ph.duration)}", color = Color.White, fontSize = 11.sp,
                                    modifier = Modifier.align(Alignment.BottomEnd).padding(4.dp).background(Color(0xAA000000)).padding(horizontal = 4.dp))
                            }
                        }
                    }
                }
                item(span = { GridItemSpan(maxLineSpan) }) { LaunchedEffect(photos.size) { if (!done) more() } }
            }
        }
        AnimatedVisibility(viewer != null, enter = fadeIn(), exit = fadeOut()) {
            viewer?.let { start -> PhotoViewer(photos, start, onClose = { viewer = null }, onPlay = { nav.open(it) }) }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun PhotoViewer(items: List<Item>, start: Int, onClose: () -> Unit, onPlay: (Item) -> Unit) {
    val pager = rememberPagerState(initialPage = start) { items.size }
    var chrome by remember { mutableStateOf(true) }
    androidx.activity.compose.BackHandler { onClose() }
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        HorizontalPager(pager, beyondViewportPageCount = 1) { page ->
            val it = items[page]
            var scale by remember { mutableFloatStateOf(1f) }
            var offsetX by remember { mutableFloatStateOf(0f) }
            var offsetY by remember { mutableFloatStateOf(0f) }
            Box(Modifier.fillMaxSize()
                .pointerInput(page) {
                    detectTransformGestures { _, pan, zoom, _ ->
                        scale = (scale * zoom).coerceIn(1f, 6f)
                        if (scale > 1f) { offsetX += pan.x; offsetY += pan.y } else { offsetX = 0f; offsetY = 0f }
                    }
                }
                .clickable { chrome = !chrome }, contentAlignment = Alignment.Center) {
                Thumb(it.id, Modifier.fillMaxSize().graphicsLayer { scaleX = scale; scaleY = scale; translationX = offsetX; translationY = offsetY },
                    contain = true, preview = it.kind == "photo")
                if (it.kind == "video") FilledIconButton(onClick = { onPlay(it) }, modifier = Modifier.size(72.dp)) { Icon(Icons.Filled.PlayArrow, "Play", Modifier.size(40.dp)) }
            }
        }
        AnimatedVisibility(chrome, enter = fadeIn(), exit = fadeOut()) {
            val cur = items.getOrNull(pager.currentPage)
            Row(Modifier.fillMaxWidth().background(Color(0x88000000)).statusBarsPadding().padding(4.dp), verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = onClose) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Close", tint = Color.White) }
                Column(Modifier.weight(1f)) {
                    Text(cur?.title ?: "", color = Color.White, maxLines = 1)
                    Text(listOfNotNull(fmtDate(cur?.taken_at).ifBlank { null }, if (cur?.width != null && cur.height != null) "${cur.width}×${cur.height}" else null).joinToString(" · "), color = Color(0xFFBBBBBB), fontSize = 12.sp)
                }
                Text("${pager.currentPage + 1} / ${items.size}", color = Color(0xFFBBBBBB), modifier = Modifier.padding(end = 12.dp))
            }
        }
    }
}
