package com.kaltsit.msmobile.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

private val AppColorScheme = darkColorScheme(
    primary = Accent,
    secondary = AccentHover,
    background = Bg,
    surface = Surface,
    onPrimary = Text,
    onSecondary = Text,
    onBackground = Text,
    onSurface = Text,
    error = Danger,
)

@Composable
fun KaltsitTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = AppColorScheme,
        typography = AppTypography,
        content = content,
    )
}
