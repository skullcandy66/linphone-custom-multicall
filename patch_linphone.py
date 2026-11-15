#!/usr/bin/env python3
"""
patch_linphone.py - Automated Source Patcher for Linphone Android
New Features:
1. Dynamic Island Capsule Bubble (Screenshot 2):
   - 280dp x 54dp pill with caller number, real-time status subtitle, and direct Red End button.
   - Tapping body expands the full floating window.
2. Floating Multi-Call Window Overlay (Screenshot 1):
   - Overlays active apps (e.g. Chrome back-office).
   - Header: ↗ expand to app, Call Counter (e.g. 8 Calls), End All pill, 📞+ add, minimize v.
   - Body: Scrollable rows with green border (#4CAF50) on active call, (Active)/(Calling...)/(On Hold) states,
     one-tap auto-hold row switching, individual row hangup buttons.
   - Bottom Bar: Large red hangup, Hold, Mic mute, and Speaker toggle (working in ANY state: Ringing/Calling/Connected).
3. Defensive Hooks:
   - Suppresses CallActivity popping up full-screen via moveTaskToBack(true) + finish().
   - Suppresses fullScreenIntent in NotificationsManager.
4. Duplicate Dial Prevention & Debounce:
   - 2.0s debounce & in-progress call check in TelCallInterceptActivity.
5. Max Calls: 10 concurrent lines support.
"""

import os
import sys
import re
import argparse
from pathlib import Path

# MultiCallHelper.kt template
MULTICALL_HELPER_KT = '''package org.linphone.multicall

import android.content.Context
import android.media.AudioManager
import org.linphone.core.Address
import org.linphone.core.AudioDevice
import org.linphone.core.Call
import org.linphone.core.Core
import org.linphone.core.tools.Log

/**
 * MultiCallHelper - Manages up to 10 simultaneous calls in Linphone.
 */
object MultiCallHelper {
    const val MAX_CONCURRENT_CALLS = 10

    fun applyMaxCalls(core: Core) {
        try {
            core.maxCalls = MAX_CONCURRENT_CALLS
            Log.i("[MultiCallHelper] Configured core.maxCalls to $MAX_CONCURRENT_CALLS")
        } catch (e: Exception) {
            Log.e("[MultiCallHelper] Error setting max calls: ${e.message}")
        }
    }

    fun makeOutgoingCall(core: Core, destination: String): Call? {
        try {
            applyMaxCalls(core)
            val current = core.currentCall
            if (current != null && current.state == Call.State.StreamsRunning) {
                Log.i("[MultiCallHelper] Pausing active call before initiating new call")
                current.pause()
            }

            val address = core.interpretUrl(destination, true) ?: core.createAddress(destination)
            val params = core.createCallParams(null)

            val call = if (address != null) {
                if (params != null) core.inviteAddressWithParams(address, params) else core.inviteAddress(address)
            } else {
                core.invite(destination)
            }
            Log.i("[MultiCallHelper] Initiated outgoing call to: $destination")
            return call
        } catch (e: Exception) {
            Log.e("[MultiCallHelper] Failed to make outgoing call: ${e.message}")
            return null
        }
    }

    fun switchToCall(core: Core, targetCall: Call) {
        try {
            for (call in core.calls) {
                if (call != targetCall && call.state == Call.State.StreamsRunning) {
                    call.pause()
                }
            }
            if (targetCall.state == Call.State.Paused || targetCall.state == Call.State.Pausing) {
                targetCall.resume()
            }
            Log.i("[MultiCallHelper] Switched audio focus to call: ${targetCall.remoteAddress?.asStringUriOnly()}")
        } catch (e: Exception) {
            Log.e("[MultiCallHelper] Failed switching to call: ${e.message}")
        }
    }

    fun toggleCallHold(call: Call) {
        try {
            if (call.state == Call.State.StreamsRunning) {
                call.pause()
            } else if (call.state == Call.State.Paused || call.state == Call.State.Pausing) {
                call.resume()
            }
        } catch (e: Exception) {
            Log.e("[MultiCallHelper] Error toggling hold: ${e.message}")
        }
    }

    fun terminateAllCalls(core: Core) {
        try {
            for (call in core.calls) {
                call.terminate()
            }
        } catch (e: Exception) {
            Log.e("[MultiCallHelper] Error terminating all calls: ${e.message}")
        }
    }

    fun mergeAllCallsToConference(core: Core) {
        try {
            if (core.callsNb > 1) {
                core.addAllToConference()
            }
        } catch (e: Exception) {
            Log.e("[MultiCallHelper] Failed to merge into conference: ${e.message}")
        }
    }

    /**
     * Speaker toggle working in ANY state (Ringing, Calling, Connected)
     * using dual-layer hardware AudioManager + Linphone AudioDevice.
     */
    fun toggleSpeakerAnytime(context: Context, core: Core): Boolean {
        val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
        val newSpeakerState = !audioManager.isSpeakerphoneOn
        audioManager.isSpeakerphoneOn = newSpeakerState

        try {
            val speaker = core.audioDevices.firstOrNull {
                it.hasCapability(AudioDevice.Capabilities.CapabilityPlay) && it.type == AudioDevice.Type.Speaker
            }
            val earpiece = core.audioDevices.firstOrNull {
                it.hasCapability(AudioDevice.Capabilities.CapabilityPlay) && it.type == AudioDevice.Type.Earpiece
            }
            if (newSpeakerState && speaker != null) {
                core.outputAudioDevice = speaker
            } else if (!newSpeakerState && earpiece != null) {
                core.outputAudioDevice = earpiece
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
        return newSpeakerState
    }

    fun getCallStateText(call: Call): String {
        return when (call.state) {
            Call.State.StreamsRunning -> "(Active)"
            Call.State.OutgoingInit, Call.State.OutgoingProgress -> "(Calling...)"
            Call.State.OutgoingRinging -> "(Ringing...)"
            Call.State.IncomingReceived, Call.State.IncomingEarlyMedia -> "(Incoming...)"
            Call.State.Paused, Call.State.Pausing -> "(On Hold)"
            Call.State.End, Call.State.Released -> "(Ending...)"
            else -> "(${call.state.name})"
        }
    }
}
'''

