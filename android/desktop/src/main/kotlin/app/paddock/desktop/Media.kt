package app.paddock.desktop

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.toComposeImageBitmap
import androidx.compose.ui.layout.ContentScale
import app.paddock.ui.theme.LocalPaddock
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Request
import java.net.InetAddress
import java.net.InetSocketAddress
import java.security.SecureRandom
import java.util.Base64
import java.util.concurrent.Executors

/** Thumbnails and photos, fetched through the pinned client and kept in a small in-memory cache. */
object Images {
    private class Lru(val max: Int) : LinkedHashMap<String, ImageBitmap>(64, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, ImageBitmap>?) = size > max
    }
    private val thumbs = Lru(400)
    private val large = Lru(12)

    fun cached(path: String): ImageBitmap? = synchronized(this) { thumbs[path] ?: large[path] }

    suspend fun load(path: String, big: Boolean = false): ImageBitmap? {
        cached(path)?.let { return it }
        return withContext(Dispatchers.IO) {
            val base = Connection.ensure() ?: return@withContext null
            val client = Connection.client ?: return@withContext null
            runCatching {
                client.newCall(Request.Builder().url(base + path).build()).execute().use { r ->
                    if (!r.isSuccessful) return@use null
                    val bytes = r.body?.bytes() ?: return@use null
                    org.jetbrains.skia.Image.makeFromEncoded(bytes).toComposeImageBitmap()
                }
            }.getOrNull()?.also { synchronized(this@Images) { (if (big) large else thumbs)[path] = it } }
        }
    }
}

@Composable
fun RemoteImage(path: String?, modifier: Modifier = Modifier, contentScale: ContentScale = ContentScale.Crop, big: Boolean = false) {
    val p = LocalPaddock.current
    val img by produceState(path?.let { Images.cached(it) }, path) { if (path != null && value == null) value = Images.load(path, big) }
    val bmp = img
    if (bmp != null) Image(bmp, null, modifier, contentScale = contentScale)
    else Box(modifier.background(p.surface2))
}

/**
 * The video player can't use the pinned client directly, so it reads from this loopback-only relay, which
 * forwards each request (with Range) to the server through the pinned, authenticated client. A random
 * secret in every URL keeps other programs on the computer from using it.
 */
object MediaProxy {
    private val secret = ByteArray(18).also { SecureRandom().nextBytes(it) }.let { Base64.getUrlEncoder().withoutPadding().encodeToString(it) }
    private val server: HttpServer = HttpServer.create(InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 64).apply {
        executor = Executors.newCachedThreadPool { r -> Thread(r, "paddock-media").apply { isDaemon = true } }
        createContext("/") { ex -> runCatching { handle(ex) }.onFailure { runCatching { ex.close() } } }
        start()
    }

    fun url(path: String): String = "http://127.0.0.1:${server.address.port}/$secret$path"

    private fun handle(ex: HttpExchange) {
        val raw = ex.requestURI.rawPath
        if (!raw.startsWith("/$secret/") || ex.requestMethod !in setOf("GET", "HEAD")) { ex.sendResponseHeaders(404, -1); ex.close(); return }
        val path = raw.removePrefix("/$secret") + (ex.requestURI.rawQuery?.let { "?$it" } ?: "")
        val base = Connection.state.value.baseUrl
        val client = Connection.client
        if (base == null || client == null) { ex.sendResponseHeaders(503, -1); ex.close(); return }
        val req = Request.Builder().url(base + path).apply {
            ex.requestHeaders.getFirst("Range")?.let { header("Range", it) }
            if (ex.requestMethod == "HEAD") head()
        }.build()
        client.newCall(req).execute().use { r ->
            for (h in listOf("Content-Type", "Content-Range", "Accept-Ranges", "Cache-Control")) r.header(h)?.let { ex.responseHeaders.set(h, it) }
            val body = r.body
            val len = body?.contentLength() ?: -1
            if (ex.requestMethod == "HEAD") { ex.sendResponseHeaders(r.code, -1); ex.close(); return }
            ex.sendResponseHeaders(r.code, if (len >= 0) (if (len == 0L) -1 else len) else 0)
            ex.responseBody.use { out -> body?.byteStream()?.use { it.copyTo(out, 256 * 1024) } }
        }
    }
}
