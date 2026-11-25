package com.brightwater.halden;

import static org.junit.Assert.*;

import java.util.List;
import java.util.Map;
import org.junit.Test;

public class StockContractTest extends ContractTestSupport {
    private static final String HEADER = "ITEM;WH;ON_HAND;ALLOCATED\r\n";
    private static final String[] SEED_STOCK = {
        "BW-1001;DUS;240;0", "BW-1001;MAN;60;0", "BW-1002;DUS;0;0", "BW-1002;MAN;0;0",
        "BW-1003;DUS;3;0", "BW-1004;DUS;18;3", "BW-1004;MAN;4;0", "BW-1005;DUS;50;5",
        "BW-1005;MAN;?;0", "BW-1006;DUS;500 ;0", "BW-1006;MAN;1.200;0", "BW-1007;DUS;12;0",
        "BW-1008;DUS;7;0", "BW-1009;DUS;25;3", "bw-1009 ;MAN;10;0", "BW-1010;DUS;2;0", "BW-1011;DUS;-3;0"
    };

    @Test
    public void ga08_stockCsvOrderRawValuesAndCrLf() throws Exception {
        String ticket = erp.fresh();
        assertEquals(HEADER + String.join("\r\n", SEED_STOCK) + "\r\n", erp.stock(ticket, null));
        assertEquals(HEADER + "BW-1009;DUS;25;3\r\nbw-1009 ;MAN;10;0\r\n", erp.stock(ticket, " bw-1009 "));
        assertEquals(HEADER, erp.stock(ticket, "UNKNOWN"));
    }

    @Test
    public void ga21_seedIdentity() throws Exception {
        String ticket = erp.fresh();
        Map<String, List<List<String>>> tables = erp.tables();
        assertEquals(8, tables.get("CUSTMAST").size());
        assertEquals(12, tables.get("ITEMMAST").size());
        assertEquals(17, tables.get("STOCKBAL").size());
        assertEquals(10, tables.get("ORDHDR").size());
        assertEquals(10, tables.get("ORDLIN").size());
        assertEquals(2, erp.scalar("SELECT COUNT(*) FROM CUSTMAST WHERE CRHOLD IN ('J', 'Y')"));
        assertEquals(11, erp.scalar("SELECT COUNT(DISTINCT UPPER(TRIM(ITMNO))) FROM ITEMMAST"));
        assertEquals("100.00", erp.rows("SELECT TOTAL FROM ORDHDR WHERE ORDNO='SO-240109'").get(0).get(0));
        assertEquals("23.10", erp.rows("SELECT UPRICE FROM ORDLIN WHERE ORDNO='SO-240109'").get(0).get(0));

        String firstCustomers = erp.get(CUSTOMER, ticket).status(200).body;
        String restCustomers = erp.get(CUSTOMER + "?offset=5", ticket).status(200).body;
        assertEquals(5, occurrences(firstCustomers, "\"acct\":"));
        assertEquals(3, occurrences(restCustomers, "\"acct\":"));
        int[] orderPageSizes = erp.orderPages(ticket, null, false).stream()
                .mapToInt(page -> occurrences(page, "\"OrderNo\":")).toArray();
        assertArrayEquals(new int[]{3, 3, 3, 1}, orderPageSizes);
        assertEquals(HEADER + String.join("\r\n", SEED_STOCK) + "\r\n", erp.stock(ticket, null));

        for (int page = 1; page <= 4; page++) {
            Response items = erp.get(ITEMS + "?page=" + page, ticket).status(200).contains("pages=\"3\"");
            assertEquals(page == 4 ? 0 : 4, ItemContractTest.root(items.body).getElementsByTagName("ITEM").getLength());
        }
        assertEquals(1, erp.scalar("SELECT COUNT(*) FROM ITEMMAST WHERE ID=11 AND CATG IS NULL AND AVAIL_FLG=''"));
        assertEquals(1, erp.scalar("SELECT COUNT(*) FROM ITEMMAST WHERE ID=4 AND DISC_FLG IS NULL"));
    }
}
