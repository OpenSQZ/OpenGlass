package org.opensqz.openglass.rokid

import java.net.URI

internal const val USB_BASE_URL = "http://127.0.0.1:18080"

/** Keep URL validation independent of Android so the selection rules can be unit tested. */
internal object ConnectionPolicy {
    fun normalizeUrl(value: String): String {
        val uri = URI(value.trim().trimEnd('/'))
        require(uri.scheme == "http" && !uri.host.isNullOrBlank() &&
            uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null &&
            uri.rawPath.isNullOrEmpty() && (uri.port == -1 || uri.port in 1..65535)) {
            "Enter http://<iPhone address>:18080 without a path or password"
        }
        return uri.toASCIIString()
    }

    fun isLoopback(url: String): Boolean =
        URI(url).host.lowercase() in setOf("127.0.0.1", "localhost", "[::1]", "::1")

    fun websocketUrl(url: String): String = "ws://${URI(url).rawAuthority}/rokid/audio"

    fun receiverMatches(name: String, preferred: String?): Boolean =
        name.matches(Regex("OpenGlass-[0-9a-fA-F]{8}")) &&
            (preferred.isNullOrEmpty() || name == preferred)

    // A previously chosen phone must not silently become a different hotel guest's phone.
    fun selectService(available: Set<String>, preferred: String?): String? = when {
        !preferred.isNullOrEmpty() -> preferred.takeIf { it in available }
        available.size == 1 -> available.first()
        else -> null
    }

    fun nextDelay(delay: Long): Long = (delay * 2).coerceIn(1_000L, 30_000L)
}
