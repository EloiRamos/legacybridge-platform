package com.brightwater.halden;

import static org.junit.Assert.*;

import java.util.List;
import java.util.Map;
import org.junit.Test;

/**
 * Gateway classification must follow the servlet mapping Tomcat actually dispatches to,
 * not the raw request-line text: a non-canonical spelling of a product path receives the
 * same ticket, rate-limit and armed-fault checks as the canonical path (HALDEN API invariant I1).
 */
public class GatewayRouteClassificationContractTest extends ContractTestSupport {
    static final String[] READ_ALIASES = {
        "/_sim/../halden/customers?acct=417",
        "/_sim/../halden/items",
        "/_sim/../halden/stock?item=BW-1001",
        "/_sim/../halden/orders",
        "/_sim/../halden/orders/SO-240101",
    };
    static final String[] ORDER_ALIASES = {
        "/_sim/../halden/orders",
        "/_sim/./../halden/orders",
        "/halden/ping/../orders",
        "/halden/session/../orders",
        "/halden//orders",
        "/halden/./orders",
    };
    static final String[] CONTROL_ALIASES = {"/halden/../_sim/faults", "/halden/ping/../../_sim/faults"};
    static final String FORBIDDEN = "<html><body><h2>403 Forbidden</h2></body></html>";
    static final String INTERNAL_ERROR = "<html><body><h1>HALDEN ERP - Internal Error</h1></body></html>";

    static Response book(Fixture fixture, String path, String ticket) throws Exception {
        return fixture.post(path, ticket, "acct", "00417", "ref", "H01-ALIAS", "item", "BW-1001", "qty", "10");
    }

    static void assertNoTicketRejected(Fixture fixture) throws Exception {
        for (String alias : READ_ALIASES) {
            fixture.get(alias, null).status(401).type("text/plain").body("NO TICKET");
        }
        // HttpURLConnection discards the body of a 401 answer to a fixed-length streamed POST,
        // so POST 401 is asserted by status and type here; http_acceptance.sh checks its body.
        for (String alias : ORDER_ALIASES) {
            book(fixture, alias, null).status(401).type("text/plain");
        }
    }

    static void assertUnknownTicketRejected(Fixture fixture) throws Exception {
        for (String alias : READ_ALIASES) {
            fixture.get(alias, "UNKNOWN").status(403).type("text/html").body(FORBIDDEN);
        }
        for (String alias : ORDER_ALIASES) {
            book(fixture, alias, "UNKNOWN").status(403).type("text/html").body(FORBIDDEN);
        }
    }

    @Test
    public void ga05_h01_nonCanonicalPathsReceiveGatewayChecksWithSimControl() throws Exception {
        erp.reset("rate_limit", "0");
        Map<String, List<List<String>>> seed = erp.tables();
        assertNoTicketRejected(erp);
        assertEquals("State after 401 on non-canonical paths", seed, erp.tables());
        assertUnknownTicketRejected(erp);
        assertEquals("State after 403 on non-canonical paths", seed, erp.tables());

        erp.reset("rate_limit", "0", "ticket_ttl_sec", "0");
        String expired = erp.login();
        for (String alias : ORDER_ALIASES) {
            book(erp, alias, expired).status(440).type("application/json").body("{\"fault\":\"TICKET_EXPIRED\"}");
        }
        assertEquals("State after 440 on non-canonical paths", seed, erp.tables());

        erp.reset("rate_limit", "1", "rate_window_sec", "60");
        String throttled = erp.login();
        erp.get(CUSTOMER, throttled).status(200);
        for (String alias : READ_ALIASES) {
            assertNotNull(erp.get(alias, throttled).status(429).type("text/plain").body("THROTTLED").wait);
        }
        for (String alias : ORDER_ALIASES) {
            Response limited = book(erp, alias, throttled).status(429).type("text/plain").body("THROTTLED");
            long wait = Long.parseLong(limited.wait);
            assertTrue("wait must be in seconds", wait >= 1 && wait <= 60);
        }
        assertEquals("State after 429 on non-canonical paths", seed, erp.tables());

        erp.reset("rate_limit", "0");
        String ticket = erp.login();
        for (String alias : ORDER_ALIASES) {
            erp.arm("POST", ORDERS, "error_500").status(200).body("{\"armed\":1}");
            book(erp, alias, ticket).status(500).type("text/html").body(INTERNAL_ERROR);
            erp.get(FAULTS, null).status(200).body("{\"faults\":[]}");
        }
        assertEquals("State after armed error_500 on non-canonical paths", seed, erp.tables());

        for (String alias : CONTROL_ALIASES) {
            erp.get(alias, null).status(200).type("application/json").body("{\"faults\":[]}");
        }
        book(erp, ORDERS, null).status(401).type("text/plain");
        book(erp, ORDERS, ticket).status(200).body("{\"rc\":0,\"order_no\":\"SO-240111\",\"total\":\"48.50\"}");
    }

    @Test
    public void ga19_h01_nonCanonicalPathsReceiveGatewayChecksWithoutSimControl() throws Exception {
        try (Fixture ordinary = Fixture.start(false, OrderBook.NO_PROBE)) {
            Map<String, List<List<String>>> seed = ordinary.tables();
            assertNoTicketRejected(ordinary);
            assertUnknownTicketRejected(ordinary);
            for (String alias : CONTROL_ALIASES) {
                ordinary.get(alias, null).status(404).type("text/plain").body("NOT FOUND");
            }
            ordinary.post("/halden/../_sim/reset", null).status(404).type("text/plain").body("NOT FOUND");
            ordinary.post("/halden/../_sim/external-order", null, "acct", "00417", "item", "BW-1001", "qty", "1")
                    .status(404).type("text/plain").body("NOT FOUND");
            assertEquals("State after rejected non-canonical requests", seed, ordinary.tables());

            String ticket = ordinary.login();
            book(ordinary, ORDERS, null).status(401).type("text/plain");
            book(ordinary, ORDERS, ticket).status(200)
                    .body("{\"rc\":0,\"order_no\":\"SO-240111\",\"total\":\"48.50\"}");
            assertTrue(ordinary.stock(ticket, "BW-1001").contains("BW-1001;DUS;240;10"));
        }
    }
}
