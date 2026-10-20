package app.paddock

import android.app.PictureInPictureParams
import android.content.res.Configuration
import android.os.Bundle
import android.util.Rational
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import app.paddock.ui.PaddockRoot
import app.paddock.ui.screens.NowPlaying
import app.paddock.ui.theme.PaddockTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) = PaddockApp.instance.conn.refresh()   // re-check home vs away on return
        })
        setContent {
            val theme by PaddockApp.instance.theme.collectAsState()
            PaddockTheme(theme) { PaddockRoot() }
        }
    }

    /** Leaving the app while a video plays → keep watching in a floating window. */
    override fun onUserLeaveHint() {
        super.onUserLeaveHint()
        if (NowPlaying.player?.isPlaying == true) {
            runCatching { enterPictureInPictureMode(PictureInPictureParams.Builder().setAspectRatio(Rational(16, 9)).build()) }
        }
    }

    override fun onPictureInPictureModeChanged(isInPictureInPictureMode: Boolean, newConfig: Configuration) {
        super.onPictureInPictureModeChanged(isInPictureInPictureMode, newConfig)
        NowPlaying.inPip.value = isInPictureInPictureMode
    }
}
