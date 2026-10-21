package app.paddock.net

import app.paddock.data.PaddockJson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

class ApiException(val code: Int, message: String) : IOException(message)

/** JSON over a pinned Paddock connection; each app supplies its client and how to reach the server. */
open class JsonApi(private val clientOf: () -> OkHttpClient?, private val baseOf: suspend () -> String) {
    private val jsonType = "application/json".toMediaType()

    suspend fun base(): String = baseOf()

    suspend fun url(path: String): String = base() + path

    suspend fun raw(method: String, path: String, body: RequestBody? = null, timeoutS: Long = 60): String = withContext(Dispatchers.IO) {
        val client = clientOf() ?: throw ApiException(0, "No server")
        val req = Request.Builder().url(base() + path).method(method, body ?: if (method == "POST" || method == "PUT" || method == "PATCH") "{}".toRequestBody(jsonType) else null).build()
        val c = if (timeoutS != 60L) client.newBuilder().readTimeout(timeoutS, TimeUnit.SECONDS).build() else client
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

    suspend inline fun <reified T> put(path: String, body: JsonElement): T =
        PaddockJson.decodeFromString(raw("PUT", path, body.toString().toRequestBody("application/json".toMediaType())))

    suspend fun delete(path: String) { raw("DELETE", path) }
}
