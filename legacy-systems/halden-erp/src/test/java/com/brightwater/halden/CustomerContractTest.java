package com.brightwater.halden;

import static org.junit.Assert.*;

import org.junit.Test;

public class CustomerContractTest extends ContractTestSupport {
    @Test
    public void ga06_customerPagesFiltersAndRawValues() throws Exception {
        String ticket = erp.fresh();
        String first = erp.get(CUSTOMER + "?limit=99", ticket).status(200).type("application/json").body;
        String second = erp.get(CUSTOMER + "?offset=5&limit=99", ticket).status(200).type("application/json").body;
        assertEquals(5, occurrences(first, "\"acct\":"));
        assertTrue(first, first.endsWith("\"next_offset\":5}"));
        assertEquals(3, occurrences(second, "\"acct\":"));
        assertTrue(second, second.endsWith("\"next_offset\":null}"));
        assertTrue(first.contains("\"acct\":417"));
        assertTrue(first.indexOf("\"acct\":417") < first.indexOf("\"acct\":4450"));
        erp.get(CUSTOMER + "?offset=bad&limit=-1", ticket).status(200).body(first);

        Response alder = erp.get(CUSTOMER + "?name=alder", ticket).status(200)
                .contains("\"acct\":10233").contains("\"acct\":10871");
        assertEquals(2, occurrences(alder.body, "\"acct\":"));
        String account = erp.customer(ticket, "417");
        assertEquals("{\"rows\":[{\"acct\":417,\"nm\":\"NORDKAP MARINE SERVICES AS\","
                + "\"contact\":\"HOLM, ERIK\",\"mail\":\" Erik.Holm@Nordkap-Marine.example \","
                + "\"tel\":\"+47 22 33 44 55\",\"hold\":\"N\",\"since\":\"2019-03-04\","
                + "\"city\":\"Bergen\",\"ctry\":\"NO\"}],\"next_offset\":null}", account);
        erp.get(CUSTOMER + "?name=nordkap&acct=%20417%20", ticket).status(200).body(account);
        erp.get(CUSTOMER + "?name=alder&acct=417", ticket).status(200).body(emptyRows());
        assertEquals(emptyRows(), erp.customer(ticket, "abc"));
        assertTrue(erp.customer(ticket, "20514").contains("\"city\":\"Düsseldorf\""));
        erp.get(CUSTOMER + "?name=D%C3%BCsseldorf", ticket).status(200).body(emptyRows());
        String solano = erp.customer(ticket, "60018");
        assertTrue(solano.contains("\"contact\":\"Lucía Ferrer\""));
        assertTrue(solano.contains("\"city\":\"Almería\""));
        assertTrue(solano.contains("\"hold\":\"\""));
        assertTrue(erp.customer(ticket, "31002").contains("\"hold\":null"));
    }

    private static String emptyRows() {
        return "{\"rows\":[],\"next_offset\":null}";
    }
}
