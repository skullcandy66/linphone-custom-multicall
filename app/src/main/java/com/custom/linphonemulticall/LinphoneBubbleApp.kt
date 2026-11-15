package com.custom.linphonemulticall

import android.Manifest
import android.app.Activity
import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import org.linphone.core.Account
import org.linphone.core.AudioDevice
import org.linphone.core.Call
import org.linphone.core.Core
import org.linphone.core.CoreListenerStub
import org.linphone.core.Factory
import org.linphone.core.RegistrationState
import org.linphone.core.TransportType
import kotlin.math.abs

class LinphoneApp : Application() {
    override fun onCreate() {
        super.onCreate()
        LinphoneManager.init(this)
    }
}

object LinphoneManager {
    const val MAX_CALLS = 10
    var core: Core? = null
        private set
    var statusText: String = "Not Registered"
        private set
    var isSpeakerOn: Boolean = false
        private set

    private val handler = Handler(Looper.getMainLooper())
    private val listeners = mutableSetOf<() -> Unit>()

    private val ticker = object : Runnable {
        override fun run() {
            core?.iterate()
            handler.postDelayed(this, 20L)
        }
    }

    private val coreListener = object : CoreListenerStub() {
        override fun onAccountRegistrationStateChanged(
            core: Core,
            account: Account,
            state: RegistrationState?,
            message: String
        ) {
            statusText = "${state?.name ?: "Unknown"}: $message"
            notifyUi()
        }

        override fun onCallStateChanged(
            core: Core,
            call: Call,
            state: Call.State?,
            message: String
        ) {
            notifyUi()
        }
    }

    fun init(context: Context) {
        if (core != null) return
        Factory.instance().setDebugMode(true, "LinphoneMultiCall")
        val c = Factory.instance().createCore(null, null, context.applicationContext)
        c.maxCalls = MAX_CALLS
        c.addListener(coreListener)
        c.start()
        core = c
        handler.post(ticker)
    }

    fun addListener(l: () -> Unit) { listeners.add(l) }
    fun removeListener(l: () -> Unit) { listeners.remove(l) }
    private fun notifyUi() { handler.post { listeners.forEach { it.invoke() } } }

    fun register(user: String, pass: String, domain: String, transport: TransportType) {
        val c = core ?: return
        c.clearAccounts()
        c.clearAllAuthInfo()
        c.addAuthInfo(Factory.instance().createAuthInfo(user, null, pass, null, null, domain))
        val params = c.createAccountParams()
        params.identityAddress = Factory.instance().createAddress("sip:$user@$domain")
        val server = Factory.instance().createAddress("sip:$domain")
        server?.transport = transport
        params.serverAddress = server
        params.isRegisterEnabled = true
        val acc = c.createAccount(params)
        c.addAccount(acc)
        c.defaultAccount = acc
        statusText = "Registering..."
        notifyUi()
    }

    fun call(target: String): Boolean {
        val c = core ?: return false
        if (c.callsNb >= MAX_CALLS) return false
        val addr = c.interpretUrl(target.trim()) ?: return false
        val params = c.createCallParams(null) ?: return false
        c.inviteAddressWithParams(addr, params)
        notifyUi()
        return true
    }

    fun toggleSpeaker() {
        val c = core ?: return
        isSpeakerOn = !isSpeakerOn
        val type = if (isSpeakerOn) AudioDevice.Type.Speaker else AudioDevice.Type.Earpiece
        c.audioDevices.firstOrNull { it.type == type }?.let { c.outputAudioDevice = it }
        notifyUi()
    }

    fun endAll() {
        core?.terminateAllCalls()
        notifyUi()
    }
}

class FloatingBubbleService : Service() {
    private lateinit var wm: WindowManager
    private lateinit var bubbleView: View
    private lateinit var windowView: View
    private lateinit var bubbleParams: WindowManager.LayoutParams
    private lateinit var windowParams: WindowManager.LayoutParams
    private lateinit var badgeTv: TextView
    private lateinit var titleTv: TextView
    private lateinit var callsBox: LinearLayout
    private lateinit var speakerBtn: Button
    private var open = false

    private val uiListener: () -> Unit = { refresh() }

    override fun onCreate() {
        super.onCreate()
        startNotif()
        wm = getSystemService(WINDOW_SERVICE) as WindowManager
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }

