package app.paddock.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Secrets (device token, tunnel private key) are AES-GCM encrypted with a non-exportable Android Keystore key. */
object Secure {
    private const val ALIAS = "paddock-secrets-v1"

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return gen.generateKey()
    }

    fun encrypt(plain: String): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, key())
        val out = c.iv + c.doFinal(plain.toByteArray())
        return Base64.encodeToString(out, Base64.NO_WRAP)
    }

    fun decrypt(blob: String): String {
        val raw = Base64.decode(blob, Base64.NO_WRAP)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, raw, 0, 12))
        return String(c.doFinal(raw, 12, raw.size - 12))
    }
}

/** One paired Paddock server. A phone can be paired with several (home, cabin, a friend's). */
@Serializable
data class ServerProfile(
    val id: String,
    val name: String,
    val lanUrls: List<String>,
    val caPem: String,
    val pin: String,
    val tokenEnc: String,
    val deviceId: Int,
    val user: User,
    val wg: WgProfile = WgProfile(),
    val wgPrivateKeyEnc: String? = null,
) {
    val token: String get() = Secure.decrypt(tokenEnc)
    val wgPrivateKey: String? get() = wgPrivateKeyEnc?.let { Secure.decrypt(it) }
}

class ServerStore(context: Context) {
    private val prefs = context.getSharedPreferences("servers", Context.MODE_PRIVATE)
    private val _servers = MutableStateFlow(load())
    private val _activeId = MutableStateFlow(prefs.getString("active", null) ?: _servers.value.firstOrNull()?.id)
    val servers: StateFlow<List<ServerProfile>> = _servers
    val activeId: StateFlow<String?> = _activeId

    val active: ServerProfile? get() = _servers.value.firstOrNull { it.id == _activeId.value } ?: _servers.value.firstOrNull()

    private fun load(): List<ServerProfile> = prefs.getString("list", null)?.let {
        runCatching { PaddockJson.decodeFromString<List<ServerProfile>>(it) }.getOrNull()
    } ?: emptyList()

    private fun save(list: List<ServerProfile>) {
        prefs.edit().putString("list", PaddockJson.encodeToString(list)).apply()
        _servers.value = list
    }

    fun add(p: ServerProfile) {
        save(_servers.value.filterNot { it.id == p.id } + p)
        setActive(p.id)
    }

    fun update(p: ServerProfile) = save(_servers.value.map { if (it.id == p.id) p else it })

    fun remove(id: String) {
        save(_servers.value.filterNot { it.id == id })
        if (_activeId.value == id) setActive(_servers.value.firstOrNull()?.id)
    }

    fun setActive(id: String?) {
        prefs.edit().putString("active", id).apply()
        _activeId.value = id
    }
}

/** Plain user preferences (nothing secret). */
class Settings(context: Context) {
    private val p = context.getSharedPreferences("settings", Context.MODE_PRIVATE)
    var backupEnabled: Boolean get() = p.getBoolean("backup", false); set(v) = p.edit().putBoolean("backup", v).apply()
    var backupVideos: Boolean get() = p.getBoolean("backup_videos", true); set(v) = p.edit().putBoolean("backup_videos", v).apply()
    var backupWifiOnly: Boolean get() = p.getBoolean("wifi_only", true); set(v) = p.edit().putBoolean("wifi_only", v).apply()
    var backupChargingOnly: Boolean get() = p.getBoolean("charging_only", false); set(v) = p.edit().putBoolean("charging_only", v).apply()
    var backupRemote: Boolean get() = p.getBoolean("backup_remote", false); set(v) = p.edit().putBoolean("backup_remote", v).apply()
    var cacheGb: Int get() = p.getInt("cache_gb", 4); set(v) = p.edit().putInt("cache_gb", v).apply()
    /** "smart" = original file when the phone can play it, otherwise the adaptive (Auto) stream. */
    var quality: String get() = p.getString("quality_v2", "smart")!!; set(v) = p.edit().putString("quality_v2", v).apply()
    var captions: String get() = p.getString("captions", "off")!!; set(v) = p.edit().putString("captions", v).apply()
    var autoplay: Boolean get() = p.getBoolean("autoplay", true); set(v) = p.edit().putBoolean("autoplay", v).apply()
    var theme: String get() = p.getString("theme", "system")!!; set(v) = p.edit().putString("theme", v).apply()
}
