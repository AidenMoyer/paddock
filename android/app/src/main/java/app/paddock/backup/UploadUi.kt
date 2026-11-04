package app.paddock.backup

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.CloudUpload
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.paddock.PaddockApp
import app.paddock.net.UploadStatus
import app.paddock.ui.theme.LocalPaddock
import kotlinx.coroutines.delay

private fun files(n: Int) = if (n == 1) "1 file" else "$n files"

/**
 * What's happening to what you uploaded, above the navigation bar: progress while it goes, then that it landed or
 * exactly why it didn't. Lives in the app because Android may not be allowed to show a notification.
 */
@Composable
fun UploadBanner(onOpenDetails: () -> Unit) {
    val ctx = LocalContext.current
    val p = LocalPaddock.current
    val state by UploadStatus.state.collectAsState()
    val changes by BackupReport.changes.collectAsState()
    val problems = remember(changes) { BackupReport.unseen(ctx) }
    val s = state
    val running = s?.running == true
    val clean = s != null && !running && s.failed == 0 && problems.isEmpty()
    // a clean finish says so for a moment, then gets out of the way
    LaunchedEffect(clean) { if (clean) { delay(5000); UploadStatus.dismiss() } }

    AnimatedVisibility(running || problems.isNotEmpty() || clean) {
        when {
            running -> Card(Icons.Filled.CloudUpload, p.link,
                title = s!!.waiting ?: s.current?.let { "Uploading $it" } ?: "Getting ${files(s.total)} ready",
                detail = "${minOf(s.finished + 1, s.total)} of ${s.total}".takeIf { s.total > 1 },
                progress = if (s.waiting == null) s.fraction else null,
                action = "Cancel", onAction = { ManualUpload.cancel(ctx) })
            problems.isNotEmpty() -> {
                val reasons = problems.map { it.reason }.distinct()
                Card(Icons.Filled.ErrorOutline, p.danger,
                    title = if (problems.size == 1) "${problems[0].name} didn't upload" else "${files(problems.size)} didn't upload",
                    detail = if (reasons.size == 1) reasons[0] else "${reasons.last()} (and ${reasons.size - 1} more: tap to see)",
                    onTap = onOpenDetails,
                    action = "Try again", onAction = { UploadStatus.dismiss(); BackupReport.retry(ctx) },
                    dismissBelow = { UploadStatus.dismiss(); BackupReport.markSeen(ctx) })
            }
            clean -> {
                val server = PaddockApp.instance.servers.active?.name ?: "your server"
                Card(Icons.Filled.CheckCircle, p.ok,
                    title = when {
                        s!!.uploaded == 0 && s.already == 1 -> "That's already on $server"
                        s.uploaded == 0 -> "Those ${s.already} files are already on $server"
                        s.already == 0 -> "Uploaded ${files(s.uploaded)} to $server"
                        else -> "Uploaded ${files(s.uploaded)}. ${s.already} more ${if (s.already == 1) "was" else "were"} already there."
                    },
                    onClose = { UploadStatus.dismiss() })
            }
        }
    }
}

@Composable
private fun Card(icon: ImageVector, tint: Color, title: String, detail: String? = null, progress: Float? = null,
                 onTap: (() -> Unit)? = null, action: String? = null, onAction: () -> Unit = {},
                 onClose: (() -> Unit)? = null, dismissBelow: (() -> Unit)? = null) {
    val p = LocalPaddock.current
    Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp).clip(RoundedCornerShape(14.dp)).background(p.surface)
        .then(if (onTap != null) Modifier.clickable(onClick = onTap) else Modifier)) {
        val below = dismissBelow != null    // a reason deserves the full width, so its buttons go underneath
        Row(Modifier.fillMaxWidth().padding(start = 12.dp, top = if (below) 12.dp else 8.dp, bottom = if (below) 0.dp else 8.dp, end = if (below) 12.dp else 4.dp),
            verticalAlignment = if (below) Alignment.Top else Alignment.CenterVertically) {
            Icon(icon, null, tint = tint)
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text(title, color = p.text, fontWeight = FontWeight.Medium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                if (detail != null) Text(detail, color = p.text2, style = MaterialTheme.typography.bodySmall)
            }
            if (action != null && !below) TextButton(onClick = onAction) { Text(action, color = p.link) }
            if (onClose != null) IconButton(onClick = onClose) { Icon(Icons.Filled.Close, "Dismiss", tint = p.text2) }
        }
        if (below) Row(Modifier.align(Alignment.End).padding(end = 4.dp)) {
            TextButton(onClick = dismissBelow!!) { Text("Dismiss", color = p.text2) }
            if (action != null) TextButton(onClick = onAction) { Text(action, color = p.link) }
        }
        if (progress != null) LinearProgressIndicator(progress = { progress }, Modifier.fillMaxWidth(), color = p.link, trackColor = Color.Transparent)
    }
}