        bubbleParams = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = 24
            y = 240
        }

        windowParams = WindowManager.LayoutParams(
            dp(320),
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT
        ).apply { gravity = Gravity.CENTER }

        bubbleView = buildBubble()
        windowView = buildWindow()
        wm.addView(bubbleView, bubbleParams)
        LinphoneManager.addListener(uiListener)
        refresh()
    }

    private fun buildBubble(): View {
        val root = FrameLayout(this)
        val circle = TextView(this).apply {
            text = "SIP"
            setTextColor(Color.WHITE)
            textSize = 14f
            gravity = Gravity.CENTER
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#1565C0"))
                setStroke(dp(2), Color.WHITE)
            }
        }
        root.addView(circle, FrameLayout.LayoutParams(dp(56), dp(56)))

        badgeTv = TextView(this).apply {
            text = "0"
            setTextColor(Color.WHITE)
            textSize = 11f
            gravity = Gravity.CENTER
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#E53935"))
                setStroke(dp(1), Color.WHITE)
            }
        }
        root.addView(badgeTv, FrameLayout.LayoutParams(dp(22), dp(22), Gravity.TOP or Gravity.END))

        var ix = 0; var iy = 0; var tx = 0f; var ty = 0f; var moved = false
        root.setOnTouchListener { _, e ->
            when (e.action) {
                MotionEvent.ACTION_DOWN -> { ix = bubbleParams.x; iy = bubbleParams.y; tx = e.rawX; ty = e.rawY; moved = false; true }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (e.rawX - tx).toInt(); val dy = (e.rawY - ty).toInt()
                    if (abs(dx) > 10 || abs(dy) > 10) moved = true
                    bubbleParams.x = ix + dx; bubbleParams.y = iy + dy
                    wm.updateViewLayout(root, bubbleParams); true
                }
                MotionEvent.ACTION_UP -> {
                    if (!moved) openWindow() else {
                        val w = resources.displayMetrics.widthPixels
                        bubbleParams.x = if (bubbleParams.x + root.width / 2 < w / 2) 16 else w - root.width - 16
                        wm.updateViewLayout(root, bubbleParams)
                    }
                    true
                }
                else -> false
            }
        }
        return root
    }

    private fun buildWindow(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(12), dp(12), dp(12))
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#F8FAFC"))
                cornerRadius = dp(16).toFloat()
                setStroke(dp(1), Color.parseColor("#CBD5E1"))
            }
        }
        val header = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        titleTv = TextView(this).apply { text = "MultiCall (0/10)"; setTextColor(Color.BLACK); textSize = 15f }
        header.addView(titleTv, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        header.addView(Button(this).apply { text = "Bubble"; setOnClickListener { closeWindow() } })
        header.addView(Button(this).apply { text = "X"; setOnClickListener { stopSelf() } })
        root.addView(header)

        val dial = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val et = EditText(this).apply { hint = "Number / SIP URI"; setTextColor(Color.BLACK) }
        dial.addView(et, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        dial.addView(Button(this).apply {
            text = "Call"
            setOnClickListener {
                val t = et.text.toString()
                if (t.isNotBlank()) {
                    if (!LinphoneManager.call(t)) Toast.makeText(this@FloatingBubbleService, "Max 10 calls or invalid URI", Toast.LENGTH_SHORT).show()
                    else et.text.clear()
                }
            }
        })
        root.addView(dial)

        val scroll = ScrollView(this)
        callsBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        scroll.addView(callsBox)
        root.addView(scroll, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(210)))

        val footer = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        speakerBtn = Button(this).apply { text = "Speaker"; setOnClickListener { LinphoneManager.toggleSpeaker() } }
        footer.addView(speakerBtn, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        footer.addView(Button(this).apply { text = "End All"; setOnClickListener { LinphoneManager.endAll() } },
            LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(footer)
        return root
    }

    private fun openWindow() {
        if (open) return
        bubbleView.visibility = View.GONE
        wm.addView(windowView, windowParams)
        open = true
        refresh()
    }

    private fun closeWindow() {
        if (!open) return
        wm.removeView(windowView)
        bubbleView.visibility = View.VISIBLE
        open = false
    }

    private fun refresh() {
        val calls = LinphoneManager.core?.calls ?: emptyArray()
        badgeTv.text = calls.size.toString()
        if (!open) return
        titleTv.text = "MultiCall (${calls.size}/10)"
        speakerBtn.text = if (LinphoneManager.isSpeakerOn) "Speaker: ON" else "Speaker: OFF"
        callsBox.removeAllViews()
        for (c in calls) {
            val item = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(4), dp(4), dp(4), dp(4)) }
            item.addView(TextView(this).apply { text = "${c.remoteAddress.asStringUriOnly()} [${c.state.name}]"; setTextColor(Color.BLACK); textSize = 12f })
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            row.addView(Button(this).apply {
                text = when (c.state) {
                    Call.State.IncomingReceived -> "Accept"
                    Call.State.Paused, Call.State.Pausing, Call.State.PausedByRemote -> "Resume"
                    else -> "Hold"
                }
                setOnClickListener {
                    when (c.state) {
                        Call.State.IncomingReceived -> c.accept()
                        Call.State.Paused, Call.State.Pausing, Call.State.PausedByRemote -> c.resume()
                        else -> c.pause()
                    }
                }
            })
            row.addView(Button(this).apply {
                text = if (c.microphoneMuted) "Unmute" else "Mute"
                setOnClickListener { c.microphoneMuted = !c.microphoneMuted; refresh() }
            })
            row.addView(Button(this).apply { text = "End"; setOnClickListener { c.terminate() } })
            item.addView(row)
            callsBox.addView(item)
        }
    }

    private fun startNotif() {
        val ch = "linphone_bubble"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(ch, "Floating Call Bubble", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val pi = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(this, ch)
            .setContentTitle("Linphone MultiCall")
            .setContentText("Chat head ready (max 10 calls)")
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentIntent(pi)
            .build()
        startForeground(1001, n)
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    override fun onDestroy() {
        super.onDestroy()
        LinphoneManager.removeListener(uiListener)
        if (open) runCatching { wm.removeView(windowView) }
        runCatching { wm.removeView(bubbleView) }
    }

    override fun onBind(intent: Intent?): IBinder? = null
}

class MainActivity : Activity() {
    private lateinit var statusTv: TextView
    private lateinit var activeTv: TextView
    private val uiListener: () -> Unit = {
        statusTv.text = "Status: ${LinphoneManager.statusText}"
        activeTv.text = "Active Calls: ${LinphoneManager.core?.callsNb ?: 0} / 10"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestPerms()

        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(40, 60, 40, 40) }
        root.addView(TextView(this).apply { text = "Linphone MultiCall (Max 10)"; textSize = 20f })
        statusTv = TextView(this).apply { text = "Status: Not Registered" }
        root.addView(statusTv)

        val etUser = EditText(this).apply { hint = "SIP Username" }
        val etPass = EditText(this).apply { hint = "Password"; inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD }
        val etDomain = EditText(this).apply { hint = "Domain (e.g. sip.linphone.org)" }
        val sp = Spinner(this).apply {
            adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, listOf("UDP", "TCP", "TLS"))
        }
        root.addView(etUser); root.addView(etPass); root.addView(etDomain); root.addView(sp)

        root.addView(Button(this).apply {
            text = "Register SIP"
            setOnClickListener {
                val u = etUser.text.toString().trim(); val p = etPass.text.toString(); val d = etDomain.text.toString().trim()
                if (u.isEmpty() || d.isEmpty()) Toast.makeText(this@MainActivity, "Enter username & domain", Toast.LENGTH_SHORT).show()
                else {
                    val tr = when (sp.selectedItem.toString()) { "TCP" -> TransportType.Tcp; "TLS" -> TransportType.Tls; else -> TransportType.Udp }
                    LinphoneManager.register(u, p, d, tr)
                }
            }
        })

        root.addView(Button(this).apply {
            text = "Launch Floating Chat Head Bubble"
            setOnClickListener {
                if (!Settings.canDrawOverlays(this@MainActivity)) {
                    startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
                } else {
                    val i = Intent(this@MainActivity, FloatingBubbleService::class.java)
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(i) else startService(i)
                }
            }
        })

        val etCallee = EditText(this).apply { hint = "Number or sip:user@domain" }
        root.addView(etCallee)
        root.addView(Button(this).apply {
            text = "Call"
            setOnClickListener { if (!LinphoneManager.call(etCallee.text.toString())) Toast.makeText(this@MainActivity, "Cannot place call", Toast.LENGTH_SHORT).show() }
        })
        root.addView(Button(this).apply { text = "End All Calls"; setOnClickListener { LinphoneManager.endAll() } })
        activeTv = TextView(this).apply { text = "Active Calls: 0 / 10" }
        root.addView(activeTv)

        setContentView(ScrollView(this).apply { addView(root) })
    }

    override fun onResume() { super.onResume(); LinphoneManager.addListener(uiListener); uiListener() }
    override fun onPause() { super.onPause(); LinphoneManager.removeListener(uiListener) }

    private fun requestPerms() {
        val list = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) list.add(Manifest.permission.POST_NOTIFICATIONS)
        val missing = list.filter { ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), 101)
    }
}
