package com.brightwater.halden;

import java.io.IOException;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

final class StockServlet extends HttpServlet {
    static final String HEADER = "ITEM;WH;ON_HAND;ALLOCATED\r\n";
    private static final String SELECT = "SELECT ITEM, WH, ON_HAND, ALLOCATED FROM STOCKBAL";
    private final Database db;

    StockServlet(Database db) {
        this.db = db;
    }

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response)
            throws IOException, ServletException {
        String item = request.getParameter("item");
        try (Connection connection = db.open()) {
            List<String> rows = item == null
                    ? Database.query(connection, SELECT + " ORDER BY rowid", StockServlet::csvRow)
                    : Database.query(connection, SELECT + " WHERE UPPER(TRIM(ITEM)) = UPPER(TRIM(?)) ORDER BY rowid",
                            StockServlet::csvRow, item);
            LegacyCodec.send(response, 200, LegacyCodec.TEXT, HEADER + String.join("", rows));
        } catch (SQLException failure) {
            throw new ServletException(failure);
        }
    }

    private static String csvRow(ResultSet row) throws SQLException {
        return LegacyCodec.csv(row.getString("ITEM")) + ";" + LegacyCodec.csv(row.getString("WH"))
                + ";" + LegacyCodec.csv(row.getString("ON_HAND")) + ";"
                + LegacyCodec.csv(row.getString("ALLOCATED")) + "\r\n";
    }
}