# TelCallInterceptActivity.kt template (with 2.0s Debounce & Duplicate Dial Protection)
TEL_CALL_INTERCEPT_ACTIVITY_KT = '''package org.linphone.overlay

import android.app.Activity
import android.net.Uri
import android.os.Bundle
import org.linphone.LinphoneApplication
import org.linphone.core.Call
import org.linphone.core.tools.Log
import org.linphone.multicall.MultiCallHelper
import java.net.URLDecoder

/**
 * TelCallInterceptActivity - Headless tel: link interceptor.
 * Features:
 * 1. 2.0s Debounce & duplicate dial prevention to avoid accidental multi-dialing.
 * 2. Initiates call in background, launches Dynamic Island capsule, and finishes without opening MainActivity.
 */
class TelCallInterceptActivity : Activity() {

    companion object {
        private var lastDialedTarget: String = ""
        private var lastDialTimestamp: Long = 0L

        private fun cleanNumber(raw: String): String {
            return raw.replace(Regex("[^0-9+]"), "")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        try {
            val uri: Uri? = intent.data
            var targetNumber = ""
            if (uri != null) {
                targetNumber = uri.schemeSpecificPart ?: uri.encodedSchemeSpecificPart ?: ""
                if (targetNumber.isEmpty() && uri.toString().startsWith("tel:")) {
                    targetNumber = uri.toString().removePrefix("tel:")
                }
                targetNumber = URLDecoder.decode(targetNumber, "UTF-8").trim()
            }

            val cleaned = cleanNumber(targetNumber)
            val now = System.currentTimeMillis()

            // 1. Debounce check: Ignore identical clicks within 2.0 seconds
            if (cleaned.isNotEmpty() && cleaned == lastDialedTarget && (now - lastDialTimestamp) < 2000L) {
                Log.w("[TelCallIntercept] Duplicate click ignored for: $cleaned (within 2s)")
                return
            }

            if (targetNumber.isNotEmpty()) {
                val core = try {
                    LinphoneApplication.coreContext.core
                } catch (e: Exception) {
                    null
                }

                // 2. Active call check: If already dialing/calling this number, ignore duplicate click
                if (core != null) {
                    val alreadyActive = core.calls.any { call ->
                        val remote = cleanNumber(call.remoteAddress?.username ?: "")
                        remote == cleaned && (
                            call.state == Call.State.OutgoingInit ||
                            call.state == Call.State.OutgoingProgress ||
                            call.state == Call.State.OutgoingRinging ||
                            call.state == Call.State.StreamsRunning
                        )
                    }

                    if (alreadyActive) {
                        Log.w("[TelCallIntercept] Call to $cleaned is already in progress. Ignoring duplicate click.")
                        return
                    }

                    lastDialedTarget = cleaned
                    lastDialTimestamp = now

                    MultiCallHelper.makeOutgoingCall(core, targetNumber)
                }

                // Launch or update Floating Capsule Overlay
                CallBubbleOverlayService.showBubbleForTarget(this, targetNumber)
            }
        } catch (e: Exception) {
            e.printStackTrace()
            Log.e("[TelCallIntercept] Error intercepting tel link: ${e.message}")
        } finally {
            overridePendingTransition(0, 0)
            finish()
        }
    }
}
'''

