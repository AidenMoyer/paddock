package app.paddock.ui.screens

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import app.paddock.ui.fmtDuration
import app.paddock.ui.theme.LocalPaddock
import app.paddock.ui.theme.Motion
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive

/**
 * YouTube-style controls drawn in Compose over the video surface. Lives inside the screen's safe
 * area, so system bars, navigation buttons and camera cutouts never cover a control.
 */
@Composable
fun PlayerControls(
    player: ExoPlayer,
    title: String,
    fullscreen: Boolean,
    captionsOn: Boolean,
    hasCaptions: Boolean,
    hasNext: Boolean,
    onMinimize: () -> Unit,
    onToggleFullscreen: () -> Unit,
    onCaptions: () -> Unit,
    onSettings: () -> Unit,
    onNext: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val p = LocalPaddock.current
    var visible by remember { mutableStateOf(true) }
    var playing by remember { mutableStateOf(player.isPlaying) }
    var buffering by remember { mutableStateOf(player.playbackState == Player.STATE_BUFFERING) }
    var position by remember { mutableLongStateOf(player.currentPosition) }
    var duration by remember { mutableLongStateOf(player.duration.coerceAtLeast(0)) }
    var buffered by remember { mutableLongStateOf(0L) }
    var dragging by remember { mutableStateOf<Float?>(null) }
    var seekHint by remember { mutableStateOf<String?>(null) }
    var lastInteraction by remember { mutableLongStateOf(System.currentTimeMillis()) }

    DisposableEffect(player) {
        val l = object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) { playing = isPlaying }
            override fun onPlaybackStateChanged(state: Int) { buffering = state == Player.STATE_BUFFERING }
        }
        player.addListener(l)
        onDispose { player.removeListener(l) }
    }
    LaunchedEffect(player) {
        while (isActive) {
            position = player.currentPosition
            duration = player.duration.coerceAtLeast(0)
            buffered = player.bufferedPosition
            if (visible && playing && dragging == null && System.currentTimeMillis() - lastInteraction > 3000) visible = false
            delay(250)
        }
    }
    LaunchedEffect(seekHint) { if (seekHint != null) { delay(700); seekHint = null } }
    fun poke() { lastInteraction = System.currentTimeMillis(); visible = true }

    Box(modifier
        .fillMaxSize()
        .pointerInput(player) {
            detectTapGestures(
                onTap = { if (visible) visible = false else poke() },
                onDoubleTap = { o ->
                    val forward = o.x > size.width / 2
                    player.seekTo((player.currentPosition + if (forward) 10_000 else -10_000).coerceAtLeast(0))
                    seekHint = if (forward) "10 s »" else "« 10 s"
                },
            )
        }) {

        // thin progress line when controls are hidden (portrait), like YouTube
        if (!visible && !fullscreen && duration > 0) {
            Box(Modifier.align(Alignment.BottomStart).fillMaxWidth().height(2.dp).background(Color(0x44FFFFFF))) {
                Box(Modifier.fillMaxWidth((position.toFloat() / duration).coerceIn(0f, 1f)).height(2.dp).background(p.accent))
            }
        }
        if (buffering) CircularProgressIndicator(Modifier.align(Alignment.Center).size(48.dp), color = Color.White, strokeWidth = 3.dp)
        seekHint?.let {
            Text(it, color = Color.White, fontWeight = FontWeight.Bold,
                modifier = Modifier.align(if (it.startsWith("«")) Alignment.CenterStart else Alignment.CenterEnd).padding(horizontal = 48.dp)
                    .background(Color(0x88000000), RoundedCornerShape(24.dp)).padding(horizontal = 16.dp, vertical = 8.dp))
        }

        AnimatedVisibility(visible, enter = fadeIn(Motion.base()), exit = fadeOut(Motion.base())) {
            Box(Modifier.fillMaxSize().background(Color(0x66000000))) {
                // top row
                Row(Modifier.align(Alignment.TopStart).fillMaxWidth().padding(4.dp), verticalAlignment = Alignment.CenterVertically) {
                    IconButton(onClick = { poke(); if (fullscreen) onToggleFullscreen() else onMinimize() }) {
                        Icon(Icons.Filled.KeyboardArrowDown, if (fullscreen) "Exit full screen" else "Minimize", tint = Color.White)
                    }
                    Text(if (fullscreen) title else "", color = Color.White, maxLines = 1, overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f), fontWeight = FontWeight.Medium)
                    if (hasCaptions) IconButton(onClick = { poke(); onCaptions() }) {
                        Icon(if (captionsOn) Icons.Filled.ClosedCaption else Icons.Filled.ClosedCaptionOff, "Captions", tint = Color.White)
                    }
                    IconButton(onClick = { poke(); onSettings() }) { Icon(Icons.Filled.Settings, "Quality, speed and captions", tint = Color.White) }
                }
                // center row
                Row(Modifier.align(Alignment.Center), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(36.dp)) {
                    IconButton(onClick = { poke(); player.seekTo((player.currentPosition - 10_000).coerceAtLeast(0)) }, Modifier.size(48.dp)) {
                        Icon(Icons.Filled.Replay10, "Back 10 seconds", tint = Color.White, modifier = Modifier.size(32.dp))
                    }
                    AnimatedVisibility(!buffering, enter = scaleIn(Motion.fast()) + fadeIn(), exit = fadeOut()) {
                        IconButton(onClick = {
                            poke()
                            if (player.playbackState == Player.STATE_ENDED) player.seekTo(0)
                            if (player.isPlaying) player.pause() else player.play()
                        }, Modifier.size(64.dp).clip(CircleShape).background(Color(0x55000000))) {
                            Icon(if (playing) Icons.Filled.Pause else Icons.Filled.PlayArrow, if (playing) "Pause" else "Play",
                                tint = Color.White, modifier = Modifier.size(44.dp))
                        }
                    }
                    if (buffering) Spacer(Modifier.size(64.dp))
                    IconButton(onClick = { poke(); player.seekTo(player.currentPosition + 10_000) }, Modifier.size(48.dp)) {
                        Icon(Icons.Filled.Forward10, "Forward 10 seconds", tint = Color.White, modifier = Modifier.size(32.dp))
                    }
                }
                // bottom: time, next, fullscreen, seek bar
                Column(Modifier.align(Alignment.BottomStart).fillMaxWidth().padding(horizontal = 12.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        val shown = dragging?.let { (it * duration).toLong() } ?: position
                        Text("${fmtDuration(shown / 1000.0)} / ${fmtDuration(duration / 1000.0)}", color = Color.White, fontSize = 12.sp)
                        Spacer(Modifier.weight(1f))
                        if (hasNext) IconButton(onClick = { poke(); onNext() }) { Icon(Icons.Filled.SkipNext, "Next", tint = Color.White) }
                        IconButton(onClick = { poke(); onToggleFullscreen() }) {
                            Icon(if (fullscreen) Icons.Filled.FullscreenExit else Icons.Filled.Fullscreen, if (fullscreen) "Exit full screen" else "Full screen", tint = Color.White)
                        }
                    }
                    SeekBar(
                        value = dragging ?: if (duration > 0) position.toFloat() / duration else 0f,
                        buffered = if (duration > 0) buffered.toFloat() / duration else 0f,
                        onChange = { poke(); dragging = it },
                        onDone = { dragging?.let { player.seekTo((it * duration).toLong()) }; dragging = null },
                    )
                }
            }
        }
    }
}

@Composable
private fun SeekBar(value: Float, buffered: Float, onChange: (Float) -> Unit, onDone: () -> Unit) {
    val p = LocalPaddock.current
    Box(Modifier.fillMaxWidth().height(28.dp), contentAlignment = Alignment.Center) {
        Box(Modifier.fillMaxWidth().height(3.dp).background(Color(0x44FFFFFF))) {
            Box(Modifier.fillMaxWidth(buffered.coerceIn(0f, 1f)).height(3.dp).background(Color(0x88FFFFFF)))
        }
        Slider(
            value = value.coerceIn(0f, 1f), onValueChange = onChange, onValueChangeFinished = onDone,
            colors = SliderDefaults.colors(thumbColor = p.accent, activeTrackColor = p.accent, inactiveTrackColor = Color.Transparent),
            modifier = Modifier.fillMaxWidth().height(28.dp),
        )
    }
}
