package app.paddock.net

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * What picked files are doing right now, for the banner above the navigation bar. A worker that retries starts its
 * list over, so this keeps an outcome per file instead of counting up.
 */
object UploadStatus {
    enum class Outcome { Uploaded, Already, Failed }

    data class State(
        val total: Int, val finished: Int, val uploaded: Int, val already: Int, val failed: Int,
        val current: String?, val share: Float, val waiting: String?,
    ) {
        val running get() = finished < total
        /** How far along the whole batch is, counting the file in progress. */
        val fraction get() = if (total == 0) 0f else (finished + share) / total
    }

    private val queued = linkedSetOf<String>()
    private val outcomes = HashMap<String, Outcome>()
    private var current: String? = null
    private var share = 0f
    private var waiting: String? = null
    private val _state = MutableStateFlow<State?>(null)
    val state: StateFlow<State?> = _state
    private val _landed = MutableStateFlow(0)
    /** Goes up when a batch that got anything onto the server finishes, so whatever's on screen can show it. */
    val landed: StateFlow<Int> = _landed

    private fun publish() {
        val n = { o: Outcome -> outcomes.values.count { it == o } }
        _state.value = if (queued.isEmpty()) null
            else State(queued.size, outcomes.size, n(Outcome.Uploaded), n(Outcome.Already), n(Outcome.Failed), current, share, waiting)
    }

    /** New files picked. They start fresh, even ones that failed a moment ago. */
    @Synchronized fun queue(uris: List<String>) {
        if (outcomes.size >= queued.size) { queued.clear(); outcomes.clear() }  // the last batch is over
        queued += uris
        uris.forEach { outcomes.remove(it) }
        waiting = null
        publish()
    }

    /** A worker picking its list up: it may have outlived the app's memory, or be retrying after a dropped connection. */
    @Synchronized fun remaining(uris: List<String>): List<String> {
        queued += uris
        publish()
        return uris.filter { it !in outcomes }
    }

    @Synchronized fun working(name: String, fraction: Float) {
        current = name; share = fraction.coerceIn(0f, 1f); waiting = null
        publish()
    }

    @Synchronized fun waiting(why: String) {
        current = null; share = 0f; waiting = why
        publish()
    }

    @Synchronized fun finished(uri: String, outcome: Outcome) {
        outcomes[uri] = outcome
        current = null; share = 0f; waiting = null
        publish()
        if (outcomes.size >= queued.size && outcomes.values.any { it != Outcome.Failed }) _landed.value++
    }

    /** Forget the batch: it's over, or was cancelled. */
    @Synchronized fun dismiss() {
        queued.clear(); outcomes.clear()
        current = null; share = 0f; waiting = null
        publish()
    }
}
