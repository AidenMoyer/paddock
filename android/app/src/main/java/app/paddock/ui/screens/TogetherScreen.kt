package app.paddock.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.paddock.PaddockApp
import app.paddock.data.CallInfo
import app.paddock.data.Person
import app.paddock.data.VoicePlaces
import app.paddock.live.BrandAvatar
import app.paddock.live.Live
import app.paddock.live.PartyInfo
import app.paddock.live.VoiceHub
import app.paddock.live.rememberJoinVoice
import app.paddock.ui.Nav
import app.paddock.ui.Thumb
import app.paddock.ui.theme.LocalPaddock
import app.paddock.ui.theme.Tokens
import app.paddock.ui.timeAgo
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/** Together: watch parties happening now, your communities' voice lounges, and calling people. */
@Composable
fun TogetherScreen(nav: Nav) {
    val app = PaddockApp.instance
    val api = app.api
    val p = LocalPaddock.current
    val scope = rememberCoroutineScope()
    val join = rememberJoinVoice()
    var parties by remember { mutableStateOf<List<PartyInfo>>(emptyList()) }
    var places by remember { mutableStateOf(VoicePlaces()) }
    var people by remember { mutableStateOf<List<Person>>(emptyList()) }
    var tick by remember { mutableIntStateOf(0) }
    val room = VoiceHub.room.value
    LaunchedEffect(tick, room) {
        runCatching { parties = api.get("/api/parties") }
        runCatching { places = api.get("/api/voice/places") }
        runCatching { people = api.get("/api/people") }
    }
    LaunchedEffect(Unit) { while (true) { delay(8000); tick++ } }
    LaunchedEffect(Unit) { Live.notices.collect { tick++ } }

    fun call(ids: List<Int>, label: String) = scope.launch {
        runCatching { api.post<CallInfo>("/api/calls", buildJsonObject { put("user_ids", JsonArray(ids.map { JsonPrimitive(it) })) }) }
            .onSuccess { c -> join(c.room, label); tick++ }
            .onFailure { android.widget.Toast.makeText(app, it.message ?: "Couldn't call", android.widget.Toast.LENGTH_SHORT).show() }
    }

    LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            Text("Together", style = MaterialTheme.typography.headlineMedium, color = p.text)
            Text("Watch in sync and talk, wherever everyone is. Voice keeps going while you browse.", color = p.text2)
        }
        item { Section("Happening now") }
        if (parties.isEmpty()) item { Text("Nobody's watching together right now. Start a party from any video with Watch together.", color = p.text3) }
        items(parties, key = { "party-${it.id}" }) { party ->
            Card(onClick = { nav.go("watch/${party.item_id}?party=${party.id}") }) {
                Thumb(party.item_id, Modifier.size(112.dp, 63.dp).clip(RoundedCornerShape(8.dp)))
                Column(Modifier.weight(1f).padding(horizontal = 12.dp)) {
                    Text(party.title, color = p.text, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Text(listOfNotNull("${party.host}'s party", party.community.ifBlank { null }, "${party.members.size} watching",
                        if (party.queue.isNotEmpty()) "${party.queue.size} queued" else null).joinToString(" · "), color = p.text2, style = MaterialTheme.typography.bodySmall)
                }
                BrandButton("Join") { nav.go("watch/${party.item_id}?party=${party.id}") }
            }
        }
        item { Section("Lounges") }
        if (places.lounges.isEmpty()) item { Text("You're not in a community with a voice lounge.", color = p.text3) }
        items(places.lounges, key = { it.room }) { l ->
            val here = room == l.room
            Card(onClick = null) {
                Box(Modifier.size(44.dp).clip(RoundedCornerShape(12.dp)).background(Brush.linearGradient(Tokens.Brand.gradient)), contentAlignment = Alignment.Center) {
                    Icon(Icons.Filled.Groups, null, tint = Color.White)
                }
                Column(Modifier.weight(1f).padding(horizontal = 12.dp)) {
                    Text(l.name, color = p.text, fontWeight = FontWeight.Medium)
                    Text(if (l.members.isEmpty()) "Empty" else l.members.joinToString { it.username }, color = p.text2, style = MaterialTheme.typography.bodySmall,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                if (here) OutlinedButton(onClick = { VoiceHub.leave() }) { Text("Leave", color = p.danger) }
                else BrandButton("Join") { join(l.room, "${l.name} lounge") }
            }
        }
        if (places.calls.isNotEmpty()) {
            item { Section("Your calls") }
            items(places.calls, key = { "call-${it.id}" }) { c ->
                Card(onClick = null) {
                    Icon(Icons.Filled.Call, null, tint = p.ok)
                    Column(Modifier.weight(1f).padding(horizontal = 12.dp)) {
                        Text(c.people.joinToString { it.name }, color = p.text, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        Text("started by ${c.host} ${timeAgo(c.created)} · ${c.in_call.size} talking", color = p.text2, style = MaterialTheme.typography.bodySmall)
                    }
                    if (room == c.room) Text("You're in it", color = p.ok) else BrandButton("Join") { join(c.room, "Call with ${c.host}") }
                }
            }
        }
        item { Section("Call someone") }
        if (people.isEmpty()) item { Text("People in your communities show up here.", color = p.text3) }
        items(people, key = { "person-${it.id}" }) { person ->
            val online = person.id in places.online
            Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                BrandAvatar(person.name, 36)
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text(person.name, color = p.text)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(8.dp).clip(CircleShape).background(if (online) p.ok else p.text3))
                        Spacer(Modifier.width(6.dp))
                        Text(if (online) "Online" else "Away", color = if (online) p.ok else p.text3, style = MaterialTheme.typography.bodySmall)
                    }
                }
                FilledTonalButton(onClick = { call(listOf(person.id), "Call with ${person.name}") }) {
                    Icon(Icons.Filled.Call, null, Modifier.size(18.dp)); Spacer(Modifier.width(6.dp)); Text("Call")
                }
            }
        }
        item { Spacer(Modifier.height(72.dp)) }
    }
}

@Composable
private fun Section(title: String) {
    Text(title, style = MaterialTheme.typography.titleMedium, color = LocalPaddock.current.text, modifier = Modifier.padding(top = 8.dp))
}

@Composable
private fun Card(onClick: (() -> Unit)?, content: @Composable RowScope.() -> Unit) {
    val p = LocalPaddock.current
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.surface).then(if (onClick != null) Modifier.clickable { onClick() } else Modifier)
        .padding(12.dp), verticalAlignment = Alignment.CenterVertically, content = content)
}

/** A primary action in the logo's gradient. */
@Composable
fun BrandButton(text: String, modifier: Modifier = Modifier, onClick: () -> Unit) {
    Box(modifier.clip(RoundedCornerShape(50)).background(Brush.linearGradient(Tokens.Brand.gradient)).clickable { onClick() }
        .padding(horizontal = 16.dp, vertical = 8.dp), contentAlignment = Alignment.Center) {
        Text(text, color = Color.White, fontWeight = FontWeight.SemiBold)
    }
}
