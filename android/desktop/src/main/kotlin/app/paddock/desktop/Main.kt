package app.paddock.desktop

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.type
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.WindowPlacement
import androidx.compose.ui.window.application
import androidx.compose.ui.window.rememberWindowState
import app.paddock.data.Library
import app.paddock.ui.theme.LocalPaddock
import app.paddock.ui.theme.Motion
import app.paddock.ui.theme.PaddockTheme

const val APP_VERSION = "0.3.0"

sealed interface Screen {
    data object Home : Screen
    data object Videos : Screen
    data object Photos : Screen
    data object Music : Screen
    data object Parties : Screen
    data object Settings : Screen
    data class Library(val id: Int, val name: String) : Screen
    data class Group(val key: String, val name: String, val music: Boolean) : Screen
    data class Search(val query: String) : Screen
    data class Watch(val id: Int, val party: String? = null) : Screen
}

/** Back stack and window state shared by every screen. */
object Nav {
    val stack = mutableStateListOf<Screen>(Screen.Home)
    val current: Screen get() = stack.last()
    val fullscreen = mutableStateOf(false)
    fun go(s: Screen) { if (current != s) stack.add(s) }
    fun root(s: Screen) { stack.clear(); stack.add(s) }
    fun back() { if (stack.size > 1) stack.removeAt(stack.lastIndex) }
}

fun main() = application {
    val state = rememberWindowState(size = DpSize(1360.dp, 860.dp))
    val store by Store.state.collectAsState()
    LaunchedEffect(store.settings.activeId, store.servers.size) { Connection.use(Store.active) }
    Window(onCloseRequest = { Voice.leave(); exitApplication(); Runtime.getRuntime().halt(0) }, state = state, title = "Paddock",
        icon = painterResource("paddock.png"), onPreviewKeyEvent = { e ->
            if (e.type == KeyEventType.KeyDown && e.key == Key.Escape && Nav.fullscreen.value) { Nav.fullscreen.value = false; true }
            else if (e.type == KeyEventType.KeyDown && e.key == Key.F11) { Nav.fullscreen.value = !Nav.fullscreen.value; true } else false
        }) {
        val full by Nav.fullscreen
        LaunchedEffect(full) { state.placement = if (full) WindowPlacement.Fullscreen else WindowPlacement.Floating }
        PaddockTheme(store.settings.theme) {
            val p = LocalPaddock.current
            Surface(color = p.bg, modifier = Modifier.fillMaxSize()) {
                if (store.servers.isEmpty()) PairScreen() else Shell()
            }
        }
    }
}

@Composable
private fun Shell() {
    val p = LocalPaddock.current
    val full by Nav.fullscreen
    val screen = Nav.current
    Box(Modifier.fillMaxSize()) {
    Row(Modifier.fillMaxSize()) {
        AnimatedVisibility(!full) { Sidebar(screen) }
        Column(Modifier.fillMaxSize()) {
            AnimatedVisibility(!full) { TopBar() }
            ConnectionBanner()
            NoticeBar()
            Box(Modifier.weight(1f).fillMaxWidth()) {
                AnimatedContent(screen, transitionSpec = { fadeIn(Motion.enter()) togetherWith fadeOut(Motion.fast()) }, label = "screen") { s ->
                    when (s) {
                        Screen.Home -> HomeScreen()
                        Screen.Videos -> VideosScreen()
                        Screen.Photos -> PhotosScreen()
                        Screen.Music -> MusicScreen()
                        Screen.Parties -> TogetherScreen()
                        Screen.Settings -> SettingsScreen()
                        is Screen.Library -> LibraryScreen(s.id, s.name)
                        is Screen.Group -> GroupScreen(s.key, s.name, s.music)
                        is Screen.Search -> SearchScreen(s.query)
                        is Screen.Watch -> WatchScreen(s.id, s.party)
                    }
                }
            }
            if (!full) MusicBar()
        }
    }
    PhotoViewerOverlay()
    IncomingCallDialog()
    }
}

