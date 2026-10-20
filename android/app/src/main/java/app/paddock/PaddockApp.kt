package app.paddock

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import app.paddock.backup.Backup
import app.paddock.data.Library
import app.paddock.data.ServerStore
import app.paddock.data.Settings
import app.paddock.net.Api
import app.paddock.net.ConnectionManager
import app.paddock.net.Mode
import app.paddock.net.PaddockTunnel
import app.paddock.player.Players
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
        scope.launch {
            servers.activeId.collectLatest {
                conn.use(servers.active)
                players.reset()
            }
        }
        scope.launch {
            conn.state.collectLatest { st ->
                if (st.mode == Mode.Home || st.mode == Mode.Tunnel) {
                    runCatching { libraries.value = api.get<List<Library>>("/api/libraries") }
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
