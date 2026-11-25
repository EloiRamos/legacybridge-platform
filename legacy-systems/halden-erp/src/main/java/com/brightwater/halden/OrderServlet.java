package com.brightwater.halden;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.Base64;
import java.util.Collections;
import java.util.List;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

final class OrderServlet extends HttpServlet {
    private static final int PAGE_SIZE = 3;
    private static final String[] KEYS = {"OrderNo", "Acct", "Date", "Stat", "Total", "Ref"};
    private static final String SELECT = "SELECT ORDNO, CUSNO, ORDDT, STAT, TOTAL, CUSTREF FROM ORDHDR";
    private final Database db;
    private final OrderBook book;

    OrderServlet(Database db, OrderBook book) {
        this.db = db;
        this.book = book;
    }

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response)
            throws IOException, ServletException {
        String pathInfo = request.getPathInfo();
        if (pathInfo != null && pathInfo.indexOf('/', 1) >= 0) {
            response.sendError(404);
            return;
        }
        try (Connection connection = db.open()) {
            if (pathInfo == null) {
                list(request, response, connection);
            } else {
                detail(response, connection, pathInfo.substring(1).trim());
            }
        } catch (SQLException failure) {
            throw new ServletException(failure);
        }
    }

    @Override
    protected void doPost(HttpServletRequest request, HttpServletResponse response)
            throws IOException, ServletException {
        if (request.getPathInfo() != null) {
            response.sendError(405);
            return;
        }
        try {
            OrderBook.Result result = book.book(request.getParameterValues("acct"), request.getParameter("ref"),
                    request.getParameterValues("item"), request.getParameterValues("qty"));
            if (result.booked && "commit_then_500".equals(request.getAttribute(GatewayFilter.FAULT_ATTRIBUTE))) {
                LegacyCodec.internalError(response);
            } else {
                LegacyCodec.send(response, result.status, result.contentType, result.body);
            }
        } catch (SQLException failure) {
            throw new ServletException(failure);
        }
    }

    private static void list(HttpServletRequest request, HttpServletResponse response, Connection connection)
            throws SQLException, IOException {
        int offset = offset(request.getParameter("cursor"));
        String rawAccount = request.getParameter("acct");
        Integer account = LegacyCodec.integer(rawAccount);
        String page = " ORDER BY ORDNO LIMIT ? OFFSET ?";
        List<String> rows;
        if (rawAccount == null) {
            rows = Database.query(connection, SELECT + page, OrderServlet::rowJson, PAGE_SIZE + 1, offset);
        } else if (account == null) {
            rows = Collections.emptyList();
        } else {
            rows = Database.query(connection, SELECT + " WHERE CAST(CUSNO AS INTEGER) = ?" + page,
                    OrderServlet::rowJson, account, PAGE_SIZE + 1, offset);
        }
        boolean more = rows.size() > PAGE_SIZE;
        String next = more ? encodeOffset((long) offset + PAGE_SIZE) : "";
        String body = "{\"data\":{\"orders\":[" + String.join(",", rows.subList(0, Math.min(PAGE_SIZE, rows.size())))
                + "]},\"continue\":" + LegacyCodec.json(next) + "}";
        LegacyCodec.send(response, 200, LegacyCodec.JSON, body);
    }

    private static void detail(HttpServletResponse response, Connection connection, String orderNo)
            throws SQLException, IOException {
        connection.setAutoCommit(false);
        List<String> headers = Database.query(connection, SELECT + " WHERE ORDNO = ? COLLATE NOCASE",
                OrderServlet::rowJson, orderNo);
        if (headers.isEmpty()) {
            connection.commit();
            LegacyCodec.send(response, 200, LegacyCodec.JSON, "{\"rc\":404,\"msg\":\"ORDER NOT ON FILE\"}");
            return;
        }
        List<String> lines = Database.query(connection,
                "SELECT LN, ITEM, QTY, UPRICE FROM ORDLIN WHERE ORDNO = ? COLLATE NOCASE ORDER BY LN",
                OrderServlet::lineJson, orderNo);
        connection.commit();
        String header = headers.get(0);
        String body = "{\"rc\":0,\"order\":" + header.substring(0, header.length() - 1)
                + ",\"lines\":[" + String.join(",", lines) + "]}}";
        LegacyCodec.send(response, 200, LegacyCodec.JSON, body);
    }

    private static String rowJson(ResultSet result) throws SQLException {
        StringBuilder out = new StringBuilder("{");
        for (int i = 0; i < KEYS.length; i++) {
            if (i != 0) {
                out.append(',');
            }
            String value = result.getString(i + 1);
            if (i == 1) {
                value = Integer.toString(Integer.parseInt(value));
            }
            out.append('"').append(KEYS[i]).append("\":").append(LegacyCodec.json(value));
        }
        return out.append('}').toString();
    }

    private static String lineJson(ResultSet row) throws SQLException {
        return "{\"ln\":" + row.getInt("LN") + ",\"item\":" + LegacyCodec.json(row.getString("ITEM"))
                + ",\"qty\":" + LegacyCodec.json(row.getString("QTY"))
                + ",\"price\":" + LegacyCodec.json(row.getString("UPRICE")) + "}";
    }

    private static int offset(String cursor) {
        if (cursor == null || cursor.isEmpty()) {
            return 0;
        }
        try {
            String decoded = new String(Base64.getUrlDecoder().decode(cursor), StandardCharsets.US_ASCII);
            int offset = Integer.parseInt(decoded);
            return offset >= 0 && encodeOffset(offset).equals(cursor) ? offset : 0;
        } catch (IllegalArgumentException ignored) {
            return 0;
        }
    }

    private static String encodeOffset(long offset) {
        return Base64.getUrlEncoder().encodeToString(Long.toString(offset).getBytes(StandardCharsets.US_ASCII));
    }
}
