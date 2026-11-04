package app.paddock.player

import android.app.PendingIntent
import android.content.Intent
import androidx.annotation.OptIn
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.DefaultMediaNotificationProvider
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import app.paddock.MainActivity
import app.paddock.R

/** Keeps music alive with the screen off, and gives it lock screen, notification and headphone controls. */
@OptIn(UnstableApi::class)
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        setMediaNotificationProvider(DefaultMediaNotificationProvider.Builder(this).build().apply { setSmallIcon(R.drawable.ic_notification) })
        Music.player?.let { start(it) } ?: stopSelf()
    }

    private fun start(player: Player) {
        val open = PendingIntent.getActivity(this, 45, Intent(this, MainActivity::class.java), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        session?.release()
        session = MediaSession.Builder(this, player).setSessionActivity(open).build()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo) = session

    /** Swiping the app away stops it, unless music is playing: then it's the one thing that should keep going. */
    override fun onTaskRemoved(rootIntent: Intent?) {
        val p = session?.player
        if (p == null || !p.playWhenReady || p.mediaItemCount == 0 || p.playbackState == Player.STATE_ENDED) stopSelf()
    }

    override fun onDestroy() {
        instance = null
        session?.release()      // the player itself belongs to the app, which goes on using it
        session = null
        super.onDestroy()
    }

    companion object {
        private var instance: PlaybackService? = null

        /** The app replaced its player (it switched server). */
        fun use(player: Player) { instance?.start(player) }
    }
}
