package com.memorybridge.memorybridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import android.telephony.TelephonyManager
import android.util.Log

/**
 * Watches for incoming calls and drives the context popup.
 *
 *   RINGING  -> show the popup (this is the moment that matters)
 *   OFFHOOK  -> leave it up; the context is most useful *during* the call
 *   IDLE     -> call over, dismiss
 *
 * Reading EXTRA_INCOMING_NUMBER requires READ_CALL_LOG on Android 9+, in
 * addition to READ_PHONE_STATE. Without it the broadcast still arrives but the
 * number is null, so we bail rather than showing a useless popup.
 */
class CallReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "MB/CallReceiver"

        /** RINGING fires repeatedly on some OEMs; ignore repeats of the same call. */
        private var lastHandledNumber: String? = null
        private var lastHandledAt: Long = 0
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != TelephonyManager.ACTION_PHONE_STATE_CHANGED) return

        val state = intent.getStringExtra(TelephonyManager.EXTRA_STATE)
        if (state == TelephonyManager.EXTRA_STATE_IDLE) {
            lastHandledNumber = null
            dismiss(context)
            return
        }
        if (state != TelephonyManager.EXTRA_STATE_RINGING) return

        if (!NativeConfig.popupEnabled(context)) {
            Log.d(TAG, "Popup disabled in settings - ignoring incoming call.")
            return
        }
        if (!canDrawOverlays(context)) {
            Log.w(TAG, "No overlay permission - cannot show the call popup.")
            return
        }

        @Suppress("DEPRECATION")
        val number = intent.getStringExtra(TelephonyManager.EXTRA_INCOMING_NUMBER)
        if (number.isNullOrBlank()) {
            // Almost always means READ_CALL_LOG was not granted.
            Log.w(TAG, "Incoming call with no number available (READ_CALL_LOG missing?).")
            return
        }

        val now = System.currentTimeMillis()
        if (number == lastHandledNumber && now - lastHandledAt < 10_000) return
        lastHandledNumber = number
        lastHandledAt = now

        Log.i(TAG, "Incoming call - showing context popup.")
        val show = Intent(context, CallerPopupService::class.java).apply {
            action = CallerPopupService.ACTION_SHOW
            putExtra(CallerPopupService.EXTRA_NUMBER, number)
        }
        startService(context, show)
    }

    private fun dismiss(context: Context) {
        val stop = Intent(context, CallerPopupService::class.java).apply {
            action = CallerPopupService.ACTION_DISMISS
        }
        // Plain startService: if the service is not running there is nothing to
        // dismiss, and we must not spin one up just to stop it.
        try {
            context.startService(stop)
        } catch (t: Throwable) {
            Log.d(TAG, "Nothing to dismiss: ${t.message}")
        }
    }

    private fun startService(context: Context, intent: Intent) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        } catch (t: Throwable) {
            Log.e(TAG, "Could not start popup service", t)
        }
    }

    private fun canDrawOverlays(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(context)
}
