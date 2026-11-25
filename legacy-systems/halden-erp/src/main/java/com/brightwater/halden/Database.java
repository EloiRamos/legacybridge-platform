package com.brightwater.halden;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;

final class Database {
    static final Object WRITE_LOCK = new Object();
    private static final String[] TABLES = {"ORDLIN", "ORDHDR", "STOCKBAL", "ITEMMAST", "CUSTMAST"};
    private final String url;

    Database(Path dbPath) {
        url = "jdbc:sqlite:" + dbPath;
    }

    Connection open() throws SQLException {
        Connection connection = DriverManager.getConnection(url);
        try (Statement statement = connection.createStatement()) {
            statement.execute("PRAGMA busy_timeout=5000");
        } catch (SQLException failure) {
            try {
                connection.close();
            } catch (SQLException closeFailure) {
                failure.addSuppressed(closeFailure);
            }
            throw failure;
        }
        return connection;
    }

    void initialize(boolean resetOnStart) throws SQLException, IOException {
        synchronized (WRITE_LOCK) {
            boolean hasCustomers;
            try (Connection connection = open(); Statement statement = connection.createStatement()) {
                statement.execute("PRAGMA journal_mode=WAL");
                try (ResultSet rows = statement.executeQuery(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='CUSTMAST'")) {
                    hasCustomers = rows.next();
                }
            }
            if (resetOnStart || !hasCustomers) {
                reset();
            }
        }
    }

    void reset() throws SQLException, IOException {
        synchronized (WRITE_LOCK) {
            try (Connection connection = open(); Statement statement = connection.createStatement()) {
                connection.setAutoCommit(false);
                try {
                    for (String table : TABLES) {
                        statement.executeUpdate("DROP TABLE IF EXISTS " + table);
                    }
                    runScript(statement, "/db/schema.sql");
                    runScript(statement, "/db/seed.sql");
                    connection.commit();
                } catch (SQLException | IOException | RuntimeException failure) {
                    rollback(connection, failure);
                    throw failure;
                }
            }
        }
    }

    static void rollback(Connection connection, Throwable failure) {
        try {
            connection.rollback();
        } catch (SQLException rollbackFailure) {
            failure.addSuppressed(rollbackFailure);
        }
    }

    private static void runScript(Statement statement, String resource) throws IOException, SQLException {
        InputStream stream = Database.class.getResourceAsStream(resource);
        if (stream == null) {
            throw new IOException("Missing SQL resource " + resource);
        }
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            StringBuilder sql = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                String trimmed = line.trim();
                if (trimmed.isEmpty() || trimmed.startsWith("--")) {
                    continue;
                }
                sql.append(line).append('\n');
                if (trimmed.endsWith(";")) {
                    statement.executeUpdate(sql.toString());
                    sql.setLength(0);
                }
            }
            if (sql.length() != 0) {
                throw new IOException("Unterminated statement in " + resource);
            }
        }
    }

    interface RowMapper<T> {
        T map(ResultSet row) throws SQLException;
    }

    static <T> List<T> query(Connection connection, String sql, RowMapper<T> mapper, Object... parameters)
            throws SQLException {
        try (PreparedStatement statement = prepare(connection, sql, parameters);
             ResultSet rows = statement.executeQuery()) {
            List<T> result = new ArrayList<>();
            while (rows.next()) {
                result.add(mapper.map(rows));
            }
            return result;
        }
    }

    static void update(Connection connection, String sql, Object... parameters) throws SQLException {
        try (PreparedStatement statement = prepare(connection, sql, parameters)) {
            statement.executeUpdate();
        }
    }

    private static PreparedStatement prepare(Connection connection, String sql, Object[] parameters)
            throws SQLException {
        PreparedStatement statement = connection.prepareStatement(sql);
        try {
            for (int i = 0; i < parameters.length; i++) {
                statement.setObject(i + 1, parameters[i]);
            }
            return statement;
        } catch (SQLException failure) {
            try {
                statement.close();
            } catch (SQLException closeFailure) {
                failure.addSuppressed(closeFailure);
            }
            throw failure;
        }
    }
}
