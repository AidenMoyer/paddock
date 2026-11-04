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
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import app.paddock.PaddockApp
import app.paddock.R
import app.paddock.data.HaveResponse
import app.paddock.data.PaddockJson
import app.paddock.data.UploadStart
import app.paddock.net.ApiException
import app.paddock.net.MediaUris
import app.paddock.net.Mode
import app.paddock.net.UploadStatus
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
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
 * Wi-Fi", "Backed up 12 photos") and the files that didn't make it with the server's reason. Shown in the banner
 * above the navigation bar and under You → Photo backup; problems also raise a notification when Android allows it.
 */
object BackupReport {
    /** [uri] and [project] are set for picked files, so "Try again" can send exactly those again. */
    data class Problem(val name: String, val reason: String, val at: Long, val uri: String? = null, val project: Int = 0)

    private val _changes = MutableStateFlow(0)
    /** Goes up whenever the list changes, so whatever's on screen can redraw. */
    val changes: StateFlow<Int> = _changes

    private fun prefs(ctx: Context) = ctx.getSharedPreferences("backup-report", Context.MODE_PRIVATE)

    fun status(ctx: Context): Pair<String, Long> = prefs(ctx).let { (it.getString("status", "") ?: "") to it.getLong("status_at", 0) }

    fun setStatus(ctx: Context, text: String) {
        prefs(ctx).edit().putString("status", text).putLong("status_at", System.currentTimeMillis()).apply()
    }

    fun problems(ctx: Context): List<Problem> = runCatching {
        val arr = org.json.JSONArray(prefs(ctx).getString("problems", "[]"))
        (0 until arr.length()).map { i ->
            arr.getJSONObject(i).let { Problem(it.getString("name"), it.getString("reason"), it.getLong("at"), if (it.has("uri")) it.getString("uri") else null, it.optInt("project")) }
        }
    }.getOrDefault(emptyList())

    private fun save(ctx: Context, list: List<Problem>) {
        val arr = org.json.JSONArray()
        list.forEach { pr ->
            val o = org.json.JSONObject().put("name", pr.name).put("reason", pr.reason).put("at", pr.at).put("project", pr.project)
            pr.uri?.let { o.put("uri", it) }
            arr.put(o)
        }
        prefs(ctx).edit().putString("problems", arr.toString()).apply()
        _changes.value++
    }

    @Synchronized fun failed(ctx: Context, name: String, reason: String, uri: String? = null, project: Int = 0) {
        val all = problems(ctx)
        val old = all.firstOrNull { it.name == name }
        // backup hits the same wall every half hour: that's old news once dismissed. A file someone just picked isn't.
        val at = if (uri == null && old?.reason == reason) old.at else System.currentTimeMillis()
        save(ctx, (all.filter { it.name != name } + Problem(name, reason, at, uri, project)).takeLast(50))
    }

    /** A file that failed earlier has made it. */
    @Synchronized fun resolved(ctx: Context, name: String) {
        val all = problems(ctx)
        if (all.any { it.name == name }) save(ctx, all.filter { it.name != name })
    }

    @Synchronized fun clear(ctx: Context) {
        prefs(ctx).edit().remove("problems").apply()
        _changes.value++
    }

    /** The problems nobody has dismissed from the banner yet. */
    fun unseen(ctx: Context): List<Problem> = prefs(ctx).getLong("seen_at", 0).let { seen -> problems(ctx).filter { it.at > seen } }

    fun markSeen(ctx: Context) {
        prefs(ctx).edit().putLong("seen_at", System.currentTimeMillis()).apply()
        _changes.value++
    }

    /** Send everything that failed again: picked files by their address, the rest through the backup scan. */
    fun retry(ctx: Context) {
        val all = problems(ctx)
        clear(ctx)
        all.filter { it.uri != null }.groupBy { it.project }.forEach { (project, list) ->
            ManualUpload.enqueue(ctx, list.map { Uri.parse(it.uri) }, project.takeIf { it > 0 })
        }
        if (all.any { it.uri == null }) Backup.runNow(ctx)
    }

    /** Tell the person, once per kind of problem, without nagging. */
    fun notify(ctx: Context, title: String, text: String) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val pi = android.app.PendingIntent.getActivity(ctx, 43, android.content.Intent(ctx, app.paddock.MainActivity::class.java).putExtra("open", "backup"),
            android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(ctx, Backup.PROBLEMS).setSmallIcon(R.drawable.ic_notification).setContentTitle(title)
            .setContentText(text).setStyle(NotificationCompat.BigTextStyle().bigText(text)).setAutoCancel(true).setContentIntent(pi).build()
        ctx.getSystemService(android.app.NotificationManager::class.java).notify(43, n)
    }

