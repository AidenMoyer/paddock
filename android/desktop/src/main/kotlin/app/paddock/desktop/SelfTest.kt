package app.paddock.desktop

import app.paddock.data.HomeFeed
import app.paddock.data.Item
import app.paddock.data.Playback
import app.paddock.live.VoiceWire
import io.github.jaredmdobson.concentus.OpusApplication
import io.github.jaredmdobson.concentus.OpusDecoder
import io.github.jaredmdobson.concentus.OpusEncoder
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlin.math.PI
import kotlin.math.sin
import kotlin.system.exitProcess

/**
 * `gradlew :desktop:selfTest -Pcode=PADDOCK-…`: pairs with a server, then checks every layer the app needs
 * without opening a window (home feed, thumbnails through Skia, a video through the media relay into libVLC,
 * and an Opus round trip). Used to verify builds; it registers a device called after this computer.
 */
fun main(args: Array<String>): Unit = runBlocking {
    val code = args.firstOrNull() ?: System.getenv("PADDOCK_CODE") ?: run { println("usage: selfTest <computer code>"); exitProcess(2) }
    var ok = true
    fun check(what: String, pass: Boolean, detail: String = "") { println("${if (pass) "PASS" else "FAIL"}  $what $detail"); ok = ok && pass }

    val s = Pairing.pair(code)
    check("pairing", true, "${s.name} as ${s.user.username}, tunnel ${if (Pairing.tunnelConfig(s) != null) "file ready" else "off"}")
    Connection.use(s)
    check("connection", Connection.ensure() != null, "${Connection.state.value.mode} ${Connection.state.value.baseUrl}")
    val home = Connection.api.get<HomeFeed>("/api/home")
    check("home feed", home.rows.isNotEmpty(), home.rows.joinToString { it.key })
    val video = Connection.api.get<List<Item>>("/api/items?kind=video&limit=20").firstOrNull { (it.duration ?: 0.0) > 5 }
    check("a video to play", video != null, video?.title ?: "")
    if (video != null) {
        val thumb = Images.load("/api/items/${video.id}/thumb")
        check("thumbnail decode", thumb != null, "${thumb?.width}x${thumb?.height}")
        val plan = Connection.api.get<Playback>("/api/items/${video.id}/playback?codecs=h264,aac,mp3,mp4")
        check("libVLC", Vlc.available, Vlc.MISSING.takeIf { !Vlc.available } ?: "")
        val player = VideoPlayer(video = true)
        player.open(MediaProxy.url(plan.hls ?: plan.url!!))
        repeat(60) { if (player.position.value > 3 || player.error.value != null) return@repeat; delay(500) }
        check("HLS playback through the relay", player.position.value > 1 && player.error.value == null,
            "position ${"%.1f".format(player.position.value)} s of ${"%.1f".format(player.duration.value)} s ${player.error.value ?: ""}")
        val f = player.frame.value
        check("video frames", f != null, "${f?.width}x${f?.height}")
        player.seek(player.duration.value / 2)
        delay(4000)
        check("seeking", player.position.value > player.duration.value / 2 - 2, "now at ${"%.1f".format(player.position.value)} s")
        player.dispose()
    }
    val enc = OpusEncoder(VoiceWire.RATE, 1, OpusApplication.OPUS_APPLICATION_VOIP).apply { setBitrate(24_000) }
    val dec = OpusDecoder(VoiceWire.RATE, 1)
    val tone = ShortArray(VoiceWire.FRAME) { (sin(2 * PI * 440 * it / VoiceWire.RATE) * 8000).toInt().toShort() }
    val packet = ByteArray(1275)
    val n = enc.encode(tone, 0, VoiceWire.FRAME, packet, 0, packet.size)
    val pcm = ShortArray(VoiceWire.FRAME)
    val m = dec.decode(packet, 0, n, pcm, 0, VoiceWire.FRAME, false)
    check("Opus voice round trip", n in 10..400 && m == VoiceWire.FRAME, "$n bytes per 20 ms")
    println(if (ok) "ALL PASSED" else "SOME CHECKS FAILED")
    Runtime.getRuntime().halt(if (ok) 0 else 1)      // libVLC's threads make a normal JVM shutdown slow
}
