package app.paddock.net

import android.util.Base64
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayInputStream
import java.security.KeyStore
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/**
 * Paddock servers use their own private certificate authority. The app trusts exactly that CA
 * and nothing else: the QR code carries the CA's public-key pin, the CA certificate is fetched once
 * and accepted only if it matches the pin, and from then on every connection is validated against it.
 */
object PinnedTls {

    fun spkiPin(cert: X509Certificate): String =
        "sha256/" + Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(cert.publicKey.encoded), Base64.NO_WRAP)

    fun parse(pem: String): X509Certificate =
        CertificateFactory.getInstance("X.509").generateCertificate(ByteArrayInputStream(pem.toByteArray())) as X509Certificate

    /** One-time, untrusted fetch of the server's CA — accepted only if it matches the QR pin. */
    fun bootstrapCa(baseUrl: String, pin: String): String {
        val trustAll = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
            override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        }
        val ctx = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trustAll), SecureRandom()) }
        val client = OkHttpClient.Builder()
            .sslSocketFactory(ctx.socketFactory, trustAll)
            .hostnameVerifier { _, _ -> true }
            .connectTimeout(4, TimeUnit.SECONDS).readTimeout(6, TimeUnit.SECONDS)
            .build()
        client.newCall(Request.Builder().url("$baseUrl/api/ca.crt").build()).execute().use { r ->
            if (!r.isSuccessful) throw IllegalStateException("Server didn't provide its certificate (${r.code})")
            val pem = r.body!!.string()
            if (spkiPin(parse(pem)) != pin) throw SecurityException("This isn't the server from the QR code — refusing to connect.")
            return pem
        }
    }

    fun trustManager(caPem: String): X509TrustManager {
        val ks = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
            load(null)
            setCertificateEntry("paddock-ca", parse(caPem))
        }
        val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(ks) }
        return tmf.trustManagers.filterIsInstance<X509TrustManager>().first()
    }

    /** A client that trusts only this server's CA. Hostnames aren't checked because LAN/tunnel addresses vary; the CA is the pin. */
    fun client(caPem: String, token: String? = null): OkHttpClient.Builder {
        val tm = trustManager(caPem)
        val ctx = SSLContext.getInstance("TLS").apply { init(null, arrayOf(tm), SecureRandom()) }
        val b = OkHttpClient.Builder()
            .sslSocketFactory(ctx.socketFactory, tm)
            .hostnameVerifier { _, _ -> true }
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
        if (token != null) {
            b.addInterceptor { chain ->
                chain.proceed(chain.request().newBuilder()
                    .header("Authorization", "Bearer $token")
                    .header("User-Agent", "Paddock-Android")
                    .build())
            }
        }
        return b
    }
}
