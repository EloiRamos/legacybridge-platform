package com.brightwater.halden;

import static org.junit.Assert.*;

import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.junit.Test;

public class SimControlContractTest extends ContractTestSupport {
    @Test
    public void ga17_resetFaultLifecycleValidationAndDelay() throws Exception {
        String ticket = erp.fresh();
        Map<String, List<List<String>>> seed = erp.tables();
        erp.order(ticket, "00417", "ERASE", "BW-1001", "1").status(200).contains("\"rc\":0");
        erp.arm("GET", ITEMS, "error_500").status(200).body("{\"armed\":1}");
        erp.reset("rate_limit", "7", "ticket_ttl_sec", "11", "rate_window_sec", "6")
                .body("{\"reset\":true,\"rate_limit\":7,\"ticket_ttl_sec\":11,\"rate_window_sec\":6}");
        assertEquals(seed, erp.tables());
        erp.get(CUSTOMER, ticket).status(403);
        ticket = erp.login();
        erp.get(FAULTS, null).status(200).body("{\"faults\":[]}");

        erp.post(RESET, null, "rate_limit", "-1").status(400).body("BAD PARAM");
        erp.post(RESET, null, "rate_window_sec", "0").status(400).body("BAD PARAM");
        erp.post(RESET, null, "ticket_ttl_sec", "abc").status(400).body("BAD PARAM");
        erp.arm("PUT", ORDERS, "error_500").status(400).body("BAD PARAM");
        erp.arm("GET", "/other", "delay").status(400).body("BAD PARAM");
        erp.arm("GET", ITEMS, "delay", "delay_ms", "30001").status(400).body("BAD PARAM");
        erp.arm("GET", ITEMS, "error_500", "count", "0").status(400).body("BAD PARAM");
        erp.arm("POST", CUSTOMER, "commit_then_500").status(400).body("BAD PARAM");
        erp.arm("GET", ORDERS, "commit_then_500").status(400).body("BAD PARAM");

        erp.arm("GET", ITEMS, "delay", "count", "2", "delay_ms", "120").status(200).body("{\"armed\":1}");
        erp.arm("GET", ITEMS, "error_500").status(200).body("{\"armed\":2}");
        erp.get(FAULTS, null).status(200).body("{\"faults\":[{\"method\":\"GET\",\"path_prefix\":\"/halden/items\","
                + "\"mode\":\"delay\",\"remaining\":2,\"delay_ms\":120},{\"method\":\"GET\","
                + "\"path_prefix\":\"/halden/items\",\"mode\":\"error_500\",\"remaining\":1,\"delay_ms\":0}]}");
        long started = System.nanoTime();
        erp.get(ITEMS + "?page=1", ticket).status(200);
        assertTrue("Fault delay shorter than 120 ms", System.nanoTime() - started >= TimeUnit.MILLISECONDS.toNanos(120));
        erp.get(FAULTS, null).status(200).contains("\"mode\":\"delay\",\"remaining\":1");
        erp.get(ITEMS, ticket).status(200);
        erp.get(ITEMS, ticket).status(500);
        erp.arm("GET", ITEMS, "error_500").status(200).body("{\"armed\":1}");
        erp.request("DELETE", FAULTS, null, null).status(200).body("{\"cleared\":true}");
        erp.get(FAULTS, null).status(200).body("{\"faults\":[]}");
        erp.get(ITEMS, ticket).status(200);
        erp.reset().body("{\"reset\":true,\"rate_limit\":30,\"ticket_ttl_sec\":900,\"rate_window_sec\":10}");
    }

    @Test
    public void ga18_externalSaleConsumesStock() throws Exception {
        String ticket = erp.fresh();
        erp.post(EXTERNAL, null, "acct", "52290", "item", "BW-1010", "qty", "2")
                .status(200).type("application/json")
                .body("{\"rc\":0,\"order_no\":\"SO-240111\",\"total\":\"237.00\"}");
        erp.get(ORDERS + "/SO-240111", ticket).status(200).contains("\"Ref\":\"EXT-000001\"");
        erp.order(ticket, "00417", "SHORT-LATE", "BW-1010", "2")
                .status(409).type("text/plain").body("SHORT:BW-1010:0");
        ticket = erp.fresh();
        erp.post(EXTERNAL, null, "acct", "52290", "item", "BW-1010", "qty", "2").status(200)
                .contains("\"order_no\":\"SO-240111\"");
        erp.get(ORDERS + "/SO-240111", ticket).status(200).contains("\"Ref\":\"EXT-000001\"");
    }

    @Test
    public void ga19_ordinaryServerDisablesSimulationOnly() throws Exception {
        try (Fixture ordinary = Fixture.start(false, OrderBook.NO_PROBE)) {
            ordinary.post(RESET, null).status(404).type("text/plain").body("NOT FOUND");
            ordinary.post(FAULTS, null).status(404).type("text/plain").body("NOT FOUND");
            ordinary.get(FAULTS, null).status(404).type("text/plain").body("NOT FOUND");
            ordinary.request("DELETE", FAULTS, null, null).status(404).type("text/plain").body("NOT FOUND");
            ordinary.post(EXTERNAL, null).status(404).type("text/plain").body("NOT FOUND");
            ordinary.get("/_sim/unknown", null).status(404).type("text/plain").body("NOT FOUND");

            ordinary.get("/halden/ping", null).status(200).body("HALDEN-ERP 3.1 READY");
            String ticket = ordinary.login();
            assertTrue(ordinary.customer(ticket, "417").contains("\"acct\":417"));
            ordinary.get(ITEMS, ticket).status(200).type("application/xml");
            assertTrue(ordinary.stock(ticket, "BW-1001").contains("BW-1001;DUS;240;0"));
            ordinary.order(ticket, "00417", "ORDINARY", "BW-1001", "10")
                    .status(200).contains("\"order_no\":\"SO-240111\"");
            ordinary.get(ORDERS + "/SO-240111", ticket).status(200).contains("\"Ref\":\"ORDINARY\"");
        }
    }
}
