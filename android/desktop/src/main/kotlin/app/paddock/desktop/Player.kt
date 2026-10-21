package app.paddock.desktop

import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asComposeImageBitmap
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.jetbrains.skia.Bitmap
import org.jetbrains.skia.ColorAlphaType
import org.jetbrains.skia.ColorType
import org.jetbrains.skia.ImageInfo
import uk.co.caprica.vlcj.binding.support.runtime.RuntimeUtil
import uk.co.caprica.vlcj.factory.MediaPlayerFactory
import uk.co.caprica.vlcj.factory.discovery.NativeDiscovery
import uk.co.caprica.vlcj.player.base.MediaPlayer
import uk.co.caprica.vlcj.player.base.MediaPlayerEventAdapter
import uk.co.caprica.vlcj.player.embedded.EmbeddedMediaPlayer
import uk.co.caprica.vlcj.player.embedded.videosurface.callback.BufferFormat
import uk.co.caprica.vlcj.player.embedded.videosurface.callback.BufferFormatCallback
import uk.co.caprica.vlcj.player.embedded.videosurface.callback.RenderCallback
import uk.co.caprica.vlcj.player.embedded.videosurface.callback.format.RV32BufferFormat
import java.io.File
import java.nio.ByteBuffer
import javax.swing.SwingUtilities

/**
 * Video and music through libVLC: HLS (the server's adaptive ladder), every common file format, accurate seeking,
 * and speed changes that keep the pitch (watch parties steer drift with ±5%). Frames are drawn by Compose, so
 * controls and reactions can sit on top of the picture. [frame] is the latest picture for [VideoView].
 */
class VideoPlayer(video: Boolean = true) {
    private val player: EmbeddedMediaPlayer = (if (video) Vlc.factory else Vlc.audioFactory).mediaPlayers().newEmbeddedMediaPlayer()

    val frame = mutableStateOf<ImageBitmap?>(null)
    val position = MutableStateFlow(0.0)
    val duration = MutableStateFlow(0.0)
    val playing = MutableStateFlow(false)
    val buffering = MutableStateFlow(false)
    val ended = MutableStateFlow(false)
    val error = MutableStateFlow<String?>(null)
    private val _rate = MutableStateFlow(1.0)
    val rate: StateFlow<Double> = _rate
    var onEnded: (() -> Unit)? = null
    var volume = 1.0
        set(v) { field = v; player.audio().setVolume((v * 100).toInt().coerceIn(0, 200)) }
    private var pendingStart = 0.0

    init {
        if (video) Surface().let { s -> player.videoSurface().set(Vlc.factory.videoSurfaces().newVideoSurface(s, s.renderer, true)) }
        player.events().addMediaPlayerEventListener(object : MediaPlayerEventAdapter() {
            override fun timeChanged(mediaPlayer: MediaPlayer, newTime: Long) { position.value = newTime / 1000.0 }
            override fun lengthChanged(mediaPlayer: MediaPlayer, newLength: Long) { if (newLength > 0) duration.value = newLength / 1000.0 }
            override fun playing(mediaPlayer: MediaPlayer) {
                playing.value = true; buffering.value = false
                if (pendingStart > 0.5) { val t = pendingStart; pendingStart = 0.0; mediaPlayer.submit { mediaPlayer.controls().setTime((t * 1000).toLong()) } }
            }
            override fun paused(mediaPlayer: MediaPlayer) { playing.value = false }
            override fun stopped(mediaPlayer: MediaPlayer) { playing.value = false }
            override fun buffering(mediaPlayer: MediaPlayer, newCache: Float) { buffering.value = newCache < 100f }
            override fun finished(mediaPlayer: MediaPlayer) { playing.value = false; ended.value = true; onEnded?.invoke() }
            override fun error(mediaPlayer: MediaPlayer) { error.value = "This can't play right now"; buffering.value = false }
        })
    }

