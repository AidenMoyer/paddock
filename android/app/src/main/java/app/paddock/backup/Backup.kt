package app.paddock.backup

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import app.paddock.PaddockApp
import app.paddock.R
import app.paddock.data.HaveResponse
import app.paddock.data.PaddockJson
import app.paddock.data.UploadStart
import app.paddock.net.Mode
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * What happened to backups, in plain words, so nothing fails quietly: the current state ("Waiting for your home
 * Wi-Fi", "Backed up 12 photos") and the files that didn't make it with the server's reason. Shown under You →
 * Photo backup; problems also raise a notification.
 */
object BackupReport {
    data class Problem(val name: String, val reason: String, val at: Long)

    private fun prefs(ctx: Context) = ctx.getSharedPreferences("backup-report", Context.MODE_PRIVATE)

    fun status(ctx: Context): Pair<String, Long> = prefs(ctx).let { (it.getString("status", "") ?: "") to it.getLong("status_at", 0) }

    fun setStatus(ctx: Context, text: String) {
        prefs(ctx).edit().putString("status", text).putLong("status_at", System.currentTimeMillis()).apply()
    }

    fun problems(ctx: Context): List<Problem> = runCatching {
        val arr = org.json.JSONArray(prefs(ctx).getString("problems", "[]"))
        (0 until arr.length()).map { i -> arr.getJSONObject(i).let { Problem(it.getString("name"), it.getString("reason"), it.getLong("at")) } }
    }.getOrDefault(emptyList())

    @Synchronized fun failed(ctx: Context, name: String, reason: String) {
        val list = (problems(ctx).filter { it.name != name } + Problem(name, reason, System.currentTimeMillis())).takeLast(50)
        val arr = org.json.JSONArray()
        list.forEach { arr.put(org.json.JSONObject().put("name", it.name).put("reason", it.reason).put("at", it.at)) }
        prefs(ctx).edit().putString("problems", arr.toString()).apply()
    }

    fun clear(ctx: Context) { prefs(ctx).edit().remove("problems").apply() }

    /** Tell the person, once per kind of problem, without nagging. */
    fun notify(ctx: Context, title: String, text: String) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val pi = android.app.PendingIntent.getActivity(ctx, 43, android.content.Intent(ctx, app.paddock.MainActivity::class.java).putExtra("open", "backup"),
            android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(ctx, Backup.PROBLEMS).setSmallIcon(R.drawable.ic_launcher).setContentTitle(title)
            .setContentText(text).setStyle(NotificationCompat.BigTextStyle().bigText(text)).setAutoCancel(true).setContentIntent(pi).build()
        ctx.getSystemService(android.app.NotificationManager::class.java).notify(43, n)
    }

    /** The server's own words when it gave them, else what probably happened. */
    fun reason(e: Throwable): String = when {
        e is app.paddock.net.ApiException && e.message?.isNotBlank() == true -> e.message!!
        e is java.io.FileNotFoundException || e is SecurityException -> "The phone wouldn't let Paddock read this file"
        e is java.net.SocketTimeoutException -> "The server took too long to answer"
        e is java.io.IOException -> "The connection to the server dropped"
        else -> e.message ?: "Something went wrong"
    }
}

object Backup {
    const val PROBLEMS = "backup-problems"
    const val CHANNEL = "backup"
    private const val PERIODIC = "paddock-backup"
    private const val NOW = "paddock-backup-now"

    private fun constraints(ctx: Context): Constraints {
        val s = PaddockApp.instance.settings
        return Constraints.Builder()
            .setRequiredNetworkType(if (s.backupWifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
            .setRequiresCharging(s.backupChargingOnly)
            .setRequiresBatteryNotLow(true)
            .build()
    }

    fun schedule(ctx: Context) {
        val wm = WorkManager.getInstance(ctx)
        if (!PaddockApp.instance.settings.backupEnabled) {
            wm.cancelUniqueWork(PERIODIC)
            return
        }
        val req = PeriodicWorkRequestBuilder<BackupWorker>(30, TimeUnit.MINUTES).setConstraints(constraints(ctx)).build()
        wm.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.UPDATE, req)
    }

    fun runNow(ctx: Context) {
        val req = OneTimeWorkRequestBuilder<BackupWorker>().setConstraints(constraints(ctx)).build()
        WorkManager.getInstance(ctx).enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, req)
    }

    fun permissions(): Array<String> = when {
        Build.VERSION.SDK_INT >= 34 -> arrayOf(Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO,
            Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED, Manifest.permission.ACCESS_MEDIA_LOCATION)
        Build.VERSION.SDK_INT >= 33 -> arrayOf(Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO,
            Manifest.permission.ACCESS_MEDIA_LOCATION)
        else -> arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.ACCESS_MEDIA_LOCATION)
    }
}