@Composable
private fun TopBar() {
    val p = LocalPaddock.current
    var q by remember { mutableStateOf("") }
    Row(Modifier.fillMaxWidth().height(60.dp).padding(horizontal = 20.dp), verticalAlignment = Alignment.CenterVertically) {
        if (Nav.stack.size > 1) IconButton(onClick = { Nav.back() }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back", tint = p.text) }
        Spacer(Modifier.weight(1f))
        OutlinedTextField(q, { q = it }, placeholder = { Text("Search movies, shows, photos, music…") }, singleLine = true,
            leadingIcon = { Icon(Icons.Outlined.Search, null) }, shape = RoundedCornerShape(50),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
            keyboardActions = KeyboardActions(onSearch = { if (q.isNotBlank()) Nav.go(Screen.Search(q.trim())) }),
            modifier = Modifier.widthIn(max = 560.dp).fillMaxWidth().height(52.dp))
        Spacer(Modifier.weight(1f))
        val conn by Connection.state.collectAsState()
        val (dot, label) = when (conn.mode) {
            Mode.Home -> p.ok to "At home"; Mode.Tunnel -> p.link to "Through your tunnel"
            Mode.Connecting -> p.text3 to "Connecting…"; Mode.Offline -> p.danger to "Offline"
        }
        Box(Modifier.size(8.dp).clip(CircleShape).background(dot))
        Spacer(Modifier.width(8.dp))
        Text(label, color = p.text2, fontSize = 13.sp)
        Spacer(Modifier.width(16.dp))
        Avatar(Store.active?.user?.let { it.display_name ?: it.username } ?: "?")
    }
}

@Composable
private fun ConnectionBanner() {
    val p = LocalPaddock.current
    val conn by Connection.state.collectAsState()
    AnimatedVisibility(conn.mode == Mode.Offline) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 4.dp).clip(RoundedCornerShape(10.dp)).background(p.surface)
            .clickable { Connection.refresh() }.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Filled.CloudOff, null, tint = p.warn)
            Spacer(Modifier.width(10.dp))
            Text(conn.detail, color = p.text, modifier = Modifier.weight(1f))
            Text("Retry", color = p.link)
        }
    }
}

@Composable
private fun Sidebar(screen: Screen) {
    val p = LocalPaddock.current
    val libs by produceState<List<Library>>(emptyList(), Connection.state.collectAsState().value.baseUrl) {
        value = runCatching { Connection.api.get<List<Library>>("/api/libraries") }.getOrDefault(value)
    }
    Column(Modifier.width(232.dp).fillMaxHeight().background(p.bg).padding(horizontal = 12.dp, vertical = 14.dp)) {
        Row(Modifier.padding(start = 8.dp, bottom = 18.dp), verticalAlignment = Alignment.CenterVertically) {
            Image(painterResource("paddock.png"), null, Modifier.size(30.dp))
            Spacer(Modifier.width(10.dp))
            Text(Store.active?.name ?: "Paddock", color = p.text, fontWeight = FontWeight.Bold, fontSize = 19.sp, maxLines = 1)
        }
        NavRow(Icons.Filled.Home, Icons.Outlined.Home, "Home", screen == Screen.Home) { Nav.root(Screen.Home) }
        NavRow(Icons.Filled.Movie, Icons.Outlined.Movie, "Videos", screen == Screen.Videos) { Nav.root(Screen.Videos) }
        NavRow(Icons.Filled.Photo, Icons.Outlined.Photo, "Photos", screen == Screen.Photos) { Nav.root(Screen.Photos) }
        NavRow(Icons.Filled.MusicNote, Icons.Outlined.MusicNote, "Music", screen == Screen.Music) { Nav.root(Screen.Music) }
        NavRow(Icons.Filled.Groups, Icons.Outlined.Groups, "Together", screen == Screen.Parties) { Nav.root(Screen.Parties) }
        HorizontalDivider(Modifier.padding(vertical = 12.dp), color = p.border)
        Text("Libraries", color = p.text2, fontSize = 13.sp, fontWeight = FontWeight.Medium, modifier = Modifier.padding(start = 12.dp, bottom = 6.dp))
        Column(Modifier.weight(1f)) {
            for (l in libs) NavRow(Icons.Filled.Folder, Icons.Outlined.Folder, l.name, screen == Screen.Library(l.id, l.name)) { Nav.root(Screen.Library(l.id, l.name)) }
        }
        VoiceBar()
        NavRow(Icons.Filled.Settings, Icons.Outlined.Settings, "Settings", screen == Screen.Settings) { Nav.root(Screen.Settings) }
    }
}