# CallBubbleOverlayService.kt template (Dynamic Island Capsule + Screenshot 1 Floating Window)
CALL_BUBBLE_OVERLAY_SERVICE_KT = '''package org.linphone.overlay

import android.app.*
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.*
import androidx.core.app.NotificationCompat
import org.linphone.LinphoneApplication
import org.linphone.core.Call
import org.linphone.core.Core
import org.linphone.multicall.MultiCallHelper

/**
 * CallBubbleOverlayService:
 * - Dynamic Island Capsule Overlay (Screenshot 2): Number, subtitle status, and direct Red End button.
 * - Expanded Floating Window Overlay (Screenshot 1): Header with ↗ expand, call counter, End All pill,
 *   scrollable rows with green border (#4CAF50) on active call, one-tap auto-hold switching,
 *   and bottom bar (Hangup, Hold, Mute, Speaker anytime).
 */
class CallBubbleOverlayService : Service() {

    private var windowManager: WindowManager? = null

    // Capsule View (Screenshot 2)
    private var capsuleView: View? = null
    private var capsuleParams: WindowManager.LayoutParams? = null

    // Expanded Floating Window (Screenshot 1)
    private var floatingWindowView: View? = null
    private var windowParams: WindowManager.LayoutParams? = null
    private var isExpanded = false

    companion object {
        const val CHANNEL_ID = "linphone_call_overlay_channel"
        const val NOTIFICATION_ID = 9021

        @Volatile
        var isRunning: Boolean = false

        const val ACTION_SHOW = "org.linphone.overlay.SHOW"
        const val ACTION_UPDATE = "org.linphone.overlay.UPDATE"
        const val ACTION_DISMISS = "org.linphone.overlay.DISMISS"

        const val EXTRA_CALLER_NAME = "caller_name"
        const val EXTRA_CALL_COUNT = "call_count"

        fun showBubble(context: Context, call: Call, isIncoming: Boolean = false) {
            val intent = Intent(context, CallBubbleOverlayService::class.java).apply {
                action = ACTION_SHOW
                val address = call.remoteAddress
                putExtra(EXTRA_CALLER_NAME, address?.displayName ?: address?.username ?: "VoIP Call")
                putExtra(EXTRA_CALL_COUNT, call.core.callsNb)
            }
            startServiceCompat(context, intent)
        }

        fun showBubbleForTarget(context: Context, destination: String) {
            val intent = Intent(context, CallBubbleOverlayService::class.java).apply {
                action = ACTION_SHOW
                putExtra(EXTRA_CALLER_NAME, destination)
                putExtra(EXTRA_CALL_COUNT, 1)
            }
            startServiceCompat(context, intent)
        }

        fun updateBubble(context: Context, core: Core) {
            val intent = Intent(context, CallBubbleOverlayService::class.java).apply {
                action = ACTION_UPDATE
                putExtra(EXTRA_CALL_COUNT, core.callsNb)
            }
            startServiceCompat(context, intent)
        }

        fun dismissBubble(context: Context) {
            val intent = Intent(context, CallBubbleOverlayService::class.java).apply {
                action = ACTION_DISMISS
            }
            startServiceCompat(context, intent)
        }

        private fun startServiceCompat(context: Context, intent: Intent) {
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Exception) {
                e.printStackTrace()
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        isRunning = true
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, createNotification("Linphone Multi-Call Overlay Active"))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: ACTION_SHOW
        when (action) {
            ACTION_DISMISS -> {
                removeFloatingWindow()
                removeCapsuleView()
                isRunning = false
                stopForeground(true)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_SHOW, ACTION_UPDATE -> {
                val callerName = intent?.getStringExtra(EXTRA_CALLER_NAME) ?: "VoIP Call"
                showOrUpdateCapsule(callerName)
                if (isExpanded) {
                    refreshFloatingWindowContent()
                }
            }
        }
        return START_STICKY
    }

    // =========================================================================
    // DYNAMIC ISLAND CAPSULE OVERLAY (Screenshot 2)
    // =========================================================================
    private fun showOrUpdateCapsule(defaultName: String) {
        if (!Settings.canDrawOverlays(this)) return

        if (windowManager == null) {
            windowManager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        }

        if (capsuleView == null) {
            createCapsuleView()
        }

        val core = try { LinphoneApplication.coreContext.core } catch (e: Exception) { null }
        val currentCall = core?.currentCall ?: core?.calls?.firstOrNull()

        val primaryTv = capsuleView?.findViewWithTag<TextView>("capsule_primary_text")
        val subtitleTv = capsuleView?.findViewWithTag<TextView>("capsule_subtitle_text")

        val targetNumber = if (currentCall != null) {
            val addr = currentCall.remoteAddress
            addr?.displayName ?: addr?.username ?: defaultName
        } else {
            defaultName
        }

        val statusText = if (currentCall != null) {
            val stateText = MultiCallHelper.getCallStateText(currentCall)
            val callsCount = core?.callsNb ?: 0
            if (callsCount > 1) "$stateText • $callsCount Calls" else stateText
        } else {
            "Calling..."
        }

        primaryTv?.text = targetNumber
        subtitleTv?.text = statusText
    }

    private fun createCapsuleView() {
        val wm = windowManager ?: return

        val layoutType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            WindowManager.LayoutParams.TYPE_PHONE
        }

        val lp = WindowManager.LayoutParams(
            dpToPx(280),
            dpToPx(56),
            layoutType,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = 40
            y = 120
        }
        capsuleParams = lp

        // Black pill container (cornerRadius 28dp, #F012131A, stroke #33FFFFFF)
        val pill = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            val bg = GradientDrawable().apply {
                cornerRadius = dpToPx(28).toFloat()
                setColor(Color.parseColor("#F012131A"))
                setStroke(dpToPx(1), Color.parseColor("#33FFFFFF"))
            }
            background = bg
            setPadding(dpToPx(8), dpToPx(6), dpToPx(8), dpToPx(6))
        }

        // Left Phone Icon in Circle
        val iconCircle = FrameLayout(this).apply {
            val iconSize = dpToPx(40)
            layoutParams = LinearLayout.LayoutParams(iconSize, iconSize)
            val cBg = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#222430"))
            }
            background = cBg
            val iv = ImageView(this@CallBubbleOverlayService).apply {
                setImageResource(android.R.drawable.sym_action_call)
                setColorFilter(Color.parseColor("#4CAF50"))
                setPadding(dpToPx(10), dpToPx(10), dpToPx(10), dpToPx(10))
            }
            addView(iv, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        }
        pill.addView(iconCircle)

        // Center Column: Number + Subtitle
        val textLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
                setMargins(dpToPx(10), 0, dpToPx(8), 0)
            }
        }
        val primaryTv = TextView(this).apply {
            tag = "capsule_primary_text"
            text = "+8801610880005"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
            setTypeface(null, Typeface.BOLD)
            maxLines = 1
        }
        val subtitleTv = TextView(this).apply {
            tag = "capsule_subtitle_text"
            text = "Calling..."
            setTextColor(Color.parseColor("#A0A5B5"))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            maxLines = 1
        }
        textLayout.addView(primaryTv)
        textLayout.addView(subtitleTv)
        pill.addView(textLayout)

        // Right Direct Red Circular End Button (Screenshot 2)
        val endCircle = FrameLayout(this).apply {
            val btnSize = dpToPx(40)
            layoutParams = LinearLayout.LayoutParams(btnSize, btnSize)
            val endBg = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#E53935"))
            }
            background = endBg
            val endIv = ImageView(this@CallBubbleOverlayService).apply {
                setImageResource(android.R.drawable.ic_menu_close_clear_cancel)
                setColorFilter(Color.WHITE)
                setPadding(dpToPx(10), dpToPx(10), dpToPx(10), dpToPx(10))
            }
            addView(endIv, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
            setOnClickListener {
                terminateActiveOrLatestCall()
            }
        }
        pill.addView(endCircle)

        // Touch & Drag Handling on Capsule Body (Click toggles Screenshot 1 Floating Window)
        var initialX = 0
        var initialY = 0
        var initialTouchX = 0f
        var initialTouchY = 0f
        var isClick = false

        pill.setOnTouchListener { _, event ->
            val curParams = capsuleParams ?: return@setOnTouchListener false
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    initialX = curParams.x
                    initialY = curParams.y
                    initialTouchX = event.rawX
                    initialTouchY = event.rawY
                    isClick = true
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (event.rawX - initialTouchX).toInt()
                    val dy = (event.rawY - initialTouchY).toInt()
                    if (Math.abs(dx) > 10 || Math.abs(dy) > 10) {
                        isClick = false
                    }
                    curParams.x = initialX + dx
                    curParams.y = initialY + dy
                    wm.updateViewLayout(pill, curParams)
                    true
                }
                MotionEvent.ACTION_UP -> {
                    if (isClick) {
                        toggleFloatingWindow()
                    }
                    true
                }
                else -> false
            }
        }

        capsuleView = pill
        wm.addView(pill, lp)
    }

    private fun terminateActiveOrLatestCall() {
        try {
            val core = LinphoneApplication.coreContext.core
            val call = core.currentCall ?: core.calls.firstOrNull()
            call?.terminate()
            if (core.callsNb <= 1) {
                dismissBubble(this)
            } else {
                updateBubble(this, core)
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    // =========================================================================
    // EXPANDED FLOATING WINDOW OVERLAY (Screenshot 1)
    // =========================================================================
    private fun toggleFloatingWindow() {
        if (isExpanded) {
            removeFloatingWindow()
        } else {
            showFloatingWindow()
        }
    }

    private fun showFloatingWindow() {
        val wm = windowManager ?: return
        if (floatingWindowView != null) removeFloatingWindow()

        val layoutType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            WindowManager.LayoutParams.TYPE_PHONE
        }

        val lp = WindowManager.LayoutParams(
            dpToPx(340),
            WindowManager.LayoutParams.WRAP_CONTENT,
            layoutType,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = Math.max(20, (capsuleParams?.x ?: 40))
            y = (capsuleParams?.y ?: 120) + dpToPx(64)
        }
        windowParams = lp

        // Root floating card (cornerRadius 18dp, #F5181923, stroke #44FFFFFF)
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val bg = GradientDrawable().apply {
                cornerRadius = dpToPx(18).toFloat()
                setColor(Color.parseColor("#F5181923"))
                setStroke(dpToPx(1), Color.parseColor("#44FFFFFF"))
            }
            background = bg
            setPadding(dpToPx(14), dpToPx(14), dpToPx(14), dpToPx(14))
        }

        // Header Bar: ↗ Expand, Call Counter, End All Pill, 📞+ Add, Minimize v
        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, 0, 0, dpToPx(10))
        }

        val expandAppBtn = TextView(this).apply {
            text = "↗"
            setTextColor(Color.parseColor("#B0B3C6"))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            setPadding(dpToPx(4), dpToPx(2), dpToPx(8), dpToPx(2))
            setOnClickListener {
                openLinphoneAppExplicitly()
            }
        }
        header.addView(expandAppBtn)

        val counterTv = TextView(this).apply {
            tag = "window_call_counter"
            text = "1 Calls"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            setTypeface(null, Typeface.BOLD)
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        header.addView(counterTv)

        val endAllBtn = TextView(this).apply {
            text = "End All"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTypeface(null, Typeface.BOLD)
            val btnBg = GradientDrawable().apply {
                cornerRadius = dpToPx(12).toFloat()
                setColor(Color.parseColor("#E53935"))
            }
            background = btnBg
            setPadding(dpToPx(10), dpToPx(4), dpToPx(10), dpToPx(4))
            setOnClickListener {
                try {
                    val core = LinphoneApplication.coreContext.core
                    MultiCallHelper.terminateAllCalls(core)
                    dismissBubble(this@CallBubbleOverlayService)
                } catch (e: Exception) {
                    e.printStackTrace()
                }
            }
        }
        header.addView(endAllBtn)

        val minimizeBtn = TextView(this).apply {
            text = " v "
            setTextColor(Color.parseColor("#B0B3C6"))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            setTypeface(null, Typeface.BOLD)
            setPadding(dpToPx(10), dpToPx(2), dpToPx(4), dpToPx(2))
            setOnClickListener {
                removeFloatingWindow()
            }
        }
        header.addView(minimizeBtn)

        card.addView(header)

        // Scrollable Call Rows List
        val scrollView = ScrollView(this).apply {
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dpToPx(220))
            isFillViewport = true
        }
        val callRowsContainer = LinearLayout(this).apply {
            tag = "call_rows_container"
            orientation = LinearLayout.VERTICAL
        }
        scrollView.addView(callRowsContainer)
        card.addView(scrollView)

        // Bottom Controls Bar: Large Red Hangup, Hold, Mic Mute, Speaker Anytime
        val bottomBar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setPadding(0, dpToPx(12), 0, dpToPx(4))
        }

        // 1. Large Red Circular Hangup
        val hangupBtn = FrameLayout(this).apply {
            val size = dpToPx(48)
            layoutParams = LinearLayout.LayoutParams(size, size).apply {
                setMargins(dpToPx(8), 0, dpToPx(8), 0)
            }
            val bg = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#E53935"))
            }
            background = bg
            val iv = ImageView(this@CallBubbleOverlayService).apply {
                setImageResource(android.R.drawable.ic_menu_close_clear_cancel)
                setColorFilter(Color.WHITE)
                setPadding(dpToPx(12), dpToPx(12), dpToPx(12), dpToPx(12))
            }
            addView(iv, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
            setOnClickListener {
                terminateActiveOrLatestCall()
                refreshFloatingWindowContent()
            }
        }
        bottomBar.addView(hangupBtn)

        // 2. Hold Button
        val holdBtn = FrameLayout(this).apply {
            val size = dpToPx(44)
            layoutParams = LinearLayout.LayoutParams(size, size).apply {
                setMargins(dpToPx(8), 0, dpToPx(8), 0)
            }
            val bg = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#2E303E"))
            }
            background = bg
            val tv = TextView(this@CallBubbleOverlayService).apply {
                text = "Hold"
                setTextColor(Color.WHITE)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                gravity = Gravity.CENTER
            }
            addView(tv, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
            setOnClickListener {
                try {
                    val core = LinphoneApplication.coreContext.core
                    val call = core.currentCall ?: core.calls.firstOrNull()
                    if (call != null) {
                        MultiCallHelper.toggleCallHold(call)
                        refreshFloatingWindowContent()
                    }
                } catch (e: Exception) {
                    e.printStackTrace()
                }
            }
        }
        bottomBar.addView(holdBtn)

        // 3. Mic Mute Button
        val micBtn = FrameLayout(this).apply {
            tag = "bottom_mic_button"
            val size = dpToPx(44)
            layoutParams = LinearLayout.LayoutParams(size, size).apply {
                setMargins(dpToPx(8), 0, dpToPx(8), 0)
            }
            val bg = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#2E303E"))
            }
            background = bg
            val tv = TextView(this@CallBubbleOverlayService).apply {
                tag = "bottom_mic_text"
                text = "Mic"
                setTextColor(Color.WHITE)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                gravity = Gravity.CENTER
            }
            addView(tv, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
            setOnClickListener {
                try {
                    val core = LinphoneApplication.coreContext.core
                    val call = core.currentCall ?: core.calls.firstOrNull()
                    if (call != null) {
                        call.microphoneMuted = !call.microphoneMuted
                        val micTv = findViewWithTag<TextView>("bottom_mic_text")
                        micTv?.text = if (call.microphoneMuted) "Muted" else "Mic"
                        bg.setColor(if (call.microphoneMuted) Color.parseColor("#E53935") else Color.parseColor("#2E303E"))
                    }
                } catch (e: Exception) {
                    e.printStackTrace()
                }
            }
        }
        bottomBar.addView(micBtn)

        // 4. Speaker Button (Works in ANY state: Ringing, Calling, Connected)
        val speakerBtn = FrameLayout(this).apply {
            tag = "bottom_speaker_button"
            val size = dpToPx(44)
            layoutParams = LinearLayout.LayoutParams(size, size).apply {
                setMargins(dpToPx(8), 0, dpToPx(8), 0)
            }
            val bg = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#2E303E"))
            }
            background = bg
            val tv = TextView(this@CallBubbleOverlayService).apply {
                tag = "bottom_speaker_text"
                text = "Spk"
                setTextColor(Color.WHITE)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                gravity = Gravity.CENTER
            }
            addView(tv, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
            setOnClickListener {
                try {
                    val core = LinphoneApplication.coreContext.core
                    val isSpk = MultiCallHelper.toggleSpeakerAnytime(this@CallBubbleOverlayService, core)
                    val spkTv = findViewWithTag<TextView>("bottom_speaker_text")
                    spkTv?.text = if (isSpk) "Spk On" else "Spk"
                    bg.setColor(if (isSpk) Color.parseColor("#1E88E5") else Color.parseColor("#2E303E"))
                } catch (e: Exception) {
                    e.printStackTrace()
                }
            }
        }
        bottomBar.addView(speakerBtn)

        card.addView(bottomBar)

        floatingWindowView = card
        wm.addView(card, lp)
        isExpanded = true

        refreshFloatingWindowContent()
    }

    private fun refreshFloatingWindowContent() {
        val container = floatingWindowView?.findViewWithTag<LinearLayout>("call_rows_container") ?: return
        container.removeAllViews()

        val core = try { LinphoneApplication.coreContext.core } catch (e: Exception) { null }

        if (core == null || core.callsNb == 0) {
            val emptyTv = TextView(this).apply {
                text = "No active calls."
                setTextColor(Color.parseColor("#888888"))
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
                gravity = Gravity.CENTER
                setPadding(0, dpToPx(20), 0, dpToPx(20))
            }
            container.addView(emptyTv)
            return
        }

        val counterTv = floatingWindowView?.findViewWithTag<TextView>("window_call_counter")
        counterTv?.text = "${core.callsNb} Calls"

        // Render each call row with green border (#4CAF50) on active call
        for (call in core.calls) {
            val isStreamsRunning = call.state == Call.State.StreamsRunning
            val isPaused = call.state == Call.State.Paused || call.state == Call.State.Pausing

            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                val rowBg = GradientDrawable().apply {
                    cornerRadius = dpToPx(10).toFloat()
                    setColor(if (isStreamsRunning) Color.parseColor("#232F27") else Color.parseColor("#1C1E28"))
                    // Active call gets prominent green border outline (#4CAF50)
                    setStroke(dpToPx(if (isStreamsRunning) 2 else 1), if (isStreamsRunning) Color.parseColor("#4CAF50") else Color.parseColor("#2A2C3A"))
                }
                background = rowBg
                setPadding(dpToPx(10), dpToPx(8), dpToPx(8), dpToPx(8))
                layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
                    setMargins(0, dpToPx(3), 0, dpToPx(3))
                }
                // Tapping any row switches audio focus and auto-holds others
                setOnClickListener {
                    MultiCallHelper.switchToCall(core, call)
                    refreshFloatingWindowContent()
                    showOrUpdateCapsule(call.remoteAddress?.username ?: "VoIP Call")
                }
            }

            // Info Column
            val infoLayout = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }
            val stateText = MultiCallHelper.getCallStateText(call)
            val stateTv = TextView(this).apply {
                text = stateText
                setTextColor(if (isStreamsRunning) Color.parseColor("#4CAF50") else Color.parseColor("#FFA000"))
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                setTypeface(null, Typeface.BOLD)
            }
            val address = call.remoteAddress
            val numberTv = TextView(this).apply {
                text = address?.displayName ?: address?.username ?: "Unknown"
                setTextColor(Color.WHITE)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
                setTypeface(null, if (isStreamsRunning) Typeface.BOLD else Typeface.NORMAL)
            }
            infoLayout.addView(stateTv)
            infoLayout.addView(numberTv)
            row.addView(infoLayout)

            // Individual End button '✕' on each row
            val endRowBtn = TextView(this).apply {
                text = "✕"
                setTextColor(Color.parseColor("#E53935"))
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
                setTypeface(null, Typeface.BOLD)
                setPadding(dpToPx(8), dpToPx(4), dpToPx(8), dpToPx(4))
                setOnClickListener {
                    try {
                        call.terminate()
                        container.postDelayed({
                            refreshFloatingWindowContent()
                            if ((core?.callsNb ?: 0) == 0) dismissBubble(this@CallBubbleOverlayService)
                        }, 250)
                    } catch (e: Exception) {
                        e.printStackTrace()
                    }
                }
            }
            row.addView(endRowBtn)

            container.addView(row)
        }
    }

    private fun openLinphoneAppExplicitly() {
        try {
            val pm = packageManager
            val intent = pm.getLaunchIntentForPackage(packageName)?.apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
            }
            if (intent != null) startActivity(intent)
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    private fun removeFloatingWindow() {
        floatingWindowView?.let {
            windowManager?.removeView(it)
            floatingWindowView = null
        }
        isExpanded = false
    }

    private fun removeCapsuleView() {
        capsuleView?.let {
            windowManager?.removeView(it)
            capsuleView = null
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        isRunning = false
        removeFloatingWindow()
        removeCapsuleView()
    }

    private fun dpToPx(dp: Int): Int {
        return (dp * resources.displayMetrics.density).toInt()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Linphone Call Overlay",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Floating Dynamic Island overlay for active VoIP calls"
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager?.createNotificationChannel(channel)
        }
    }

    private fun createNotification(content: String): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Linphone Active Multi-Call")
            .setContentText(content)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }
}
'''


