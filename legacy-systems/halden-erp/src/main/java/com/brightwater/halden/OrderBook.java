package com.brightwater.halden;

import java.math.BigDecimal;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

final class OrderBook {
    interface BookingProbe {
        void beforeWriteLock(String ref);
    }

    static final BookingProbe NO_PROBE = ref -> { };
    private static final ZoneId BERLIN = ZoneId.of("Europe/Berlin");
    private static final DateTimeFormatter ORDER_DATE = DateTimeFormatter.ofPattern("dd.MM.yyyy", Locale.ROOT);
    private static final String STOCK =
            "SELECT rowid, ON_HAND, ALLOCATED FROM STOCKBAL WHERE UPPER(TRIM(ITEM)) = ? "
            + "ORDER BY CASE WH WHEN 'DUS' THEN 0 WHEN 'MAN' THEN 1 ELSE 2 END, rowid";
    private final Database db;
    private final BookingProbe probe;
    private long externalSequence;

    OrderBook(Database db, BookingProbe probe) {
        this.db = db;
        this.probe = probe;
    }

    void resetExternalSequence() {
        synchronized (Database.WRITE_LOCK) {
            externalSequence = 0;
        }
    }

    Result book(String[] accounts, String reference, String[] items, String[] quantities) throws SQLException {
        return book(accounts, reference, items, quantities, false);
    }

    Result externalOrder(String[] accounts, String item, String quantity) throws SQLException {
        return book(accounts, null, item == null ? null : new String[]{item},
                quantity == null ? null : new String[]{quantity}, true);
    }

    private Result book(String[] accounts, String reference, String[] items, String[] quantities,
                        boolean external) throws SQLException {
        probe.beforeWriteLock(reference);
        synchronized (Database.WRITE_LOCK) {
            try (Connection connection = db.open()) {
                connection.setAutoCommit(false);
                try {
                    String ref = external ? String.format(Locale.ROOT, "EXT-%06d", externalSequence + 1)
                            : normalizeReference(reference);
                    Result result = bookInTransaction(connection, accounts, ref, items, quantities);
                    if (result.booked) {
                        connection.commit();
                        if (external) {
                            externalSequence++;
                        }
                    } else {
                        connection.rollback();
                    }
                    return result;
                } catch (SQLException | RuntimeException failure) {
                    Database.rollback(connection, failure);
                    throw failure;
                }
            }
        }
    }

    private Result bookInTransaction(Connection connection, String[] accounts, String ref,
                                     String[] items, String[] quantities) throws SQLException {
        if (accounts == null || accounts.length != 1 || accounts[0].trim().isEmpty()
                || items == null || items.length < 1 || items.length > 20
                || quantities == null || quantities.length != items.length) {
            return Result.reject(40, "BAD_PAYLOAD");
        }
        String account = accounts[0].trim();
        List<String> held = Database.query(connection,
                "SELECT CRHOLD FROM CUSTMAST WHERE CUSNO = ?", row -> row.getString(1), account);
        if (held.isEmpty()) {
            return Result.reject(11, "UNKNOWN_ACCOUNT");
        }
        if (LegacyCodec.legacyTruthy(held.get(0))) {
            return Result.reject(12, "ACCOUNT_ON_HOLD");
        }

        List<Line> lines = new ArrayList<>();
        Map<String, Integer> requested = new LinkedHashMap<>();
        BigDecimal total = BigDecimal.ZERO;
        for (int i = 0; i < items.length; i++) {
            List<Item> matches = Database.query(connection,
                    "SELECT UPPER(TRIM(ITMNO)), DISC_FLG, UPRICE FROM ITEMMAST "
                    + "WHERE UPPER(TRIM(ITMNO)) = UPPER(TRIM(?)) ORDER BY ID LIMIT 1", Item::new, items[i]);
            if (matches.isEmpty()) {
                return Result.reject(21, "UNKNOWN_ITEM");
            }
            Item item = matches.get(0);
            if (item.discontinued) {
                return Result.reject(22, "ITEM_DISCONTINUED");
            }
            BigDecimal price = LegacyCodec.legacyPrice(item.price);
            if (price == null) {
                return Result.reject(23, "NO_PRICE");
            }
            Integer quantity = LegacyCodec.integer(quantities[i]);
            if (quantity == null || quantity < 1 || quantity > 999) {
                return Result.reject(30, "BAD_QTY");
            }
            lines.add(new Line(item.sku, quantity, LegacyCodec.money(price)));
            requested.merge(item.sku, quantity, Integer::sum);
            total = total.add(price.multiply(BigDecimal.valueOf(quantity)));
        }

        Map<String, List<Balance>> stock = new LinkedHashMap<>();
        for (Map.Entry<String, Integer> entry : requested.entrySet()) {
            String sku = entry.getKey();
            List<Balance> balances = Database.query(connection, STOCK, Balance::new, sku);
            long available = balances.stream().mapToLong(Balance::free).sum();
            if (entry.getValue() > available) {
                return new Result(409, LegacyCodec.TEXT, "SHORT:" + sku + ":" + available, false);
            }
            stock.put(sku, balances);
        }

        String orderNo = nextOrderNumber(connection);
        String formattedTotal = LegacyCodec.money(total);
        Database.update(connection, "INSERT INTO ORDHDR VALUES (?, ?, ?, 'OP', ?, ?)",
                orderNo, account, LocalDate.now(BERLIN).format(ORDER_DATE), formattedTotal, ref);
        int lineNo = 0;
        for (Line line : lines) {
            Database.update(connection, "INSERT INTO ORDLIN VALUES (?, ?, ?, ?, ?)",
                    orderNo, ++lineNo, line.sku, Integer.toString(line.quantity), line.price);
            allocate(connection, stock.get(line.sku), line.quantity);
        }
        return new Result(200, LegacyCodec.JSON,
                "{\"rc\":0,\"order_no\":" + LegacyCodec.json(orderNo)
                + ",\"total\":" + LegacyCodec.json(formattedTotal) + "}", true);
    }

