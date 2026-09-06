package com.memorybridge.memorybridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Draws the incoming-call context card over whatever is on screen.
 *
 * Runs as a foreground service so the work survives the broadcast receiver
 * returning. The overlay itself is a WindowManager view rather than an
 * activity, so it floats above the system call UI without taking focus - the
 * user can still answer the call with the popup up.
 */
class CallerPopupService : Service() {

    companion object {
        private const val TAG = "MB/CallerPopup"
        const val ACTION_SHOW = "com.memorybridge.SHOW_POPUP"
        const val ACTION_DISMISS = "com.memorybridge.DISMISS_POPUP"
        const val EXTRA_NUMBER = "number"

        private const val CHANNEL_ID = "memorybridge_call_popup"
        private const val NOTIFICATION_ID = 4711
    }

    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private var windowManager: WindowManager? = null
    private var overlay: View? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_DISMISS -> {
                removeOverlay()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_SHOW -> {
                val number = intent.getStringExtra(EXTRA_NUMBER).orEmpty()
                startForegroundSafely()
                showLoading(number)
                worker.execute { loadBriefing(number) }
            }
            else -> stopSelf()
        }
        return START_NOT_STICKY
    }

    // --- foreground service plumbing ---------------------------------------

    private fun startForegroundSafely() {
        try {
            val manager = getSystemService(NotificationManager::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val channel = NotificationChannel(
                    CHANNEL_ID,
                    "Incoming call context",
                    NotificationManager.IMPORTANCE_LOW,
                ).apply { description = "Shown while MemoryBridge displays caller context." }
                manager.createNotificationChannel(channel)
            }
            val notification: Notification = Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("MemoryBridge")
                .setContentText("Showing caller context")
                .setSmallIcon(android.R.drawable.ic_menu_info_details)
                .setOngoing(true)
                .build()
            startForeground(NOTIFICATION_ID, notification)
        } catch (t: Throwable) {
            // A suppressed notification must not take the popup down with it.
            Log.w(TAG, "Could not enter foreground: ${t.message}")
        }
    }

    // --- overlay ------------------------------------------------------------

    private fun showLoading(number: String) {
        val view = ensureOverlay() ?: return
        view.findViewById<TextView>(R.id.caller_name).text =
            if (number.isBlank()) "Incoming call" else number
        view.findViewById<TextView>(R.id.caller_subtitle).apply {
            text = "Looking up what you remember…"
            visibility = View.VISIBLE
        }
        view.findViewById<View>(R.id.questions_section).visibility = View.GONE
        view.findViewById<View>(R.id.context_section).visibility = View.GONE
    }

    private fun ensureOverlay(): View? {
        overlay?.let { return it }
        return try {
            val manager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
            val view = LayoutInflater.from(this).inflate(R.layout.popup_caller, null)
            view.findViewById<View>(R.id.close_button).setOnClickListener {
                removeOverlay()
                stopSelf()
            }

            val type =
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                else
                    @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE

            val params = WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                type,
                // NOT_FOCUSABLE: never steal key input from the call screen.
                // NOT_TOUCH_MODAL: taps outside the card fall through, so the
                // user can still answer the call with this on screen.
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                    WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
                PixelFormat.TRANSLUCENT,
            ).apply {
                gravity = Gravity.TOP
                y = (48 * resources.displayMetrics.density).toInt()
            }

            manager.addView(view, params)
            windowManager = manager
            overlay = view
            view
        } catch (t: Throwable) {
            Log.e(TAG, "Could not add overlay", t)
            null
        }
    }

    private fun removeOverlay() {
        val view = overlay ?: return
        try {
            windowManager?.removeView(view)
        } catch (t: Throwable) {
            Log.d(TAG, "Overlay already gone: ${t.message}")
        }
        overlay = null
    }

    // --- data ---------------------------------------------------------------

    private fun loadBriefing(number: String) {
        val result = try {
            fetch(number)
        } catch (t: Throwable) {
            Log.w(TAG, "Lookup failed", t)
            Briefing(displayName = number, note = noteForError(t))
        }
        main.post { render(result) }
    }

    private data class Briefing(
        val displayName: String,
        val questions: List<String> = emptyList(),
        val contextLines: List<String> = emptyList(),
        val note: String? = null,
    )

    private fun noteForError(t: Throwable): String = when (t) {
        is java.net.SocketTimeoutException -> "Backend timed out. Showing caller only."
        is java.net.UnknownHostException,
        is java.net.ConnectException -> "Backend unreachable. Showing caller only."
        else -> "Context unavailable right now."
    }

    private fun fetch(number: String): Briefing {
        val base = NativeConfig.backendUrl(this)
        if (base.isEmpty()) {
            return Briefing(number, note = "Set the backend URL in MemoryBridge first.")
        }

        val connection = (URL("$base/callers/lookup").openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            doOutput = true
            connectTimeout = 8_000
            // The phone rings for roughly 25s; a Gemini call is a few seconds.
            readTimeout = 20_000
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("ngrok-skip-browser-warning", "true")
            NativeConfig.geminiKey(this@CallerPopupService)
                .takeIf { it.isNotEmpty() }
                ?.let { setRequestProperty("X-Gemini-Api-Key", it) }
        }

        try {
            connection.outputStream.use { out ->
                val body = JSONObject().put("phone_number", number).toString()
                out.write(body.toByteArray())
            }
            if (connection.responseCode !in 200..299) {
                return Briefing(number, note = "Backend error ${connection.responseCode}.")
            }
            val text = connection.inputStream.bufferedReader().use(BufferedReader::readText)
            return parse(number, JSONObject(text))
        } finally {
            connection.disconnect()
        }
    }

    private fun parse(number: String, json: JSONObject): Briefing {
        val name = json.optString("contact_name").takeIf {
            it.isNotEmpty() && it != "null"
        } ?: number

        val questions = json.optJSONArray("questions").toStringList()
        val contextLines = json.optJSONArray("context").toStringList()

        val note = when {
            json.optString("degraded") == "contact_not_found" ->
                "Unknown contact\nNo relationship context available."
            json.optString("degraded") == "no_memories" ->
                "No previous memories found.\nStart a conversation naturally."
            json.optString("degraded") == "unparseable_number" ->
                "No caller number available."
            json.optString("degraded") == "llm_unavailable" && contextLines.isEmpty() ->
                "Context unavailable right now."
            questions.isEmpty() && contextLines.isEmpty() ->
                "No previous memories found.\nStart a conversation naturally."
            else -> null
        }
        return Briefing(name, questions, contextLines, note)
    }

    private fun org.json.JSONArray?.toStringList(): List<String> {
        if (this == null) return emptyList()
        return (0 until length()).mapNotNull { optString(it).takeIf(String::isNotBlank) }
    }

    // --- rendering ----------------------------------------------------------

    private fun render(briefing: Briefing) {
        val view = overlay ?: return
        view.findViewById<TextView>(R.id.caller_name).text = briefing.displayName

        view.findViewById<TextView>(R.id.caller_subtitle).apply {
            text = briefing.note.orEmpty()
            visibility = if (briefing.note.isNullOrBlank()) View.GONE else View.VISIBLE
        }

        fillSection(
            view.findViewById(R.id.questions_section),
            view.findViewById(R.id.questions_list),
            briefing.questions,
        )
        fillSection(
            view.findViewById(R.id.context_section),
            view.findViewById(R.id.context_list),
            briefing.contextLines,
        )
    }

    private fun fillSection(section: View, list: LinearLayout, items: List<String>) {
        list.removeAllViews()
        if (items.isEmpty()) {
            section.visibility = View.GONE
            return
        }
        section.visibility = View.VISIBLE
        val inflater = LayoutInflater.from(this)
        for (item in items) {
            val row = inflater.inflate(R.layout.popup_bullet, list, false)
            row.findViewById<TextView>(R.id.bullet_text).text = item
            list.addView(row)
        }
    }

    override fun onDestroy() {
        removeOverlay()
        worker.shutdownNow()
        super.onDestroy()
    }
}
