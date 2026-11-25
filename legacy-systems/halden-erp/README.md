# HALDEN ERP 3.1

HALDEN ERP 3.1 is a fictional legacy ERP written from scratch for this
portfolio project. The simulator, contracts, and fictional seed data
originated with this project.

Embedded Tomcat serves the HALDEN HTTP interface on Java 8. The
application uses JDBC to store customers, items, stock and orders in a
single SQLite database.

## Build and run

Run these commands from the repository root with Docker Desktop or Docker
Engine and BuildKit.

```bash
docker build --no-cache --progress=plain -t legacybridge/halden-erp:3.1 legacy-erp
docker run -d --name halden-erp -p 127.0.0.1:8090:8090 legacybridge/halden-erp:3.1
curl -fsS http://127.0.0.1:8090/halden/ping
```

The build runs the Java 8 JUnit contract suite with Maven. After the
simulator responds, start the ordinary-path instance and run both sets
of HTTP checks.

```bash
docker run -d --name halden-erp-ordinary -p 127.0.0.1:8091:8090 -e HALDEN_SIM_CONTROL=false legacybridge/halden-erp:3.1
curl -fsS http://127.0.0.1:8091/halden/ping
bash legacy-erp/acceptance/http_acceptance.sh http://127.0.0.1:8090 http://127.0.0.1:8091 halden-http-observations.md
docker rm -f halden-erp halden-erp-ordinary
```

The HTTP suite resets and books orders on the sim-on instance and
books a seed-data order on the ordinary instance. Use disposable
containers. Published ports bind to loopback. Local users and
processes on the host can reach them.

## Configuration

| Environment variable | Default |
|---|---|
| `HALDEN_PORT` | `8090` |
| `HALDEN_DB_PATH` | `/var/lib/halden/halden.db` |
| `HALDEN_LOGIN` | `svc_bridge` (synthetic demo login) |
| `HALDEN_SECRET` | `halden-demo-secret` (synthetic demo credential) |
| `HALDEN_TICKET_TTL_SEC` | `900` |
| `HALDEN_RATE_LIMIT` | `30` |
| `HALDEN_RATE_WINDOW_SEC` | `10` |
| `HALDEN_RESET_ON_START` | `true` |
| `HALDEN_SIM_CONTROL` | `true` |

The simulation controls are for local demos and tests. Set
`HALDEN_SIM_CONTROL=false` to expose the ordinary product interface.
The [HALDEN API reference](../docs/LEGACY_API.md) accompanies the
integration.