@Composable
private fun NavRow(on: ImageVector, off: ImageVector, label: String, selected: Boolean, onClick: () -> Unit) {
    val p = LocalPaddock.current
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(if (selected) p.surface2 else Color.Transparent)
        .clickable(onClick = onClick).padding(horizontal = 12.dp, vertical = 9.dp), verticalAlignment = Alignment.CenterVertically) {
        Icon(if (selected) on else off, null, tint = p.text, modifier = Modifier.size(22.dp))
        Spacer(Modifier.width(16.dp))
        Text(label, color = p.text, fontWeight = if (selected) FontWeight.Medium else FontWeight.Normal, maxLines = 1)
    }
}

/** Voice chat stays on while you move around the app; this is where you see and control it. */
@Composable
private fun VoiceBar() {
    val p = LocalPaddock.current
    val room by Voice.room.collectAsState()
    val members by Voice.members.collectAsState()
    val speaking by Voice.speaking.collectAsState()
    val muted by Voice.muted.collectAsState()
    val deaf by Voice.deafened.collectAsState()
    val level by Voice.level.collectAsState()
    val err by Voice.error.collectAsState()
    if (room == null && err == null) return
    Column(Modifier.fillMaxWidth().padding(bottom = 10.dp).clip(RoundedCornerShape(12.dp)).background(p.surface).padding(12.dp)) {
        if (room != null) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Filled.GraphicEq, null, tint = p.ok, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(6.dp))
                Text(Voice.title.collectAsState().value.ifBlank { "Voice chat" }, color = p.text, fontWeight = FontWeight.Medium, fontSize = 14.sp, maxLines = 1)
            }
            Spacer(Modifier.height(8.dp))
            for (m in members) Row(Modifier.padding(vertical = 2.dp), verticalAlignment = Alignment.CenterVertically) {
                Avatar(m.username, 22, ring = m.id in speaking)
                Spacer(Modifier.width(8.dp))
                Text(m.username + if (m.id == Voice.me) " (you)" else "", color = p.text2, fontSize = 13.sp, modifier = Modifier.weight(1f), maxLines = 1)
                if (m.muted) Icon(Icons.Filled.MicOff, null, tint = p.text3, modifier = Modifier.size(14.dp))
            }
            LinearProgressIndicator(progress = { if (muted) 0f else level }, modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp), color = p.ok, trackColor = p.surface3)
            Row {
                IconButton(onClick = { Voice.setMuted(!muted) }) { Icon(if (muted) Icons.Filled.MicOff else Icons.Filled.Mic, if (muted) "Unmute" else "Mute", tint = if (muted) p.danger else p.text) }
                IconButton(onClick = { Voice.setDeafened(!deaf) }) { Icon(if (deaf) Icons.Filled.HeadsetOff else Icons.Filled.Headset, if (deaf) "Undeafen" else "Deafen", tint = if (deaf) p.danger else p.text) }
                Spacer(Modifier.weight(1f))
                IconButton(onClick = { Voice.leave() }) { Icon(Icons.Filled.CallEnd, "Leave voice", tint = p.danger) }
            }
        }
        if (err != null) Text(err!!, color = p.warn, fontSize = 12.sp)
    }
}
