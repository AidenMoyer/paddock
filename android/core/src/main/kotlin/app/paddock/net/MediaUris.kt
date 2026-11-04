package app.paddock.net

/** Facts about Android's media addresses, kept free of Android so they can be tested. */
object MediaUris {
    private const val MEDIA_STORE = "media"

    /**
     * Whether asking for the unredacted original can work. Only a real MediaStore row takes it. The photo picker's
     * addresses have the same authority (content://media/picker/... and picker_get_content) but refuse the flag, and
     * Android offers no call to tell them apart, so this goes by the path.
     */
    fun acceptsRequireOriginal(authority: String?, path: String?): Boolean =
        authority == MEDIA_STORE && path?.trimStart('/')?.startsWith("picker") != true

    /** A worker's input has to fit in 10 KB, and a long pick of files with long addresses doesn't. */
    fun batches(uris: List<String>, budget: Int = 8000): List<List<String>> {
        val out = mutableListOf<MutableList<String>>()
        var used = 0
        for (u in uris) {
            val cost = u.length + 8     // the address plus what WorkManager adds around each string
            if (out.isEmpty() || used + cost > budget) {
                out.add(mutableListOf())
                used = 0
            }
            out.last().add(u)
            used += cost
        }
        return out
    }
}
