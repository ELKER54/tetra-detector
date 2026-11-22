package com.elker54.tetraalert

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Handler
import android.os.HandlerThread
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.math.PI
import kotlin.math.min
import kotlin.math.sin

/**
 * Alert sounds. A beep test in the car showed the
 * navigation-guidance route reaches the car speakers during wireless Android
 * Auto and ducks the music; media also worked; alarm and notification did not.
 * So navigation is the default and media is the only alternative.
 */
object Tones {
    private const val RATE = 44100
    private lateinit var app: Context
    private lateinit var worker: Handler
    private var tts: TextToSpeech? = null
    @Volatile private var ttsReady = false

    fun init(context: Context) {
        if (::app.isInitialized) return
        app = context.applicationContext
        worker = Handler(HandlerThread("tones").apply { start() }.looper)
        tts = TextToSpeech(app) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            if (ttsReady) {
                tts?.language = Locale.UK
                applyVoice()
            }
        }
    }

    /** "Tetra", then: tone 0 weak (one low pip), 1 medium (two), 2 STRONG (three high). */
    fun alert(tone: Int) = play(listOf(pipsFor(tone)), say = "Tetra")

    /** The same pips without the word: repeats while a signal stays STRONG. */
    fun repeat(tone: Int) = play(listOf(pipsFor(tone)))

    /** Said once a STRONG signal has gone weak or silent. */
    fun clear() = play(emptyList(), say = "Tetra clear")

    private fun pipsFor(tone: Int): Pip {
        val (hz, pips) = when (tone) { 0 -> 600.0 to 1; 1 -> 1000.0 to 2; else -> 1600.0 to 3 }
        return Pip(hz, 0.12, pips)
    }

    // ---- voice choice

    private const val VOICE_PREF = "voice"

    /** On-device English voices, UK first. Network voices are left out: no signal, no voice. */
    fun voices(): List<android.speech.tts.Voice> {
        val t = tts ?: return emptyList()
        if (!ttsReady) return emptyList()
        return (t.voices ?: emptySet())
            .filter { it.locale.language == "en" && !it.isNetworkConnectionRequired &&
                TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED !in it.features }
            .sortedWith(compareBy({ it.locale.country != "GB" }, { it.locale.country }, { it.name }))
    }

    /** Short label: accent plus the voice's own code, e.g. "UK · gbd". */
    fun voiceLabel(v: android.speech.tts.Voice): String {
        val accent = when (v.locale.country) {
            "GB" -> "UK"; "US" -> "US"; "AU" -> "Australian"; "IN" -> "Indian"; "NG" -> "Nigerian"
            else -> v.locale.displayCountry.ifEmpty { "English" }
        }
        val code = v.name.substringAfter("-x-", v.name).substringBefore("-")
        return "$accent · $code"
    }

    fun savedVoice(): String? = app.getSharedPreferences("tetra", 0).getString(VOICE_PREF, null)

    fun chooseVoice(name: String?) {
        app.getSharedPreferences("tetra", 0).edit().putString(VOICE_PREF, name).apply()
        applyVoice()
    }

    private fun applyVoice() {
        val t = tts ?: return
        val want = savedVoice()
        val v = want?.let { n -> voices().firstOrNull { it.name == n } }
        if (v != null) t.voice = v else t.language = Locale.UK
    }

    /** Spoken when the link is up and the Pi says the detector is running. */
    fun active() = play(emptyList(), say = "Tetra active")

    /** Falling two-note warning, deliberately unlike any alert. */
    fun offline() = play(listOf(Pip(800.0, 0.25, 1), Pip(400.0, 0.45, 1)))

    private data class Pip(val hz: Double, val secs: Double, val count: Int)

    private fun play(pips: List<Pip>, say: String? = null) {
        worker.post {
            val attrs = AudioAttributes.Builder().apply {
                if (DetectorState.useMedia(app)) {
                    setUsage(AudioAttributes.USAGE_MEDIA)
                    setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                } else {
                    setUsage(AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE)
                    setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                }
            }.build()
            val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                .setAudioAttributes(attrs).build()
            am.requestAudioFocus(focus)
            try {
                if (say != null) speak(say, attrs)
                if (pips.isNotEmpty()) render(attrs, synth(pips))
            } finally {
                am.abandonAudioFocusRequest(focus)
            }
        }
    }

    /** Blocks until spoken (or 4 s), on the same route as the tones. */
    private fun speak(text: String, attrs: AudioAttributes) {
        val t = tts ?: return
        if (!ttsReady) return
        val done = CountDownLatch(1)
        t.setAudioAttributes(attrs)
        t.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(id: String?) {}
            override fun onDone(id: String?) = done.countDown()
            @Deprecated("Deprecated in Java")
            override fun onError(id: String?) = done.countDown()
        })
        t.speak(text, TextToSpeech.QUEUE_FLUSH, null, text)
        done.await(4, TimeUnit.SECONDS)
    }

    private fun synth(pips: List<Pip>): ShortArray {
        val gap = (0.06 * RATE).toInt()
        val out = ArrayList<Short>()
        for (p in pips) repeat(p.count) {
            val n = (p.secs * RATE).toInt()
            for (i in 0 until n) {
                val ramp = min(1.0, min(i, n - i) / (0.01 * RATE))   // no clicks
                out.add((0.6 * ramp * sin(2 * PI * p.hz * i / RATE) * Short.MAX_VALUE).toInt().toShort())
            }
            repeat(gap) { out.add(0) }
        }
        return out.toShortArray()
    }

    private fun render(attrs: AudioAttributes, pcm: ShortArray) {
        val track = AudioTrack.Builder()
            .setAudioAttributes(attrs)
            .setAudioFormat(AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(RATE)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .build())
            .setTransferMode(AudioTrack.MODE_STATIC)
            .setBufferSizeInBytes(pcm.size * 2)
            .build()
        try {
            track.write(pcm, 0, pcm.size)
            track.play()
            Thread.sleep(pcm.size * 1000L / RATE + 150)
        } finally {
            track.release()
        }
    }
}
