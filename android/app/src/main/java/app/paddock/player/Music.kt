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
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
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
 *
 * The next song waits in the player's own playlist. Besides playing without a gap, that means the player never
 * stops between songs: if it did, Android could take away the background permission that keeps the app alive and not
 * give it back, and the music would die some minutes later.
 */
object Music {
    /** The song playing now, or null when it's a video or nothing. */
    val song = mutableStateOf<Item?>(null)
    var player: ExoPlayer? = null
        private set
    private var next: List<Item> = emptyList()
    /** The song waiting in the player after this one. */
    private var following: Item? = null
    private val scope = MainScope()
    private var loading: Job? = null
    private var preparing: Job? = null
    private var session: ListenableFuture<MediaController>? = null

    private val listener = object : Player.Listener {
        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            val arrived = following?.takeIf { mediaItem?.mediaId == it.id.toString() } ?: return
            val p = player ?: return
            song.value?.let { left ->
                val played = reason == Player.MEDIA_ITEM_TRANSITION_REASON_AUTO
                saveProgress(left, ((left.duration ?: 0.0) * 1000).toLong(), if (played) null else lastPosition)
            }
            if (p.currentMediaItemIndex > 0) p.removeMediaItems(0, p.currentMediaItemIndex)
            following = null
            arrive(p, arrived)
            lookAhead(arrived)
        }

        override fun onPlaybackStateChanged(state: Int) {
            if (state == Player.STATE_ENDED) song.value?.let { saveProgress(it, player?.duration ?: 0) }
        }

        override fun onPositionDiscontinuity(oldPosition: Player.PositionInfo, newPosition: Player.PositionInfo, reason: Int) {
            lastPosition = oldPosition.positionMs
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            if (isPlaying) failures = 0
        }

        /**
         * A song that can't play (say, a damaged file) shouldn't end the music. This has to happen here and now: if the
         * player sits stopped for even a moment, the app loses its background permission and may not get it back.
         */
        override fun onPlayerError(error: PlaybackException) {
            val p = player ?: return
            if (song.value == null || !PaddockApp.instance.settings.autoplay || failures >= 3) return       // a few in a row: the server is probably gone
            when {
                following != null && p.hasNextMediaItem() -> { failures++; skip() }
                preparing?.isActive == true -> p.prepare()      // what's next is still being worked out: stay busy until it's known
                nextSong() != null -> { failures++; skip() }
            }
        }
    }
    private var lastPosition = 0L
    private var failures = 0

    private fun arrive(p: ExoPlayer, item: Item) {
        NowPlaying.queue.removeAll { it.id == item.id }
        song.value = item
        NowPlaying.itemId = item.id
        decorate(p, item)
    }

    /** The player screen hands a song over once it's playing. */
    fun begin(context: Context, p: ExoPlayer, item: Item, upNext: List<Item>) {
        if (player !== p) {
            player?.removeListener(listener)
            player = p
            p.addListener(listener)
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
        prepareFollowing()
    }

    /** A video or a watch party is taking over the player. */
    fun release() {
        song.value = null
        next = emptyList()
        following = null
        loading?.cancel()
        preparing?.cancel()
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
        val p = player ?: return
        if (following != null && p.hasNextMediaItem()) {
            lastPosition = p.currentPosition
            p.seekToNextMediaItem()
            if (p.playbackState == Player.STATE_IDLE) p.prepare()
            p.playWhenReady = true
        } else {
            nextSong()?.let { play(it) }
        }
    }

    fun play(item: Item) {
        val p = player ?: return
        loading?.cancel()
        preparing?.cancel()
        loading = scope.launch {
            val media = mediaFor(item) ?: return@launch
            following = null
            p.setMediaItem(media, 0L)
            p.prepare()
            p.playWhenReady = true
            arrive(p, item)
            lookAhead(item)
        }
    }

    /** What the queue or the screen says is next has changed: put that in the player. */
    fun prepareFollowing() {
        val p = player ?: return
        if (song.value == null) return      // a video has the player: songs queued meanwhile wait for it to finish
        val app = PaddockApp.instance
        val target = if (app.settings.autoplay) nextSong() else null
        val waiting = p.mediaItemCount > p.currentMediaItemIndex + 1
        if (target?.id == following?.id && waiting == (target != null)) return
        preparing?.cancel()
        if (waiting) p.removeMediaItems(p.currentMediaItemIndex + 1, p.mediaItemCount)
        following = null
        if (target == null) return
        preparing = scope.launch {
            val media = mediaFor(target) ?: return@launch
            p.addMediaItem(media)
            following = target
        }
    }

    private fun lookAhead(item: Item) {
        next = emptyList()
        preparing?.cancel()
        preparing = scope.launch {
            next = runCatching { PaddockApp.instance.api.get<List<Item>>("/api/items/${item.id}/up_next") }.getOrDefault(emptyList())
            prepareFollowing()
        }
    }

    private suspend fun mediaFor(item: Item): MediaItem? {
        val app = PaddockApp.instance
        val plan = runCatching { app.api.get<Playback>("/api/items/${item.id}/playback?codecs=${Players.codecs()}") }.getOrNull() ?: return null
        val quality = plan.qualities.firstOrNull { it.id == if (plan.mode == "direct") "original" else "auto" } ?: plan.qualities.firstOrNull() ?: return null
        return app.players.mediaItem(app.api, quality, plan, item)
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
            if (now.mediaId != item.id.toString()) return@launch
            p.replaceMediaItem(p.currentMediaItemIndex, now.buildUpon().setMediaMetadata(
                now.mediaMetadata.buildUpon().setArtworkData(cover, MediaMetadata.PICTURE_TYPE_FRONT_COVER).build()).build())
        }
    }

    /** With no position it counts as played to the end. */
    private fun saveProgress(item: Item, durationMs: Long, positionMs: Long? = null) {
        val dur = durationMs.takeIf { it > 0 }?.div(1000.0) ?: return
        scope.launch {
            runCatching {
                PaddockApp.instance.api.post<JsonObject>("/api/items/${item.id}/progress", buildJsonObject { put("position", (positionMs?.div(1000.0)) ?: dur); put("duration", dur) })
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
                if (player.playbackState == Player.STATE_IDLE) player.prepare()     // it stopped on an error: this is the retry
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
