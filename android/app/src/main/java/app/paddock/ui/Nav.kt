package app.paddock.ui

import android.app.Activity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import app.paddock.PaddockApp
import app.paddock.R
import app.paddock.backup.ManualUpload
import app.paddock.data.Item
import app.paddock.net.Mode
import app.paddock.ui.screens.*
import app.paddock.ui.theme.LocalPaddock
import app.paddock.ui.theme.Motion
import java.net.URLDecoder

class Nav(val c: NavHostController) {
    fun go(route: String) = c.navigate(route) { launchSingleTop = true }
    fun open(item: Item) = go(if (item.playable) "watch/${item.id}" else "item/${item.id}")
    fun replace(item: Item) { c.popBackStack(); open(item) }
    fun back() { c.popBackStack() }
}

private val ROOTS = setOf("home", "photos", "projects", "you")

@Composable
fun PaddockRoot() {
    val app = PaddockApp.instance
    val servers by app.servers.servers.collectAsState()
    var pairing by remember { mutableStateOf(false) }
    if (servers.isEmpty()) {
        PairScreen(onPaired = {})
        return
    }
    val controller = rememberNavController()
    val nav = remember(controller) { Nav(controller) }
    val entry by controller.currentBackStackEntryAsState()
    val route = entry?.destination?.route?.substringBefore("/")?.substringBefore("?") ?: "home"
    val p = LocalPaddock.current
    val conn by app.conn.state.collectAsState()
    val fullscreen by NowPlaying.fullscreen
    val inPip by NowPlaying.inPip
    val ctx = LocalContext.current
    val consent = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        if (r.resultCode == Activity.RESULT_OK) app.conn.refresh()
    }
    val pick = rememberLauncherForActivityResult(ActivityResultContracts.PickMultipleVisualMedia(50)) { uris ->
        if (uris.isNotEmpty()) ManualUpload.enqueue(ctx, uris)
    }

    Scaffold(
        containerColor = p.bg,
        topBar = {
            AnimatedVisibility(route in ROOTS && !fullscreen && !inPip, enter = fadeIn(Motion.base()), exit = fadeOut(Motion.fast())) {
                Column(Modifier.statusBarsPadding()) {
                    Row(Modifier.fillMaxWidth().height(56.dp).padding(start = 12.dp, end = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                        Image(painterResource(R.drawable.ic_launcher), null, Modifier.size(28.dp))
                        Spacer(Modifier.width(8.dp))
                        Text(app.servers.active?.name ?: "Paddock", fontWeight = FontWeight.Bold, fontSize = 19.sp, color = p.text)
                        Spacer(Modifier.width(8.dp))
                        Box(Modifier.size(8.dp).clip(CircleShape).background(when (conn.mode) {
                            Mode.Home -> p.ok; Mode.Tunnel -> p.link; Mode.Connecting -> p.text3; else -> p.danger }))
                        Spacer(Modifier.weight(1f))
                        IconButton(onClick = { nav.go("search") }) { Icon(Icons.Outlined.Search, "Search", tint = p.text) }
                        IconButton(onClick = { nav.go("assistant") }) { Icon(Icons.Outlined.AutoAwesome, "Assistant", tint = p.text) }
                    }
                    AnimatedVisibility(conn.mode == Mode.NeedsVpnConsent || conn.mode == Mode.Offline) {
                        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp).clip(RoundedCornerShape(10.dp)).background(p.surface)
                            .clickable {
                                if (conn.mode == Mode.NeedsVpnConsent) app.tunnel.consentIntent()?.let { consent.launch(it) } else app.conn.refresh()
                            }.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                            Icon(if (conn.mode == Mode.NeedsVpnConsent) Icons.Filled.VpnKey else Icons.Filled.CloudOff, null, tint = p.warn)
                            Spacer(Modifier.width(10.dp))
                            Column(Modifier.weight(1f)) {
                                Text(if (conn.mode == Mode.NeedsVpnConsent) "Tap to allow Paddock's private tunnel" else "Can't reach ${app.servers.active?.name}", color = p.text, fontWeight = FontWeight.Medium)
                                Text(if (conn.mode == Mode.NeedsVpnConsent) "Android asks once. Only Paddock's own traffic uses it." else conn.detail, color = p.text2, style = MaterialTheme.typography.bodySmall)
                            }
                            Text(if (conn.mode == Mode.NeedsVpnConsent) "Allow" else "Retry", color = p.link)
                        }
                    }
                }
            }
        },
        bottomBar = {
            AnimatedVisibility(route in ROOTS && !inPip, enter = slideInVertically(Motion.enter()) { it }, exit = slideOutVertically(Motion.exit()) { it }) {
                NavigationBar(containerColor = p.bg, tonalElevation = 0.dp) {
                    NavItem("home", route, Icons.Filled.Home, Icons.Outlined.Home, "Home") { nav.go("home") }
                    NavItem("photos", route, Icons.Filled.Photo, Icons.Outlined.Photo, "Photos") { nav.go("photos") }
                    NavigationBarItem(selected = false, onClick = { pick.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageAndVideo)) },
                        icon = { Icon(Icons.Outlined.AddCircleOutline, "Upload", Modifier.size(36.dp)) }, label = null,
                        colors = NavigationBarItemDefaults.colors(unselectedIconColor = p.text, indicatorColor = Color.Transparent))
                    NavItem("projects", route, Icons.Filled.Work, Icons.Outlined.WorkOutline, "Projects") { nav.go("projects") }
                    NavItem("you", route, Icons.Filled.AccountCircle, Icons.Outlined.AccountCircle, "You") { nav.go("you") }
                }
            }
        },
    ) { pad ->
        NavHost(controller, startDestination = "home", modifier = Modifier.padding(if (route in ROOTS) pad else PaddingValues(0.dp)),
            enterTransition = { fadeIn(Motion.enter()) + slideInHorizontally(Motion.enter()) { it / 12 } },
            exitTransition = { fadeOut(Motion.fast()) },
            popEnterTransition = { fadeIn(Motion.enter()) },
            popExitTransition = { fadeOut(Motion.fast()) }) {
            composable("home") { HomeScreen(nav) }
            composable("photos") { PhotosScreen(nav) }
            composable("projects") { ProjectsScreen(nav) }
            composable("you") { YouScreen(nav) }
            composable("assistant") { Box(Modifier.statusBarsPadding()) { AssistantScreen() } }
            composable("pair") { PairScreen(onPaired = { nav.go("home") }, onCancel = { nav.back() }) }
            composable("search?q={q}", arguments = listOf(navArgument("q") { type = NavType.StringType; defaultValue = "" })) {
                Box(Modifier.statusBarsPadding()) { SearchScreen(nav, it.arguments?.getString("q").orEmpty()) }
            }
            composable("browse/{kind}") { Box(Modifier.statusBarsPadding()) { BrowseScreen(it.arguments?.getString("kind") ?: "movies", nav) } }
            composable("group/{key}") { Box(Modifier.statusBarsPadding()) { GroupScreen(URLDecoder.decode(it.arguments?.getString("key") ?: "", "UTF-8"), nav) } }
            composable("watch/{id}?party={party}", arguments = listOf(navArgument("party") { type = NavType.StringType; nullable = true; defaultValue = null })) {
                WatchScreen(it.arguments?.getString("id")!!.toInt(), nav, it.arguments?.getString("party"))
            }
            composable("item/{id}") { Box(Modifier.statusBarsPadding()) { ItemScreen(it.arguments?.getString("id")!!.toInt(), nav) } }
            composable("project/{id}") { Box(Modifier.statusBarsPadding()) { ProjectScreen(it.arguments?.getString("id")!!.toInt(), nav) } }
        }
    }
    ItemActionsSheet(nav)
}

