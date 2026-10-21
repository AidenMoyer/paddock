package app.paddock.net

import app.paddock.data.PaddockJson
import app.paddock.data.PairPayload
import java.util.Base64

object PairCode {
    const val PREFIX = "PADDOCK-"

    /** The QR code's JSON, or the copyable form shown for computers (PADDOCK-<base64url of the JSON>). */
    fun parse(text: String): PairPayload {
        val t = text.trim()
        val json = if (t.startsWith(PREFIX, ignoreCase = true)) {
            runCatching { String(Base64.getUrlDecoder().decode(t.substring(PREFIX.length).filter { !it.isWhitespace() })) }.getOrNull()
        } else t
        val p = json?.let { runCatching { PaddockJson.decodeFromString<PairPayload>(it) }.getOrNull() }
            ?: throw IllegalArgumentException("That isn't a Paddock pairing code.")
        if (p.app != "paddock" || p.pin.isNullOrBlank() || p.lan.isEmpty()) throw IllegalArgumentException("That isn't a Paddock pairing code.")
        return p
    }
}
