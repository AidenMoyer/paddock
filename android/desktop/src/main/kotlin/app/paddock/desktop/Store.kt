package app.paddock.desktop

import app.paddock.data.PaddockJson
import app.paddock.data.User
import app.paddock.data.WgProfile
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.PosixFilePermissions

@Serializable
data class DesktopServer(
    val id: String,
    val name: String,
    val lanUrls: List<String>,
    val caPem: String,
    val pin: String,
    val token: String,
    val deviceId: Int,
    val user: User,
    val wg: WgProfile = WgProfile(),
    val wgPrivateKey: String? = null,
)

@Serializable
data class DesktopSettings(
    val theme: String = "system",
    val activeId: String? = null,
    val volume: Float = 1f,
    val autoplay: Boolean = true,
)

@Serializable
data class StoreFile(val servers: List<DesktopServer> = emptyList(), val settings: DesktopSettings = DesktopSettings())

/**
 * Paired servers and settings, in the user's own config folder (%APPDATA%\Paddock or ~/.config/paddock).
 * The device token in here is what lets this computer in, so the file is readable by this user only.
 */
object Store {
    private val windows = System.getProperty("os.name").lowercase().contains("win")
    val dir: File = if (windows) File(System.getenv("APPDATA") ?: System.getProperty("user.home"), "Paddock")
    else File(System.getenv("XDG_CONFIG_HOME") ?: (System.getProperty("user.home") + "/.config"), "paddock")
    private val file = File(dir, "paddock.json")
    private val _state = MutableStateFlow(load())
    val state: StateFlow<StoreFile> = _state

    val active: DesktopServer? get() = _state.value.let { s -> s.servers.firstOrNull { it.id == s.settings.activeId } ?: s.servers.firstOrNull() }
    val settings: DesktopSettings get() = _state.value.settings

    private fun load(): StoreFile = runCatching { PaddockJson.decodeFromString<StoreFile>(file.readText()) }.getOrDefault(StoreFile())

    @Synchronized fun update(change: (StoreFile) -> StoreFile) {
        val next = change(_state.value)
        _state.value = next
        dir.mkdirs()
        val tmp = File(dir, "paddock.json.tmp")
        tmp.writeText(PaddockJson.encodeToString(next))
        if (!windows) runCatching { Files.setPosixFilePermissions(tmp.toPath(), PosixFilePermissions.fromString("rw-------")) }
        Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
    }

    fun addServer(s: DesktopServer) = update { f -> f.copy(servers = f.servers.filter { it.pin != s.pin } + s, settings = f.settings.copy(activeId = s.id)) }
    fun removeServer(id: String) = update { f -> f.copy(servers = f.servers.filter { it.id != id }) }
    fun setActive(id: String) = update { f -> f.copy(settings = f.settings.copy(activeId = id)) }
    fun setSettings(change: (DesktopSettings) -> DesktopSettings) = update { f -> f.copy(settings = change(f.settings)) }
}
