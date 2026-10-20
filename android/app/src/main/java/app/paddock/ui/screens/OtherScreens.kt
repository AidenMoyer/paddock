package app.paddock.ui.screens

import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import app.paddock.PaddockApp
import app.paddock.backup.Backup
import app.paddock.data.*
import app.paddock.net.Mode
import app.paddock.ui.*
import app.paddock.ui.theme.LocalPaddock
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

// ---------------- item detail (photos, CAD, 3D, G-code, PCB) ----------------

@Composable
fun ItemScreen(id: Int, nav: Nav) {
    val api = PaddockApp.instance.api
    val p = LocalPaddock.current
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val (load, retry) = rememberLoad(id) { api.get<Item>("/api/items/$id") }
    Column(Modifier.fillMaxSize()) {
        TopBar((load as? Load.Ok)?.data?.title ?: "", nav)
        LoadView(load, retry) { item ->
            LazyColumn {
                item {
                    Box(Modifier.fillMaxWidth().aspectRatio(if (item.kind == "photo") 4f / 3f else 16f / 9f).padding(horizontal = 16.dp).clip(RoundedCornerShape(12.dp))) {
                        Thumb(if (item.has_thumb) item.id else null, Modifier.fillMaxSize(), contain = true, preview = item.kind == "photo", fallback = kindIcon(item.kind))
                    }
                    Text(item.title, style = MaterialTheme.typography.titleLarge, color = p.text, modifier = Modifier.padding(16.dp))
                    Row(Modifier.padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        PillButton("Share link", Icons.Filled.Share) {
                            scope.launch {
                                val r = runCatching { api.post<JsonObject>("/api/share", buildJsonObject { put("item_id", id); put("hours", 72) }) }.getOrNull()
                                r?.get("url")?.jsonPrimitive?.content?.let { url ->
                                    ctx.startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, url), "Share"))
                                }
                            }
                        }
                    }
                    val m = item.meta ?: JsonObject(emptyMap())
                    val keys = listOf("format" to "Format", "category" to "Category", "revision" to "Revision", "size_x" to "Width (mm)", "size_y" to "Depth (mm)",
                        "size_z" to "Height (mm)", "volume_cm3" to "Volume (cm³)", "print_time" to "Print time", "filament_g" to "Filament (g)",
                        "filament_type" to "Material", "printer" to "Printer", "slicer" to "Slicer", "originating_system" to "Exported from",
                        "kicad_title" to "Board", "make" to "Camera make", "model" to "Camera", "lens" to "Lens", "iso" to "ISO", "caption" to "AI caption")
                    Column(Modifier.padding(16.dp).fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface).padding(12.dp)) {
                        for ((k, label) in keys) m.str(k)?.let { v ->
                            Row(Modifier.padding(vertical = 3.dp)) {
                                Text(label, color = p.text2, modifier = Modifier.width(120.dp), style = MaterialTheme.typography.bodySmall)
                                Text(v, color = p.text, style = MaterialTheme.typography.bodySmall)
                            }
                        }
                        Row(Modifier.padding(vertical = 3.dp)) {
                            Text("File", color = p.text2, modifier = Modifier.width(120.dp), style = MaterialTheme.typography.bodySmall)
                            Text(listOfNotNull(item.path, fmtBytes(item.size).ifBlank { null }).joinToString(" · "), color = p.text, style = MaterialTheme.typography.bodySmall)
                        }
                    }
                    if (!item.related.isNullOrEmpty()) Text(if (item.group?.startsWith("eng:") == true) "Revisions & exports" else "Related",
                        style = MaterialTheme.typography.titleMedium, color = p.text, modifier = Modifier.padding(16.dp))
                }
                items(item.related.orEmpty().filter { it.id != id }) { r -> Box(Modifier.padding(horizontal = 16.dp, vertical = 6.dp)) { VideoCard(r, compact = true) { nav.open(r) } } }
            }
        }
    }
}

// ---------------- projects ----------------

@Composable
fun ProjectsScreen(nav: Nav) {
    val api = PaddockApp.instance.api
    val p = LocalPaddock.current
    val (load, retry) = rememberLoad(Unit) { api.get<List<ProjectSummary>>("/api/projects") }
    Column(Modifier.fillMaxSize()) {
        Text("Projects", style = MaterialTheme.typography.titleLarge, color = p.text, modifier = Modifier.padding(16.dp))
        LoadView(load, retry) { list ->
            if (list.isEmpty()) EmptyState(Icons.Filled.Work, "No projects yet", "Create projects on the web or ask the assistant — they gather footage, photos, CAD and a video plan for each build.")
            LazyVerticalGrid(GridCells.Adaptive(280.dp), contentPadding = PaddingValues(16.dp), horizontalArrangement = Arrangement.spacedBy(12.dp),
                verticalArrangement = Arrangement.spacedBy(20.dp)) {
                items(list) { pr -> ProjectCard(pr) { nav.go("project/${pr.id}") } }
            }
        }
    }
}

