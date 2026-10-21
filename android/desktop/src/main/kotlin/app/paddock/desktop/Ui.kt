package app.paddock.desktop

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.hoverable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsHoveredAsState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.paddock.data.Group
import app.paddock.data.Item
import app.paddock.ui.theme.LocalPaddock
import app.paddock.ui.theme.Motion

/** The logo's orange → red, used for primary actions and highlights everywhere (matches the web and phone). */
val BrandGradient = Brush.linearGradient(listOf(Color(0xFFFF7A2F), Color(0xFFE5243B)))

fun fmtDuration(s: Double?): String {
    val t = (s ?: 0.0).toLong().coerceAtLeast(0)
    val h = t / 3600; val m = t % 3600 / 60; val sec = t % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, sec) else "%d:%02d".format(m, sec)
}

@Composable
fun SectionTitle(text: String, modifier: Modifier = Modifier) {
    Text(text, style = MaterialTheme.typography.titleMedium, color = LocalPaddock.current.text, modifier = modifier.padding(vertical = 8.dp))
}

@Composable
fun Loading(modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth().padding(48.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator(strokeWidth = 3.dp) }
}

@Composable
fun Problem(text: String, onRetry: (() -> Unit)? = null) {
    val p = LocalPaddock.current
    Column(Modifier.fillMaxWidth().padding(48.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Icon(Icons.Filled.ErrorOutline, null, tint = p.text3, modifier = Modifier.size(40.dp))
        Spacer(Modifier.height(12.dp))
        Text(text, color = p.text2)
        if (onRetry != null) TextButton(onClick = onRetry) { Text("Try again", color = p.link) }
    }
}

@Composable
fun BrandButton(text: String, onClick: () -> Unit, enabled: Boolean = true, icon: ImageVector? = null) {
    val p = LocalPaddock.current
    Row(Modifier.clip(RoundedCornerShape(50)).background(if (enabled) BrandGradient else Brush.linearGradient(listOf(p.surface3, p.surface3)))
        .clickable(enabled = enabled, onClick = onClick).padding(horizontal = 18.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
        if (icon != null) { Icon(icon, null, tint = Color.White, modifier = Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)) }
        Text(text, color = Color.White, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
fun SoftButton(text: String, onClick: () -> Unit, icon: ImageVector? = null, active: Boolean = false) {
    val p = LocalPaddock.current
    Row(Modifier.clip(RoundedCornerShape(50)).background(if (active) p.chipActive else p.surface2).clickable(onClick = onClick)
        .padding(horizontal = 14.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        if (icon != null) { Icon(icon, null, tint = if (active) p.chipActiveText else p.text, modifier = Modifier.size(18.dp)); Spacer(Modifier.width(6.dp)) }
        Text(text, color = if (active) p.chipActiveText else p.text, fontWeight = FontWeight.Medium, fontSize = 14.sp)
    }
}

@Composable
private fun hoverScale(source: MutableInteractionSource): Float {
    val hovered by source.collectIsHoveredAsState()
    return animateFloatAsState(if (hovered) 1.03f else 1f, Motion.fast()).value
}

/** A video, photo, song or file card: thumbnail with duration/progress, then title and subtitle. */
@Composable
fun ItemCard(item: Item, width: Int = 240, onClick: () -> Unit) {
    val p = LocalPaddock.current
    val src = remember { MutableInteractionSource() }
    val scale = hoverScale(src)
    Column(Modifier.width(width.dp).hoverable(src).clickable(interactionSource = src, indication = null, onClick = onClick)) {
        Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f).scale(scale).clip(RoundedCornerShape(12.dp)).background(p.surface2)) {
            if (item.has_thumb) RemoteImage("/api/items/${item.id}/thumb", Modifier.fillMaxSize())
            if (item.duration != null && item.duration!! > 0) {
                Text(fmtDuration(item.duration), color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Medium,
                    modifier = Modifier.align(Alignment.BottomEnd).padding(6.dp).clip(RoundedCornerShape(4.dp)).background(Color(0xCC000000)).padding(horizontal = 4.dp, vertical = 1.dp))
            }
            val prog = item.progress
            if (prog != null && !prog.completed && item.duration != null && item.duration!! > 0) {
                Box(Modifier.align(Alignment.BottomStart).fillMaxWidth().height(3.dp).background(Color(0x66FFFFFF))) {
                    Box(Modifier.fillMaxHeight().fillMaxWidth((prog.position / item.duration!!).toFloat().coerceIn(0f, 1f)).background(BrandGradient))
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        Text(item.title, color = p.text, fontWeight = FontWeight.Medium, maxLines = 2, overflow = TextOverflow.Ellipsis)
        val sub = listOfNotNull(item.show?.let { s -> "$s · S${item.season} E${item.episode}" } ?: item.artist, item.year?.toString() ?: item.date, item.library).take(2)
        if (sub.isNotEmpty()) Text(sub.joinToString(" · "), color = p.text2, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
fun GroupCard(g: Group, width: Int = 200, square: Boolean = false, onClick: () -> Unit) {
    val p = LocalPaddock.current
    val src = remember { MutableInteractionSource() }
    val scale = hoverScale(src)
    Column(Modifier.width(width.dp).hoverable(src).clickable(interactionSource = src, indication = null, onClick = onClick)) {
        Box(Modifier.fillMaxWidth().aspectRatio(if (square) 1f else 16f / 9f).scale(scale).clip(RoundedCornerShape(12.dp)).background(p.surface2)) {
            if (g.cover_id != null) RemoteImage("/api/items/${g.cover_id}/thumb", Modifier.fillMaxSize())
        }
        Spacer(Modifier.height(8.dp))
        Text(g.name, color = p.text, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text("${g.count} items", color = p.text2, fontSize = 13.sp)
    }
}

@Composable
fun Avatar(name: String, size: Int = 32, ring: Boolean = false) {
    val p = LocalPaddock.current
    Box(Modifier.size(size.dp).clip(CircleShape).background(if (ring) BrandGradient else Brush.linearGradient(listOf(p.surface3, p.surface3))).padding(if (ring) 2.dp else 0.dp)
        .clip(CircleShape).background(Color(0xFFFF6A2B)), contentAlignment = Alignment.Center) {
        Text(name.take(1).uppercase(), color = Color.White, fontWeight = FontWeight.Bold, fontSize = (size * 0.45).sp)
    }
}
