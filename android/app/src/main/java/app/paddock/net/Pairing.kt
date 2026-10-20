package app.paddock.net

import android.os.Build
import app.paddock.data.PaddockJson
import app.paddock.data.PairPayload
import app.paddock.data.PairResponse
import app.paddock.data.Secure
import app.paddock.data.ServerProfile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.UUID

object Pairing {
    fun parse(qr: String): PairPayload {
        val p = runCatching { PaddockJson.decodeFromString<PairPayload>(qr) }.getOrNull()
            ?: throw IllegalArgumentException("That isn't a Paddock pairing code.")
        if (p.app != "paddock" || p.pin.isNullOrBlank() || p.lan.isEmpty()) throw IllegalArgumentException("That isn't a Paddock pairing code.")
        return p
    }

    /**
     * Pair over the home network: verify the server against the pinned CA from the QR code, generate
     * this phone's tunnel key pair locally, and exchange the one-time code for a long-lived device token.
     */
    suspend fun pair(qr: String): ServerProfile = withContext(Dispatchers.IO) {
        val p = parse(qr)
        var lastError: Exception? = null
        for (url in p.lan) {
            try {
                val ca = PinnedTls.bootstrapCa(url, p.pin!!)
                val (priv, pub) = if (p.wg) PaddockTunnel.newKeyPair() else (null to null)
                val body = buildJsonObject {
                    put("code", p.code)
                    put("name", "${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}".take(60))
                    put("platform", "android")
                    if (pub != null) put("wg_public_key", pub)
                }
                val client = PinnedTls.client(ca).build()
                val req = Request.Builder().url("$url/api/pair").post(body.toString().toRequestBody("application/json".toMediaType())).build()
                client.newCall(req).execute().use { r ->
                    val text = r.body?.string().orEmpty()
                    if (!r.isSuccessful) {
                        val msg = runCatching { PaddockJson.parseToJsonElement(text).let { it as kotlinx.serialization.json.JsonObject }["detail"].toString().trim('"') }.getOrNull()
                        throw ApiException(r.code, msg ?: "Pairing failed (${r.code})")
                    }
                    val res = PaddockJson.decodeFromString<PairResponse>(text)
                    val lan = (res.server.lan.ifEmpty { p.lan }).let { if (url in it) listOf(url) + (it - url) else listOf(url) + it }
                    return@withContext ServerProfile(
                        id = UUID.randomUUID().toString(),
                        name = res.server.name,
                        lanUrls = lan,
                        caPem = ca,
                        pin = p.pin,
                        tokenEnc = Secure.encrypt(res.token),
                        deviceId = res.device_id,
                        user = res.user,
                        wg = res.wireguard,
                        wgPrivateKeyEnc = priv?.let { Secure.encrypt(it) },
                    )
                }
            } catch (e: SecurityException) {
                throw e
            } catch (e: ApiException) {
                throw e
            } catch (e: Exception) {
                lastError = e
            }
        }
        throw IllegalStateException("Couldn't reach ${p.name}. Make sure this phone is on your home Wi-Fi. (${lastError?.message})")
    }
}
