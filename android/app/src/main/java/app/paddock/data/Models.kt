package app.paddock.data

import kotlinx.serialization.KSerializer
import kotlinx.serialization.Serializable
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonDecoder
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull

/** Accepts true/false, 1/0 and "true"/"1" — SQLite-backed flags must never break a screen. */
object LenientBool : KSerializer<Boolean> {
    override val descriptor = PrimitiveSerialDescriptor("LenientBool", PrimitiveKind.BOOLEAN)
    override fun serialize(encoder: Encoder, value: Boolean) = encoder.encodeBoolean(value)
    override fun deserialize(decoder: Decoder): Boolean {
        val el = (decoder as? JsonDecoder)?.decodeJsonElement() ?: return decoder.decodeBoolean()
        val p = el as? JsonPrimitive ?: return false
        return p.booleanOrNull ?: p.doubleOrNull?.let { it != 0.0 } ?: (p.content == "true")
    }
}

val PaddockJson = Json { ignoreUnknownKeys = true; explicitNulls = false; coerceInputValues = true; isLenient = true }

// ---- pairing ---------------------------------------------------------------------------

@Serializable
data class PairPayload(
    val v: Int = 1,
    val app: String = "",
    val name: String = "Paddock",
    val lan: List<String> = emptyList(),
    val pin: String? = null,
    val fp: String? = null,
    val code: String,
    val wg: Boolean = false,
)

@Serializable
data class User(
    val id: Int = 0,
    val username: String = "",
    val display_name: String? = null,
    @Serializable(with = LenientBool::class) val is_admin: Boolean = false,
    @Serializable(with = LenientBool::class) val allow_ai_changes: Boolean = false,
)

@Serializable
data class ServerInfo(val name: String = "Paddock", val version: String = "", val lan: List<String> = emptyList())

@Serializable
data class WgProfile(
    @Serializable(with = LenientBool::class) val enabled: Boolean = false,
    val endpoint: String? = null,
    val server_public_key: String? = null,
    val address: String? = null,
    val allowed_ips: String? = null,
    val server_ip: String? = null,
    val api_port: Int? = null,
    val keepalive: Int? = null,
)

@Serializable
data class PairResponse(
    val token: String,
    val device_id: Int,
    val user: User,
    val server: ServerInfo,
    val wireguard: WgProfile = WgProfile(),
)

// ---- catalog -------------------------------------------------------------------------------

@Serializable
data class Progress(val position: Double = 0.0, @Serializable(with = LenientBool::class) val completed: Boolean = false)

@Serializable
data class Item(
    val id: Int,
    val title: String = "",
    val kind: String = "video",
    val ext: String = "",
    val library_id: Int = 0,
    val library: String? = null,
    val library_kind: String? = null,
    val path: String? = null,
    val size: Long = 0,
    val taken_at: Double? = null,
    val date: String? = null,
    val added_at: Double? = null,
    val duration: Double? = null,
    val width: Int? = null,
    val height: Int? = null,
    val lat: Double? = null,
    val lon: Double? = null,
    @Serializable(with = LenientBool::class) val has_thumb: Boolean = false,
    @Serializable(with = LenientBool::class) val favorite: Boolean = false,
    val group: String? = null,
    val tags: List<String>? = null,
    val artist: String? = null,
    val album: String? = null,
    val show: String? = null,
    val season: Int? = null,
    val episode: Int? = null,
    val year: Int? = null,
    val camera: String? = null,
    val reel: Int? = null,
    val clip: Int? = null,
    val camera_model: String? = null,
    val format: String? = null,
    val category: String? = null,
    val revision: String? = null,
    val print_time: String? = null,
    val filament_type: String? = null,
    val fps: Double? = null,
    val progress: Progress? = null,
    val meta: JsonObject? = null,
    val related: List<Item>? = null,
    val projects: List<ProjectRef>? = null,
    val role: String? = null,
    val note: String? = null,
) {
    val playable: Boolean get() = kind in setOf("video", "audio", "red_clip", "braw_clip")
}

