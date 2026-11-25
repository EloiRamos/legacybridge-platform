package com.brightwater.halden;

import static org.junit.Assert.*;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.AfterClass;
import org.junit.BeforeClass;

public abstract class ContractTestSupport {
    static final String CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8";
    static final String LOGIN = "svc_bridge";
    static final String SECRET = "halden-demo-secret";
    static final String CUSTOMER = "/halden/customers";
    static final String ITEMS = "/halden/items";
    static final String STOCK = "/halden/stock";
    static final String ORDERS = "/halden/orders";
    static final String SESSION = "/halden/session";
    static final String RESET = "/_sim/reset";
    static final String FAULTS = "/_sim/faults";
    static final String EXTERNAL = "/_sim/external-order";
    static final Pattern NEXT = Pattern.compile("\"continue\":\"([A-Za-z0-9_=-]*)\"");

    static Fixture erp;

    @BeforeClass
    public static void startFixture() throws Exception {
        erp = Fixture.start(true, OrderBook.NO_PROBE);
    }

    @AfterClass
    public static void stopFixture() throws Exception {
        if (erp != null) {
            erp.close();
        }
    }

    static String form(String... pairs) throws IOException {
        assertEquals(0, pairs.length % 2);
        List<String> fields = new ArrayList<>();
        for (int i = 0; i < pairs.length; i += 2) {
            fields.add(URLEncoder.encode(pairs[i], "UTF-8") + "=" + URLEncoder.encode(pairs[i + 1], "UTF-8"));
        }
        return String.join("&", fields);
    }

    static int occurrences(String text, String needle) {
        int count = 0;
        for (int index = text.indexOf(needle); index >= 0; index = text.indexOf(needle, index + needle.length())) {
            count++;
        }
        return count;
    }

    static String group(Pattern pattern, String body) {
        Matcher match = pattern.matcher(body);
        assertTrue("Missing pattern " + pattern + " in " + body, match.find());
        return match.group(1);
    }

    static final class Response {
        final int status;
        final String type;
        final String wait;
        final String body;

        Response(int status, String type, String wait, String body) {
            this.status = status;
            this.type = type;
            this.wait = wait;
            this.body = body;
        }

        Response status(int expected) {
            assertEquals(body, expected, status);
            return this;
        }

        Response type(String expected) {
            assertNotNull(type);
            assertEquals(expected, type.split(";", 2)[0]);
            return this;
        }

        Response body(String expected) {
            assertEquals(expected, body);
            return this;
        }

        Response contains(String expected) {
            assertTrue("Missing " + expected + " in " + body, body.contains(expected));
            return this;
        }
    }

    static final class Fixture implements AutoCloseable {
        final Path temp;
        final Path dbPath;
        final HaldenServer server;
        private final String baseUrl;

        private Fixture(Path temp, Path dbPath, HaldenServer server) {
            this.temp = temp;
            this.dbPath = dbPath;
            this.server = server;
            this.baseUrl = "http://127.0.0.1:" + server.port();
        }

        static Fixture start(boolean simControl, OrderBook.BookingProbe probe) throws Exception {
            Path temp = Files.createTempDirectory("halden-contract-");
            Path dbPath = temp.resolve("halden.db");
            try {
                HaldenConfig config = new HaldenConfig(0, dbPath, LOGIN, SECRET, 900, 30, 10, true, simControl);
                return new Fixture(temp, dbPath, HaldenServer.start(config, probe));
            } catch (Exception failure) {
                try {
                    deleteDb(temp, dbPath);
                } catch (IOException cleanupFailure) {
                    failure.addSuppressed(cleanupFailure);
                }
                throw failure;
            }
        }

        Response get(String path, String ticket) throws IOException {
            return request("GET", path, ticket, null);
        }

        Response post(String path, String ticket, String... pairs) throws IOException {
            return request("POST", path, ticket, form(pairs));
        }

        Response request(String method, String path, String ticket, String body) throws IOException {
            HttpURLConnection con = (HttpURLConnection) new URL(baseUrl + path).openConnection();
            con.setRequestMethod(method);
            con.setInstanceFollowRedirects(false);
            con.setConnectTimeout(4000);
            con.setReadTimeout(8000);
            try {
                if (ticket != null) {
                    con.setRequestProperty("X-Halden-Ticket", ticket);
                }
                if (body != null) {
                    byte[] data = body.getBytes(StandardCharsets.UTF_8);
                    con.setDoOutput(true);
                    con.setRequestProperty("Content-Type", CONTENT_TYPE);
                    con.setFixedLengthStreamingMode(data.length);
                    try (OutputStream stream = con.getOutputStream()) {
                        stream.write(data);
                    }
                }
                int status = con.getResponseCode();
                InputStream response = status < 400 ? con.getInputStream() : con.getErrorStream();
                ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                if (response != null) {
                    try (InputStream stream = response) {
                        byte[] buffer = new byte[1024];
                        int count;
                        while ((count = stream.read(buffer)) != -1) {
                            bytes.write(buffer, 0, count);
                        }
                    }
                }
                return new Response(status, con.getContentType(), con.getHeaderField("X-Halden-Wait"),
                        new String(bytes.toByteArray(), StandardCharsets.UTF_8));
            } finally {
                con.disconnect();
            }
        }