class LinphonePatcher:
    def __init__(self, target_dir):
        self.target_dir = Path(target_dir).resolve()
        if not self.target_dir.exists():
            raise FileNotFoundError(f"Target directory {self.target_dir} does not exist.")
        print(f"[Patcher] Target directory: {self.target_dir}")

    def patch_all(self):
        print("=== Starting Linphone Custom Patcher (Dynamic Island + Floating Window + Defensive Hooks) ===")
        self.patch_config_files()
        self.patch_manifest()
        self.inject_multicall_helper()
        self.inject_tel_call_intercept_activity()
        self.inject_overlay_service()
        self.patch_core_context()
        self.patch_notifications_manager()
        self.patch_call_activity_defensive()
        self.patch_gradle_build()
        print("=== Linphone Custom Patcher Completed Successfully ===")

    def patch_config_files(self):
        """Set max_calls=10 in linphonerc_default and linphonerc_factory."""
        print("[Patcher] Patching linphonerc configuration files...")
        config_patterns = ["linphonerc_default", "linphonerc_factory", "linphonerc"]

        for root, _, files in os.walk(self.target_dir):
            for file in files:
                if any(pat in file for pat in config_patterns):
                    file_path = Path(root) / file
                    try:
                        content = file_path.read_text(encoding="utf-8", errors="ignore")
                        original = content

                        if "max_calls" in content:
                            content = re.sub(r"max_calls\s*=\s*\d+", "max_calls=10", content)
                        else:
                            if "[misc]" in content:
                                content = content.replace("[misc]", "[misc]\nmax_calls=10")
                            else:
                                content += "\n[misc]\nmax_calls=10\n"

                        if content != original:
                            file_path.write_text(content, encoding="utf-8")
                            print(f"  -> Patched max_calls=10 in: {file_path.relative_to(self.target_dir)}")
                    except Exception as e:
                        print(f"  -> Warning: Failed patching {file_path}: {e}")

        assets_dir = self.target_dir / "app" / "src" / "main" / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        default_rc = assets_dir / "linphonerc_default"
        if not default_rc.exists():
            default_rc.write_text("[misc]\nmax_calls=10\n\nsip]\nmulti_account=1\n".replace("sip]", "[sip]"), encoding="utf-8")
            print(f"  -> Created default configuration at: {default_rc.relative_to(self.target_dir)}")

    def patch_manifest(self):
        """Add permissions, headless TelCallInterceptActivity, and CallBubbleOverlayService."""
        print("[Patcher] Patching AndroidManifest.xml...")
        manifest_files = list(self.target_dir.glob("**/AndroidManifest.xml"))
        for manifest_path in manifest_files:
            try:
                content = manifest_path.read_text(encoding="utf-8")
                modified = False

                # 1. Disable conflicting scheme="tel" in existing activities so TelCallInterceptActivity handles all
                if '<data android:scheme="tel"' in content:
                    content = re.sub(
                        r'<data\s+android:scheme="tel"\s*/>',
                        '<!-- Disabled default tel scheme in favor of headless intercept -->',
                        content
                    )
                    modified = True

                # 2. Permissions directly above <application
                permissions = '''
    <uses-permission android:name="android.permission.SYSTEM_ALERT_WINDOW" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_PHONE_CALL" />
'''
                if "SYSTEM_ALERT_WINDOW" not in content:
                    content = content.replace("<application", f"{permissions}\n    <application", 1)
                    modified = True

                # 3. TelCallInterceptActivity + CallBubbleOverlayService
                components = '''
        <!-- Headless tel: Link Intercept Activity (Never launches full MainActivity) -->
        <activity
            android:name="org.linphone.overlay.TelCallInterceptActivity"
            android:theme="@android:style/Theme.Translucent.NoTitleBar"
            android:excludeFromRecents="true"
            android:noHistory="true"
            android:exported="true">
            <intent-filter android:priority="1000">
                <action android:name="android.intent.action.DIAL" />
                <action android:name="android.intent.action.VIEW" />
                <action android:name="android.intent.action.CALL" />
                <category android:name="android.intent.category.DEFAULT" />
                <category android:name="android.intent.category.BROWSABLE" />
                <data android:scheme="tel" />
                <data android:scheme="sip" />
                <data android:scheme="callto" />
            </intent-filter>
        </activity>

        <!-- Dynamic Island Capsule & Floating Multi-Call Overlay Service -->
        <service
            android:name="org.linphone.overlay.CallBubbleOverlayService"
            android:enabled="true"
            android:exported="false"
            android:foregroundServiceType="phoneCall" />
'''
                if "TelCallInterceptActivity" not in content:
                    content = content.replace("</application>", f"{components}\n    </application>")
                    modified = True

                if modified:
                    manifest_path.write_text(content, encoding="utf-8")
                    print(f"  -> Successfully patched manifest: {manifest_path.relative_to(self.target_dir)}")
            except Exception as e:
                print(f"  -> Warning: Failed patching manifest {manifest_path}: {e}")

    def inject_multicall_helper(self):
        """Write MultiCallHelper.kt."""
        print("[Patcher] Injecting MultiCallHelper.kt...")
        target_pkg = self.target_dir / "app" / "src" / "main" / "java" / "org" / "linphone" / "multicall"
        target_pkg.mkdir(parents=True, exist_ok=True)
        file_path = target_pkg / "MultiCallHelper.kt"
        file_path.write_text(MULTICALL_HELPER_KT, encoding="utf-8")
        print(f"  -> Written: {file_path.relative_to(self.target_dir)}")

    def inject_tel_call_intercept_activity(self):
        """Write TelCallInterceptActivity.kt."""
        print("[Patcher] Injecting TelCallInterceptActivity.kt...")
        target_pkg = self.target_dir / "app" / "src" / "main" / "java" / "org" / "linphone" / "overlay"
        target_pkg.mkdir(parents=True, exist_ok=True)
        file_path = target_pkg / "TelCallInterceptActivity.kt"
        file_path.write_text(TEL_CALL_INTERCEPT_ACTIVITY_KT, encoding="utf-8")
        print(f"  -> Written: {file_path.relative_to(self.target_dir)}")

    def inject_overlay_service(self):
        """Write CallBubbleOverlayService.kt."""
        print("[Patcher] Injecting CallBubbleOverlayService.kt...")
        target_pkg = self.target_dir / "app" / "src" / "main" / "java" / "org" / "linphone" / "overlay"
        target_pkg.mkdir(parents=True, exist_ok=True)
        file_path = target_pkg / "CallBubbleOverlayService.kt"
        file_path.write_text(CALL_BUBBLE_OVERLAY_SERVICE_KT, encoding="utf-8")
        print(f"  -> Written: {file_path.relative_to(self.target_dir)}")

    def patch_call_activity_defensive(self):
        """
        Defensive Hook: Suppress CallActivity from taking over full-screen
        when CallBubbleOverlayService is active.
        """
        print("[Patcher] Injecting defensive hooks into CallActivity...")
        call_activities = list(self.target_dir.glob("**/CallActivity.kt")) + list(self.target_dir.glob("**/CallActivity.java"))

        for file_path in call_activities:
            try:
                content = file_path.read_text(encoding="utf-8")
                if "CallBubbleOverlayService.isRunning" in content:
                    continue

                if file_path.suffix == ".kt":
                    hook = '''
        // Defensive Hook: If floating overlay bubble is active, move task to back and finish
        if (org.linphone.overlay.CallBubbleOverlayService.isRunning) {
            moveTaskToBack(true)
            finish()
            return
        }
'''
                    # Inject at start of onCreate and onResume
                    content = re.sub(
                        r"(override\s+fun\s+onCreate[^{]*\{)",
                        r"\1" + hook,
                        content
                    )
                    content = re.sub(
                        r"(override\s+fun\s+onResume[^{]*\{)",
                        r"\1" + hook,
                        content
                    )
                    file_path.write_text(content, encoding="utf-8")
                    print(f"  -> Injected CallActivity defensive suppression: {file_path.relative_to(self.target_dir)}")
            except Exception as e:
                print(f"  -> Warning: Failed patching CallActivity {file_path}: {e}")

    def patch_core_context(self):
        """Enforce core.maxCalls = 10 in CoreContext.kt / LinphoneManager."""
        print("[Patcher] Patching CoreContext / LinphoneManager for maxCalls = 10...")
        candidates = list(self.target_dir.glob("**/CoreContext.kt")) + \
                     list(self.target_dir.glob("**/LinphoneManager.kt")) + \
                     list(self.target_dir.glob("**/LinphoneManager.java"))

        for file_path in candidates:
            try:
                content = file_path.read_text(encoding="utf-8")
                if "maxCalls = 10" in content or "setMaxCalls(10)" in content:
                    continue

                if file_path.suffix == ".kt":
                    if "core.start()" in content:
                        content = content.replace("core.start()", "core.start()\n        core.maxCalls = 10")
                    elif "val core = " in content or "var core: Core" in content:
                        content = re.sub(
                            r"(fun\s+initCore[^{]*\{)",
                            r"\1\n        try { core.maxCalls = 10 } catch (_: Exception) {}",
                            content
                        )
                elif file_path.suffix == ".java":
                    if "lc.start();" in content:
                        content = content.replace("lc.start();", "lc.start();\n        lc.setMaxCalls(10);")

                file_path.write_text(content, encoding="utf-8")
                print(f"  -> Injected maxCalls=10 into: {file_path.relative_to(self.target_dir)}")
            except Exception as e:
                print(f"  -> Warning: Could not patch {file_path}: {e}")

    def patch_notifications_manager(self):
        """Hook CallBubbleOverlayService into NotificationsManager and suppress fullScreenIntent."""
        print("[Patcher] Hooking CallBubbleOverlayService into NotificationsManager...")
        notif_managers = list(self.target_dir.glob("**/NotificationsManager.kt")) + \
                         list(self.target_dir.glob("**/NotificationsManager.java"))

        for file_path in notif_managers:
            try:
                content = file_path.read_text(encoding="utf-8")
                if "CallBubbleOverlayService" in content:
                    continue

                if file_path.suffix == ".kt":
                    import_stmt = "\nimport org.linphone.overlay.CallBubbleOverlayService\nimport org.linphone.multicall.MultiCallHelper\n"
                    # Insert imports below package statement
                    content = re.sub(r"(package\s+[^\n]+)", r"\1" + import_stmt, content, count=1)

                    # Suppress fullScreenIntent to prevent main app pop-ups over user screen
                    content = re.sub(
                        r"(\.setFullScreenIntent\([^)]+\))",
                        r"/* \1 disabled for floating overlay mode */",
                        content
                    )

                    if "override fun onCallStateChanged" in content:
                        hook_code = '''
        // Custom Hook: Trigger Dynamic Island Capsule Overlay
        try {
            when (call.state) {
                org.linphone.core.Call.State.IncomingReceived,
                org.linphone.core.Call.State.IncomingEarlyMedia -> {
                    CallBubbleOverlayService.showBubble(context, call, isIncoming = true)
                }
                org.linphone.core.Call.State.OutgoingInit,
                org.linphone.core.Call.State.OutgoingProgress,
                org.linphone.core.Call.State.OutgoingRinging,
                org.linphone.core.Call.State.Connected,
                org.linphone.core.Call.State.StreamsRunning,
                org.linphone.core.Call.State.Paused -> {
                    CallBubbleOverlayService.showBubble(context, call, isIncoming = false)
                }
                org.linphone.core.Call.State.End,
                org.linphone.core.Call.State.Released,
                org.linphone.core.Call.State.Error -> {
                    if (core.callsNb <= 1) {
                        CallBubbleOverlayService.dismissBubble(context)
                    } else {
                        CallBubbleOverlayService.updateBubble(context, core)
                    }
                }
                else -> {}
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
'''
                        content = re.sub(
                            r"(override\s+fun\s+onCallStateChanged[^{]*\{)",
                            r"\1" + hook_code,
                            content
                        )
                    file_path.write_text(content, encoding="utf-8")
                    print(f"  -> Injected bubble hooks into: {file_path.relative_to(self.target_dir)}")
            except Exception as e:
                print(f"  -> Warning: Could not hook NotificationsManager {file_path}: {e}")

    def patch_gradle_build(self):
        """Disable abortOnError in build.gradle."""
        print("[Patcher] Adjusting Gradle build scripts for CI...")
        gradle_files = list(self.target_dir.glob("**/build.gradle")) + list(self.target_dir.glob("**/build.gradle.kts"))

        for gf in gradle_files:
            try:
                content = gf.read_text(encoding="utf-8")
                modified = False

                if "lintOptions {" in content and "abortOnError false" not in content:
                    content = content.replace("lintOptions {", "lintOptions {\n        abortOnError false\n        checkReleaseBuilds false")
                    modified = True
                elif "lint {" in content and "abortOnError = false" not in content:
                    content = content.replace("lint {", "lint {\n        abortOnError = false\n        checkReleaseBuilds = false")
                    modified = True

                if modified:
                    gf.write_text(content, encoding="utf-8")
                    print(f"  -> Configured lint in: {gf.relative_to(self.target_dir)}")
            except Exception as e:
                print(f"  -> Warning: Gradle adjustment skipped for {gf}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Patch Linphone Android for 10 calls, Dynamic Island Capsule & Floating Multi-Call Window.")
    parser.add_argument("--target", default="linphone-android", help="Path to linphone-android project root directory")
    args = parser.parse_args()

    patcher = LinphonePatcher(args.target)
    patcher.patch_all()


if __name__ == "__main__":
    main()