/**
 * Backs up new photos and videos with Paddock's resumable upload protocol:
 *  hash on the phone → ask which ones the server already has → upload only the missing ones in
 *  8 MB chunks that resume after any interruption → server verifies the SHA-256 and files them by date.
 * GPS metadata is kept (ACCESS_MEDIA_LOCATION + the original, unredacted file).
 */
class BackupWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    private val app = PaddockApp.instance
    private val prefs = ctx.getSharedPreferences("backup-state", Context.MODE_PRIVATE)

    data class Media(val id: Long, val uri: Uri, val name: String, val size: Long, val taken: Long, val added: Long, val video: Boolean)

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        if (!app.settings.backupEnabled || app.servers.active == null) return@withContext Result.success()
        app.conn.ensure()
        val mode = app.conn.state.value.mode
        val server = app.servers.active?.name ?: "your server"
        if (mode == Mode.Tunnel && !app.settings.backupRemote) {      // wait until home
            BackupReport.setStatus(ctx, "Waiting for your home network (backing up away from home is off)")
            return@withContext Result.success()
        }
        if (mode != Mode.Home && mode != Mode.Tunnel) {
            BackupReport.setStatus(ctx, "Can't reach $server right now. Paddock will try again.")
            return@withContext Result.retry()
        }
        if (!allowed()) {
            BackupReport.setStatus(ctx, "Paddock isn't allowed to see your photos. Turn on photo access for Paddock in Android settings.")
            BackupReport.notify(ctx, "Photos aren't backing up", "Paddock isn't allowed to see your photos. Open Paddock → You → Photo backup.")
            return@withContext Result.success()
        }

        val pending = scan()
        if (pending.isEmpty()) {
            BackupReport.setStatus(ctx, "Everything's backed up to $server")
            return@withContext Result.success()
        }
        runCatching { setForeground(foreground(0, pending.size)) }
        var done = 0
        var failed = 0
        try {
            for (batch in pending.chunked(50)) {
                val hashed = batch.mapNotNull { m ->
                    runCatching { m to sha256(m.uri) }.onFailure { BackupReport.failed(ctx, m.name, BackupReport.reason(it)); failed++ }.getOrNull()
                }
                val have = app.api.post<HaveResponse>("/api/uploads/check", buildJsonObject {
                    put("sha256", JsonArray(hashed.map { JsonPrimitive(it.second) }))
                }).have.toSet()
                for ((m, hash) in hashed) {
                    if (isStopped) return@withContext Result.retry()
                    try {
                        if (hash !in have) upload(m, hash)
                        markDone(m)
                        done++
                    } catch (e: app.paddock.net.ApiException) {
                        failed++
                        BackupReport.failed(ctx, m.name, BackupReport.reason(e))
                        when (e.code) {
                            415 -> markDone(m)                 // a type the server doesn't keep: don't try forever
                            403, 404, 409, 503, 507 -> {       // a problem on the server's side that every file would hit
                                BackupReport.setStatus(ctx, "Backup paused: ${BackupReport.reason(e)}")
                                BackupReport.notify(ctx, "Photos aren't backing up", BackupReport.reason(e))
                                return@withContext Result.success()
                            }
                        }
                    }
                    runCatching { setForeground(foreground(done + failed, pending.size)) }
                }
            }
        } catch (e: java.io.IOException) {
            BackupReport.setStatus(ctx, "The connection dropped after $done files. Paddock will carry on when it can.")
            return@withContext Result.retry()
        }
        prefs.edit().putLong("last_run", System.currentTimeMillis()).putInt("last_count", done).apply()
        BackupReport.setStatus(ctx, if (failed == 0) "Backed up $done ${if (done == 1) "file" else "files"} to $server"
            else "Backed up $done, but $failed didn't go. See below for why.")
        if (failed > 0) BackupReport.notify(ctx, "$failed ${if (failed == 1) "file" else "files"} didn't back up", "Open Paddock → You → Photo backup to see why.")
        Result.success()
    }

    private fun allowed(): Boolean {
        val ctx = applicationContext
        return Backup.permissions().filter { it != Manifest.permission.ACCESS_MEDIA_LOCATION && it != "android.permission.READ_MEDIA_VISUAL_USER_SELECTED" }
            .any { ContextCompat.checkSelfPermission(ctx, it) == PackageManager.PERMISSION_GRANTED } ||
            (Build.VERSION.SDK_INT >= 34 && ContextCompat.checkSelfPermission(ctx, "android.permission.READ_MEDIA_VISUAL_USER_SELECTED") == PackageManager.PERMISSION_GRANTED)
    }

    private fun scan(): List<Media> {
        val ctx = applicationContext
        val granted = Backup.permissions().filter { it != Manifest.permission.ACCESS_MEDIA_LOCATION && it != "android.permission.READ_MEDIA_VISUAL_USER_SELECTED" }
            .any { ContextCompat.checkSelfPermission(ctx, it) == PackageManager.PERMISSION_GRANTED } ||
            (Build.VERSION.SDK_INT >= 34 && ContextCompat.checkSelfPermission(ctx, "android.permission.READ_MEDIA_VISUAL_USER_SELECTED") == PackageManager.PERMISSION_GRANTED)
        if (!granted) return emptyList()
        val since = prefs.getLong("since", 0L)  // seconds; 0 = everything on first run
        val doneIds = prefs.getStringSet("done", emptySet())!!
        val out = mutableListOf<Media>()
        val sources = mutableListOf(MediaStore.Images.Media.EXTERNAL_CONTENT_URI to false)
        if (app.settings.backupVideos) sources += MediaStore.Video.Media.EXTERNAL_CONTENT_URI to true
        for ((base, video) in sources) {
            val proj = arrayOf(MediaStore.MediaColumns._ID, MediaStore.MediaColumns.DISPLAY_NAME, MediaStore.MediaColumns.SIZE,
                MediaStore.MediaColumns.DATE_ADDED, MediaStore.MediaColumns.DATE_TAKEN)
            ctx.contentResolver.query(base, proj, "${MediaStore.MediaColumns.DATE_ADDED} >= ?", arrayOf((since - 86400).coerceAtLeast(0).toString()),
                "${MediaStore.MediaColumns.DATE_ADDED} ASC")?.use { c ->
                while (c.moveToNext()) {
                    val id = c.getLong(0)
                    val key = "${if (video) "v" else "i"}$id"
                    if (key in doneIds) continue
                    val taken = c.getLong(4).takeIf { it > 0 } ?: (c.getLong(3) * 1000)
                    out += Media(id, ContentUris.withAppendedId(base, id), c.getString(1) ?: "file", c.getLong(2), taken, c.getLong(3), video)
                }
            }
        }
        return out.sortedBy { it.added }
    }

    private fun sha256(uri: Uri) = Transfer.sha256(applicationContext, uri)

    private suspend fun upload(m: Media, hash: String) = Transfer.upload(applicationContext, m.uri, m.name, m.size, m.taken, hash)

    private fun markDone(m: Media) {
        val done = prefs.getStringSet("done", emptySet())!!.toMutableSet()
        done += "${if (m.video) "v" else "i"}${m.id}"
        // "since" is a DATE_ADDED watermark; the done-set covers the one-day overlap window
        val since = maxOf(prefs.getLong("since", 0L), m.added)
        prefs.edit().putStringSet("done", if (done.size > 20000) done.toList().takeLast(10000).toSet() else done).putLong("since", since).apply()
    }

    private fun foreground(done: Int, total: Int): ForegroundInfo {
        val n = NotificationCompat.Builder(applicationContext, Backup.CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle("Backing up to ${app.servers.active?.name ?: "Paddock"}")
            .setContentText("$done of $total")
            .setProgress(total, done, false)
            .setOngoing(true)
            .setSilent(true)
            .build()
        return if (Build.VERSION.SDK_INT >= 29) ForegroundInfo(42, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC) else ForegroundInfo(42, n)
    }
}

