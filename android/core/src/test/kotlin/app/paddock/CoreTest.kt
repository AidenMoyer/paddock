package app.paddock

import app.paddock.data.PaddockJson
import app.paddock.live.PartyClock
import app.paddock.live.PartyState
import app.paddock.live.VoiceGate
import app.paddock.live.VoiceMixer
import app.paddock.live.VoiceWire
import app.paddock.net.MediaUris
import app.paddock.net.PairCode
import java.util.Base64
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class CoreTest {
    private val json = """{"v":1,"app":"paddock","name":"Pit Lane","lan":["https://192.168.1.5:443"],"pin":"sha256/abc=","code":"xyz","wg":true}"""

    @Test fun onlyRealMediaStoreRowsAreAskedForTheOriginal() {
        assertTrue(MediaUris.acceptsRequireOriginal("media", "/external/images/media/1000012345"))
        assertTrue(MediaUris.acceptsRequireOriginal("media", "/external_primary/video/media/7"))
        assertFalse(MediaUris.acceptsRequireOriginal("media", "/picker/0/com.android.providers.media.photopicker/media/1000012345"))
        assertFalse(MediaUris.acceptsRequireOriginal("media", "/picker_get_content/0/com.android.providers.media.photopicker/media/1000012345"))
        assertFalse(MediaUris.acceptsRequireOriginal("com.android.providers.media.documents", "/document/image:1000012345"))
        assertFalse(MediaUris.acceptsRequireOriginal(null, null))
    }

    @Test fun pairingCodesWorkAsQrTextOrTypedCode() {
        assertEquals("xyz", PairCode.parse(json).code)
        val typed = "PADDOCK-" + Base64.getUrlEncoder().withoutPadding().encodeToString(json.toByteArray())
        val p = PairCode.parse("  ${typed.lowercase().replaceFirst("paddock-", "Paddock-")}  ".let { typed.chunked(40).joinToString("\n") })
        assertEquals("Pit Lane", p.name)
        assertTrue(p.wg)
        assertFailsWith<IllegalArgumentException> { PairCode.parse("PADDOCK-not-base64!!") }
        assertFailsWith<IllegalArgumentException> { PairCode.parse("""{"app":"other","code":"x","lan":["a"],"pin":"p"}""") }
    }

    @Test fun voiceFramesRoundTrip() {
        val f = VoiceWire.frame(VoiceWire.CODEC_OPUS, 0x1234, byteArrayOf(9, 8, 7))
        assertEquals(listOf<Byte>(1, 0x12, 0x34, 9, 8, 7), f.toList())
        // what the server sends on: [speaker u16][codec][seq u16][payload]
        val incoming = VoiceWire.parse(byteArrayOf(0, 5) + f)!!
        assertEquals(5, incoming.speaker); assertEquals(VoiceWire.CODEC_OPUS, incoming.codec); assertEquals(0x1234, incoming.seq)
        assertEquals(listOf<Byte>(9, 8, 7), incoming.payload.toList())
        val pcm = ShortArray(VoiceWire.FRAME) { (it % 100).toShort() }
        assertEquals(VoiceWire.FRAME, VoiceWire.fromPcm16(VoiceWire.toPcm16(pcm)).size)
    }

    @Test fun mixerWaitsForACushionThenMixes() {
        val m = VoiceMixer()
        val out = ShortArray(VoiceWire.FRAME)
        m.add(1, ShortArray(VoiceWire.FRAME) { 1000 })
        m.mix(out)
        assertEquals(0, out[0].toInt())                       // not started: under 60 ms buffered
        repeat(3) { m.add(1, ShortArray(VoiceWire.FRAME) { 1000 }); m.add(2, ShortArray(VoiceWire.FRAME) { 32000 }) }
        m.mix(out)
        assertEquals(32767, out[0].toInt())                   // two speakers summed, clipped
    }

    @Test fun gateOpensOnSpeechAndStaysQuietWhenMuted() {
        val g = VoiceGate()
        val silence = ShortArray(VoiceWire.FRAME)
        val loud = ShortArray(VoiceWire.FRAME) { if (it % 2 == 0) 12000 else -12000 }
        assertFalse(g.active(silence, muted = false, now = 0))
        assertTrue(g.active(loud, muted = false, now = 20))
        assertTrue(g.active(silence, muted = false, now = 300))    // hang time keeps the word's tail
        assertFalse(g.active(silence, muted = false, now = 900))
        assertFalse(g.active(loud, muted = true, now = 1000))
    }

    @Test fun partyClockFollowsTheServer() {
        val c = PartyClock()
        val now = System.currentTimeMillis() / 1000.0
        c.hello(now + 100)                                    // server clock is 100 s ahead
        assertTrue(kotlin.math.abs(c.now() - (now + 100)) < 0.5)
        val s = PaddockJson.decodeFromString<PartyState>("""{"item_id":1,"playing":true,"position":10,"at":${now + 98},"rate":1}""")
        assertTrue(kotlin.math.abs(c.target(s) - 12.0) < 0.5)
        val paused = s.copy(playing = false)
        assertEquals(10.0, c.target(paused))
    }
}