/** "⋮" / long-press menu for any video: play, play next, add to queue — like YouTube. */
@kotlin.OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ItemActionsSheet(nav: Nav) {
    val item = NowPlaying.actionsFor.value ?: return
    val p = LocalPaddock.current
    val ctx = LocalContext.current
    fun close() { NowPlaying.actionsFor.value = null }
    fun say(msg: String) = android.widget.Toast.makeText(ctx, msg, android.widget.Toast.LENGTH_SHORT).show()
    ModalBottomSheet(onDismissRequest = { close() }, containerColor = p.bgElev) {
        Text(item.title, color = p.text, style = MaterialTheme.typography.titleMedium, maxLines = 2,
            modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
        @Composable
        fun Action(icon: androidx.compose.ui.graphics.vector.ImageVector, label: String, onClick: () -> Unit) {
            Row(Modifier.fillMaxWidth().clickable { onClick(); close() }.padding(horizontal = 24.dp, vertical = 14.dp), verticalAlignment = Alignment.CenterVertically) {
                Icon(icon, null, tint = p.text); Spacer(Modifier.width(20.dp)); Text(label, color = p.text)
            }
        }
        Action(Icons.Filled.PlayArrow, "Play") { nav.open(item) }
        Action(Icons.Filled.SkipNext, "Play next") { NowPlaying.playNext(item); say("Plays next") }
        Action(Icons.Filled.QueueMusic, "Add to queue") { NowPlaying.addToQueue(item); say("Added to queue · ${NowPlaying.queue.size}") }
        Spacer(Modifier.navigationBarsPadding().height(16.dp))
    }
}

@Composable
private fun RowScope.NavItem(key: String, route: String, filled: androidx.compose.ui.graphics.vector.ImageVector,
                             outlined: androidx.compose.ui.graphics.vector.ImageVector, label: String, onClick: () -> Unit) {
    val p = LocalPaddock.current
    NavigationBarItem(selected = route == key, onClick = onClick, icon = { Icon(if (route == key) filled else outlined, label) },
        label = { Text(label, fontSize = 10.sp) },
        colors = NavigationBarItemDefaults.colors(selectedIconColor = p.text, selectedTextColor = p.text, unselectedIconColor = p.text,
            unselectedTextColor = p.text2, indicatorColor = Color.Transparent))
}
