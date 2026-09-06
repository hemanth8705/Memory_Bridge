package com.memorybridge.memorybridge

import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {

    companion object {
        private const val CHANNEL = "memorybridge/call_popup"
        private const val CALL_PERMISSION_REQUEST = 9101

        // READ_CALL_LOG is the one that actually carries the caller's number on
        // Android 9+. permission_handler has no binding for it, so both call
        // permissions are requested here instead.
        private val CALL_PERMISSIONS = arrayOf(
            android.Manifest.permission.READ_PHONE_STATE,
            android.Manifest.permission.READ_CALL_LOG,
        )
    }

    private var pendingPermissionResult: MethodChannel.Result? = null

    private fun granted(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) ==
            PackageManager.PERMISSION_GRANTED

    /**
     * Only READ_PHONE_STATE is required. READ_CALL_LOG is a hard-restricted
     * permission that some installers refuse to allowlist; without it the
     * popup still appears, just without the caller's number, so it must not
     * gate the feature.
     */
    private fun hasCallPermissions(): Boolean =
        granted(android.Manifest.permission.READ_PHONE_STATE)

    private fun isIgnoringBatteryOptimizations(): Boolean = try {
        val manager = getSystemService(PowerManager::class.java)
        manager?.isIgnoringBatteryOptimizations(packageName) ?: false
    } catch (t: Throwable) {
        false
    }

    private fun canDrawOverlays(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(this)

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != CALL_PERMISSION_REQUEST) return
        // Success means READ_PHONE_STATE landed. READ_CALL_LOG is a bonus: on
        // devices where it cannot be granted, requiring it here would leave the
        // feature permanently un-enableable.
        pendingPermissionResult?.success(hasCallPermissions())
        pendingPermissionResult = null
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    // Dart owns configuration; mirror what the popup needs into
                    // native SharedPreferences, which the receiver/service can
                    // read without a Flutter engine attached.
                    "saveConfig" -> {
                        NativeConfig.save(
                            context = applicationContext,
                            backendUrl = call.argument("backend_url"),
                            geminiKey = call.argument("gemini_api_key"),
                            popupEnabled = call.argument("popup_enabled"),
                        )
                        result.success(true)
                    }

                    "isPopupEnabled" ->
                        result.success(NativeConfig.popupEnabled(applicationContext))

                    "hasCallPermissions" -> result.success(hasCallPermissions())

                    // Reported separately: phone state is required for the
                    // popup to work at all, call log only adds the number.
                    "permissionDetail" -> result.success(
                        mapOf(
                            "phone_state" to granted(android.Manifest.permission.READ_PHONE_STATE),
                            "call_log" to granted(android.Manifest.permission.READ_CALL_LOG),
                            "overlay" to canDrawOverlays(),
                            "battery_unrestricted" to isIgnoringBatteryOptimizations(),
                        ),
                    )

                    "getDiagnostics" ->
                        result.success(CallDiagnostics.read(applicationContext))

                    "clearDiagnostics" -> {
                        CallDiagnostics.clear(applicationContext)
                        result.success(true)
                    }

                    "openBatterySettings" -> {
                        try {
                            startActivity(
                                Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS),
                            )
                        } catch (t: Throwable) {
                            startActivity(
                                Intent(
                                    Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                    Uri.parse("package:$packageName"),
                                ),
                            )
                        }
                        result.success(true)
                    }

                    // Draws the card with canned content: proves the overlay
                    // path works without a call or the backend.
                    "testOverlay" -> {
                        val intent = Intent(this, CallerPopupService::class.java).apply {
                            action = CallerPopupService.ACTION_TEST_OVERLAY
                        }
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(intent)
                        } else {
                            startService(intent)
                        }
                        result.success(true)
                    }

                    "requestCallPermissions" -> {
                        if (hasCallPermissions()) {
                            result.success(true)
                        } else {
                            pendingPermissionResult = result
                            ActivityCompat.requestPermissions(
                                this, CALL_PERMISSIONS, CALL_PERMISSION_REQUEST,
                            )
                        }
                    }

                    "canDrawOverlays" -> result.success(canDrawOverlays())

                    // Many OEM ROMs suppress the runtime dialog for call log
                    // (it is a hard-restricted permission) but still allow it
                    // to be switched on by hand from App info -> Permissions.
                    "openAppSettings" -> {
                        startActivity(
                            Intent(
                                Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                Uri.parse("package:$packageName"),
                            ),
                        )
                        result.success(true)
                    }

                    "openOverlaySettings" -> {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                            startActivity(
                                Intent(
                                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                    Uri.parse("package:$packageName"),
                                ),
                            )
                        }
                        result.success(true)
                    }

                    // Lets the Setup screen prove the whole popup path works
                    // without waiting for a real call to come in.
                    "showTestPopup" -> {
                        val number = call.argument<String>("phone_number").orEmpty()
                        val intent = Intent(this, CallerPopupService::class.java).apply {
                            action = CallerPopupService.ACTION_SHOW
                            putExtra(CallerPopupService.EXTRA_NUMBER, number)
                        }
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(intent)
                        } else {
                            startService(intent)
                        }
                        result.success(true)
                    }

                    "dismissPopup" -> {
                        startService(
                            Intent(this, CallerPopupService::class.java)
                                .apply { action = CallerPopupService.ACTION_DISMISS },
                        )
                        result.success(true)
                    }

                    else -> result.notImplemented()
                }
            }
    }
}