        Response reset(String... settings) throws IOException {
            return post(RESET, null, settings).status(200).type("application/json").contains("\"reset\":true");
        }

        String login() throws IOException {
            Response grant = post(SESSION, null, "login", LOGIN, "secret", SECRET).status(200).type("text/plain");
            return group(Pattern.compile("TICKET=([A-F0-9]{32})\\r\\n"), grant.body);
        }

        String fresh() throws IOException {
            reset("rate_limit", "0");
            return login();
        }

        Response order(String ticket, String account, String ref, String item, String qty) throws IOException {
            return post(ORDERS, ticket, "acct", account, "ref", ref, "item", item, "qty", qty);
        }

        Response arm(String method, String prefix, String mode, String... extras) throws IOException {
            List<String> form = new ArrayList<>(Arrays.asList("method", method, "path_prefix", prefix, "mode", mode));
            form.addAll(Arrays.asList(extras));
            return post(FAULTS, null, form.toArray(new String[0]));
        }

        String customer(String ticket, String acct) throws IOException {
            return get(CUSTOMER + "?acct=" + acct, ticket).status(200).type("application/json").body;
        }

        String stock(String ticket, String item) throws IOException {
            String query = item == null ? "" : "?item=" + URLEncoder.encode(item, "UTF-8");
            return get(STOCK + query, ticket).status(200).type("text/plain").body;
        }

        List<String> orderPages(String ticket, String account, boolean freshPerPage) throws IOException {
            List<String> pages = new ArrayList<>();
            String next = "";
            do {
                String query = (account == null ? "" : "acct=" + account + "&")
                        + "cursor=" + URLEncoder.encode(next, "UTF-8");
                String page = get(ORDERS + "?" + query, freshPerPage ? login() : ticket)
                        .status(200).type("application/json").body;
                pages.add(page);
                assertTrue("Cursor pagination exceeded the expected data size", pages.size() < 25);
                next = group(NEXT, page);
            } while (!next.isEmpty());
            return pages;
        }

        int orderCount(String ticket) throws IOException {
            return countOrders(orderPages(ticket, null, false));
        }

        static int countOrders(List<String> pages) {
            return pages.stream().mapToInt(page -> occurrences(page, "\"OrderNo\":")).sum();
        }

        List<List<String>> rows(String query, Object... parameters) throws SQLException {
            try (Connection connection = new Database(dbPath).open()) {
                return Database.query(connection, query, Fixture::readRow, parameters);
            }
        }

        int scalar(String query) throws SQLException {
            return Integer.parseInt(rows(query).get(0).get(0));
        }

        List<List<String>> customerRow(String account) throws SQLException {
            return rows("SELECT * FROM CUSTMAST WHERE CUSNO = ?", account);
        }

        Map<String, List<List<String>>> tables() throws SQLException {
            Map<String, List<List<String>>> snapshot = new LinkedHashMap<>();
            snapshot.put("CUSTMAST", rows("SELECT * FROM CUSTMAST ORDER BY CUSNO"));
            snapshot.put("ITEMMAST", rows("SELECT * FROM ITEMMAST ORDER BY ID"));
            snapshot.put("STOCKBAL", rows("SELECT rowid, * FROM STOCKBAL ORDER BY rowid"));
            snapshot.put("ORDHDR", rows("SELECT * FROM ORDHDR ORDER BY ORDNO"));
            snapshot.put("ORDLIN", rows("SELECT * FROM ORDLIN ORDER BY ORDNO, LN"));
            return snapshot;
        }

        private static List<String> readRow(ResultSet result) throws SQLException {
            List<String> row = new ArrayList<>();
            for (int i = 1; i <= result.getMetaData().getColumnCount(); i++) {
                row.add(result.getString(i));
            }
            return row;
        }

        @Override
        public void close() throws Exception {
            try {
                server.stop();
            } finally {
                deleteDb(temp, dbPath);
            }
        }

        private static void deleteDb(Path temp, Path dbPath) throws IOException {
            Files.deleteIfExists(dbPath.resolveSibling("halden.db-wal"));
            Files.deleteIfExists(dbPath.resolveSibling("halden.db-shm"));
            Files.deleteIfExists(dbPath);
            Files.deleteIfExists(temp);
        }
    }
}
