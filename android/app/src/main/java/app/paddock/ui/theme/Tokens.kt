// Generated from design/tokens.json by design/gen_tokens.py — do not edit by hand.
package app.paddock.ui.theme

import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

object Tokens {
    object Dark {
        val bg = Color(0xFF0F0F0F)
        val bgElev = Color(0xFF181818)
        val surface = Color(0xFF212121)
        val surface2 = Color(0xFF272727)
        val surface3 = Color(0xFF3A3A3A)
        val hover = Color(0x1AFFFFFF)
        val press = Color(0x33FFFFFF)
        val text = Color(0xFFF1F1F1)
        val text2 = Color(0xFFAAAAAA)
        val text3 = Color(0xFF717171)
        val border = Color(0x1AFFFFFF)
        val accent = Color(0xFFFF3B30)
        val accent2 = Color(0xFFFF6A2B)
        val accentText = Color(0xFFFFFFFF)
        val link = Color(0xFF3EA6FF)
        val ok = Color(0xFF2BA640)
        val warn = Color(0xFFF2A60C)
        val danger = Color(0xFFFF4E45)
        val chip = Color(0xFF272727)
        val chipActive = Color(0xFFF1F1F1)
        val chipActiveText = Color(0xFF0F0F0F)
        val scrim = Color(0x99000000)
    }
    object Light {
        val bg = Color(0xFFFFFFFF)
        val bgElev = Color(0xFFFFFFFF)
        val surface = Color(0xFFF2F2F2)
        val surface2 = Color(0xFFE5E5E5)
        val surface3 = Color(0xFFD0D0D0)
        val hover = Color(0x0D000000)
        val press = Color(0x1A000000)
        val text = Color(0xFF0F0F0F)
        val text2 = Color(0xFF606060)
        val text3 = Color(0xFF909090)
        val border = Color(0x1A000000)
        val accent = Color(0xFFFF3B30)
        val accent2 = Color(0xFFFF6A2B)
        val accentText = Color(0xFFFFFFFF)
        val link = Color(0xFF065FD4)
        val ok = Color(0xFF2BA640)
        val warn = Color(0xFFC98A00)
        val danger = Color(0xFFE5352C)
        val chip = Color(0xFFF2F2F2)
        val chipActive = Color(0xFF0F0F0F)
        val chipActiveText = Color(0xFFFFFFFF)
        val scrim = Color(0x73000000)
    }
    object Radius {
        val xs = 4.dp
        val sm = 8.dp
        val md = 12.dp
        val lg = 16.dp
        val pill = 999.dp
    }
    object Space {
        val xs = 4.dp
        val sm = 8.dp
        val md = 12.dp
        val lg = 16.dp
        val xl = 24.dp
        val xxl = 40.dp
    }
    object Type {
        val displaySize = 28.sp
        val displayLine = 34.sp
        const val displayWeight = 700
        val titleSize = 20.sp
        val titleLine = 28.sp
        const val titleWeight = 700
        val headingSize = 16.sp
        val headingLine = 22.sp
        const val headingWeight = 600
        val bodySize = 14.sp
        val bodyLine = 20.sp
        const val bodyWeight = 400
        val labelSize = 12.sp
        val labelLine = 16.sp
        const val labelWeight = 500
        val captionSize = 11.sp
        val captionLine = 14.sp
        const val captionWeight = 400
    }
    object Motion {
        const val fast = 120
        const val base = 200
        const val slow = 320
        const val page = 260
        val standard = CubicBezierEasing(0.2f, 0.0f, 0.0f, 1.0f)
        val decelerate = CubicBezierEasing(0.05f, 0.7f, 0.1f, 1.0f)
        val accelerate = CubicBezierEasing(0.3f, 0.0f, 0.8f, 0.15f)
    }
}
