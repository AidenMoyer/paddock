package app.paddock

import android.app.PictureInPictureParams
import android.content.Intent
import android.content.res.Configuration
import android.os.Bundle
import android.util.Rational
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import app.paddock.ui.PaddockRoot
import app.paddock.ui.screens.NowPlaying
import app.paddock.ui.theme.PaddockTheme
import kotlinx.coroutines.flow.MutableSharedFlow

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) = PaddockApp.instance.conn.refresh()   // re-check home vs away on return
        })
        setContent {
            val theme by PaddockApp.instance.theme.collectAsState()
            val dark = when (theme) { "dark" -> true; "light" -> false; else -> isSystemInDarkTheme() }
            DisposableEffect(dark) {
                // see-through at the top; a light veil at the bottom keeps the navigation buttons readable over scrolling content
                val veilOnDark = android.graphics.Color.argb(0x99, 0x11, 0x11, 0x11)
                val veilOnLight = android.graphics.Color.argb(0xCC, 0xFF, 0xFF, 0xFF)
                val clear = android.graphics.Color.TRANSPARENT
                enableEdgeToEdge(
                    statusBarStyle = if (dark) SystemBarStyle.dark(clear) else SystemBarStyle.light(clear, clear),
                    navigationBarStyle = if (dark) SystemBarStyle.dark(veilOnDark) else SystemBarStyle.light(veilOnLight, veilOnDark),
                )
                onDispose { }
            }
            PaddockTheme(theme) { PaddockRoot() }
        }
        handle(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    /** Notifications open the app at what they're about (a call ringing, a watch party). */
    private fun handle(intent: Intent?) {
        intent?.getStringExtra("open")?.let { opens.tryEmit(it) }
        intent?.removeExtra("open")
    }

    companion object {
        val opens = MutableSharedFlow<String>(replay = 1, extraBufferCapacity = 4)
    }

    /** Leaving the app while a video plays → keep watching in a floating window. */
    override fun onUserLeaveHint() {
        super.onUserLeaveHint()
        if (NowPlaying.player?.isPlaying == true && app.paddock.player.Music.song.value == null) {
            runCatching { enterPictureInPictureMode(PictureInPictureParams.Builder().setAspectRatio(Rational(16, 9)).build()) }
        }
    }

    override fun onPictureInPictureModeChanged(isInPictureInPictureMode: Boolean, newConfig: Configuration) {
        super.onPictureInPictureModeChanged(isInPictureInPictureMode, newConfig)
        NowPlaying.inPip.value = isInPictureInPictureMode
    }
}
