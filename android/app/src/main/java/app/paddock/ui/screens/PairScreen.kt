package app.paddock.ui.screens

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.animation.AnimatedContent
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import app.paddock.PaddockApp
import app.paddock.R
import app.paddock.net.Pairing
import app.paddock.ui.theme.LocalPaddock
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.coroutines.launch

@Composable
fun PairScreen(onPaired: () -> Unit, onCancel: (() -> Unit)? = null) {
    val p = LocalPaddock.current
    val scope = rememberCoroutineScope()
    var stage by remember { mutableStateOf("intro") }   // intro | pairing | error
    var error by remember { mutableStateOf("") }

    val scan = rememberLauncherForActivityResult(ScanContract()) { result ->
        val text = result.contents ?: return@rememberLauncherForActivityResult
        stage = "pairing"
        scope.launch {
            try {
                val profile = Pairing.pair(text)
                PaddockApp.instance.servers.add(profile)
                onPaired()
            } catch (e: Exception) {
                error = e.message ?: "Pairing failed"
                stage = "error"
            }
        }
    }
    fun launchScan() = scan.launch(ScanOptions().setPrompt("Point at the code in Paddock → Connect a phone")
        .setBeepEnabled(false).setOrientationLocked(false).setDesiredBarcodeFormats(ScanOptions.QR_CODE))

    Box(Modifier.fillMaxSize().background(p.bg).systemBarsPadding().padding(24.dp), contentAlignment = Alignment.Center) {
        AnimatedContent(stage, label = "pair") { s ->
            Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.widthIn(max = 420.dp)) {
                Image(painterResource(R.drawable.ic_launcher), null, Modifier.size(72.dp))
                Spacer(Modifier.height(24.dp))
                when (s) {
                    "pairing" -> {
                        Text("Connecting securely…", style = MaterialTheme.typography.headlineMedium, color = p.text)
                        Spacer(Modifier.height(16.dp))
                        CircularProgressIndicator(color = p.accent)
                        Text("Verifying the server's identity and setting up your private tunnel key.", color = p.text2,
                            textAlign = TextAlign.Center, modifier = Modifier.padding(top = 16.dp))
                    }
                    "error" -> {
                        Text("Couldn't pair", style = MaterialTheme.typography.headlineMedium, color = p.text)
                        Text(error, color = p.text2, textAlign = TextAlign.Center, modifier = Modifier.padding(vertical = 16.dp))
                        Button(onClick = { launchScan() }, colors = ButtonDefaults.buttonColors(containerColor = p.accent)) { Text("Scan again") }
                    }
                    else -> {
                        Text("Welcome to Paddock", style = MaterialTheme.typography.headlineMedium, color = p.text, fontWeight = FontWeight.Bold)
                        Text("Your movies, photos, footage and builds — on your own server.", color = p.text2, textAlign = TextAlign.Center,
                            modifier = Modifier.padding(top = 8.dp, bottom = 28.dp))
                        Step(1, "Join your home Wi-Fi.")
                        Step(2, "On a computer, open Paddock → Your account → Connect a phone.")
                        Step(3, "Scan the code that appears.")
                        Spacer(Modifier.height(28.dp))
                        Button(onClick = { launchScan() }, modifier = Modifier.fillMaxWidth().height(52.dp), shape = RoundedCornerShape(26.dp),
                            colors = ButtonDefaults.buttonColors(containerColor = p.accent)) {
                            Icon(Icons.Filled.QrCodeScanner, null); Spacer(Modifier.width(8.dp)); Text("Scan pairing code", fontWeight = FontWeight.SemiBold)
                        }
                        if (onCancel != null) TextButton(onClick = onCancel) { Text("Cancel", color = p.text2) }
                        Row(Modifier.padding(top = 20.dp), verticalAlignment = Alignment.Top) {
                            Icon(Icons.Filled.Shield, null, tint = p.text3, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(8.dp))
                            Text("Nothing goes through the cloud. The code works once, on your network, and pins your server's certificate.",
                                color = p.text3, style = MaterialTheme.typography.bodySmall)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun Step(n: Int, text: String) {
    val p = LocalPaddock.current
    Row(Modifier.fillMaxWidth().padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(28.dp).background(p.surface2, RoundedCornerShape(14.dp)), contentAlignment = Alignment.Center) {
            Text("$n", color = p.text, fontWeight = FontWeight.Bold)
        }
        Spacer(Modifier.width(12.dp))
        Text(text, color = p.text)
    }
}
