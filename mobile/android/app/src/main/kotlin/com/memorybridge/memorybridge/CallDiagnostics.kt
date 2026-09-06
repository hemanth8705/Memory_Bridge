package com.memorybridge.memorybridge

import android.content.Context
import android.util.Log
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * A persistent breadcrumb trail for the incoming-call path.
 *
 * The whole chain - broadcast, permission checks, service start, overlay
 * creation, HTTP - runs while the app is backgrounded or killed, so plain
 * Logcat is invisible to anyone without adb attached. Every step writes here
 * as well, and the Call popup screen reads it back, so a failure can be
 * diagnosed on the phone itself.
 */
object CallDiagnostics {
    const val TAG = "MemoryBridge"

    private const val PREFS = "memorybridge_diagnostics"
    private const val KEY_TRACE = "trace"
    private const val MAX_LINES = 60
    private const val SEPARATOR = "\n"

    private val stamp = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)

    /** Record one step. [ok] false marks it as the point the chain broke. */
    @Synchronized
    fun record(context: Context, step: String, detail: String = "", ok: Boolean = true) {
        val marker = if (ok) "OK  " else "FAIL"
        val line = "${stamp.format(Date())}  $marker  $step" +
            if (detail.isNotEmpty()) " - $detail" else ""

        if (ok) Log.i(TAG, line) else Log.w(TAG, line)

        try {
            val prefs = context.applicationContext
                .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val existing = prefs.getString(KEY_TRACE, "").orEmpty()
            val lines = (if (existing.isEmpty()) emptyList() else existing.split(SEPARATOR)) + line
            prefs.edit()
                .putString(KEY_TRACE, lines.takeLast(MAX_LINES).joinToString(SEPARATOR))
                .apply()
        } catch (t: Throwable) {
            Log.e(TAG, "Could not persist diagnostics: ${t.message}")
        }
    }

    /** Marks the start of a fresh call so the trace is easy to read. */
    fun startRun(context: Context, label: String) {
        record(context, "──────── $label ────────")
    }

    fun read(context: Context): String =
        context.applicationContext
            .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_TRACE, "").orEmpty()

    fun clear(context: Context) {
        context.applicationContext
            .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().remove(KEY_TRACE).apply()
    }
}
