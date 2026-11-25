package com.brightwater.halden;

import java.io.IOException;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;
import java.util.Locale;
import java.util.stream.Collectors;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

final class ItemServlet extends HttpServlet {
    private static final int PAGE_SIZE = 4;
    private static final String[] COLUMNS = {
        "ITMNO", "DESCR", "CATG", "UPRICE", "UOM", "AVAIL_FLG", "DISC_FLG", "UPDTS"
    };
    private final Database db;

    ItemServlet(Database db) {
        this.db = db;
    }

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response)
            throws IOException, ServletException {
        int page = LegacyCodec.intOrDefault(request.getParameter("page"), 1, 1);
        String query = request.getParameter("q");
        String needle = query == null ? "" : query.toLowerCase(Locale.ROOT);
        String category = request.getParameter("cat");
        try (Connection connection = db.open()) {
            List<Item> matches = Database.query(connection, "SELECT * FROM ITEMMAST ORDER BY ID", Item::new)
                    .stream()
                    .filter(item -> item.matches(needle, category))
                    .collect(Collectors.toList());
            int pages = Math.max(1, (matches.size() + PAGE_SIZE - 1) / PAGE_SIZE);
            StringBuilder out = new StringBuilder("<?xml version=\"1.0\" encoding=\"UTF-8\"?>")
                    .append("<ITEMS page=\"").append(page).append("\" pages=\"").append(pages).append("\">");
            if (page <= pages) {
                int start = (page - 1) * PAGE_SIZE;
                for (int i = start; i < Math.min(start + PAGE_SIZE, matches.size()); i++) {
                    matches.get(i).appendXml(out);
                }
            }
            LegacyCodec.send(response, 200, "application/xml", out.append("</ITEMS>").toString());
        } catch (SQLException failure) {
            throw new ServletException(failure);
        }
    }

    private static final class Item {
        final int id;
        final String[] values = new String[COLUMNS.length];

        Item(ResultSet row) throws SQLException {
            id = row.getInt("ID");
            for (int i = 0; i < COLUMNS.length; i++) {
                values[i] = row.getString(COLUMNS[i]);
            }
        }

        boolean matches(String needle, String category) {
            return (category == null || category.equals(values[2]))
                    && (values[1].toLowerCase(Locale.ROOT).contains(needle)
                    || values[0].trim().toLowerCase(Locale.ROOT).contains(needle));
        }

        void appendXml(StringBuilder out) {
            out.append("<ITEM id=\"").append(id).append("\">");
            for (int i = 0; i < COLUMNS.length; i++) {
                LegacyCodec.xmlElement(out, COLUMNS[i], values[i]);
            }
            out.append("</ITEM>");
        }
    }
}
