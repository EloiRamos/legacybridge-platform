package com.brightwater.halden;

import static org.junit.Assert.*;

import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;
import org.junit.Test;

public class GatewayContractTest extends ContractTestSupport {
    @Test
    public void ga01_ping() throws Exception {
        erp.get("/halden/ping", null).status(200).type("text/plain").body("HALDEN-ERP 3.1 READY");
    }

    @Test
    public void ga02_loginFormatAndRevocation() throws Exception {
        erp.reset();
        erp.post(SESSION, null, "login", LOGIN, "secret", "incorrect").status(200).type("text/plain")
                .body("STATUS=DENIED\r\nREASON=BAD_CREDENTIALS\r\n");
        erp.post(SESSION, null).status(200).type("text/plain").body("STATUS=DENIED\r\nREASON=BAD_CREDENTIALS\r\n");
        String body = erp.post(SESSION, null, "login", LOGIN, "secret", SECRET)
                .status(200).type("text/plain").body;
        assertTrue(body, body.matches("STATUS=GRANTED\\r\\nTICKET=[A-F0-9]{32}\\r\\nEXPIRES_IN=900\\r\\n"));
        String ticket = group(Pattern.compile("TICKET=([A-F0-9]{32})"), body);
        erp.get(CUSTOMER, ticket).status(200);
        String next = erp.login();
        assertNotEquals(ticket, next);
        erp.get(CUSTOMER, ticket).status(403).type("text/html")
                .body("<html><body><h2>403 Forbidden</h2></body></html>");
        erp.get(CUSTOMER, next).status(200);
    }

    @Test
    public void ga03_missingUnknownAndExpiredTickets() throws Exception {
        erp.reset("ticket_ttl_sec", "0");
        erp.get(CUSTOMER, null).status(401).type("text/plain").body("NO TICKET");
        erp.get(CUSTOMER, " ").status(401).type("text/plain").body("NO TICKET");
        erp.get(CUSTOMER, "BAD").status(403).type("text/html")
                .body("<html><body><h2>403 Forbidden</h2></body></html>");
        erp.get(CUSTOMER, erp.login()).status(440).type("application/json")
                .body("{\"fault\":\"TICKET_EXPIRED\"}");
    }

    @Test
    public void ga04_thirdRequestThrottled() throws Exception {
        erp.reset("rate_limit", "2", "rate_window_sec", "10");
        String ticket = erp.login();
        erp.get(CUSTOMER, ticket).status(200);
        erp.get(ITEMS, ticket).status(200);
        Response third = erp.get(STOCK, ticket).status(429).type("text/plain").body("THROTTLED");
        long wait = Long.parseLong(third.wait);
        assertTrue("wait must be in seconds", wait >= 1 && wait <= 10);
    }

    @Test
    public void ga05_preHandlerResponsesDoNotMutateOrdersOrStock() throws Exception {
        for (int status : new int[]{401, 403, 440, 429, 500}) {
            erp.reset("rate_limit", status == 429 ? "2" : "0",
                    "ticket_ttl_sec", status == 440 ? "1" : "900");
            String stockBefore = erp.stock(erp.login(), null);
            int before = Fixture.countOrders(erp.orderPages(null, null, true));
            assertEquals(10, before);
            String ticket = erp.login();
            if (status == 401) {
                ticket = null;
            } else if (status == 403) {
                ticket = "UNKNOWN";
            } else if (status == 440) {
                TimeUnit.MILLISECONDS.sleep(1100);
            } else if (status == 429) {
                erp.get(CUSTOMER, ticket).status(200);
                erp.get(ITEMS, ticket).status(200);
            } else {
                erp.arm("POST", ORDERS, "error_500").status(200);
            }
            erp.order(ticket, "00417", "NO-BOOK", "BW-1001", "10").status(status);
            assertEquals("Order count after HTTP " + status, before,
                    Fixture.countOrders(erp.orderPages(null, null, true)));
            assertEquals("Stock after HTTP " + status, stockBefore, erp.stock(erp.login(), null));
        }
    }
}
