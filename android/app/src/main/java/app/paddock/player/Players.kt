package app.paddock.player

import android.content.Context
import android.media.MediaCodecList
import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.CacheKeyFactory
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import app.paddock.PaddockApp
import app.paddock.data.Playback
import app.paddock.data.Quality
import okhttp3.OkHttpClient

/**
 * ExoPlayer wired to Paddock: pinned TLS + device token on every request, and an on-device
 * LRU cache so seeking back and re-watching never re-download. Cache keys drop the host, so the
 * same video is a cache hit whether you're at home or on the tunnel.
 */
@OptIn(UnstableApi::class)
class Players(private val context: Context) {
    private var cache: SimpleCache? = null

    private fun cache(): SimpleCache {
        cache?.let { return it }
        val gb = PaddockApp.instance.settings.cacheGb.coerceIn(1, 64)
        return SimpleCache(context.cacheDir.resolve("media"), LeastRecentlyUsedCacheEvictor(gb * 1_000_000_000L),
            StandaloneDatabaseProvider(context)).also { cache = it }
    }

    private var shared: ExoPlayer? = null
    private var sharedClient: OkHttpClient? = null

    /** Players must be released on the main thread; server switches can happen from anywhere. */
    fun reset() = android.os.Handler(android.os.Looper.getMainLooper()).post { shared?.release(); shared = null; sharedClient = null }

    /** One player for the whole app: switching videos reuses decoders and buffers instead of rebuilding them. */
    fun player(client: OkHttpClient): ExoPlayer {
        if (shared == null || sharedClient !== client) {
            shared?.release()
            shared = create(client)
            sharedClient = client
        }
        return shared!!
    }

    fun create(client: OkHttpClient): ExoPlayer {
        val http = OkHttpDataSource.Factory(client).setUserAgent("Paddock-Android")
        val cached = CacheDataSource.Factory()
            .setCache(cache())
            .setUpstreamDataSourceFactory(http)
            // one key per resource (host dropped so LAN and tunnel share it); never include the byte position
            .setCacheKeyFactory(CacheKeyFactory { spec -> spec.uri.path + "?" + (spec.uri.query ?: "") })
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
        val load = DefaultLoadControl.Builder()
            .setBufferDurationsMs(15_000, 120_000, 1_500, 3_000)   // generous forward buffer for smooth playback
            .setBackBuffer(60_000, true)
            .build()
        return ExoPlayer.Builder(context)
            .setMediaSourceFactory(DefaultMediaSourceFactory(cached))
            .setLoadControl(load)
            .setSeekBackIncrementMs(10_000)
            .setSeekForwardIncrementMs(10_000)
            .setHandleAudioBecomingNoisy(true)
            .build()
    }

    fun mediaItem(api: app.paddock.net.Api, q: Quality, plan: Playback, title: String): MediaItem {
        val subs = plan.subtitles.map {
            MediaItem.SubtitleConfiguration.Builder(Uri.parse(api.absolute(it.url)))
                .setMimeType(MimeTypes.TEXT_VTT).setLabel(it.label).setLanguage(it.label.take(2))
                .setSelectionFlags(0).build()
        }
        val b = MediaItem.Builder().setUri(api.absolute(q.url)).setSubtitleConfigurations(subs)
            .setMediaMetadata(androidx.media3.common.MediaMetadata.Builder().setTitle(title).build())
        if (q.type == "hls") b.setMimeType(MimeTypes.APPLICATION_M3U8)
        return b.build()
    }

    companion object {
        /** What this phone decodes in hardware/software — the server direct-plays anything in this list. */
        fun codecs(): String {
            val types = MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos.filter { !it.isEncoder }.flatMap { it.supportedTypes.toList() }.toSet()
            val out = mutableListOf("h264", "aac", "mp3", "opus", "vorbis", "flac", "mkv", "ts")
            if ("video/hevc" in types) out += "hevc"
            if ("video/av01" in types) out += "av1"
            if ("video/x-vnd.on2.vp9" in types) out += "vp9"
            if ("video/x-vnd.on2.vp8" in types) out += "vp8"
            if ("audio/ac3" in types) out += "ac3"
            if ("audio/eac3" in types) out += "eac3"
            return out.joinToString(",")
        }

        const val UNSET = C.TIME_UNSET
    }
}
