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
}
