package com.memorybridge.memorybridge

import android.content.Context
import android.content.SharedPreferences

/**
 * Settings the native call-popup layer needs, mirrored out of Dart.
 *
 * The Flutter side owns configuration (Setup screen -> shared_preferences), but
 * the popup runs in a broadcast receiver / service with no Flutter engine
 * attached, so it cannot read those. Rather than depend on the
 * shared_preferences plugin's internal storage format, AppConfig.save() pushes
 * the few values needed here through a MethodChannel into this dedicated file.
 */
object NativeConfig {
    const val PREFS_NAME = "memorybridge_native"

    const val KEY_BACKEND_URL = "backend_url"
    const val KEY_GEMINI_KEY = "gemini_api_key"
    const val KEY_POPUP_ENABLED = "popup_enabled"

    private fun prefs(context: Context): SharedPreferences =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun backendUrl(context: Context): String =
        prefs(context).getString(KEY_BACKEND_URL, "")?.trim()?.trimEnd('/') ?: ""

    fun geminiKey(context: Context): String =
        prefs(context).getString(KEY_GEMINI_KEY, "")?.trim() ?: ""

    /** The popup is opt-in - the user turns it on after granting permissions. */
    fun popupEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_POPUP_ENABLED, false)

    fun save(context: Context, backendUrl: String?, geminiKey: String?, popupEnabled: Boolean?) {
        val editor = prefs(context).edit()
        backendUrl?.let { editor.putString(KEY_BACKEND_URL, it) }
        geminiKey?.let { editor.putString(KEY_GEMINI_KEY, it) }
        popupEnabled?.let { editor.putBoolean(KEY_POPUP_ENABLED, it) }
        editor.apply()
    }
}
