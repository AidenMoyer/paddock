package app.paddock.player

import android.content.ComponentName
import android.content.Context
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Audiotrack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.SkipNext
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import app.paddock.PaddockApp
import app.paddock.data.Item
import app.paddock.data.Playback
import app.paddock.ui.Thumb
import app.paddock.ui.screens.NowPlaying
import app.paddock.ui.subtitle
import app.paddock.ui.theme.LocalPaddock
import com.google.common.util.concurrent.ListenableFuture
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.Request

/**
 * Songs keep playing when you leave the player or the app, and carry on to the next one, which the screen can't do:
 * Compose stops drawing once the app is in the background, so nothing on a screen would ever get to run.
 */
object Music {
    /** The song playing now, or null when it's a video or nothing. */
    val song = mutableStateOf<Item?>(null)
    var player: ExoPlayer? = null
        private set
    private var next: List<Item> = emptyList()
    private val scope = MainScope()
    private var loading: Job? = null
    private var session: ListenableFuture<MediaController>? = null

    private val whenItEnds = object : Player.Listener {
        override fun onPlaybackStateChanged(state: Int) {
            if (state != Player.STATE_ENDED) return
            val done = song.value ?: return
            saveProgress(done, player?.duration ?: 0)
            if (PaddockApp.instance.settings.autoplay) skip()
        }
    }

    /** The player screen hands a song over once it's playing. */
    fun begin(context: Context, p: ExoPlayer, item: Item, upNext: List<Item>) {
        if (player !== p) {
            player?.removeListener(whenItEnds)
            player = p
            p.addListener(whenItEnds)
            PlaybackService.use(p)
        }
        song.value = item
        next = upNext
        NowPlaying.itemId = item.id
        if (session == null) {
            // connecting is what starts the service that keeps the app alive and shows the lock screen controls
            session = MediaController.Builder(context.applicationContext,
                SessionToken(context.applicationContext, ComponentName(context.applicationContext, PlaybackService::class.java))).buildAsync()
        }
        decorate(p, item)
    }

    /** A video or a watch party is taking over the player. */
    fun release() {
        song.value = null
        next = emptyList()
        loading?.cancel()
        session?.let { MediaController.releaseFuture(it) }
        session = null
    }

    /** Close the mini player: the music stops. */
    fun stop() {
        val p = player
        val item = song.value
        if (p != null && item != null) saveProgress(item, p.duration, p.currentPosition)
        release()
        p?.stop()
        p?.clearMediaItems()
        NowPlaying.itemId = null
    }

    fun nextSong(): Item? = NowPlaying.queue.firstOrNull { it.kind == "audio" && it.id != song.value?.id } ?: next.firstOrNull()

    /** On to the next song. Everything on screen follows [song]. */
    fun skip() {
        val target = nextSong() ?: return
        play(target)
    }

    fun play(item: Item) {
        val p = player ?: return
        val app = PaddockApp.instance
        loading?.cancel()
        loading = scope.launch {
            val plan = runCatching { app.api.get<Playback>("/api/items/${item.id}/playback?codecs=${Players.codecs()}") }.getOrNull() ?: return@launch
            val quality = plan.qualities.firstOrNull { it.id == if (plan.mode == "direct") "original" else "auto" } ?: plan.qualities.firstOrNull() ?: return@launch
            NowPlaying.queue.removeAll { it.id == item.id }
            p.setMediaItem(app.players.mediaItem(app.api, quality, plan, item), 0L)
            p.prepare()
            p.playWhenReady = true
            song.value = item
            NowPlaying.itemId = item.id
            decorate(p, item)
            next = runCatching { app.api.get<List<Item>>("/api/items/${item.id}/up_next") }.getOrDefault(emptyList())
        }
    }

    /** The cover for the lock screen. It comes through the app's own pinned connection, so Android can't fetch it itself. */
    private fun decorate(p: ExoPlayer, item: Item) {
        if (!item.has_thumb) return
        scope.launch {
            val app = PaddockApp.instance
            val cover = withContext(Dispatchers.IO) {
                runCatching {
                    val client = app.conn.client ?: return@runCatching null
                    client.newCall(Request.Builder().url(app.api.absolute("/api/items/${item.id}/thumb")).build()).execute().use { if (it.isSuccessful) it.body?.bytes() else null }
                }.getOrNull()
            } ?: return@launch
            val now = p.currentMediaItem ?: return@launch
            if (song.value?.id != item.id) return@launch
            p.replaceMediaItem(p.currentMediaItemIndex, now.buildUpon().setMediaMetadata(
                now.mediaMetadata.buildUpon().setArtworkData(cover, MediaMetadata.PICTURE_TYPE_FRONT_COVER).build()).build())
        }
    }

    private fun saveProgress(item: Item, durationMs: Long, positionMs: Long = durationMs) {
        val dur = durationMs.takeIf { it > 0 }?.div(1000.0) ?: return
        scope.launch {
            runCatching {
                PaddockApp.instance.api.post<JsonObject>("/api/items/${item.id}/progress", buildJsonObject { put("position", positionMs / 1000.0); put("duration", dur) })
            }
        }
    }
}

/** What's playing while you browse: above the navigation bar, wherever you are in the app. */
@Composable
fun MiniPlayer(onOpen: (Item) -> Unit) {
    val p = LocalPaddock.current
    val item = Music.song.value ?: return
    val player = Music.player ?: return
    var playing by remember { mutableStateOf(player.isPlaying) }
    var fraction by remember { mutableFloatStateOf(0f) }
    LaunchedEffect(player) {
        while (isActive) {
            playing = player.isPlaying
            fraction = if (player.duration > 0) (player.currentPosition.toFloat() / player.duration).coerceIn(0f, 1f) else 0f
            delay(400)
        }
    }
    Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp).clip(RoundedCornerShape(14.dp)).background(p.surface).clickable { onOpen(item) }) {
        Row(Modifier.padding(start = 8.dp, top = 6.dp, bottom = 6.dp, end = 2.dp), verticalAlignment = Alignment.CenterVertically) {
            Thumb(if (item.has_thumb) item.id else null, Modifier.size(40.dp).clip(RoundedCornerShape(8.dp)), fallback = Icons.Filled.Audiotrack)
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text(item.title, color = p.text, fontWeight = FontWeight.Medium, fontSize = 14.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(subtitle(item), color = p.text2, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            IconButton(onClick = {
                if (player.playbackState == Player.STATE_ENDED) player.seekTo(0)
                if (player.isPlaying) player.pause() else player.play()
                playing = !playing
            }) { Icon(if (playing) Icons.Filled.Pause else Icons.Filled.PlayArrow, if (playing) "Pause" else "Play", tint = p.text) }
            if (Music.nextSong() != null) IconButton(onClick = { Music.skip() }) { Icon(Icons.Filled.SkipNext, "Next", tint = p.text) }
            IconButton(onClick = { Music.stop() }) { Icon(Icons.Filled.Close, "Stop", tint = p.text2) }
        }
        Box(Modifier.fillMaxWidth().height(2.dp).background(p.surface3)) {
            Box(Modifier.fillMaxWidth(fraction).height(2.dp).background(p.accent))
        }
    }
}