    fun open(url: String, startAt: Double = 0.0, autoplay: Boolean = true) {
        error.value = null; ended.value = false; buffering.value = true; position.value = startAt; duration.value = 0.0
        pendingStart = startAt
        player.submit {
            if (autoplay) player.media().play(url) else player.media().prepare(url)
            player.controls().setRate(_rate.value.toFloat())
        }
    }

    fun play() = player.submit { player.controls().play() }
    fun pause() = player.submit { player.controls().setPause(true) }
    fun toggle() = if (playing.value) pause() else play()
    fun seek(seconds: Double) {
        position.value = seconds
        player.submit { player.controls().setTime((seconds.coerceAtLeast(0.0) * 1000).toLong()) }
    }
    fun setRate(r: Double) { _rate.value = r; player.submit { player.controls().setRate(r.toFloat()) } }

    fun dispose() { player.submit { player.controls().stop(); player.release() } }

    /** Copies each decoded frame (BGRA) into a Skia bitmap that Compose draws. */
    private inner class Surface : BufferFormatCallback {
        private var info: ImageInfo? = null
        private var bytes = ByteArray(0)
        private val bitmap = Bitmap()

        override fun getBufferFormat(sourceWidth: Int, sourceHeight: Int): BufferFormat = RV32BufferFormat(sourceWidth, sourceHeight)
        override fun newFormatSize(bufferWidth: Int, bufferHeight: Int, displayWidth: Int, displayHeight: Int) {}
        override fun allocatedBuffers(buffers: Array<out ByteBuffer>) {
            bytes = ByteArray(buffers[0].capacity())
        }

        val renderer = object : RenderCallback {
            override fun lock(mediaPlayer: MediaPlayer) {}
            override fun unlock(mediaPlayer: MediaPlayer) {}
            override fun display(mediaPlayer: MediaPlayer, nativeBuffers: Array<out ByteBuffer>, format: BufferFormat, displayWidth: Int, displayHeight: Int) {
                val buf = nativeBuffers[0]
                val copy: ByteArray
                synchronized(this@Surface) {
                    if (bytes.size != buf.capacity()) bytes = ByteArray(buf.capacity())
                    buf.rewind(); buf.get(bytes); buf.rewind()
                    copy = bytes
                }
                SwingUtilities.invokeLater {
                    val i = info?.takeIf { it.width == format.width && it.height == format.height }
                        ?: ImageInfo(format.width, format.height, ColorType.BGRA_8888, ColorAlphaType.OPAQUE).also { info = it; bitmap.allocPixels(it) }
                    synchronized(this@Surface) { bitmap.installPixels(i, copy, format.width * 4) }
                    frame.value = bitmap.asComposeImageBitmap()
                }
            }
        }
    }
}

/** Finds libVLC: the copy bundled with the app (Windows), else the system's VLC. */
object Vlc {
    val available: Boolean by lazy {
        val bundled = System.getProperty("compose.application.resources.dir")?.let { File(it, "vlc") }?.takeIf { File(it, if (RuntimeUtil.isWindows()) "libvlc.dll" else "libvlc.so").exists() }
        if (bundled != null) {
            System.setProperty("jna.library.path", bundled.absolutePath)     // libVLC finds plugins/ next to itself
        }
        bundled != null || NativeDiscovery().discover()
    }

    private val args = arrayOf("--quiet", "--no-video-title-show", "--no-osd", "--no-snapshot-preview", "--network-caching=1500",
        "--http-reconnect", "--audio-time-stretch", "--no-metadata-network-access")

    val factory: MediaPlayerFactory by lazy { check(available) { MISSING }; MediaPlayerFactory(*args) }
    val audioFactory: MediaPlayerFactory by lazy { check(available) { MISSING }; MediaPlayerFactory(*args, "--no-video") }

    val MISSING = if (RuntimeUtil.isWindows()) "Video playback isn't available: the bundled player is missing. Reinstall Paddock."
    else "Video playback needs VLC. Install it with: sudo apt install vlc"
}