    /** The server's own words when it gave them, else what probably happened. */
    fun reason(e: Throwable): String = when {
        e is ApiException && e.message?.isNotBlank() == true -> e.message!!
        e is Unreadable -> e.message!!
        e is java.io.FileNotFoundException || e is SecurityException -> "The phone wouldn't let Paddock read this file"
        e is java.net.SocketTimeoutException -> "The server took too long to answer"
        e is java.io.IOException -> "The connection to the server dropped"
        e is OutOfMemoryError -> "The phone ran out of memory sending this file"
        else -> e.message ?: "Something went wrong"
    }
}

/** The file couldn't be read on the phone. No amount of retrying the connection will help. */
class Unreadable(message: String, cause: Throwable? = null) : Exception(message, cause)

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

    data class Media(val id: Long, val uri: Uri, val name: String, val taken: Long, val added: Long, val video: Boolean)

    override suspend fun doWork(): Result {
        // Turning backup on starts the schedule and "back up now" together, and they'd send the same files. One run covers both.
        if (!running.tryLock()) return Result.success()
        try {
            return backUp()
        } finally {
            running.unlock()
        }
    }

    private suspend fun backUp(): Result = withContext(Dispatchers.IO) {
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
                    runCatching { m to Transfer.measure(ctx, m.uri) }.onFailure { BackupReport.failed(ctx, m.name, BackupReport.reason(it)); failed++ }.getOrNull()
                }
                val have = app.api.post<HaveResponse>("/api/uploads/check", buildJsonObject {
                    put("sha256", JsonArray(hashed.map { JsonPrimitive(it.second.sha256) }))
                }).have.toSet()
                for ((m, file) in hashed) {
                    if (isStopped) return@withContext Result.retry()
                    try {
                        if (file.sha256 !in have) Transfer.upload(ctx, file, m.name, m.taken)
                        markDone(m)
                        BackupReport.resolved(ctx, m.name)
                        done++
                    } catch (e: ApiException) {
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
            val proj = arrayOf(MediaStore.MediaColumns._ID, MediaStore.MediaColumns.DISPLAY_NAME,
                MediaStore.MediaColumns.DATE_ADDED, MediaStore.MediaColumns.DATE_TAKEN)
            ctx.contentResolver.query(base, proj, "${MediaStore.MediaColumns.DATE_ADDED} >= ?", arrayOf((since - 86400).coerceAtLeast(0).toString()),
                "${MediaStore.MediaColumns.DATE_ADDED} ASC")?.use { c ->
                while (c.moveToNext()) {
                    val id = c.getLong(0)
                    val key = "${if (video) "v" else "i"}$id"
                    if (key in doneIds) continue
                    val taken = c.getLong(3).takeIf { it > 0 } ?: (c.getLong(2) * 1000)
                    out += Media(id, ContentUris.withAppendedId(base, id), c.getString(1) ?: "file", taken, c.getLong(2), video)
                }
            }
        }
        return out.sortedBy { it.added }
    }

    private fun markDone(m: Media) {
        val done = prefs.getStringSet("done", emptySet())!!.toMutableSet()
        done += "${if (m.video) "v" else "i"}${m.id}"
        // "since" is a DATE_ADDED watermark; the done-set covers the one-day overlap window
        val since = maxOf(prefs.getLong("since", 0L), m.added)
        prefs.edit().putStringSet("done", if (done.size > 20000) done.toList().takeLast(10000).toSet() else done).putLong("since", since).apply()
    }

    private fun foreground(done: Int, total: Int): ForegroundInfo {
        val n = NotificationCompat.Builder(applicationContext, Backup.CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Backing up to ${app.servers.active?.name ?: "Paddock"}")
            .setContentText("$done of $total")
            .setProgress(total, done, false)
            .setOngoing(true)
            .setSilent(true)
            .build()
        return if (Build.VERSION.SDK_INT >= 29) ForegroundInfo(42, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC) else ForegroundInfo(42, n)
    }

    private companion object { val running = Mutex() }
}

/** Hashing + resumable upload, shared by automatic backup and manual uploads. */
object Transfer {
    /** A file as the phone actually hands it over: the address that opened, its hash, and its real length. */
    data class Measured(val uri: Uri, val sha256: String, val size: Long)

    /** What a picked file says about itself. */
    data class Picked(val name: String, val mime: String?, val takenMs: Long)

