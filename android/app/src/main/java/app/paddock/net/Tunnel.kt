package app.paddock.net

import android.content.Context
import android.content.Intent
import com.wireguard.android.backend.GoBackend
import com.wireguard.android.backend.Tunnel
import com.wireguard.config.Config
import com.wireguard.config.InetEndpoint
import com.wireguard.config.InetNetwork
import com.wireguard.config.Interface
import com.wireguard.config.Peer
import com.wireguard.crypto.Key
import com.wireguard.crypto.KeyPair
import app.paddock.data.WgProfile

/**
 * The app-only tunnel. Uses the official WireGuard userspace backend with
 * `includeApplication(<this app>)`, so ONLY Paddock's own traffic can enter it, and its single
 * allowed route is the server's tunnel address — the rest of the phone is untouched.
 */
class PaddockTunnel(private val context: Context) {
    private val backend by lazy { GoBackend(context.applicationContext) }
    @Volatile var isUp = false
        private set

    private val tunnel = object : Tunnel {
        override fun getName() = "paddock"
        override fun onStateChange(newState: Tunnel.State) { isUp = newState == Tunnel.State.UP }
    }

    /** Non-null when Android still needs the user's one-time consent to create a VPN interface. */
    fun consentIntent(): Intent? = GoBackend.VpnService.prepare(context)

    fun up(profile: WgProfile, privateKey: String) {
        val iface = Interface.Builder()
            .setKeyPair(KeyPair(Key.fromBase64(privateKey)))
            .addAddress(InetNetwork.parse(profile.address!!))
            .includeApplication(context.packageName)
            .build()
        val peer = Peer.Builder()
            .setPublicKey(Key.fromBase64(profile.server_public_key!!))
            .setEndpoint(InetEndpoint.parse(profile.endpoint!!))
            .addAllowedIp(InetNetwork.parse(profile.allowed_ips ?: "${profile.server_ip}/32"))
            .setPersistentKeepalive(profile.keepalive ?: 25)
            .build()
        val config = Config.Builder().setInterface(iface).addPeer(peer).build()
        backend.setState(tunnel, Tunnel.State.UP, config)
        isUp = true
    }

    fun down() {
        if (!isUp) return
        runCatching { backend.setState(tunnel, Tunnel.State.DOWN, null) }
        isUp = false
    }

    companion object {
        /** Generate the device's tunnel key pair on the phone; only the public half is ever sent. */
        fun newKeyPair(): Pair<String, String> {
            val kp = KeyPair()
            return kp.privateKey.toBase64() to kp.publicKey.toBase64()
        }
    }
}
