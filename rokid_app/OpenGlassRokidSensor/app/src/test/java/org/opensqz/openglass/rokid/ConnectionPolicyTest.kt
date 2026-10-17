package org.opensqz.openglass.rokid

import org.junit.Assert.*
import org.junit.Test

class ConnectionPolicyTest {
    @Test fun acceptsManualIpv4AndIpv6WithoutChangingPorts() {
        assertEquals("http://172.20.10.1:18080", ConnectionPolicy.normalizeUrl(" http://172.20.10.1:18080/ "))
        assertEquals("ws://[fe80::1%25wlan0]:18080/rokid/audio",
            ConnectionPolicy.websocketUrl("http://[fe80::1%25wlan0]:18080"))
    }

    @Test fun rejectsCredentialsPathsQueriesAndInvalidPorts() {
        listOf("https://phone:18080", "http://user:pass@phone", "http://phone/path",
            "http://phone?token=x", "http://phone#x", "http://phone:70000", "http://phone:0")
            .forEach { value ->
                assertTrue("Should reject $value", runCatching { ConnectionPolicy.normalizeUrl(value) }.isFailure)
            }
    }

    @Test fun loopbackStaysOffWifiBoundSockets() {
        assertTrue(ConnectionPolicy.isLoopback(USB_BASE_URL))
        assertTrue(ConnectionPolicy.isLoopback("http://[::1]:18080"))
        assertFalse(ConnectionPolicy.isLoopback("http://172.20.10.1:18080"))
    }

    @Test fun newSelectionRequiresExactlyOnePhone() {
        assertEquals("OpenGlass-one", ConnectionPolicy.selectService(setOf("OpenGlass-one"), null))
        assertNull(ConnectionPolicy.selectService(setOf("OpenGlass-one", "OpenGlass-two"), null))
        assertNull(ConnectionPolicy.selectService(emptySet(), null))
    }

    @Test fun doesNotReplaceAnUnavailablePreviouslySelectedPhone() {
        assertNull(ConnectionPolicy.selectService(setOf("OpenGlass-other"), "OpenGlass-mine"))
        assertEquals("OpenGlass-mine", ConnectionPolicy.selectService(
            setOf("OpenGlass-other", "OpenGlass-mine"), "OpenGlass-mine"))
    }

    @Test fun backoffIsBounded() {
        assertEquals(2_000L, ConnectionPolicy.nextDelay(1_000))
        assertEquals(30_000L, ConnectionPolicy.nextDelay(16_000))
        assertEquals(30_000L, ConnectionPolicy.nextDelay(30_000))
    }

    @Test fun hotspotAndSavedIpMustStillMatchTheRememberedPhone() {
        assertTrue(ConnectionPolicy.receiverMatches("OpenGlass-1234ABcd", null))
        assertTrue(ConnectionPolicy.receiverMatches("OpenGlass-1234ABcd", "OpenGlass-1234ABcd"))
        assertFalse(ConnectionPolicy.receiverMatches("OpenGlass-1234ABcd", "OpenGlass-99999999"))
        assertFalse(ConnectionPolicy.receiverMatches("", null))
        assertFalse(ConnectionPolicy.receiverMatches("some-other-service", null))
    }
}