    /** The unredacted file (with its GPS) where this address can give it, the plain one where it can't. */
    private fun bestUri(ctx: Context, uri: Uri): Uri {
        val allowed = Build.VERSION.SDK_INT >= 29 && MediaUris.acceptsRequireOriginal(uri.authority, uri.path) &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_MEDIA_LOCATION) == PackageManager.PERMISSION_GRANTED
        if (!allowed) return uri
        val original = runCatching { MediaStore.setRequireOriginal(uri) }.getOrNull() ?: return uri
        val opens = runCatching { ctx.contentResolver.openInputStream(original)?.use { true } ?: false }.getOrDefault(false)
        return if (opens) original else uri
    }

    /** Read the whole file once. Everything sent afterwards uses this, because what a phone reports about a file isn't always so. */
    fun measure(ctx: Context, uri: Uri): Measured {
        val best = bestUri(ctx, uri)
        val md = MessageDigest.getInstance("SHA-256")
        var size = 0L
        try {
            val input = ctx.contentResolver.openInputStream(best) ?: throw java.io.FileNotFoundException("no file behind this address")
            input.use {
                val buf = ByteArray(1 shl 20)
                while (true) {
                    val n = it.read(buf)
                    if (n < 0) break
                    md.update(buf, 0, n)
                    size += n
                }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // say what Android said: there's often no log to read on the phone that hit this
            throw Unreadable("The phone wouldn't let Paddock read this file (${e.message ?: e.javaClass.simpleName})", e)
        }
        if (size == 0L) throw Unreadable("The phone handed over an empty file. If this photo is stored in the cloud, open it once so it downloads, then try again.")
        return Measured(best, md.digest().joinToString("") { "%02x".format(it) }, size)
    }

    fun describe(ctx: Context, uri: Uri): Picked {
        val resolver = ctx.contentResolver
        val name = runCatching {
            resolver.query(uri, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)?.use { if (it.moveToFirst()) it.getString(0) else null }
        }.getOrNull()?.takeIf { it.isNotBlank() } ?: "upload"
        // the photo picker calls this column date_taken and MediaStore datetaken; other file apps have neither
        val taken = listOf("date_taken", MediaStore.MediaColumns.DATE_TAKEN).firstNotNullOfOrNull { column ->
            runCatching { resolver.query(uri, arrayOf(column), null, null, null)?.use { if (it.moveToFirst()) it.getLong(0).takeIf { ms -> ms > 0 } else null } }.getOrNull()
        }
        return Picked(name, runCatching { resolver.getType(uri) }.getOrNull(), taken ?: System.currentTimeMillis())
    }

    /** One file at a time from this phone: automatic backup and picked files can reach for the same photo at once. */
    private val sending = Mutex()

    /** The server has moved on from what we were sending. Carrying on can't work, but asking where things stand can. */
    private class OutOfStep(val reply: ApiException) : Exception()

    /** True when the file is newly on the server (or back from its trash), false when it was already there. */
    suspend fun upload(ctx: Context, file: Measured, name: String, takenMs: Long, projectId: Int? = null, mime: String? = null,
                       onSent: (Float) -> Unit = {}): Boolean = sending.withLock {
        // another device may be sending the same file, or an older version may have left a stuck attempt behind
        var conflict: ApiException? = null
        repeat(3) { attempt ->
            if (attempt > 0) delay(1000)
            try {
                return@withLock send(ctx, file, name, takenMs, projectId, mime, onSent)
            } catch (e: OutOfStep) {
                conflict = e.reply
            }
        }
        throw conflict!!
    }

    private suspend fun send(ctx: Context, file: Measured, name: String, takenMs: Long, projectId: Int?, mime: String?, onSent: (Float) -> Unit): Boolean {
        val app = PaddockApp.instance
        val size = file.size
        val start = app.api.post<UploadStart>("/api/uploads", buildJsonObject {
            put("filename", name); put("size", size); put("sha256", file.sha256); put("taken_at", takenMs / 1000.0)
            if (mime != null) put("mime", mime)
            if (projectId != null) put("project_id", projectId)
        })
        if (start.status == "duplicate") return start.restored
        val id = start.upload_id!!
        var offset = start.offset
        val octet = "application/octet-stream".toMediaType()
        try {
            ctx.contentResolver.openInputStream(file.uri)!!.use { input ->
                var skipped = 0L
                while (skipped < offset) {
                    val s = input.skip(offset - skipped)
                    if (s > 0) { skipped += s; continue }
                    // skip() may do nothing without being at the end: a read tells the difference
                    if (input.read() < 0) throw java.io.EOFException("The file got shorter while it was uploading")
                    skipped++
                }
                val buf = ByteArray(start.chunk_size.toInt())
                while (offset < size) {
                    var n = 0
                    while (n < buf.size) {
                        val r = input.read(buf, n, buf.size - n)
                        if (r < 0) break
                        n += r
                    }
                    if (n == 0) break
                    val res = app.api.raw("PUT", "/api/uploads/$id?offset=$offset", buf.toRequestBody(octet, 0, n), timeoutS = 300)
                    offset = (PaddockJson.parseToJsonElement(res) as kotlinx.serialization.json.JsonObject)["offset"].toString().toLong()
                    onSent(offset.toFloat() / size)
                }
            }
            app.api.raw("POST", "/api/uploads/$id/complete", timeoutS = 600)
        } catch (e: ApiException) {
            throw if (e.code == 409) OutOfStep(e) else e
        }
        return true
    }
}

