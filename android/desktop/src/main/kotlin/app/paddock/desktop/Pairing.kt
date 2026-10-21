package app.paddock.desktop

import app.paddock.data.PaddockJson
import app.paddock.data.PairResponse
import app.paddock.net.ApiException
import app.paddock.net.PairCode
import app.paddock.net.PinnedTls
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.net.InetAddress
import java.security.KeyPairGenerator
import java.util.Base64
import java.util.UUID

object Pairing {
    /** A WireGuard key pair made on this computer (X25519, raw 32-byte keys); only the public half is sent. */
    fun newTunnelKeys(): Pair<String, String> {
        val kp = KeyPairGenerator.getInstance("X25519").generateKeyPair()
        val enc = Base64.getEncoder()
        return enc.encodeToString(kp.private.encoded.takeLast(32).toByteArray()) to enc.encodeToString(kp.public.encoded.takeLast(32).toByteArray())
    }

    private fun computerName(): String {
        val host = runCatching { InetAddress.getLocalHost().hostName }.getOrNull()?.takeIf { it.isNotBlank() }
        val os = System.getProperty("os.name")
        return listOfNotNull(host, os).joinToString(" · ").take(60)
    }

    /** Same as the phone: check the server against the pinned CA from the code, then trade the one-time code for a device token. */
    suspend fun pair(code: String): DesktopServer = withContext(Dispatchers.IO) {
        val p = PairCode.parse(code)
        var lastError: Exception? = null
        for (url in p.lan) {
            try {
                val ca = PinnedTls.bootstrapCa(url, p.pin!!)
                val (priv, pub) = if (p.wg) newTunnelKeys() else (null to null)
                val body = buildJsonObject {
                    put("code", p.code)
                    put("name", computerName())
                    put("platform", if (System.getProperty("os.name").lowercase().contains("win")) "windows" else "linux")
                    if (pub != null) put("wg_public_key", pub)
                }
                val client = PinnedTls.client(ca, agent = "Paddock-Desktop").build()
                val req = Request.Builder().url("$url/api/pair").post(body.toString().toRequestBody("application/json".toMediaType())).build()
                client.newCall(req).execute().use { r ->
                    val text = r.body?.string().orEmpty()
                    if (!r.isSuccessful) {
                        val msg = runCatching { PaddockJson.parseToJsonElement(text).jsonObject["detail"]?.jsonPrimitive?.content }.getOrNull()
                        throw ApiException(r.code, msg ?: "Pairing failed (${r.code})")
                    }
                    val res = PaddockJson.decodeFromString<PairResponse>(text)
                    val lan = res.server.lan.ifEmpty { p.lan }.let { listOf(url) + (it - url) }
                    return@withContext DesktopServer(UUID.randomUUID().toString(), res.server.name, lan, ca, p.pin!!, res.token,
                        res.device_id, res.user, res.wireguard, priv)
                }
            } catch (e: SecurityException) {
                throw e
            } catch (e: ApiException) {
                throw e
            } catch (e: Exception) {
                lastError = e
            }
        }
        throw IllegalStateException("Couldn't reach ${p.name}. Pair while this computer is on your home network. (${lastError?.message})")
    }

    /**
     * The tunnel as a WireGuard file. Importing it into the WireGuard app lets this computer reach Paddock from
     * anywhere; its only route is the server's tunnel address, so nothing else of the computer's traffic uses it.
     */
    fun tunnelConfig(s: DesktopServer): String? {
        val wg = s.wg
        if (!wg.enabled || s.wgPrivateKey == null || wg.address == null || wg.server_public_key == null || wg.endpoint == null) return null
        return buildString {
            appendLine("# Paddock tunnel for ${s.name}: only the Paddock server is reachable through it")
            appendLine("[Interface]")
            appendLine("PrivateKey = ${s.wgPrivateKey}")
            appendLine("Address = ${wg.address}")
            appendLine()
            appendLine("[Peer]")
            appendLine("PublicKey = ${wg.server_public_key}")
            appendLine("Endpoint = ${wg.endpoint}")
            appendLine("AllowedIPs = ${wg.allowed_ips ?: "${wg.server_ip}/32"}")
            appendLine("PersistentKeepalive = ${wg.keepalive ?: 25}")
        }
    }

    fun saveTunnelConfig(s: DesktopServer, to: File): Boolean {
        val text = tunnelConfig(s) ?: return false
        to.writeText(text)
        runCatching { to.setReadable(false, false); to.setReadable(true, true) }
        return true
    }
}
