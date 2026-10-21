package app.paddock

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import app.paddock.backup.Backup
import app.paddock.data.AuthState
import app.paddock.data.Library
import app.paddock.data.User
import app.paddock.live.Live
import app.paddock.data.ServerStore
import app.paddock.data.Settings
import app.paddock.net.Api
import app.paddock.net.ConnectionManager
import app.paddock.net.Mode
import app.paddock.net.PaddockTunnel
import app.paddock.player.Players
import app.paddock.update.Updater
import coil.ImageLoader
import coil.ImageLoaderFactory
import coil.disk.DiskCache
import coil.memory.MemoryCache
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

class PaddockApp : Application(), ImageLoaderFactory {
    lateinit var servers: ServerStore
    lateinit var settings: Settings
    lateinit var tunnel: PaddockTunnel
    lateinit var conn: ConnectionManager
    lateinit var api: Api
    lateinit var players: Players
    val libraries = MutableStateFlow<List<Library>>(emptyList())
    /** This person on the active server, refreshed on every connect (media-only mode, communities...). */
    val me = MutableStateFlow<User?>(null)
    val theme = MutableStateFlow("system")
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override fun onCreate() {
        super.onCreate()
        instance = this
        servers = ServerStore(this)
        settings = Settings(this)
        tunnel = PaddockTunnel(this)
        conn = ConnectionManager(this, tunnel)
        api = Api(conn)
        players = Players(this)
        theme.value = settings.theme
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(Backup.CHANNEL, getString(R.string.backup_channel), NotificationManager.IMPORTANCE_LOW)
        )
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(Live.CHANNEL, "Calls and watch parties", NotificationManager.IMPORTANCE_HIGH)
        )
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(Backup.PROBLEMS, "Backup problems", NotificationManager.IMPORTANCE_DEFAULT)
        )
        me.value = servers.active?.user
        scope.launch {
            servers.activeId.collectLatest {
                conn.use(servers.active)
                players.reset()
            }
        }
        scope.launch {
            conn.state.collectLatest { st ->
                val online = st.mode == Mode.Home || st.mode == Mode.Tunnel
                Live.follow(if (online) st.baseUrl else null)
                if (online) {
                    runCatching { api.get<AuthState>("/api/auth/state").user?.let { me.value = it } }
                    runCatching { libraries.value = api.get<List<Library>>("/api/libraries") }
                    Updater.check(this@PaddockApp)
                }
            }
        }
        Backup.schedule(this)
    }

    fun themeChanged() { theme.value = settings.theme }

    /** Thumbnails load through the same pinned, authenticated connection as everything else. */
    override fun newImageLoader(): ImageLoader = ImageLoader.Builder(this)
        .callFactory { okhttp3.Call.Factory { request -> (conn.client ?: OkHttpClient()).newCall(request) } }  // follows server switches
        .memoryCache { MemoryCache.Builder(this).maxSizePercent(0.2).build() }
        .diskCache { DiskCache.Builder().directory(cacheDir.resolve("thumbs")).maxSizeBytes(512L * 1024 * 1024).build() }
        .crossfade(180)
        .respectCacheHeaders(false)
        .build()

    companion object {
        lateinit var instance: PaddockApp
            private set
    }
}