@Composable
fun ProjectScreen(id: Int, nav: Nav) {
    val api = PaddockApp.instance.api
    val p = LocalPaddock.current
    val (load, retry) = rememberLoad(id) { api.get<ProjectDetail>("/api/projects/$id") }
    var tab by remember { mutableStateOf("media") }
    Column(Modifier.fillMaxSize()) {
        TopBar((load as? Load.Ok)?.data?.name ?: "Project", nav)
        LoadView(load, retry) { pr ->
            Column {
                if (pr.goal.isNotBlank()) Text("Goal: ${pr.goal}", color = p.text2, modifier = Modifier.padding(horizontal = 16.dp))
                ChipRow(listOf("media" to "Media (${pr.items.size})", "plan" to "Video plan", "notes" to "Notes (${pr.notes.size})", "ask" to "Ask AI"), tab) { tab = it }
                when (tab) {
                    "media" -> LazyVerticalGrid(GridCells.Adaptive(220.dp), contentPadding = PaddingValues(16.dp), horizontalArrangement = Arrangement.spacedBy(10.dp),
                        verticalArrangement = Arrangement.spacedBy(16.dp)) { items(pr.items) { it -> VideoCard(it, compact = true) { nav.open(it) } } }
                    "plan" -> LazyColumn(contentPadding = PaddingValues(16.dp)) {
                        item {
                            if (pr.plan.isNullOrBlank()) EmptyState(Icons.Filled.AutoAwesome, "No plan yet", "Generate one from the web app's project page.")
                            else MarkdownText(pr.plan)
                        }
                    }
                    "notes" -> LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        items(pr.notes) { n ->
                            Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface).padding(12.dp)) {
                                Text("${n.author ?: ""} · ${timeAgo(n.created_at)}", color = p.text3, style = MaterialTheme.typography.labelMedium)
                                Text(n.body, color = p.text)
                            }
                        }
                    }
                    else -> AssistantScreen(projectId = id, embedded = true)
                }
            }
        }
    }
}

