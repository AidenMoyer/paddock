package app.paddock.net

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import app.paddock.data.ServerProfile
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

enum class Mode { Connecting, Home, Tunnel, NeedsVpnConsent, Offline }

data class ConnState(val mode: Mode = Mode.Connecting, val baseUrl: String? = null, val detail: String = "")

/**
 * Picks the best route to the active server and keeps it current:
 *   1. On the home network → straight to Paddock over HTTPS (full LAN speed, no tunnel).
 *   2. Anywhere else → bring up the app-only WireGuard tunnel and talk to Paddock through it.
 * Re-evaluates whenever the phone's network changes.
 */
class ConnectionManager(private val context: Context, private val tunnel: PaddockTunnel) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val _state = MutableStateFlow(ConnState())
    val state: StateFlow<ConnState> = _state
    private val lock = Mutex()
    private var server: ServerProfile? = null
    var client: OkHttpClient? = null
        private set

    init {
        val cm = context.getSystemService(ConnectivityManager::class.java)
        cm.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) { refresh(delayMs = 800) }
            override fun onLost(network: Network) { refresh(delayMs = 800) }
            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                // Wi-Fi ↔ cellular switches
                if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) != lastWifi) {
                    lastWifi = caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                    refresh(delayMs = 800)
                }
            }
        })
    }

    @Volatile private var lastWifi: Boolean? = null

    fun use(profile: ServerProfile?) {
        server = profile
        client = profile?.let { PinnedTls.client(it.caPem, it.token).build() }
        refresh()
    }

    fun refresh(delayMs: Long = 0) {
        scope.launch {
            if (delayMs > 0) delay(delayMs)
            connect()
        }
    }

    suspend fun ensure(): String? {
        state.value.baseUrl?.let { if (state.value.mode == Mode.Home || state.value.mode == Mode.Tunnel) return it }
        connect()
        return state.value.baseUrl
    }

    private suspend fun connect() = lock.withLock {
        val s = server ?: run { _state.value = ConnState(Mode.Offline, null, "No server paired"); return }
        val c = client ?: return
        _state.value = _state.value.copy(mode = Mode.Connecting)
        // 1) home network
        for (url in s.lanUrls) {
            if (probe(c, url)) {
                tunnel.down()
                _state.value = ConnState(Mode.Home, url, "On your home network")
                return
            }
        }
        // 2) tunnel
        val wg = s.wg
        val key = s.wgPrivateKey
        if (!wg.enabled || wg.endpoint == null || key == null) {
            _state.value = ConnState(Mode.Offline, null, "Not on your home network, and remote access isn't set up on the server")
            return
        }
        if (tunnel.consentIntent() != null) {
            _state.value = ConnState(Mode.NeedsVpnConsent, null, "Allow Paddock's private tunnel to connect from outside")
            return
        }
        val tunnelUrl = "https://${wg.server_ip}:${wg.api_port ?: 8443}"
        try {
            withContext(Dispatchers.IO) { tunnel.up(wg, key) }
        } catch (e: Exception) {
            _state.value = ConnState(Mode.Offline, null, "Tunnel failed: ${e.message}")
            return
        }
        repeat(6) {
            if (probe(c, tunnelUrl, timeoutS = 4)) {
                _state.value = ConnState(Mode.Tunnel, tunnelUrl, "Away · private encrypted tunnel")
                return
            }
            delay(1000)
        }
        tunnel.down()
        _state.value = ConnState(Mode.Offline, null, "Can't reach home (is the router port open?)")
    }

    private fun probe(client: OkHttpClient, base: String, timeoutS: Long = 2): Boolean = runCatching {
        client.newBuilder().connectTimeout(timeoutS, TimeUnit.SECONDS).readTimeout(timeoutS + 1, TimeUnit.SECONDS).build()
            .newCall(Request.Builder().url("$base/api/health").build()).execute().use { it.isSuccessful }
    }.getOrDefault(false)

    fun shutdown() = tunnel.down()
}
