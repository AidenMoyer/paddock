package app.paddock.update

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import app.paddock.PaddockApp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import okhttp3.Request
import java.io.File
import java.security.MessageDigest

/** The Android app the server's release carries (GET /api/app/android). */
@Serializable
data class AndroidRelease(
    val available: Boolean = false,
    val version_name: String = "",
    val version_code: Long = 0,
    val sha256: String = "",
    val size: Long = 0,
    val url: String = "",
)

sealed interface UpdateState {
    data object Idle : UpdateState
    data class Available(val release: AndroidRelease) : UpdateState
    data class Downloading(val release: AndroidRelease, val progress: Float) : UpdateState
    data object Installing : UpdateState
    data class Failed(val message: String) : UpdateState
}

/**
 * Updates come from the user's own server, not an app store: each server release bundles the matching
 * signed app. Checked when the app connects (at most every few hours) and from You → Check for updates.
 * The download goes through the same pinned, authenticated connection as everything else, is checked
 * against the server's SHA-256, and Android itself verifies it's signed with the same key before installing.
 */
object Updater {
    private const val CHECK_EVERY_MS = 6 * 60 * 60 * 1000L
    private const val SNOOZE_MS = 24 * 60 * 60 * 1000L
    private const val ACTION_RESULT = "app.paddock.update.RESULT"

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)   // outlives the screen that started it
    private val _state = MutableStateFlow<UpdateState>(UpdateState.Idle)
    val state: StateFlow<UpdateState> = _state

    fun installedVersion(ctx: Context): Pair<String, Long> {
        val info = ctx.packageManager.getPackageInfo(ctx.packageName, 0)
        @Suppress("DEPRECATION")
        val code = if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong()
        return (info.versionName ?: "") to code
    }

    /** Returns the newer release, or null. [manual] ignores the throttle and a snoozed "Later". */
    suspend fun check(app: PaddockApp, manual: Boolean = false): AndroidRelease? {
        val prefs = app.getSharedPreferences("updates", Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        if (!manual && now - prefs.getLong("last_check", 0) < CHECK_EVERY_MS) return null
        if (_state.value is UpdateState.Downloading || _state.value is UpdateState.Installing) return null
        val rel = runCatching { app.api.get<AndroidRelease>("/api/app/android") }.getOrElse { if (manual) throw it; return null }
        prefs.edit().putLong("last_check", now).apply()
        if (!rel.available || rel.version_code <= installedVersion(app).second) return null
        val snoozed = prefs.getLong("snoozed_code", 0) == rel.version_code && now < prefs.getLong("snoozed_until", 0)
        if (manual || !snoozed) _state.value = UpdateState.Available(rel)
        return rel
    }

    fun later(ctx: Context) {
        val rel = (_state.value as? UpdateState.Available)?.release
        if (rel != null) {
            ctx.getSharedPreferences("updates", Context.MODE_PRIVATE).edit()
                .putLong("snoozed_code", rel.version_code).putLong("snoozed_until", System.currentTimeMillis() + SNOOZE_MS).apply()
        }
        _state.value = UpdateState.Idle
    }

    fun dismiss() { _state.value = UpdateState.Idle }

    fun start(app: PaddockApp, rel: AndroidRelease) { scope.launch { install(app, rel) } }

    private suspend fun install(app: PaddockApp, rel: AndroidRelease) {
        _state.value = UpdateState.Downloading(rel, 0f)
        val apk = runCatching { download(app, rel) }.getOrElse {
            _state.value = UpdateState.Failed(it.message ?: "The download didn't finish. Try again."); return
        }
        _state.value = UpdateState.Installing
        runCatching { commit(app, apk) }.onFailure {
            _state.value = UpdateState.Failed("Android couldn't start the install: ${it.message}")
        }
    }

    private suspend fun download(app: PaddockApp, rel: AndroidRelease): File = withContext(Dispatchers.IO) {
        val client = app.conn.client ?: error("Not connected to your server")
        val dir = File(app.cacheDir, "update").apply { deleteRecursively(); mkdirs() }
        val out = File(dir, "paddock-${rel.version_code}.apk")
        val digest = MessageDigest.getInstance("SHA-256")
        client.newCall(Request.Builder().url(app.api.url(rel.url)).build()).execute().use { r ->
            if (!r.isSuccessful) error("The server answered ${r.code}")
            val body = r.body ?: error("Empty download")
            body.byteStream().use { input ->
                out.outputStream().use { output ->
                    val buf = ByteArray(64 * 1024)
                    var done = 0L
                    var shown = 0f
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        output.write(buf, 0, n)
                        digest.update(buf, 0, n)
                        done += n
                        val p = if (rel.size > 0) done.toFloat() / rel.size else 0f
                        if (p - shown >= 0.01f) { shown = p; _state.value = UpdateState.Downloading(rel, p) }
                    }
                }
            }
        }
        val sha = digest.digest().joinToString("") { "%02x".format(it) }
        if (!sha.equals(rel.sha256, ignoreCase = true) || out.length() != rel.size) {
            out.delete()
            error("The download was damaged on the way. Try again.")
        }
        out
    }

    private fun commit(ctx: Context, apk: File) {
        val installer = ctx.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(ctx.packageName)
            setSize(apk.length())
            // Android 12+: an app updating itself can skip the extra confirmation once the user allowed it
            if (Build.VERSION.SDK_INT >= 31) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            session.openWrite("paddock.apk", 0, apk.length()).use { out ->
                apk.inputStream().use { it.copyTo(out) }
                session.fsync(out)
            }
            val intent = Intent(ctx, UpdateReceiver::class.java).setAction(ACTION_RESULT)
            // mutable: the installer adds the result and, if needed, the confirmation screen to this intent
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
            session.commit(PendingIntent.getBroadcast(ctx, id, intent, flags).intentSender)
        }
    }

    internal fun result(ctx: Context, intent: Intent) {
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                else intent.getParcelableExtra(Intent.EXTRA_INTENT)
                if (confirm != null) ctx.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
            PackageInstaller.STATUS_SUCCESS -> {
                File(ctx.cacheDir, "update").deleteRecursively()
                _state.value = UpdateState.Idle          // Android restarts the app on the new version
            }
            PackageInstaller.STATUS_FAILURE_ABORTED -> _state.value = UpdateState.Idle
            PackageInstaller.STATUS_FAILURE_CONFLICT, PackageInstaller.STATUS_FAILURE_INCOMPATIBLE ->
                _state.value = UpdateState.Failed("This copy of Paddock was installed from a different build (for example a test build), " +
                    "so Android won't update it in place. Uninstall Paddock, then install it from your server once: " +
                    "open your server's address followed by /download/android in the phone's browser. After that, updates install here.")
            PackageInstaller.STATUS_FAILURE_STORAGE -> _state.value = UpdateState.Failed("Not enough free space on this phone for the update.")
            else -> _state.value = UpdateState.Failed(intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "The update didn't install.")
        }
    }
}

class UpdateReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) = Updater.result(context, intent)
}
