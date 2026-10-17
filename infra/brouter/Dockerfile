# BRouter (https://github.com/abrensch/brouter): the cycling router the route editor calls.
# Built from the official release zip; routing tiles come from the shared /segments4 volume (see docker-compose.yml).
FROM eclipse-temurin:17-jre-jammy

ARG BROUTER_VERSION=1.7.10
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /tmp/brouter.zip "https://github.com/abrensch/brouter/releases/download/v${BROUTER_VERSION}/brouter-${BROUTER_VERSION}.zip" \
 && mkdir -p /tmp/b /app /customprofiles /segments4 \
 && unzip -q /tmp/brouter.zip -d /tmp/b \
 && cp "$(find /tmp/b -name 'brouter*.jar' | head -n 1)" /app/brouter.jar \
 && cp -r "$(find /tmp/b -type d -name profiles2 | head -n 1)" /profiles2 \
 && rm -rf /tmp/b /tmp/brouter.zip

EXPOSE 17777
# <segments> <profiles> <custom profiles> <port> <threads> <bind address>. A request that runs over 60 s is cut off.
CMD ["java", "-Xmx1g", "-Xms256m", "-DmaxRunningTime=60", "-DuseRFCMimeType=false", "-cp", "/app/brouter.jar", \
     "btools.server.RouteServer", "/segments4", "/profiles2", "/customprofiles", "17777", "4", "0.0.0.0"]
