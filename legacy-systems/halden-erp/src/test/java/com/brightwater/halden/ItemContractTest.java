package com.brightwater.halden;

import static org.junit.Assert.*;

import java.io.StringReader;
import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import org.junit.Test;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;
import org.xml.sax.InputSource;

public class ItemContractTest extends ContractTestSupport {
    @Test
    public void ga07_itemXmlPagesFiltersAndMissingElements() throws Exception {
        String ticket = erp.fresh();
        String first = itemPage(ticket, "?page=1");
        Element page1 = root(first);
        Element page2 = root(itemPage(ticket, "?page=2"));
        String last = itemPage(ticket, "?page=3");
        Element page3 = root(last);
        Element empty = root(itemPage(ticket, "?page=4"));
        for (Element page : new Element[]{page1, page2, page3, empty}) {
            assertEquals("ITEMS", page.getTagName());
            assertEquals("3", page.getAttribute("pages"));
        }
        assertEquals("4", empty.getAttribute("page"));
        assertIds(page1, "1", "2", "3", "4");
        assertIds(page2, "5", "6", "7", "8");
        assertIds(page3, "9", "10", "11", "12");
        assertIds(empty);
        assertEquals(first, itemPage(ticket, "?page=bad"));
        assertEquals(first, itemPage(ticket, "?page=0"));
        assertIds(root(itemPage(ticket, "?q=hose")), "9", "12");
        assertIds(root(itemPage(ticket, "?q=BW-1009")), "9", "12");
        assertIds(root(itemPage(ticket, "?cat=Bearings")), "1");

        Element item4 = (Element) page1.getElementsByTagName("ITEM").item(3);
        Element item11 = (Element) page3.getElementsByTagName("ITEM").item(2);
        assertEquals(0, item4.getElementsByTagName("DISC_FLG").getLength());
        assertEquals(0, item11.getElementsByTagName("CATG").getLength());
        assertEquals("", item11.getElementsByTagName("AVAIL_FLG").item(0).getTextContent());
        assertTrue(last.contains("<AVAIL_FLG></AVAIL_FLG>"));
        assertEquals("bw-1009 ", page3.getElementsByTagName("ITMNO").item(3).getTextContent());
        assertEquals("€12,40", page1.getElementsByTagName("UPRICE").item(1).getTextContent());
    }

    private static String itemPage(String ticket, String query) throws Exception {
        Response response = erp.get(ITEMS + query, ticket).status(200).type("application/xml");
        assertTrue(response.type, response.type.toUpperCase(java.util.Locale.ROOT).contains("CHARSET=UTF-8"));
        assertTrue(response.body.startsWith("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"));
        return response.body;
    }

    static Element root(String xml) throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "");
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_SCHEMA, "");
        return factory.newDocumentBuilder().parse(new InputSource(new StringReader(xml))).getDocumentElement();
    }

    private static void assertIds(Element root, String... ids) {
        NodeList items = root.getElementsByTagName("ITEM");
        assertEquals(ids.length, items.getLength());
        for (int index = 0; index < ids.length; index++) {
            assertEquals(ids[index], ((Element) items.item(index)).getAttribute("id"));
        }
    }
}
