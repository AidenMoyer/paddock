package app.paddock.update

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.SystemUpdate
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import app.paddock.PaddockApp
import app.paddock.ui.fmtBytes
import app.paddock.ui.theme.LocalPaddock

/** The "update available" prompt and its progress, shown over whatever screen is open. */
@Composable
fun UpdatePrompt() {
    val app = PaddockApp.instance
    val ctx = LocalContext.current
    val p = LocalPaddock.current
    val st by Updater.state.collectAsState()
    when (val s = st) {
        is UpdateState.Available -> AlertDialog(
            onDismissRequest = { Updater.later(ctx) },
            icon = { Icon(Icons.Filled.SystemUpdate, null) },
            title = { Text("Update Paddock") },
            text = {
                Text("Version ${s.release.version_name} is ready on ${app.servers.active?.name ?: "your server"} (${fmtBytes(s.release.size)}). " +
                    "It comes straight from your server and installs in a few seconds. Your servers, settings and downloads stay.")
            },
            confirmButton = { TextButton(onClick = { Updater.start(app, s.release) }) { Text("Update now", color = p.link) } },
            dismissButton = { TextButton(onClick = { Updater.later(ctx) }) { Text("Later", color = p.text2) } },
        )
        is UpdateState.Downloading -> AlertDialog(
            onDismissRequest = {},
            title = { Text("Downloading the update") },
            text = {
                Column {
                    LinearProgressIndicator(progress = { s.progress }, modifier = Modifier.fillMaxWidth())
                    Spacer(Modifier.height(10.dp))
                    Text("${(s.progress * 100).toInt()}% of ${fmtBytes(s.release.size)}", color = p.text2, style = MaterialTheme.typography.bodySmall)
                }
            },
            confirmButton = {},
        )
        UpdateState.Installing -> AlertDialog(
            onDismissRequest = {},
            title = { Text("Installing…") },
            text = { Text("Android checks the update and restarts Paddock. If it asks, allow Paddock to install updates.") },
            confirmButton = { TextButton(onClick = { Updater.dismiss() }) { Text("Hide", color = p.text2) } },
        )
        is UpdateState.Failed -> AlertDialog(
            onDismissRequest = { Updater.dismiss() },
            title = { Text("The update didn't install") },
            text = { Text(s.message) },
            confirmButton = { TextButton(onClick = { Updater.dismiss() }) { Text("OK", color = p.link) } },
        )
        UpdateState.Idle -> {}
    }
}
