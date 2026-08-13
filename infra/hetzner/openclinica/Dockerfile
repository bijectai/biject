# =============================================================================
# S4-D-11 — OpenClinica 3.17 CE image: one Tomcat, BOTH WARs (web + ws)
# =============================================================================
# Strategy (per ticket): do not fight Tomcat/Java versions — deploy the OC
# 3.17 WARs into a base image whose bundled Tomcat/JRE match what OC 3.x was
# built for. The official `tomcat:8.5-jre8` image bakes in Tomcat 8.5 + Java 8
# (OpenJDK), which is the validated pairing for the OC 3.15+ line. (The
# community `piegsaj/openclinica` images take the same approach but top out at
# older OC versions and do not bundle OpenClinica-ws — which the write-back
# spike needs — hence this small Dockerfile instead.)
#
# BEFORE BUILDING: place the two release artifacts in ./dist/ :
#     dist/OpenClinica.war        (from the OpenClinica-3.17(.x) CE release zip)
#     dist/OpenClinica-ws.war     (from the OpenClinica-ws-3.17(.x) release zip
#                                  — a SEPARATE download; see README.md)
# The WARs are ~100 MB and are NOT committed to the repo.
#
# Config: OC 3.x reads WEB-INF/classes/datainfo.properties from inside the
# deployed webapp. We therefore explode the WARs at build time and let
# docker-entrypoint.sh render datainfo.properties (from the committed
# template) into BOTH webapps at container start, from environment variables.
# =============================================================================

# Debian-based; Tomcat 8.5 + OpenJDK JRE 8 baked in. Pinned tag: this image
# line is frozen (EOL) which is exactly what we want for reproducibility.
FROM tomcat:8.5-jre8

# unzip        — explode the WARs (the JRE image ships no `jar` tool)
# gettext-base — provides `envsubst` for the entrypoint's template rendering
RUN apt-get update \
 && apt-get install -y --no-install-recommends unzip gettext-base \
 && rm -rf /var/lib/apt/lists/*

# Drop Tomcat's default webapps (docs/examples/manager) — attack surface with
# zero demo value.
RUN rm -rf /usr/local/tomcat/webapps/*

# Deploy both artifacts EXPLODED so the entrypoint can drop the rendered
# datainfo.properties into WEB-INF/classes before Tomcat starts (deploying the
# .war files directly would race the config injection against auto-deploy).
COPY dist/OpenClinica.war /tmp/OpenClinica.war
COPY dist/OpenClinica-ws.war /tmp/OpenClinica-ws.war
RUN mkdir -p /usr/local/tomcat/webapps/OpenClinica /usr/local/tomcat/webapps/OpenClinica-ws \
 && unzip -q /tmp/OpenClinica.war    -d /usr/local/tomcat/webapps/OpenClinica \
 && unzip -q /tmp/OpenClinica-ws.war -d /usr/local/tomcat/webapps/OpenClinica-ws \
 && rm -f /tmp/OpenClinica.war /tmp/OpenClinica-ws.war

# Committed config template + entrypoint that renders it from env at start.
COPY datainfo.properties   /opt/oc/datainfo.properties.template
COPY docker-entrypoint.sh  /opt/oc/entrypoint.sh
RUN chmod +x /opt/oc/entrypoint.sh \
 # OC data directory (filePath) — a named volume is mounted here by compose.
 && mkdir -p /usr/local/tomcat/openclinica.data

# Overridden by compose; sane default if run bare.
ENV JAVA_OPTS="-Xms512m -Xmx1408m -XX:+UseG1GC -Djava.awt.headless=true"

# Internal only — compose publishes NO host port for this container; Tomcat is
# reachable solely over biject-edc-internal (proxy + Traefik).
EXPOSE 8080

ENTRYPOINT ["/opt/oc/entrypoint.sh"]
CMD ["catalina.sh", "run"]
