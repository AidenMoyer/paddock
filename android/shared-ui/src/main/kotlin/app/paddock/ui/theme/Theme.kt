package app.paddock.ui.theme

import androidx.compose.animation.core.FiniteAnimationSpec
import androidx.compose.animation.core.tween
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight

/** Paddock's palette — generated from design/tokens.json, identical to the web app's CSS variables. */
@Immutable
data class PaddockColors(
    val bg: Color, val bgElev: Color, val surface: Color, val surface2: Color, val surface3: Color,
    val text: Color, val text2: Color, val text3: Color, val border: Color, val accent: Color, val accent2: Color,
    val link: Color, val ok: Color, val warn: Color, val danger: Color, val chip: Color, val chipActive: Color,
    val chipActiveText: Color, val scrim: Color, val dark: Boolean,
)

private val DarkPalette = with(Tokens.Dark) {
    PaddockColors(bg, bgElev, surface, surface2, surface3, text, text2, text3, border, accent, accent2, link, ok, warn, danger,
        chip, chipActive, chipActiveText, scrim, true)
}
private val LightPalette = with(Tokens.Light) {
    PaddockColors(bg, bgElev, surface, surface2, surface3, text, text2, text3, border, accent, accent2, link, ok, warn, danger,
        chip, chipActive, chipActiveText, scrim, false)
}

val LocalPaddock = staticCompositionLocalOf { DarkPalette }

object Motion {
    fun <T> fast(): FiniteAnimationSpec<T> = tween(Tokens.Motion.fast, easing = Tokens.Motion.standard)
    fun <T> base(): FiniteAnimationSpec<T> = tween(Tokens.Motion.base, easing = Tokens.Motion.standard)
    fun <T> enter(): FiniteAnimationSpec<T> = tween(Tokens.Motion.page, easing = Tokens.Motion.decelerate)
    fun <T> exit(): FiniteAnimationSpec<T> = tween(Tokens.Motion.base, easing = Tokens.Motion.accelerate)
}

private fun style(size: androidx.compose.ui.unit.TextUnit, line: androidx.compose.ui.unit.TextUnit, weight: Int) =
    TextStyle(fontSize = size, lineHeight = line, fontWeight = FontWeight(weight))

private val PaddockType = Typography(
    headlineMedium = style(Tokens.Type.displaySize, Tokens.Type.displayLine, Tokens.Type.displayWeight),
    titleLarge = style(Tokens.Type.titleSize, Tokens.Type.titleLine, Tokens.Type.titleWeight),
    titleMedium = style(Tokens.Type.headingSize, Tokens.Type.headingLine, Tokens.Type.headingWeight),
    bodyLarge = style(Tokens.Type.headingSize, Tokens.Type.headingLine, 400),
    bodyMedium = style(Tokens.Type.bodySize, Tokens.Type.bodyLine, Tokens.Type.bodyWeight),
    labelLarge = style(Tokens.Type.bodySize, Tokens.Type.bodyLine, 500),
    labelMedium = style(Tokens.Type.labelSize, Tokens.Type.labelLine, Tokens.Type.labelWeight),
    bodySmall = style(Tokens.Type.labelSize, Tokens.Type.labelLine, 400),
    labelSmall = style(Tokens.Type.captionSize, Tokens.Type.captionLine, Tokens.Type.captionWeight),
)

@Composable
fun PaddockTheme(theme: String = "system", content: @Composable () -> Unit) {
    val dark = when (theme) { "dark" -> true; "light" -> false; else -> isSystemInDarkTheme() }
    val p = if (dark) DarkPalette else LightPalette
    val scheme = if (dark) darkColorScheme(
        primary = p.accent, onPrimary = Color.White, secondary = p.accent2, background = p.bg, onBackground = p.text,
        surface = p.bg, onSurface = p.text, surfaceVariant = p.surface, onSurfaceVariant = p.text2, outline = p.border,
        surfaceContainer = p.surface, surfaceContainerHigh = p.surface2, surfaceContainerHighest = p.surface3,
        surfaceContainerLow = p.bgElev, error = p.danger, tertiary = p.link,
    ) else lightColorScheme(
        primary = p.accent, onPrimary = Color.White, secondary = p.accent2, background = p.bg, onBackground = p.text,
        surface = p.bg, onSurface = p.text, surfaceVariant = p.surface, onSurfaceVariant = p.text2, outline = p.border,
        surfaceContainer = p.surface, surfaceContainerHigh = p.surface2, surfaceContainerHighest = p.surface3,
        surfaceContainerLow = p.bgElev, error = p.danger, tertiary = p.link,
    )
    val shapes = Shapes(
        extraSmall = RoundedCornerShape(Tokens.Radius.xs), small = RoundedCornerShape(Tokens.Radius.sm),
        medium = RoundedCornerShape(Tokens.Radius.md), large = RoundedCornerShape(Tokens.Radius.lg),
        extraLarge = RoundedCornerShape(Tokens.Radius.lg),
    )
    CompositionLocalProvider(LocalPaddock provides p) {
        MaterialTheme(colorScheme = scheme, typography = PaddockType, shapes = shapes, content = content)
    }
}
