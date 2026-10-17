package org.opensqz.openglass.rokid

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.URI
import java.net.URL
import java.util.concurrent.Executors

internal data class ServerEndpoint(val baseUrl: String, val network: Network?, val source: String) {
    val websocketUrl: String get() = ConnectionPolicy.websocketUrl(baseUrl)
    fun connection(path: String): HttpURLConnection =
        (network?.openConnection(URL(baseUrl + path)) ?: URL(baseUrl + path).openConnection()) as HttpURLConnection
}

/** All mutable state belongs to the main looper; only bounded HTTP probes run on the worker. */
internal class EndpointController(
    context: Context,
    private val onEndpoint: (ServerEndpoint?) -> Unit,
    private val onStatus: (String) -> Unit,
) {
    private val app = context.applicationContext
    private val prefs = app.getSharedPreferences("pc_connection", Context.MODE_PRIVATE)
    private val main = Handler(Looper.getMainLooper())
    private val connectivity = app.getSystemService(ConnectivityManager::class.java)
    private val nsd = app.getSystemService(NsdManager::class.java)
    private val worker = Executors.newSingleThreadExecutor { r -> Thread(r, "openglass-discovery") }
    private var active = false
    private var generation = 0
    private var probing = false
    private var fixedUrl: String? = null
    private var wifi: Network? = null
    private var gateway: String? = null
    private var endpoint: ServerEndpoint? = null
    private var retryDelay = 1_000L
    private var discovery: NsdManager.DiscoveryListener? = null
    private var multicast: WifiManager.MulticastLock? = null
    private val services = linkedMapOf<String, String>()
    private val knownServices = linkedMapOf<String, NsdServiceInfo>()
    private val resolveQueue = ArrayDeque<NsdServiceInfo>()
    private var resolving = false
    private var discoveryStartedAt = 0L
    private val probeTask = Runnable { probe() }

    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            if (!active || network == wifi) return
            wifi = network
            gateway = null
            networkChanged()
        }
        override fun onLinkPropertiesChanged(network: Network, properties: LinkProperties) {
            if (!active || network != wifi) return
            val address = properties.routes.firstOrNull { it.isDefaultRoute && it.gateway is Inet4Address }
                ?.gateway?.hostAddress
            if (address != gateway) {
                gateway = address
                scheduleProbe(0)
            }
        }
        override fun onLost(network: Network) {
            if (!active || network != wifi) return
            wifi = null
            gateway = null
            networkChanged()
        }
    }

    fun start(explicitUrl: String?) {
        if (active) stop()
        active = true
        fixedUrl = explicitUrl
        retryDelay = 1_000L
        wifi = connectivity.allNetworks.firstOrNull {
            connectivity.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        }
        gateway = wifi?.let { connectivity.getLinkProperties(it) }?.routes
            ?.firstOrNull { it.isDefaultRoute && it.gateway is Inet4Address }?.gateway?.hostAddress
        connectivity.registerNetworkCallback(
            NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build(),
            networkCallback, main,
        )
        if (fixedUrl != null) publish(endpointFor(fixedUrl!!, "manual / USB")) else {
            startDiscovery()
            scheduleProbe(0)
        }
    }

    fun stop() {
        if (!active) return
        active = false
        generation++
        main.removeCallbacks(probeTask)
        try { connectivity.unregisterNetworkCallback(networkCallback) } catch (_: IllegalArgumentException) { }
        stopDiscovery()
        publish(null)
    }

    fun destroy() { stop(); worker.shutdownNow() }

    fun retry() {
        // A WebSocket failure does not make an unverified host eligible for sensor uploads.
        if (active && fixedUrl == null) scheduleProbe(1_000)
    }

    private fun endpointFor(url: String, source: String) =
        ServerEndpoint(url, if (ConnectionPolicy.isLoopback(url)) null else wifi, source)

    private fun networkChanged() {
        generation++
        services.clear()
        knownServices.clear()
        retryDelay = 1_000L
        if (fixedUrl != null) {
            publish(endpointFor(fixedUrl!!, "manual / USB"))
        } else {
            publish(null)
            stopDiscovery()
            startDiscovery()
            scheduleProbe(500)
        }
    }

    private fun publish(value: ServerEndpoint?) {
        if (endpoint == value) return
        endpoint = value
        onEndpoint(value)
    }

    private fun scheduleProbe(delay: Long) {
        if (!active || fixedUrl != null) return
        main.removeCallbacks(probeTask)
        main.postDelayed(probeTask, delay)
    }

    private fun startDiscovery() {
        if (!active || wifi == null || discovery != null) return
        discoveryStartedAt = android.os.SystemClock.elapsedRealtime()
        val token = generation
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(type: String) = Unit
            override fun onDiscoveryStopped(type: String) = Unit
            override fun onStartDiscoveryFailed(type: String, code: Int) { main.post {
                if (active && token == generation) {
                    stopDiscovery()
                    onStatus("Bonjour unavailable; checking iPhone hotspot / saved address")
                }
            } }
            override fun onStopDiscoveryFailed(type: String, code: Int) = Unit
            override fun onServiceFound(service: NsdServiceInfo) { main.post {
                if (active && token == generation &&
                    (knownServices.containsKey(service.serviceName) || knownServices.size < 8) &&
                    resolveQueue.none { it.serviceName == service.serviceName } &&
                    service.serviceType.trimEnd('.') == "_openglass._tcp") {
                    knownServices[service.serviceName] = service
                    resolveQueue.addLast(service)
                    resolveNext(token)
                }
            } }
            override fun onServiceLost(service: NsdServiceInfo) { main.post {
                if (active && token == generation) {
                    services.remove(service.serviceName)
                    knownServices.remove(service.serviceName)
                    scheduleProbe(500)
                }
            } }
        }
        discovery = listener
        try {
            multicast = app.getSystemService(WifiManager::class.java).createMulticastLock("openglass-discovery")
                .apply { setReferenceCounted(false); acquire() }
            nsd.discoverServices("_openglass._tcp.", NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            stopDiscovery()
            Log.w("OpenGlassRokid", "Discovery unavailable: ${e.javaClass.simpleName}")
        }
    }

    private fun stopDiscovery() {
        discovery?.let { try { nsd.stopServiceDiscovery(it) } catch (_: Exception) { } }
        discovery = null
        resolveQueue.clear()
        resolving = false
        multicast?.let { if (it.isHeld) it.release() }
        multicast = null
    }

    @Suppress("DEPRECATION") // Glasses run Android versions predating the API 34 service-info callback.
    private fun resolveNext(token: Int) {
        if (resolving || resolveQueue.isEmpty() || !active || token != generation) return
        resolving = true
        val item = resolveQueue.removeFirst()
        val listener = object : NsdManager.ResolveListener {
            override fun onResolveFailed(service: NsdServiceInfo, code: Int) { main.post {
                if (token == generation) { resolving = false; resolveNext(token) }
            } }
            override fun onServiceResolved(service: NsdServiceInfo) { main.post {
                if (active && token == generation) {
                    resolving = false
                    val host = service.host?.hostAddress
                    if (host != null && service.port in 1..65535) {
                        runCatching { URI("http", null, host, service.port, null, null, null).toASCIIString() }
                            .getOrNull()?.let { url ->
                                val changed = services[service.serviceName] != url
                                services[service.serviceName] = url
                                if (changed) scheduleProbe(500)
                            }
                    }
                    resolveNext(token)
                }
            } }
        }
        try { nsd.resolveService(item, listener) } catch (_: Exception) {
            resolving = false
            resolveNext(token)
        }
    }

    private fun probe() {
        if (!active || fixedUrl != null) return
        if (wifi == null) {
            publish(null)
            onStatus("Join the iPhone hotspot or shared Wi-Fi first")
            scheduleProbe(retryDelay)
            retryDelay = ConnectionPolicy.nextDelay(retryDelay)
            return
        }
        if (probing) { scheduleProbe(500); return }
        if (discovery == null) startDiscovery()
        probing = true
        val token = generation
        val hotspot = gateway?.let { endpointFor("http://$it:18080", "iPhone hotspot") }
        val discovered = services.mapValues { endpointFor(it.value, "Bonjour: ${it.key}") }
        val saved = prefs.getString("pc_base_url", null)?.let {
            runCatching { ConnectionPolicy.normalizeUrl(it) }.getOrNull()
                ?.takeUnless { url -> ConnectionPolicy.isLoopback(url) }
                ?.let { url -> endpointFor(url, "saved address") }
        }
        val preferred = prefs.getString("iphone_service_name", null)
        val existing = endpoint
        val settled = android.os.SystemClock.elapsedRealtime() - discoveryStartedAt >= 2_500
        worker.execute {
            val checked = mutableMapOf<ServerEndpoint, HealthIdentity?>()
            fun healthy(candidate: ServerEndpoint?): Boolean = candidate != null &&
                checked.getOrPut(candidate) { health(candidate) }?.let {
                    ConnectionPolicy.receiverMatches(it.receiverName, preferred)
                } == true
            // Keep a healthy chosen endpoint; reconnecting never redirects a live session.
            var chosen = existing?.takeIf { healthy(it) }
            var ambiguous = false
            if (chosen == null && healthy(hotspot)) chosen = hotspot
            if (chosen == null && settled) {
                val available = discovered.filter { (name, candidate) ->
                    healthy(candidate) && checked[candidate]?.receiverName == name
                }
                val chosenName = ConnectionPolicy.selectService(available.keys, preferred)
                chosen = chosenName?.let { available[it] }
                ambiguous = available.isNotEmpty() && chosen == null
            }
            if (chosen == null && !ambiguous && healthy(saved)) chosen = saved
            val chosenName = chosen?.let { checked[it]?.receiverName }
            main.post {
                probing = false
                if (!active) return@post
                if (token != generation) { scheduleProbe(0); return@post }
                if (chosenName != null) prefs.edit().putString("iphone_service_name", chosenName).apply()
                publish(chosen)
                if (chosen != null) {
                    retryDelay = 1_000L
                    onStatus("${chosen!!.source}: ${chosen!!.baseUrl}")
                    scheduleProbe(10_000)
                } else {
                    // Resolve again after a failure: a known phone can acquire another DHCP address.
                    if (!resolving && resolveQueue.isEmpty()) {
                        resolveQueue.addAll(knownServices.values.take(8))
                        resolveNext(generation)
                    }
                    onStatus(if (ambiguous) "Phone selection needed: open Connection settings" else
                        "Waiting for iPhone OpenGlass; join its hotspot or the same Wi-Fi")
                    scheduleProbe(retryDelay)
                    retryDelay = ConnectionPolicy.nextDelay(retryDelay)
                }
            }
        }
    }

    private data class HealthIdentity(val receiverName: String)

    private fun health(candidate: ServerEndpoint): HealthIdentity? {
        var connection: HttpURLConnection? = null
        return try {
            connection = candidate.connection("/health").apply {
                connectTimeout = 1_200
                readTimeout = 1_200
                instanceFollowRedirects = false
            }
            if (connection.responseCode != 200) null else {
                val body = connection.inputStream.use { input ->
                    val data = ByteArray(4_097)
                    var count = 0
                    val deadline = android.os.SystemClock.elapsedRealtime() + 2_500
                    while (count < data.size) {
                        require(android.os.SystemClock.elapsedRealtime() < deadline)
                        val read = input.read(data, count, data.size - count)
                        if (read < 0) break
                        count += read
                    }
                    require(count <= 4_096)
                    String(data, 0, count, Charsets.UTF_8)
                }
                val json = JSONObject(body)
                val receiverName = json.optString("receiver_name")
                if (json.optString("service") == "openglass-ios" && json.optInt("protocol") == 1 &&
                    ConnectionPolicy.receiverMatches(receiverName, null)) HealthIdentity(receiverName) else null
            }
        } catch (_: Exception) { null } finally { connection?.disconnect() }
    }
}