/** Hashing + resumable upload, shared by automatic backup and manual uploads. */
object Transfer {
    fun originalUri(ctx: Context, uri: Uri): Uri =
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_MEDIA_LOCATION) == PackageManager.PERMISSION_GRANTED)
            runCatching { MediaStore.setRequireOriginal(uri) }.getOrDefault(uri) else uri

    fun sha256(ctx: Context, uri: Uri): String {
        val md = MessageDigest.getInstance("SHA-256")
        ctx.contentResolver.openInputStream(originalUri(ctx, uri))!!.use { input ->
            val buf = ByteArray(1 shl 20)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    suspend fun upload(ctx: Context, uri: Uri, name: String, size: Long, takenMs: Long, hash: String, projectId: Int? = null) {
        val app = PaddockApp.instance
        val start = app.api.post<UploadStart>("/api/uploads", buildJsonObject {
            put("filename", name); put("size", size); put("sha256", hash); put("taken_at", takenMs / 1000.0)
            if (projectId != null) put("project_id", projectId)
        })
        if (start.status == "duplicate") return
        val id = start.upload_id!!
        var offset = start.offset
        val octet = "application/octet-stream".toMediaType()
        ctx.contentResolver.openInputStream(originalUri(ctx, uri))!!.use { input ->
            var skipped = 0L
            while (skipped < offset) skipped += input.skip(offset - skipped)
            val buf = ByteArray(start.chunk_size.toInt())
            while (offset < size) {
                var n = 0
                while (n < buf.size) {
                    val r = input.read(buf, n, buf.size - n)
                    if (r < 0) break
                    n += r
                }
                if (n == 0) break
                val res = app.api.raw("PUT", "/api/uploads/$id?offset=$offset", buf.copyOf(n).toRequestBody(octet), timeoutS = 300)
                offset = (PaddockJson.parseToJsonElement(res) as kotlinx.serialization.json.JsonObject)["offset"].toString().toLong()
            }
        }
        app.api.raw("POST", "/api/uploads/$id/complete", timeoutS = 600)
    }
}

/** Files picked with the "+" button: same resumable, verified upload, run in the background. */
object ManualUpload {
    /** Upload files picked on the phone, into the person's library or straight into a project. */
    fun enqueue(ctx: Context, uris: List<Uri>, projectId: Int? = null) {
        uris.forEach { runCatching { ctx.contentResolver.takePersistableUriPermission(it, android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION) } }
        val req = OneTimeWorkRequestBuilder<ManualUploadWorker>()
            .setInputData(androidx.work.workDataOf("uris" to uris.map { it.toString() }.toTypedArray(), "project" to (projectId ?: 0)))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(ctx).enqueue(req)
    }
}

class ManualUploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val uris = inputData.getStringArray("uris")?.map { Uri.parse(it) } ?: return@withContext Result.success()
        val project = inputData.getInt("project", 0).takeIf { it > 0 }
        if (PaddockApp.instance.conn.ensure() == null) {
            BackupReport.setStatus(ctx, "Waiting to reach your server to upload ${uris.size} files")
            return@withContext Result.retry()
        }
        val failed = mutableListOf<String>()
        for (uri in uris) {
            var name = "upload"; var size = 0L
            ctx.contentResolver.query(uri, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME, android.provider.OpenableColumns.SIZE), null, null, null)?.use {
                if (it.moveToFirst()) { name = it.getString(0) ?: name; size = it.getLong(1) }
            }
            try {
                Transfer.upload(ctx, uri, name, size, System.currentTimeMillis(), Transfer.sha256(ctx, uri), project)
            } catch (e: app.paddock.net.ApiException) {
                BackupReport.failed(ctx, name, BackupReport.reason(e)); failed += name     // the server said no: retrying won't help
            } catch (e: java.io.IOException) {
                if (runAttemptCount < 5) return@withContext Result.retry()                  // the connection dropped: resume later
                BackupReport.failed(ctx, name, BackupReport.reason(e)); failed += name
            } catch (e: Exception) {
                BackupReport.failed(ctx, name, BackupReport.reason(e)); failed += name
            }
        }
        if (failed.isNotEmpty()) BackupReport.notify(ctx, if (failed.size == 1) "${failed[0]} didn't upload" else "${failed.size} files didn't upload",
            BackupReport.problems(ctx).lastOrNull()?.reason ?: "Open Paddock → You to see why.")
        Result.success()
    }
}