/** Minimal Markdown: headings, bullets, checkboxes, bold; #123 references stay readable. */
@Composable
fun MarkdownText(md: String) {
    val p = LocalPaddock.current
    Column(Modifier.animateContentSize()) {
        for (line in md.lines()) {
            val t = line.trimEnd()
            when {
                t.startsWith("### ") -> Text(t.drop(4), style = MaterialTheme.typography.titleMedium, color = p.text, modifier = Modifier.padding(top = 10.dp, bottom = 4.dp))
                t.startsWith("## ") -> Text(t.drop(3), style = MaterialTheme.typography.titleLarge.copy(fontSize = MaterialTheme.typography.titleMedium.fontSize * 1.1f), color = p.text, modifier = Modifier.padding(top = 14.dp, bottom = 4.dp))
                t.startsWith("# ") -> Text(t.drop(2), style = MaterialTheme.typography.titleLarge, color = p.text, fontWeight = FontWeight.Bold, modifier = Modifier.padding(bottom = 6.dp))
                t.matches(Regex("^\\s*[-*] \\[[ xX]\\] .*")) -> Text((if (t.contains("[x]", true)) "☑ " else "☐ ") + t.substringAfter("] "), color = p.text)
                t.matches(Regex("^\\s*([-*+]|\\d+[.)]) .*")) -> Text("•  " + t.replace(Regex("^\\s*([-*+]|\\d+[.)]) "), ""), color = p.text, modifier = Modifier.padding(start = 4.dp, bottom = 2.dp))
                t.startsWith("|") -> Text(t, color = p.text2, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
                t.isBlank() -> Spacer(Modifier.height(6.dp))
                else -> Text(t.replace("**", ""), color = p.text)
            }
        }
    }
}

// ---------------- assistant ----------------

private data class Msg(val mine: Boolean, val text: String, val tools: List<String> = emptyList())

@Composable
fun AssistantScreen(projectId: Int? = null, embedded: Boolean = false) {
    val api = PaddockApp.instance.api
    val p = LocalPaddock.current
    val scope = rememberCoroutineScope()
    val msgs = remember { mutableStateListOf<Msg>() }
    var input by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var conv by remember { mutableStateOf<String?>(null) }
    val (info, _) = rememberLoad(Unit) { api.get<AiInfo>("/api/ai/info") }
    val list = rememberLazyListState()
    fun send(text: String) {
        if (text.isBlank() || busy) return
        msgs += Msg(true, text); input = ""; busy = true
        scope.launch {
            try {
                val r = api.post<ChatReply>("/api/ai/chat", buildJsonObject {
                    put("message", text); conv?.let { put("conversation_id", it) }; projectId?.let { put("project_id", it) }
                }, timeoutS = 600)
                conv = r.conversation_id
                msgs += Msg(false, r.reply, r.steps.mapNotNull { it.tool })
            } catch (e: Exception) { msgs += Msg(false, "⚠ ${e.message}") }
            busy = false
            list.animateScrollToItem(maxOf(0, msgs.size - 1))
        }
    }
    Column(Modifier.fillMaxSize().imePadding()) {
        if (!embedded) Text("Assistant", style = MaterialTheme.typography.titleLarge, color = p.text, modifier = Modifier.padding(16.dp))
        val ai = (info as? Load.Ok)?.data
        if (ai != null && !ai.available) {
            EmptyState(Icons.Filled.AutoAwesome, "The assistant isn't set up yet", "An admin can choose a model (e.g. Qwen3.8 on your server) in Manage server → Assistant on the web.")
            return@Column
        }
        if (ai != null) Text("${ai.model ?: ai.provider} · ${if (ai.offsite) "cloud" else "runs on your server"}${if (ai.can_write) " · can make changes" else " · read-only"}",
            color = p.text3, style = MaterialTheme.typography.labelMedium, modifier = Modifier.padding(horizontal = 16.dp))
        LazyColumn(Modifier.weight(1f), state = list, contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            if (msgs.isEmpty()) items(listOf("What did I add this week?", "How much space is left on the server?", "Find my latest RED shoot day",
                "Plan a video about my newest project")) { s ->
                Text(s, color = p.text, modifier = Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface).clickable { send(s) }.padding(14.dp))
            }
            items(msgs) { m ->
                Row(Modifier.fillMaxWidth(), horizontalArrangement = if (m.mine) Arrangement.End else Arrangement.Start) {
                    Column(Modifier.widthIn(max = 520.dp).clip(RoundedCornerShape(18.dp)).background(if (m.mine) p.surface2 else Color.Transparent)
                        .padding(horizontal = if (m.mine) 14.dp else 0.dp, vertical = 8.dp)) {
                        if (m.tools.isNotEmpty()) Text(m.tools.joinToString("  ") { "✓ $it" }, color = p.text3, style = MaterialTheme.typography.labelSmall, fontFamily = FontFamily.Monospace)
                        if (m.mine) Text(m.text, color = p.text) else MarkdownText(m.text)
                    }
                }
            }
            if (busy) item { CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp) }
        }
        Row(Modifier.fillMaxWidth().padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            TextField(input, { input = it }, placeholder = { Text("Ask about your media, footage or server…") }, modifier = Modifier.weight(1f),
                shape = RoundedCornerShape(24.dp), maxLines = 5,
                colors = TextFieldDefaults.colors(focusedContainerColor = p.surface, unfocusedContainerColor = p.surface,
                    focusedIndicatorColor = Color.Transparent, unfocusedIndicatorColor = Color.Transparent))
            Spacer(Modifier.width(8.dp))
            FilledIconButton(onClick = { send(input) }, enabled = !busy, colors = IconButtonDefaults.filledIconButtonColors(containerColor = p.accent)) {
                Icon(Icons.AutoMirrored.Filled.Send, "Send", tint = Color.White)
            }
        }
    }
}

// ---------------- you / settings ----------------