@Serializable
data class ProjectRef(val id: Int, val name: String = "", val role: String? = null)

@Serializable
data class Group(
    val group_key: String,
    val name: String = "",
    val count: Int = 0,
    val size: Long? = null,
    val duration: Double? = null,
    val cover_id: Int? = null,
    val first: Double? = null,
    val last: Double? = null,
    val added: Double? = null,
)

@Serializable
data class ProjectSummary(
    val id: Int,
    val name: String = "",
    val status: String = "active",
    val description: String = "",
    val goal: String = "",
    val location_name: String? = null,
    val item_count: Int = 0,
    val cover_id: Int? = null,
    val updated_at: Double = 0.0,
)

@Serializable
data class Note(val id: Int? = null, val body: String = "", val author: String? = null, val created_at: Double = 0.0)

@Serializable
data class ProjectDetail(
    val id: Int,
    val name: String = "",
    val status: String = "active",
    val description: String = "",
    val goal: String = "",
    val location_name: String? = null,
    val plan: String? = null,
    val items: List<Item> = emptyList(),
    val notes: List<Note> = emptyList(),
)

@Serializable
data class HomeRow(val key: String, val title: String, val type: String, val items: List<JsonElement> = emptyList())

@Serializable
data class HomeFeed(val rows: List<HomeRow> = emptyList())

@Serializable
data class Library(val id: Int, val name: String = "", val kind: String = "mixed", val item_count: Int = 0, val kind_label: String? = null)

// ---- playback --------------------------------------------------------------------------------

@Serializable
data class Quality(val id: String, val label: String = "", val type: String = "hls", val url: String = "", val mime: String? = null)

@Serializable
data class SubtitleTrack(val label: String = "", val url: String = "")

@Serializable
data class LiveCaptionInfo(@Serializable(with = LenientBool::class) val enabled: Boolean = false, val url: String = "", val vtt: String = "", @Serializable(with = LenientBool::class) val complete: Boolean = false)

@Serializable
data class Trickplay(
    val interval: Int = 10, val cols: Int = 10, val rows: Int = 10, val width: Int = 240, val height: Int = 136,
    val count: Int = 0, val sprites: List<String> = emptyList(), val base: String = "",
)

@Serializable
data class Playback(
    val mode: String,
    val reason: String? = null,
    val url: String? = null,
    val mime: String? = null,
    val hls: String? = null,
    val duration: Double? = null,
    val qualities: List<Quality> = emptyList(),
    val default_quality: String = "auto",
    val subtitles: List<SubtitleTrack> = emptyList(),
    val live_captions: LiveCaptionInfo? = null,
    val trickplay: Trickplay? = null,
    val resume_at: Double = 0.0,
)

@Serializable
data class Cue(val start: Double, val end: Double, val text: String)

@Serializable
data class LiveCaptions(@Serializable(with = LenientBool::class) val enabled: Boolean = false, val cues: List<Cue> = emptyList(), @Serializable(with = LenientBool::class) val complete: Boolean = false, val pending: Int = 0)

// ---- assistant / uploads -------------------------------------------------------------------

@Serializable
data class ToolStep(val tool: String? = null, @Serializable(with = LenientBool::class) val ok: Boolean = true)

@Serializable
data class ChatReply(
    val conversation_id: String = "",
    val reply: String = "",
    val steps: List<ToolStep> = emptyList(),
    val model: String? = null,
    val seconds: Double? = null,
)

@Serializable
data class UploadStart(val status: String, val upload_id: String? = null, val offset: Long = 0, val chunk_size: Long = 8L * 1024 * 1024, val item_id: Int? = null)

@Serializable
data class HaveResponse(val have: List<String> = emptyList())

@Serializable
data class AiInfo(val provider: String = "none", val model: String? = null, @Serializable(with = LenientBool::class) val offsite: Boolean = false, @Serializable(with = LenientBool::class) val can_write: Boolean = false, @Serializable(with = LenientBool::class) val available: Boolean = false)
