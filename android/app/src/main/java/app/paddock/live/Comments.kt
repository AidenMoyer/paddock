package app.paddock.live

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import app.paddock.PaddockApp
import app.paddock.data.Comment
import app.paddock.data.CommentPlace
import app.paddock.data.CommentsResponse
import app.paddock.ui.fmtDuration
import app.paddock.ui.theme.LocalPaddock
import app.paddock.ui.timeAgo
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Comments under a video or photo. Each lives in a community (or with everyone on the server) and can point
 * at a moment in a video ([position]/[onSeek]); new ones arrive live.
 */
@Composable
fun CommentsSection(itemId: Int, modifier: Modifier = Modifier, position: (() -> Double)? = null, onSeek: ((Double) -> Unit)? = null) {
    val p = LocalPaddock.current
    val api = PaddockApp.instance.api
    val scope = rememberCoroutineScope()
    var comments by remember(itemId) { mutableStateOf<List<Comment>>(emptyList()) }
    var places by remember(itemId) { mutableStateOf<List<CommentPlace>>(emptyList()) }
    var loaded by remember(itemId) { mutableStateOf(false) }
    var text by remember(itemId) { mutableStateOf("") }
    var place by remember(itemId) { mutableStateOf<CommentPlace?>(null) }
    var atMoment by remember(itemId) { mutableStateOf(false) }
    var error by remember(itemId) { mutableStateOf<String?>(null) }
    var menu by remember { mutableStateOf(false) }

    LaunchedEffect(itemId) {
        runCatching { api.get<CommentsResponse>("/api/items/$itemId/comments") }.onSuccess {
            comments = it.comments; places = it.places; place = it.places.firstOrNull(); loaded = true
        }
    }
    LaunchedEffect(itemId) {
        Live.comments.collect { c -> if (c.item_id == itemId && comments.none { it.id == c.id }) comments = comments + c }
    }
    if (!loaded) return

    Column(modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
        Text(if (comments.isEmpty()) "Comments" else "Comments · ${comments.size}", style = MaterialTheme.typography.titleMedium, color = p.text)
        Spacer(Modifier.height(8.dp))
        if (places.isEmpty()) Text("Comments are switched off for your communities.", color = p.text3, style = MaterialTheme.typography.bodySmall)
        else {
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(text, { text = it; error = null }, placeholder = { Text("Add a comment…") }, modifier = Modifier.weight(1f), maxLines = 5)
                IconButton(enabled = text.isNotBlank(), onClick = {
                    val body = text.trim()
                    scope.launch {
                        runCatching {
                            api.post<Comment>("/api/items/$itemId/comments", buildJsonObject {
                                put("body", body)
                                put("community_id", place?.id?.let { JsonPrimitive(it) } ?: JsonNull)
                                if (atMoment && position != null) put("at", position().toLong().toDouble())
                            })
                        }.onSuccess { c -> text = ""; if (comments.none { it.id == c.id }) comments = comments + c }
                            .onFailure { error = it.message }
                    }
                }) { Icon(Icons.AutoMirrored.Filled.Send, "Comment", tint = p.link) }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (places.size > 1) Box {
                    TextButton(onClick = { menu = true }) { Text("Seen by: ${place?.name ?: "Everyone"}", color = p.text2) }
                    DropdownMenu(menu, { menu = false }) {
                        for (pl in places) DropdownMenuItem(text = { Text(pl.name) }, onClick = { place = pl; menu = false })
                    }
                }
                if (position != null) Row(Modifier.clickable { atMoment = !atMoment }, verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(atMoment, { atMoment = it }); Text("At this moment", color = p.text2, style = MaterialTheme.typography.bodySmall)
                }
            }
            error?.let { Text(it, color = p.danger, style = MaterialTheme.typography.bodySmall) }
        }
        for (c in comments) {
            Row(Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
                BrandAvatar(c.display_name.ifBlank { c.username }, 34)
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text(c.display_name.ifBlank { c.username }, color = p.text, fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.bodySmall)
                        if (places.size > 1 || c.community_id != null) Text(c.community, color = p.text3, style = MaterialTheme.typography.bodySmall)
                        Text(timeAgo(c.created_at), color = p.text3, style = MaterialTheme.typography.bodySmall)
                        c.at?.let { at ->
                            Text(fmtDuration(at), color = p.link, style = MaterialTheme.typography.bodySmall,
                                modifier = Modifier.clickable(enabled = onSeek != null) { onSeek?.invoke(at) })
                        }
                    }
                    Text(c.body, color = p.text)
                    if (c.mine || PaddockApp.instance.me.value?.is_admin == true) TextButton(onClick = {
                        scope.launch { runCatching { api.delete("/api/comments/${c.id}") }.onSuccess { comments = comments.filter { it.id != c.id } } }
                    }, contentPadding = PaddingValues(0.dp)) { Text("Delete", color = p.text3, style = MaterialTheme.typography.bodySmall) }
                }
            }
        }
        if (comments.isEmpty()) Text("No comments yet.", color = p.text3, style = MaterialTheme.typography.bodySmall)
    }
}
