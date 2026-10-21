package app.paddock.net

/** Small JSON client over the active, pinned connection. */
class Api(private val conn: ConnectionManager) : JsonApi(
    clientOf = { conn.client },
    baseOf = { conn.ensure() ?: throw ApiException(0, conn.state.value.detail.ifBlank { "Not connected" }) },
) {
    // URLs handed to the image loader and the player (they go through the same pinned client)
    fun thumb(id: Int): String = (conn.state.value.baseUrl ?: "") + "/api/items/$id/thumb"
    fun preview(id: Int): String = (conn.state.value.baseUrl ?: "") + "/api/items/$id/preview"
    fun absolute(path: String): String = if (path.startsWith("http")) path else (conn.state.value.baseUrl ?: "") + path
}
