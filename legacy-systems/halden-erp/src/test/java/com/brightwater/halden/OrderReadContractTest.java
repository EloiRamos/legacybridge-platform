package com.brightwater.halden;

import static org.junit.Assert.*;

import java.util.Arrays;
import java.util.List;
import java.util.stream.Collectors;
import org.junit.Test;

public class OrderReadContractTest extends ContractTestSupport {
    @Test
    public void ga09_orderCursorContract() throws Exception {
        String ticket = erp.fresh();
        List<String> pages = erp.orderPages(ticket, null, false);
        assertEquals(Arrays.asList(3, 3, 3, 1), pages.stream()
                .map(page -> occurrences(page, "\"OrderNo\":")).collect(Collectors.toList()));
        assertEquals(Arrays.asList("Mw==", "Ng==", "OQ==", ""), pages.stream()
                .map(page -> group(NEXT, page)).collect(Collectors.toList()));
        assertTrue(pages.get(0).contains("\"OrderNo\":\"SO-240101\",\"Acct\":\"417\""));
        erp.get(ORDERS + "?cursor=!", ticket).status(200).body(pages.get(0));
        erp.get(ORDERS + "?cursor=b29wcw%3D%3D", ticket).status(200).body(pages.get(0));
        assertEquals(2, Fixture.countOrders(erp.orderPages(ticket, "52290", false)));
        assertEquals(0, Fixture.countOrders(erp.orderPages(ticket, "abc", false)));
    }

    @Test
    public void ga10_orderDetailAndMissingRc() throws Exception {
        String ticket = erp.fresh();
        String expected = "{\"rc\":0,\"order\":{\"OrderNo\":\"SO-240101\",\"Acct\":\"417\","
                + "\"Date\":\"2025-09-02\",\"Stat\":\"DL\",\"Total\":\"97.00\",\"Ref\":null,"
                + "\"lines\":[{\"ln\":1,\"item\":\"BW-1001\",\"qty\":\"20\",\"price\":\"4.85\"}]}}";
        erp.get(ORDERS + "/SO-240101", ticket).status(200).type("application/json").body(expected);
        erp.get(ORDERS + "/%20so-240101%20", ticket).status(200).type("application/json").body(expected);
        erp.get(ORDERS + "/SO-999999", ticket).status(200).type("application/json")
                .body("{\"rc\":404,\"msg\":\"ORDER NOT ON FILE\"}");
    }
}
