package com.memorybridge.memorybridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import android.telephony.TelephonyManager

/**
 * Watches for incoming calls and drives the context popup.
 *
 *   RINGING  -> show the popup (this is the moment that matters)
 *   OFFHOOK  -> leave it up; the context is most useful *during* the call
 *   IDLE     -> call over, dismiss
 *
 * Every step reports to CallDiagnostics, because this all runs with the app
 * backgrounded: if the popup does not appear, the trace on the Call popup
 * screen says which step failed.
 *
 * Note on the caller's number: EXTRA_INCOMING_NUMBER is only populated if
 * READ_CALL_LOG is granted (Android 9+), and that is a *hard-restricted*
 * permission some installers refuse to allowlist. A missing number is
 * therefore NOT treated as a failure - the popup still opens, as an unknown
 * caller. Showing something beats showing nothing.
 */
class CallReceiver : BroadcastReceiver() {

    companion object {
        /** RINGING fires repeatedly on some OEMs; ignore repeats of the same call. */
        private var lastHandledNumber: String? = null
        private var lastHandledAt: Long = 0
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != TelephonyManager.ACTION_PHONE_STATE_CHANGED) {
            return
        }

        val state = intent.getStringExtra(TelephonyManager.EXTRA_STATE)

        if (state == TelephonyManager.EXTRA_STATE_IDLE) {
            lastHandledNumber = null
            CallDiagnostics.record(context, "Call ended (IDLE) - dismissing popup")
            dismiss(context)
            return
        }
        if (state == TelephonyManager.EXTRA_STATE_OFFHOOK) {
            // Answered. Leave the card up - this is when the context is used.
            CallDiagnostics.record(context, "Call answered (OFFHOOK) - leaving popup up")
            return
        }
        if (state != TelephonyManager.EXTRA_STATE_RINGING) {
            return
        }

        CallDiagnostics.startRun(context, "INCOMING CALL")
        CallDiagnostics.record(context, "1. Broadcast received", "state=RINGING")

        // --- the number (optional) -----------------------------------------
        @Suppress("DEPRECATION")
        val number = intent.getStringExtra(TelephonyManager.EXTRA_INCOMING_NUMBER)
        if (number.isNullOrBlank()) {
            CallDiagnostics.record(
                context,
                "2. Caller number",
                "NOT provided - READ_CALL_LOG likely not granted. " +
                    "Continuing as unknown caller.",
            )
        } else {
            CallDiagnostics.record(context, "2. Caller number", "received (${number.length} digits)")
        }

        // --- gates ----------------------------------------------------------
        if (!NativeConfig.popupEnabled(context)) {
            CallDiagnostics.record(
                context,
                "3. Popup enabled check",
                "OFF - turn on 'Show context on incoming calls' in MemoryBridge",
                ok = false,
            )
            return
        }
        CallDiagnostics.record(context, "3. Popup enabled check", "on")

        if (!canDrawOverlays(context)) {
            CallDiagnostics.record(
                context,
                "4. Overlay permission",
                "NOT granted - 'Display over other apps' is off",
                ok = false,
            )
            return
        }
        CallDiagnostics.record(context, "4. Overlay permission", "granted")

        // De-duplicate repeated RINGING broadcasts for the same call. Only
        // applies when we actually have a number to compare.
        val now = System.currentTimeMillis()
        if (!number.isNullOrBlank()) {
            if (number == lastHandledNumber && now - lastHandledAt < 10_000) {
                CallDiagnostics.record(context, "Duplicate RINGING broadcast - ignored")
                return
            }
            lastHandledNumber = number
        }
        lastHandledAt = now

        // --- start the overlay service --------------------------------------
        val show = Intent(context, CallerPopupService::class.java).apply {
            action = CallerPopupService.ACTION_SHOW
            putExtra(CallerPopupService.EXTRA_NUMBER, number.orEmpty())
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(show)
            } else {
                context.startService(show)
            }
            CallDiagnostics.record(context, "5. Popup service start requested")
        } catch (t: Throwable) {
            CallDiagnostics.record(
                context,
                "5. Popup service start",
                "${t.javaClass.simpleName}: ${t.message}",
                ok = false,
            )
        }
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
            CallDiagnostics.record(context, "Dismiss skipped", "${t.message}")
        }
    }

    private fun canDrawOverlays(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(context)
}