@Composable
fun YouScreen(nav: Nav) {
    val app = PaddockApp.instance
    val p = LocalPaddock.current
    val ctx = LocalContext.current
    val servers by app.servers.servers.collectAsState()
    val activeId by app.servers.activeId.collectAsState()
    val conn by app.conn.state.collectAsState()
    val s = app.settings
    var backup by remember { mutableStateOf(s.backupEnabled) }
    var videos by remember { mutableStateOf(s.backupVideos) }
    var wifi by remember { mutableStateOf(s.backupWifiOnly) }
    var charging by remember { mutableStateOf(s.backupChargingOnly) }
    var remote by remember { mutableStateOf(s.backupRemote) }
    var theme by remember { mutableStateOf(s.theme) }
    val perms = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { r ->
        if (r.values.any { it }) { s.backupEnabled = true; backup = true; Backup.schedule(ctx); Backup.runNow(ctx) }
    }
    LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item {
            val active = servers.firstOrNull { it.id == activeId }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(56.dp).clip(CircleShape).background(p.accent2), contentAlignment = Alignment.Center) {
                    Text((active?.user?.display_name ?: active?.user?.username ?: "?").take(1).uppercase(), color = Color.White, fontWeight = FontWeight.Bold)
                }
                Spacer(Modifier.width(14.dp))
                Column {
                    Text(active?.user?.display_name ?: active?.user?.username ?: "", style = MaterialTheme.typography.titleLarge, color = p.text)
                    Text(active?.name ?: "", color = p.text2)
                }
            }
        }
        item { ConnectionCard(conn.mode, conn.detail) }
        item {
            Section("Photo backup") {
                ToggleRow("Back up photos automatically", backup) { on ->
                    if (on) perms.launch(Backup.permissions()) else { s.backupEnabled = false; backup = false; Backup.schedule(ctx) }
                }
                ToggleRow("Include videos", videos) { videos = it; s.backupVideos = it; Backup.schedule(ctx) }
                ToggleRow("Only on Wi-Fi", wifi) { wifi = it; s.backupWifiOnly = it; Backup.schedule(ctx) }
                ToggleRow("Only while charging", charging) { charging = it; s.backupChargingOnly = it; Backup.schedule(ctx) }
                ToggleRow("Also back up when away (through the tunnel)", remote) { remote = it; s.backupRemote = it }
                if (backup) TextButton(onClick = { Backup.runNow(ctx) }) { Text("Back up now", color = p.link) }
                Text("New photos and videos go to the backup library on your server, keep their location data, and are never uploaded twice.",
                    color = p.text3, style = MaterialTheme.typography.bodySmall)
            }
        }
        item {
            Section("Servers") {
                for (sv in servers) {
                    Row(Modifier.fillMaxWidth().clickable { app.servers.setActive(sv.id) }.padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                        RadioButton(selected = sv.id == activeId, onClick = { app.servers.setActive(sv.id) })
                        Column(Modifier.weight(1f)) {
                            Text(sv.name, color = p.text)
                            Text(sv.lanUrls.firstOrNull() ?: "", color = p.text3, style = MaterialTheme.typography.bodySmall)
                        }
                        TextButton(onClick = { app.servers.remove(sv.id) }) { Text("Forget", color = p.danger) }
                    }
                }
                TextButton(onClick = { nav.go("pair") }) { Icon(Icons.Filled.Add, null); Spacer(Modifier.width(6.dp)); Text("Add another server") }
            }
        }
        item {
            Section("Playback & appearance") {
                Text("Theme", color = p.text2, style = MaterialTheme.typography.labelMedium)
                ChipRow(listOf("system" to "System", "dark" to "Dark", "light" to "Light"), theme) { theme = it; s.theme = it; app.themeChanged() }
                var cache by remember { mutableIntStateOf(s.cacheGb) }
                Text("Offline video cache: $cache GB", color = p.text)
                Slider(value = cache.toFloat(), onValueChange = { cache = it.toInt() }, onValueChangeFinished = { s.cacheGb = cache }, valueRange = 1f..32f, steps = 30)
                var autoplay by remember { mutableStateOf(s.autoplay) }
                ToggleRow("Autoplay next video", autoplay) { autoplay = it; s.autoplay = it }
            }
        }
        item { Text("Paddock for Android 0.1.0 · private, self-hosted", color = p.text3, style = MaterialTheme.typography.bodySmall) }
    }
}

@Composable
fun ConnectionCard(mode: Mode, detail: String) {
    val p = LocalPaddock.current
    val (icon, color, title) = when (mode) {
        Mode.Home -> Triple(Icons.Filled.Home, p.ok, "Connected at home")
        Mode.Tunnel -> Triple(Icons.Filled.Lock, p.link, "Connected through your private tunnel")
        Mode.Connecting -> Triple(Icons.Filled.Sync, p.text2, "Connecting…")
        Mode.NeedsVpnConsent -> Triple(Icons.Filled.VpnKey, p.warn, "Allow the private tunnel")
        Mode.Offline -> Triple(Icons.Filled.CloudOff, p.danger, "Can't reach your server")
    }
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface).padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
        Icon(icon, null, tint = color)
        Spacer(Modifier.width(12.dp))
        Column { Text(title, color = p.text, fontWeight = FontWeight.Medium); if (detail.isNotBlank()) Text(detail, color = p.text2, style = MaterialTheme.typography.bodySmall) }
    }
}

@Composable
private fun Section(title: String, content: @Composable ColumnScope.() -> Unit) {
    val p = LocalPaddock.current
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface).padding(16.dp)) {
        Text(title, style = MaterialTheme.typography.titleMedium, color = p.text, modifier = Modifier.padding(bottom = 8.dp))
        content()
    }
}

@Composable
private fun ToggleRow(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    val p = LocalPaddock.current
    Row(Modifier.fillMaxWidth().clickable { onChange(!checked) }.padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = p.text, modifier = Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
