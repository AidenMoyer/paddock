package app.paddock.live

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.paddock.PaddockApp
import app.paddock.ui.Nav
import app.paddock.ui.theme.LocalPaddock
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private val Talking = Color(0xFF4CD964)

/** The watch-party card shown above "Up next": who's here, voice, reactions and chat. */
@Composable
fun PartyPanel(party: PartySession, voice: VoiceSession?, onJoinVoice: () -> Unit, onLeaveVoice: () -> Unit,
               onLeave: () -> Unit, onEnd: () -> Unit) {
    val p = LocalPaddock.current
    val ctx = LocalContext.current
    val info = party.info.value
    var message by remember { mutableStateOf("") }
    Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp).fillMaxWidth().clip(RoundedCornerShape(14.dp))
        .background(p.surface).padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Watch party", color = p.text, fontWeight = FontWeight.Bold)
                Text(info?.let { "Code ${it.id} · hosted by ${it.host}" + if (it.control == "host") " · host controls" else "" } ?: party.status.value,
                    color = p.text2, style = MaterialTheme.typography.bodySmall)
            }
            IconButton(onClick = {
                val link = "${PaddockApp.instance.conn.state.value.baseUrl ?: ""}/party/${party.id}"
                ctx.startActivity(android.content.Intent.createChooser(android.content.Intent(android.content.Intent.ACTION_SEND)
                    .setType("text/plain").putExtra(android.content.Intent.EXTRA_TEXT, "Join my watch party on Paddock: code ${party.id}\n$link"), "Invite"))
            }) { Icon(Icons.Filled.PersonAdd, "Invite", tint = p.text) }
            if (info != null && info.host_id == party.myUserId) TextButton(onClick = onEnd) { Text("End", color = p.danger) }
            else TextButton(onClick = onLeave) { Text("Leave", color = p.text2) }
        }
        if (party.status.value.isNotBlank() && info != null) Text(party.status.value, color = p.warn, style = MaterialTheme.typography.bodySmall)
        // members, with a green ring while someone talks
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            for (m in party.members) {
                val vms = voice?.members?.filter { it.user_id == m.user_id }.orEmpty()
                val talking = vms.any { voice?.speaking?.get(it.id) == true }
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Box(Modifier.size(36.dp).clip(CircleShape).background(p.surface3)
                        .border(3.dp, if (talking) Talking else Color.Transparent, CircleShape), contentAlignment = Alignment.Center) {
                        Text(m.username.take(1).uppercase(), color = p.text, fontWeight = FontWeight.Bold)
                    }
                    Text(m.username + if (m.host) " ★" else "", color = p.text2, fontSize = 11.sp, maxLines = 1)
                    if (m.buffering) Text("buffering", color = p.warn, fontSize = 10.sp)
                    else if (vms.isNotEmpty() && vms.all { it.muted }) Icon(Icons.Filled.MicOff, "Muted", tint = p.text3, modifier = Modifier.size(12.dp))
                }
            }
        }
        // voice
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            if (voice == null) {
                FilledTonalButton(onClick = onJoinVoice) { Icon(Icons.Filled.Mic, null, Modifier.size(18.dp)); Spacer(Modifier.width(6.dp)); Text("Join voice") }
            } else {
                FilledTonalButton(onClick = { voice.setMuted(!voice.muted.value) }) {
                    Icon(if (voice.muted.value) Icons.Filled.MicOff else Icons.Filled.Mic, null, Modifier.size(18.dp))
                    Spacer(Modifier.width(6.dp)); Text(if (voice.muted.value) "Unmute" else "Mute")
                }
                IconButton(onClick = { voice.setDeafened(!voice.deafened.value) }) {
                    Icon(if (voice.deafened.value) Icons.Filled.HeadsetOff else Icons.Filled.Headset, "Deafen", tint = p.text)
                }
                TextButton(onClick = onLeaveVoice) { Text("Leave voice", color = p.text2) }
                LinearProgressIndicator(progress = { voice.level.floatValue }, modifier = Modifier.weight(1f), color = Talking, trackColor = p.surface3)
            }
        }
        if (voice?.error?.value != null) Text(voice.error.value!!, color = p.danger, style = MaterialTheme.typography.bodySmall)
        // reactions
        LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            items(REACTIONS) { r ->
                Text(r, fontSize = 22.sp, modifier = Modifier.clip(RoundedCornerShape(10.dp)).background(p.surface2)
                    .clickable { party.react(r) }.padding(horizontal = 8.dp, vertical = 4.dp))
            }
        }
        // chat
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            for (line in party.chat.takeLast(8)) {
                if (line.user == null) Text(line.text, color = p.text3, style = MaterialTheme.typography.bodySmall)
                else Text(buildString { append(line.user); append("  "); append(line.text) }, color = p.text, style = MaterialTheme.typography.bodyMedium)
            }
        }
        OutlinedTextField(message, { message = it.take(500) }, placeholder = { Text("Say something…") }, singleLine = true,
            modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(20.dp),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { if (message.isNotBlank()) { party.chat(message.trim()); message = "" } }),
            trailingIcon = { IconButton(onClick = { if (message.isNotBlank()) { party.chat(message.trim()); message = "" } }) { Icon(Icons.Filled.Send, "Send", tint = p.text) } })
        Text("Tip: headphones stop the movie echoing into your mic.", color = p.text3, fontSize = 11.sp)
    }
}

