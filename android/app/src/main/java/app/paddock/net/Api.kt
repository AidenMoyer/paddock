package app.paddock.net

import app.paddock.data.PaddockJson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException

class ApiException(val code: Int, message: String) : IOException(message)

/** Small JSON client over the active, pinned connection. */
class Api(private val conn: ConnectionManager) {
    private val jsonType = "application/json".toMediaType()

    suspend fun base(): String = conn.ensure() ?: throw ApiException(0, conn.state.value.detail.ifBlank { "Not connected" })

    suspend fun url(path: String): String = base() + path

    suspend fun raw(method: String, path: String, body: RequestBody? = null, timeoutS: Long = 60): String = withContext(Dispatchers.IO) {
        val client = conn.client ?: throw ApiException(0, "No server")
        val req = Request.Builder().url(base() + path).method(method, body ?: if (method == "POST" || method == "PUT" || method == "PATCH") "{}".toRequestBody(jsonType) else null).build()
        val c = if (timeoutS != 60L) client.newBuilder().readTimeout(timeoutS, java.util.concurrent.TimeUnit.SECONDS).build() else client
        c.newCall(req).execute().use { r ->
            val text = r.body?.string().orEmpty()
            if (!r.isSuccessful) {
                val detail = runCatching { PaddockJson.parseToJsonElement(text).jsonObject["detail"]?.jsonPrimitive?.content }.getOrNull()
                throw ApiException(r.code, detail ?: "HTTP ${r.code}")
            }
            text
        }
    }

    suspend inline fun <reified T> get(path: String): T = PaddockJson.decodeFromString(raw("GET", path))

    suspend inline fun <reified T> post(path: String, body: JsonElement = JsonObject(emptyMap()), timeoutS: Long = 60): T =
        PaddockJson.decodeFromString(raw("POST", path, body.toString().toRequestBody("application/json".toMediaType()), timeoutS))

    suspend inline fun <reified T> patch(path: String, body: JsonElement): T =
        PaddockJson.decodeFromString(raw("PATCH", path, body.toString().toRequestBody("application/json".toMediaType())))

    suspend fun delete(path: String) { raw("DELETE", path) }

    // URLs handed to the image loader and the player (they go through the same pinned client)
    fun thumb(id: Int): String = (conn.state.value.baseUrl ?: "") + "/api/items/$id/thumb"
    fun preview(id: Int): String = (conn.state.value.baseUrl ?: "") + "/api/items/$id/preview"
    fun absolute(path: String): String = if (path.startsWith("http")) path else (conn.state.value.baseUrl ?: "") + path
}