    private static String nextOrderNumber(Connection connection) throws SQLException {
        List<Integer> latest = Database.query(connection, "SELECT MAX(CAST(SUBSTR(ORDNO, 4) AS INTEGER))"
                + " FROM ORDHDR WHERE ORDNO GLOB 'SO-[0-9][0-9][0-9][0-9][0-9][0-9]'",
                row -> row.getInt(1));
        return String.format(Locale.ROOT, "SO-%06d", latest.get(0) + 1);
    }

    private static void allocate(Connection connection, List<Balance> balances, int quantity) throws SQLException {
        int remaining = quantity;
        for (Balance balance : balances) {
            int take = (int) Math.min(remaining, Math.max(0, balance.free()));
            if (take != 0) {
                balance.allocated += take;
                remaining -= take;
                Database.update(connection, "UPDATE STOCKBAL SET ALLOCATED = ? WHERE rowid = ?",
                        Integer.toString(balance.allocated), balance.rowId);
            }
            if (remaining == 0) {
                return;
            }
        }
        throw new SQLException("Stock allocation did not cover the validated quantity");
    }

    private static String normalizeReference(String value) {
        if (value == null || value.trim().isEmpty()) {
            return null;
        }
        String trimmed = value.trim();
        int characters = trimmed.codePointCount(0, trimmed.length());
        return trimmed.substring(0, trimmed.offsetByCodePoints(0, Math.min(12, characters)));
    }

    static final class Result {
        final int status;
        final String contentType;
        final String body;
        final boolean booked;

        Result(int status, String contentType, String body, boolean booked) {
            this.status = status;
            this.contentType = contentType;
            this.body = body;
            this.booked = booked;
        }

        static Result reject(int code, String message) {
            return new Result(200, LegacyCodec.JSON,
                    "{\"rc\":" + code + ",\"msg\":" + LegacyCodec.json(message) + "}", false);
        }
    }

    private static final class Item {
        final String sku;
        final boolean discontinued;
        final String price;

        Item(ResultSet row) throws SQLException {
            sku = row.getString(1);
            discontinued = LegacyCodec.legacyTruthy(row.getString(2));
            price = row.getString(3);
        }
    }

    private static final class Line {
        final String sku;
        final int quantity;
        final String price;

        Line(String sku, int quantity, String price) {
            this.sku = sku;
            this.quantity = quantity;
            this.price = price;
        }
    }

    private static final class Balance {
        final long rowId;
        final int onHand;
        int allocated;

        Balance(ResultSet row) throws SQLException {
            rowId = row.getLong(1);
            onHand = Math.max(0, LegacyCodec.legacyInt(row.getString(2)));
            allocated = LegacyCodec.legacyInt(row.getString(3));
        }

        long free() {
            return (long) onHand - allocated;
        }
    }
}