/** Reactions float up over the video. */
@Composable
fun BoxScope.ReactionsOverlay(party: PartySession) {
    for (r in party.reactions) key(r.key) {
        val rise = remember { Animatable(0f) }
        val x = remember { (10..80).random() / 100f }
        LaunchedEffect(Unit) { rise.animateTo(1f, tween(2500, easing = FastOutSlowInEasing)) }
        BoxWithConstraints(Modifier.matchParentSize()) {
            Column(Modifier.offset(x = maxWidth * x, y = maxHeight * (0.8f - 0.7f * rise.value)).alpha(1f - rise.value * rise.value),
                horizontalAlignment = Alignment.CenterHorizontally) {
                Text(r.emoji, fontSize = 34.sp)
                Text(r.user, color = Color.White, fontSize = 10.sp)
            }
        }
    }
}

/** "Watch together": start a party on the current video. */
@Composable
fun WatchTogetherDialog(onStart: (everyone: Boolean, hostOnly: Boolean, waitForAll: Boolean) -> Unit, onDismiss: () -> Unit) {
    var everyone by remember { mutableStateOf(true) }
    var hostOnly by remember { mutableStateOf(false) }
    var wait by remember { mutableStateOf(true) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("Watch together") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Everyone streams from your server in their own quality, and Paddock keeps all players in step: play, pause and skip happen for everyone.")
                SwitchRow("Anyone with an account can join", everyone) { everyone = it }
                SwitchRow("Only I control playback", hostOnly) { hostOnly = it }
                SwitchRow("Wait for anyone who's buffering", wait) { wait = it }
            }
        },
        confirmButton = { TextButton(onClick = { onStart(everyone, hostOnly, wait) }) { Text("Start the party") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } })
}

@Composable
private fun SwitchRow(label: String, value: Boolean, onChange: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, modifier = Modifier.weight(1f))
        Switch(checked = value, onCheckedChange = onChange)
    }
}

/** Home: parties you can join right now. */
@Composable
fun PartiesRow(nav: Nav) {
    val p = LocalPaddock.current
    var parties by remember { mutableStateOf<List<PartyInfo>>(emptyList()) }
    LaunchedEffect(Unit) {
        while (true) {
            parties = runCatching { PaddockApp.instance.api.get<List<PartyInfo>>("/api/parties") }.getOrDefault(parties)
            delay(15_000)
        }
    }
    if (parties.isEmpty()) return
    Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
        Text("Watch parties happening now", color = p.text, fontWeight = FontWeight.Bold, modifier = Modifier.padding(bottom = 6.dp))
        for (party in parties) {
            Row(Modifier.fillMaxWidth().padding(vertical = 4.dp).clip(RoundedCornerShape(12.dp)).background(p.surface)
                .clickable { nav.go("watch/${party.item_id}?party=${party.id}") }.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Filled.Groups, null, tint = p.accent)
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text(party.title, color = p.text, maxLines = 1)
                    Text("${party.host} · ${party.members.size} watching" + if (party.state?.playing == false) " · paused" else "",
                        color = p.text2, style = MaterialTheme.typography.bodySmall)
                }
                Text("Join", color = p.accent, fontWeight = FontWeight.Bold)
            }
        }
    }
}
