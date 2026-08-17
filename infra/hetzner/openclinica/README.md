# OpenClinica 3.17 CE — self-hosted EDC for the biject sprint demo (S4-D-11)

One Tomcat (Java 8) container serving **both** OpenClinica artifacts, plus a
Postgres 9.5 container, all confined to the internal Docker network
`biject-edc-internal`:

| Context           | What                                | Reachable from                              |
|-------------------|-------------------------------------|---------------------------------------------|
| `/OpenClinica`    | Web UI                              | Internet via Traefik (**basic-auth**) only  |
| `/OpenClinica-ws` | SOAP web services (write-back API)  | **Rust verify proxy only** (internal net)   |
| `oc-db:5432`      | Postgres 9.5                        | `openclinica` container only; **no host port** |

> **API reality check — read before writing client code:**
> **OC3 CE: SOAP + session API only. No OC4 REST. No OAuth — WS-Security
> UsernameToken (password digest) and form-session auth only.**

---

## 0. Prerequisites

- The edge stack (`../docker-compose.yml`) deployed first — it creates the
  `biject-edge` / `biject-edc-internal` networks this project attaches to
  (declared `external: true` here).
- `../.env` populated from `../.env.example` (one shared env file for both
  compose projects).
- The two release WARs placed in `./dist/` (not committed, ~100 MB):

  ```
  dist/OpenClinica.war       # from the OpenClinica 3.17(.x) CE release zip
  dist/OpenClinica-ws.war    # from the OpenClinica-ws 3.17(.x) release zip  <-- SEPARATE artifact
  ```

  `OpenClinica-ws` is **not optional**: it is a separate download from the
  main webapp, and the write-back spike has nothing to hit without it. Get
  both from the OpenClinica community release archive (GitHub
  `OpenClinica/OpenClinica` releases / community download mirror); match the
  ws version to the web version. If a release zip contains a differently
  versioned WAR filename, rename it to exactly the names above (the Dockerfile
  and the entrypoint's context names depend on them).

## 1. Startup order

```bash
# 1. Edge stack (Traefik + networks) — from infra/hetzner/
cd infra/hetzner
cp .env.example .env && chmod 600 .env   # fill in real values first!
docker compose up -d

# 2. OpenClinica stack — from infra/hetzner/openclinica/
cd openclinica
docker compose --env-file ../.env -f docker-compose.openclinica.yml up -d --build
```

First boot is **slow (several minutes)**: Postgres init runs `init-db.sh`
(creates role `clinica` + db `openclinica`), then OpenClinica installs its own
schema on first webapp start. Watch it:

```bash
docker compose --env-file ../.env -f docker-compose.openclinica.yml logs -f openclinica
```

Then browse `https://$DEMO_DOMAIN/OpenClinica` — first the Traefik basic-auth
prompt (user from `OC_UI_BASICAUTH_USERS`), then the OC login page.

Persistence: study data lives in named volumes `biject-oc-pgdata` and
`biject-oc-data` and survives `down`/redeploys. `docker volume rm` on either
is data loss.

## 2. First login, admin account, demo study

1. Log in as the built-in admin: username **`root`**, initial password
   **`12345678`** — OC forces a password change on first login. Do this
   immediately.
2. Create the demo study: **Tasks → Build Study** → *Create Study* (name,
   unique protocol ID) → then within Build Study: define a CRF (upload the
   Excel CRF template), create an event definition, attach the CRF to it.
3. Create sites/subjects as needed: **Tasks → Study Setup → Sites**, and
   **Tasks → Subject Matrix → Add Subject**.
4. Create the **proxy's service account** (the identity the Rust verify proxy
   writes as — credentials go in `OC_WS_USER` / `OC_WS_PASSWORD` in `../.env`):
   **Tasks → Administration → Users → Create User** — user type
   *technician/data entry* is enough; then **assign the user to the study**
   with a role that can enter data (Data Entry Person / Data Manager). A user
   not assigned to the study gets SOAP authorization failures even with a
   correct password.

## 3. SOAP web services (what the proxy talks to)

Base URL **inside `biject-edc-internal`** (the only place it resolves):

```
http://openclinica:8080/OpenClinica-ws
```

WSDLs (replace host as appropriate; from the proxy use the internal URL):

```
.../OpenClinica-ws/ws/study/v1/studyWsdl.wsdl              # list studies/metadata
.../OpenClinica-ws/ws/studySubject/v1/studySubjectWsdl.wsdl # create/list subjects
.../OpenClinica-ws/ws/event/v1/eventWsdl.wsdl              # schedule events
.../OpenClinica-ws/ws/data/v1/dataWsdl.wsdl                # <-- import data (ODM) = the write-back call
```

Externally that would be `https://$DEMO_DOMAIN/OpenClinica-ws/ws/data/v1/dataWsdl.wsdl`
— but it is **deliberately unreachable from the internet**: the Traefik router
rule excludes `/OpenClinica-ws` (`!PathPrefix`), so the only route to the SOAP
API is `verify proxy → openclinica:8080` over the internal network. That is
the enforcement boundary; do not "fix" a 404 on that URL by widening the
router rule.

**Auth** (per the note at the top — no OAuth, no REST):

- SOAP: WS-Security **UsernameToken** in the SOAP header. OC's quirk: the
  `<wsse:Password>` value is the **SHA-1 hash (hex) of the user's password**,
  not the cleartext. Example header shape:

  ```xml
  <soapenv:Header>
    <wsse:Security soapenv:mustUnderstand="1"
        xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>ws-proxy</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">
          da39a3ee5e6b4b0d3255bfef95601890afd80709 <!-- sha1(password), hex -->
        </wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </soapenv:Header>
  ```

- Web UI: ordinary form login + JSESSIONID session cookie ("session API") —
  usable for scraping/screenshots, not a stable API.

Quick smoke test **from inside the enclave** (SOAP is invisible from outside):

```bash
docker run --rm --network biject-edc-internal curlimages/curl -sf \
  http://openclinica:8080/OpenClinica-ws/ws/data/v1/dataWsdl.wsdl | head -5
```

## 4. Operational notes

- **Postgres 9.5 is intentional.** OC 3.17's schema/migrations are validated
  against the 8.4–9.5 line; do not bump the image.
- **No host ports anywhere in this file** — acceptance criterion. If you need
  psql access: `docker compose --env-file ../.env -f docker-compose.openclinica.yml exec oc-db psql -U clinica openclinica`.
- **Config changes** (`../.env` → DB password, `DEMO_DOMAIN`, mail): rendered
  into both webapps' `datainfo.properties` by the entrypoint on every
  container start — `up -d` after editing is enough, no rebuild needed.
  Changing the WARs in `dist/` requires `up -d --build`.
- **Re-initializing the DB** (destroys all study data): `down`, then
  `docker volume rm biject-oc-pgdata`, then `up -d` (init-db.sh re-runs, OC
  re-installs its schema).
- **Mail doesn't leave the enclave** (internal network has no egress); create
  users with the admin UI and set passwords there rather than relying on
  notification emails.
