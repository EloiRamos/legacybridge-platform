package com.brightwater.halden;

import static org.junit.Assert.*;

import java.time.LocalDate;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import org.junit.Test;

public class OrderWriteContractTest extends ContractTestSupport {
    private static final String ACCEPTED = "{\"rc\":0,\"order_no\":\"SO-240111\",\"total\":\"48.50\"}";
    private static final ZoneId BERLIN = ZoneId.of("Europe/Berlin");

    @Test
    public void ga11_bookingAndReadBack() throws Exception {
        String ticket = erp.fresh();
        assertTrue(erp.stock(ticket, "BW-1001").contains("BW-1001;DUS;240;0\r\n"));
        LocalDate before = LocalDate.now(BERLIN);
        erp.order(ticket, "00417", "GATE-A-11", "BW-1001", "10")
                .status(200).type("application/json").body(ACCEPTED);
        LocalDate after = LocalDate.now(BERLIN);
        Response order = erp.get(ORDERS + "/SO-240111", ticket).status(200).type("application/json")
                .contains("\"rc\":0").contains("\"Acct\":\"417\"").contains("\"Stat\":\"OP\"")
                .contains("\"Total\":\"48.50\"").contains("\"Ref\":\"GATE-A-11\"");
        String date = group(Pattern.compile("\"Date\":\"(\\d{2}\\.\\d{2}\\.\\d{4})\""), order.body);
        DateTimeFormatter format = DateTimeFormatter.ofPattern("dd.MM.yyyy");
        assertTrue(date, date.equals(before.format(format)) || date.equals(after.format(format)));
        order.contains("\"lines\":[{\"ln\":1,\"item\":\"BW-1001\",\"qty\":\"10\",\"price\":\"4.85\"}]");
        assertTrue(erp.stock(ticket, "BW-1001").contains("BW-1001;DUS;240;10\r\n"));
        assertEquals(11, erp.orderCount(ticket));
    }

    @Test
    public void ga12_warehouseAllocationSpillsToMan() throws Exception {
        String ticket = erp.fresh();
        erp.order(ticket, "00417", "SPILL", "BW-1004", "17").status(200).contains("\"rc\":0");
        assertEquals("ITEM;WH;ON_HAND;ALLOCATED\r\nBW-1004;DUS;18;18\r\nBW-1004;MAN;4;2\r\n",
                erp.stock(ticket, "BW-1004"));
    }

    @Test
    public void ga13_rejectionsLeaveEveryTableUnchanged() throws Exception {
        String ticket = erp.fresh();
        Map<String, List<List<String>>> before = erp.tables();

        rejected(ticket, before, 40, "BAD_PAYLOAD", "item", "BW-1001", "qty", "10");
        rejected(ticket, before, 40, "BAD_PAYLOAD", "acct", "00417", "acct", "52290",
                "item", "BW-1001", "qty", "10");
        rejected(ticket, before, 40, "BAD_PAYLOAD", "acct", "00417", "item", "BW-1001");
        rejectedOrder(ticket, before, 11, "UNKNOWN_ACCOUNT", "99999", "BW-1001", "1");
        rejectedOrder(ticket, before, 12, "ACCOUNT_ON_HOLD", "10871", "BW-1001", "1");
        rejectedOrder(ticket, before, 21, "UNKNOWN_ITEM", "00417", "DOES-NOT-EXIST", "1");
        rejectedOrder(ticket, before, 22, "ITEM_DISCONTINUED", "00417", "BW-1007", "1");
        rejectedOrder(ticket, before, 23, "NO_PRICE", "00417", "BW-1008", "1");
        for (String badQty : Arrays.asList("0", "1000", "1.5", "bad")) {
            rejectedOrder(ticket, before, 30, "BAD_QTY", "00417", "BW-1001", badQty);
        }
        erp.order(ticket, "00417", "SHORT", "BW-1010", "5").status(409).type("text/plain")
                .body("SHORT:BW-1010:2");
        assertEquals(before, erp.tables());
        erp.post(ORDERS, ticket, "acct", "00417", "item", "BW-1010", "qty", "2", "item", "bw-1010", "qty", "1")
                .status(409).type("text/plain").body("SHORT:BW-1010:2");
        assertEquals(before, erp.tables());
    }

    @Test
    public void ga14_commitThen500PersistsOrder() throws Exception {
        String ticket = erp.fresh();
        erp.arm("POST", ORDERS, "commit_then_500").status(200).body("{\"armed\":1}");
        erp.order(ticket, "00417", "LATE-COMMIT", "BW-1001", "10").status(500).type("text/html")
                .body("<html><body><h1>HALDEN ERP - Internal Error</h1></body></html>");
        erp.get(ORDERS + "/SO-240111", ticket).status(200).contains("\"rc\":0").contains("\"Ref\":\"LATE-COMMIT\"");
        assertEquals(11, erp.orderCount(ticket));
        assertTrue(erp.stock(ticket, "BW-1001").contains("BW-1001;DUS;240;10\r\n"));
        erp.get(FAULTS, null).status(200).body("{\"faults\":[]}");
    }

    @Test
    public void ga15_duplicateAndTruncatedReferences() throws Exception {
        String ticket = erp.fresh();
        for (int number = 111; number <= 112; number++) {
            erp.order(ticket, "00417", "DUPLICATE", "BW-1001", "1").status(200)
                    .contains("\"order_no\":\"SO-240" + number + "\"");
            erp.get(ORDERS + "/SO-240" + number, ticket).status(200).contains("\"Ref\":\"DUPLICATE\"");
        }
        assertEquals(12, erp.orderCount(ticket));
        erp.order(ticket, "00417", "  1234567890ABCDE  ", "BW-1001", "1").status(200)
                .contains("\"order_no\":\"SO-240113\"");
        erp.get(ORDERS + "/SO-240113", ticket).status(200).contains("\"Ref\":\"1234567890AB\"");
        erp.order(ticket, "00417", " ", "BW-1001", "1").status(200).contains("\"order_no\":\"SO-240114\"");
        erp.get(ORDERS + "/SO-240114", ticket).status(200).contains("\"Ref\":null");
    }

    @Test
    public void ga16_itemFlagUnchangedAndOrderNumbersSequential() throws Exception {
        String ticket = erp.fresh();
        String before = erp.get(ITEMS + "?q=BW-1010", ticket).status(200).contains("<AVAIL_FLG>J</AVAIL_FLG>").body;
        erp.order(ticket, "00417", "FLAG", "BW-1010", "2").status(200).contains("\"order_no\":\"SO-240111\"");
        assertEquals("ITEM;WH;ON_HAND;ALLOCATED\r\nBW-1010;DUS;2;2\r\n", erp.stock(ticket, "BW-1010"));
        erp.get(ITEMS + "?q=BW-1010", ticket).status(200).body(before);
        erp.order(ticket, "00417", "NEXT", "BW-1001", "1").status(200).contains("\"order_no\":\"SO-240112\"");
    }

    private static void rejectedOrder(String ticket, Map<String, List<List<String>>> before,
                                      int code, String message, String account, String item, String quantity)
            throws Exception {
        rejected(ticket, before, code, message, "acct", account, "ref", "REJECT",
                "item", item, "qty", quantity);
    }

    private static void rejected(String ticket, Map<String, List<List<String>>> before,
                                 int code, String message, String... fields) throws Exception {
        erp.post(ORDERS, ticket, fields).status(200).type("application/json")
                .body("{\"rc\":" + code + ",\"msg\":\"" + message + "\"}");
        assertEquals("State after " + message, before, erp.tables());
    }
}
