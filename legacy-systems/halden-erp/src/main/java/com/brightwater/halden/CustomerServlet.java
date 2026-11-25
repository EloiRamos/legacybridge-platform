package com.brightwater.halden;

import java.io.IOException;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.stream.Collectors;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

final class CustomerServlet extends HttpServlet {
    private static final String[][] FIELDS = {
        {"nm", "CUSNM"}, {"contact", "CONTACT"}, {"mail", "EMAIL"}, {"tel", "PHONE"},
        {"hold", "CRHOLD"}, {"since", "SINCE"}, {"city", "CITY"}, {"ctry", "CTRY"}
    };
    private final Database db;

    CustomerServlet(Database db) {
        this.db = db;
    }

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response)
            throws IOException, ServletException {
        String rawAccount = request.getParameter("acct");
        Integer account = LegacyCodec.integer(rawAccount);
        String name = request.getParameter("name");
        String needle = name == null ? "" : name.toLowerCase(Locale.ROOT);
        int offset = LegacyCodec.intOrDefault(request.getParameter("offset"), 0, 0);
        int limit = Math.min(5, LegacyCodec.intOrDefault(request.getParameter("limit"), 1, 5));

        try (Connection connection = db.open()) {
            List<Customer> customers = rawAccount != null && account == null
                    ? Collections.emptyList()
                    : Database.query(connection, "SELECT * FROM CUSTMAST ORDER BY CUSNO", Customer::new);
            List<String> rows = customers.stream()
                    .filter(row -> (account == null || row.account == account)
                            && row.name.toLowerCase(Locale.ROOT).contains(needle))
                    .skip(offset).limit(limit + 1L).map(row -> row.json).collect(Collectors.toList());
            boolean more = rows.size() > limit;
            String body = "{\"rows\":[" + String.join(",", rows.subList(0, Math.min(limit, rows.size())))
                    + "],\"next_offset\":" + (more ? String.valueOf((long) offset + limit) : "null") + "}";
            LegacyCodec.send(response, 200, LegacyCodec.JSON, body);
        } catch (SQLException failure) {
            throw new ServletException(failure);
        }
    }

    private static final class Customer {
        final int account;
        final String name;
        final String json;

        Customer(ResultSet row) throws SQLException {
            account = Integer.parseInt(row.getString("CUSNO"));
            name = row.getString("CUSNM");
            StringBuilder out = new StringBuilder("{\"acct\":").append(account);
            for (String[] field : FIELDS) {
                out.append(",\"").append(field[0]).append("\":").append(LegacyCodec.json(row.getString(field[1])));
            }
            json = out.append('}').toString();
        }
    }
}
