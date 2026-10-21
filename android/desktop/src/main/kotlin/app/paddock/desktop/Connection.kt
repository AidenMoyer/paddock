package app.paddock.desktop

import app.paddock.net.ApiException
import app.paddock.net.JsonApi
import app.paddock.net.PinnedTls
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

enum class Mode { Connecting, Home, Tunnel, Offline }

data class ConnState(val mode: Mode = Mode.Connecting, val baseUrl: String? = null, val detail: String = "")

/**
 * How this computer reaches its server: the home addresses from pairing first, then the server's tunnel
 * address (reachable when the WireGuard app has the tunnel file from Settings imported). Every request
 * goes through a client that trusts only that server's own certificate authority.
 */
object Connection {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val lock = Mutex()
    private val _state = MutableStateFlow(ConnState())
    val state: StateFlow<ConnState> = _state
    @Volatile var client: OkHttpClient? = null
        private set
    @Volatile private var server: DesktopServer? = null

    val api = JsonApi(clientOf = { client }, baseOf = {
        ensure() ?: throw ApiException(0, _state.value.detail.ifBlank { "Can't reach your server" })
    })

    init {
        scope.launch {
            while (true) {
                delay(20_000)
                if (server != null && _state.value.mode == Mode.Offline) probe()
            }
        }
    }

    fun use(s: DesktopServer?) {
        server = s
        client = s?.let { PinnedTls.client(it.caPem, it.token, "Paddock-Desktop").build() }
        _state.value = ConnState()
        if (s != null) refresh()
    }

    fun refresh() { scope.launch { probe() } }

    /** A base URL that answered recently, or a fresh probe. */
    suspend fun ensure(): String? = _state.value.baseUrl ?: probe()

    fun tunnelUrl(s: DesktopServer): String? =
        if (s.wg.enabled && s.wg.server_ip != null) "https://${s.wg.server_ip}:${s.wg.api_port ?: 8443}" else null

    private suspend fun probe(): String? = lock.withLock {
        val s = server ?: return null
        val c = client ?: return null
        val quick = c.newBuilder().connectTimeout(2, TimeUnit.SECONDS).readTimeout(3, TimeUnit.SECONDS).build()
        val tunnel = tunnelUrl(s)
        for (url in s.lanUrls + listOfNotNull(tunnel)) {
            val ok = withContext(Dispatchers.IO) {
                runCatching { quick.newCall(Request.Builder().url("$url/api/health").build()).execute().use { it.isSuccessful } }.getOrDefault(false)
            }
            if (ok) {
                _state.value = ConnState(if (url == tunnel) Mode.Tunnel else Mode.Home, url)
                return url
            }
        }
        _state.value = ConnState(Mode.Offline, null,
            if (tunnel != null) "Can't reach ${s.name}. Away from home? Turn on the Paddock tunnel in the WireGuard app."
            else "Can't reach ${s.name}. Make sure this computer is on your home network.")
        return null
    }

    /** Called when a request fails: forget the address so the next one probes again. */
    fun dropped() {
        if (_state.value.baseUrl != null) { _state.value = _state.value.copy(baseUrl = null, mode = Mode.Connecting); refresh() }
    }
}
