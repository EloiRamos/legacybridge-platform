FROM maven:3.9.16-eclipse-temurin-8-noble AS build
WORKDIR /build
COPY pom.xml .
COPY src ./src
RUN java -version && mvn -v
RUN --mount=type=cache,target=/root/.m2 mvn -B -ntp verify

FROM eclipse-temurin:8u504-b01-jre-noble
RUN groupadd --system halden && useradd --system --gid halden --home-dir /opt/halden halden \
 && mkdir -p /var/lib/halden && chown halden:halden /var/lib/halden
COPY --from=build /build/target/halden-erp.jar /opt/halden/halden-erp.jar
COPY --from=build /build/target/lib /opt/halden/lib
USER halden
ENV HALDEN_PORT=8090 HALDEN_DB_PATH=/var/lib/halden/halden.db
EXPOSE 8090
ENTRYPOINT ["java", "-Xmx256m", "-jar", "/opt/halden/halden-erp.jar"]