/** Files picked with the "+" button: same resumable, verified upload, run in the background. */
object ManualUpload {
    private const val WORK = "paddock-manual-upload"

    /** Upload files picked on the phone, into the person's library or straight into a project. */
    fun enqueue(ctx: Context, uris: List<Uri>, projectId: Int? = null) {
        if (uris.isEmpty()) return
        uris.forEach { runCatching { ctx.contentResolver.takePersistableUriPermission(it, android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION) } }
        val texts = uris.map { it.toString() }
        UploadStatus.queue(texts)
        // one after another, however many times "+" is pressed. No network requirement: Android's means "has internet",
        // which a home network might not, and the worker can say when it can't reach the server, which Android can't.
        val requests = MediaUris.batches(texts).map { batch ->
            OneTimeWorkRequestBuilder<ManualUploadWorker>()
                .setInputData(androidx.work.workDataOf("uris" to batch.toTypedArray(), "project" to (projectId ?: 0)))
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)     // someone's waiting on this: start now
                .build()
        }
        var chain = WorkManager.getInstance(ctx).beginUniqueWork(WORK, ExistingWorkPolicy.APPEND_OR_REPLACE, requests.first())
        requests.drop(1).forEach { chain = chain.then(it) }
        chain.enqueue()
    }

    fun cancel(ctx: Context) {
        WorkManager.getInstance(ctx).cancelUniqueWork(WORK)
        UploadStatus.dismiss()
    }
}

class ManualUploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    private val app = PaddockApp.instance
    private val server get() = app.servers.active?.name ?: "your server"

    /** Older Android runs expedited work as a foreground service and asks for this; newer gets it from setForeground below. */
    override suspend fun getForegroundInfo(): ForegroundInfo {
        val n = NotificationCompat.Builder(applicationContext, Backup.CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Uploading to $server")
            .setProgress(0, 0, true)
            .setOngoing(true)
            .setSilent(true)
            .build()
        return if (Build.VERSION.SDK_INT >= 29) ForegroundInfo(44, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC) else ForegroundInfo(44, n)
    }

    private fun fail(uri: String, name: String, reason: String, project: Int) {
        BackupReport.failed(applicationContext, name, reason, uri, project)
        UploadStatus.finished(uri, UploadStatus.Outcome.Failed)
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val uris = UploadStatus.remaining(inputData.getStringArray("uris")?.toList() ?: return@withContext Result.success())
        val project = inputData.getInt("project", 0)
        runCatching { setForeground(getForegroundInfo()) }      // carry on if the screen goes off (the system can refuse; it still runs)
        if (app.conn.ensure() == null) {
            if (runAttemptCount < ATTEMPTS) {
                UploadStatus.waiting("Trying to reach $server")
                return@withContext Result.retry()
            }
            val why = "Couldn't reach $server. ${app.conn.state.value.detail}".trim()
            uris.forEach { fail(it, Transfer.describe(ctx, Uri.parse(it)).name, why, project) }
            return@withContext finish(ctx)
        }
        for (text in uris) {
            val uri = Uri.parse(text)
            val picked = Transfer.describe(ctx, uri)
            try {
                UploadStatus.working(picked.name, 0f)
                val file = Transfer.measure(ctx, uri)
                val landed = Transfer.upload(ctx, file, picked.name, picked.takenMs, project.takeIf { it > 0 }, picked.mime) { UploadStatus.working(picked.name, it) }
                BackupReport.resolved(ctx, picked.name)
                UploadStatus.finished(text, if (landed) UploadStatus.Outcome.Uploaded else UploadStatus.Outcome.Already)
            } catch (e: CancellationException) {
                throw e                                                                     // stopped, not failed
            } catch (e: ApiException) {
                fail(text, picked.name, BackupReport.reason(e), project)                    // the server said no: retrying won't help
            } catch (e: java.io.IOException) {
                if (runAttemptCount < ATTEMPTS) {                                           // the connection dropped: resume later
                    UploadStatus.waiting("The connection dropped. Paddock will carry on where it left off.")
                    return@withContext Result.retry()
                }
                fail(text, picked.name, BackupReport.reason(e), project)
            } catch (e: Throwable) {
                fail(text, picked.name, BackupReport.reason(e), project)
            }
        }
        finish(ctx)
    }

    private fun finish(ctx: Context): Result {
        val problems = BackupReport.unseen(ctx)
        if (problems.isNotEmpty()) BackupReport.notify(ctx, if (problems.size == 1) "${problems[0].name} didn't upload" else "${problems.size} files didn't upload",
            problems.last().reason)
        return Result.success()
    }

    private companion object { const val ATTEMPTS = 5 }
}
