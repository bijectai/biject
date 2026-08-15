"""OC3Client — spike client for OpenClinica 3.17 Community Edition (S4-D-12).

Auth is form-session (read) + WS-Security UsernameToken (write). Explicitly NOT
OAuth. Spike acceptance: one item value written programmatically and visible in
the OC UI; one open query read programmatically with its item context.

Why two completely different auth paths
=======================================
OpenClinica 3.x has NO REST API in the modern sense. What it has is:

* READ — the "session REST" ODM view. OC 3.x serves a Spring-MVC URL
  (``/rest/clinicaldata/xml/view/...``) that renders the study's clinical data
  as CDISC ODM 1.3 XML (with OpenClinica vendor extensions). It is guarded by
  the *same Spring Security form login* as the web UI: you must POST
  credentials to ``j_spring_security_check`` and carry the resulting
  ``JSESSIONID`` cookie on the export GET. There is no token endpoint.

* WRITE — the separately-deployed ``OpenClinica-ws`` SOAP web-services WAR.
  Its Data web service (namespace ``http://openclinica.org/ws/data/v1``)
  accepts an ODM 1.3 ``importData`` payload. Authentication is WS-Security
  UsernameToken in the SOAP header. **OC3 quirk, read carefully:** despite the
  wsse ``Type=...#PasswordText`` attribute, OpenClinica-ws does NOT want the
  clear-text password and does NOT implement the standard WSS PasswordDigest
  (nonce+created+password SHA1/base64) scheme either. It compares the supplied
  value against the SHA1 **hex digest** of the account password (that is how
  OC stores passwords). So the correct client behaviour — implemented in
  :func:`_ws_password` below — is::

      hashlib.sha1(password.encode("utf-8")).hexdigest()

  sent as the text of a ``<wsse:Password Type="...#PasswordText">`` element.
  Sending the raw password fails auth; sending a spec-compliant WSS digest
  fails auth. Yes, this is SHA1 of the password as a password-equivalent —
  acceptable for a self-hosted demo box, and it is simply what OC 3.17 does.

Also note: the WS user must be flagged as a "web services" user type in the
OC admin UI, and (for imports) needs data-entry rights on the study.

EOD Day-1 fallback plan (if this spike fails)
=============================================
# FALLBACK PLAN — evaluate at end of Day 1 if either acceptance leg fails:
#
# 1. OC4 cloud trial. OpenClinica 4 has a real REST API (OAuth2) and Enketo
#    forms. A trial tenant gets us reads + writes + queries with far less
#    protocol archaeology. Cost: study must be rebuilt in OC4's form model,
#    and it is a hosted trial (demo dependency on a third party).
# 2. Thin authenticated Postgres write service. OC3's schema is well known
#    (tables ``item_data``, ``discrepancy_note``, ``dn_item_data_map`` ...).
#    A ~100-line FastAPI sidecar on the same host doing parameterized
#    UPDATE/INSERT plus an audit row would emulate "write-back" for the demo.
#    Cost: bypasses OC's own audit trail / edit checks, so we must present it
#    honestly as a shim, not as an EDC integration.
# Decision rule: if login+export works but SOAP import does not, prefer (2)
# (keep the real read path); if OC3 itself is unstable, prefer (1).

Read path details
=================
Login flow (OC 3.x, Spring Security):

1. ``GET  {base}/pages/login/login``  — prime ``JSESSIONID``.
2. ``POST {base}/j_spring_security_check`` with form fields ``j_username`` /
   ``j_password`` (as the OC3 login form does).
3. Success → 302 to ``MainMenu``; failure → redirected back to the login page
   (``login_error`` in the URL / login form in the body). We detect failure by
   inspecting the final URL and body rather than the status code, because
   Spring answers 200 after following the redirect either way.

Export URL (OC 3.x "session REST" ODM view)::

    {base}/rest/clinicaldata/xml/view/{studyOID}/{subjectKey}/*/*
          ?includeDNs=y&includeAudits=y&showArchived=n

``subjectKey`` may be ``*`` for all subjects. ``includeDNs=y`` embeds
discrepancy notes (queries) as ``OpenClinica:DiscrepancyNotes`` extension
elements inside the ODM — that is where :meth:`OC3Client.list_open_queries`
reads them from.

Write path details
==================
:meth:`OC3Client.write_item_correction` builds a minimal ODM 1.3
``ClinicalData`` tree containing exactly one ``ItemData``, with:

* ``<OpenClinica:UpsertOn NotStarted="true" DataEntryStarted="true"
  DataEntryComplete="true"/>`` so the import updates the event/CRF regardless
  of its current workflow status, and
* ``TransactionType="Update"`` on ``ItemGroupData`` so an existing value is
  overwritten rather than rejected as a duplicate insert.

The ODM is embedded (as XML, not as an escaped string) inside a
``<v1:importRequest>`` SOAP body and POSTed with ``Content-Type: text/xml``
and an empty ``SOAPAction`` to ``{ws_base}/ws/dataImport/v1``. The response's
``<result>`` element is checked for ``Success``; anything else raises
:class:`OC3WriteError` carrying the server's ``<error>`` text(s).

Environment / construction
==========================
``OC3Client(base_url=..., username=..., password=...)`` or env vars
``OC_BASE_URL`` / ``OC_USER`` / ``OC_PASS`` (constructor args win). The SOAP
WAR usually lives next to the main app as ``{base_url}-ws`` (e.g.
``https://host/OpenClinica`` → ``https://host/OpenClinica-ws``); override with
``ws_base_url=`` or ``OC_WS_BASE_URL`` if deployed elsewhere. TLS verification
is ON by default; pass ``verify_tls=False`` only for a lab box with a
self-signed cert.

CLI (spike's manual acceptance run)
===================================
::

    python oc3_client.py list-queries S_BJTDEMO
    python oc3_client.py write I_LABS_CREAT 1.52 --subject SS_002 \\
        --study S_BJTDEMO --event SE_VISIT1 --form F_LABS --item-group IG_LABS
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as _xml_escape  # stdlib; used in messages only

import requests
from lxml import etree

# --------------------------------------------------------------------------- #
# XML namespaces                                                              #
# --------------------------------------------------------------------------- #

#: CDISC ODM 1.3 namespace (default namespace of the export document).
NS_ODM = "http://www.cdisc.org/ns/odm/v1.3"
#: OpenClinica ODM vendor-extension namespace (DiscrepancyNotes, UpsertOn, ...).
NS_OC = "http://www.openclinica.org/ns/odm_ext_v130/v3.1"
#: SOAP 1.1 envelope.
NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
#: OpenClinica-ws Data web service (importData operation).
NS_OCWS_DATA = "http://openclinica.org/ws/data/v1"
#: OASIS WS-Security secext (UsernameToken lives here).
NS_WSSE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
#: wsse Password @Type for a text password. OC3 *labels* the field PasswordText
#: but expects the SHA1 hex digest of the password as its content — see module
#: docstring and :func:`_ws_password`.
WSSE_PASSWORD_TEXT_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordText"
)

#: Prefix map for XPath over the ODM export.
_NSMAP = {"odm": NS_ODM, "oc": NS_OC}

#: Discrepancy-note statuses that count as "open" for the demo agent.
#: OC3 DN lifecycle: New -> Updated -> Resolution Proposed -> Closed
#: (plus "Not Applicable"). Only the first two are actionable queries.
OPEN_DN_STATUSES = frozenset({"New", "Updated"})


# --------------------------------------------------------------------------- #
# Typed exceptions                                                            #
# --------------------------------------------------------------------------- #


class OC3Error(RuntimeError):
    """Base class for every error this module raises deliberately."""


class OC3AuthError(OC3Error):
    """Form-session login was rejected (bad credentials / locked account)."""


class OC3ParseError(OC3Error):
    """The server answered, but not with the XML shape we expected."""


class OC3WriteError(OC3Error):
    """The SOAP importData call did not report ``<result>Success</result>``."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        #: Individual ``<error>`` texts extracted from the SOAP response.
        self.errors: list[str] = errors or []


# --------------------------------------------------------------------------- #
# Typed results                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OpenQuery:
    """One OPEN discrepancy note (query) with the item context it hangs off.

    ``item_oid`` may be ``""`` for the rare event-/form-level notes OC allows;
    item-level notes (the normal case for data queries) always carry it.
    """

    id: str  # DN OID (e.g. "DN_123") or a synthesized fallback key
    item_oid: str  # ItemDef OID the note is attached to, e.g. "I_LABS_CREAT"
    subject_key: str  # StudySubject key, e.g. "SS_002"
    event_oid: str  # StudyEventDef OID, e.g. "SE_VISIT1"
    form_oid: str  # FormDef OID, e.g. "F_LABS"
    description: str  # Human text of the query (latest child note wins)
    status: str  # "New" or "Updated" (filtered to OPEN_DN_STATUSES)


@dataclass(frozen=True)
class ItemValue:
    """One captured item value with the full OID context needed to write back."""

    item_oid: str
    value: str
    subject_key: str
    event_oid: str
    form_oid: str
    item_group_oid: str
    item_group_repeat_key: str = "1"


@dataclass
class _WriteTarget:
    """Internal bag of OIDs describing where one corrected value lands."""

    study_oid: str
    subject_key: str
    event_oid: str
    form_oid: str
    item_group_oid: str
    item_oid: str
    value: str
    item_group_repeat_key: str = "1"
    metadata_version_oid: str = "v1.0.0"
    extra_items: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _ws_password(password: str) -> str:
    """Return the credential string OpenClinica-ws expects in wsse:Password.

    OC3 stores account passwords as unsalted SHA1 hex digests and its
    WS-Security handler compares the UsernameToken password field *directly*
    against that stored digest. Therefore the client must send
    ``sha1(password).hexdigest()`` — NOT the clear password, and NOT the
    OASIS PasswordDigest (base64(sha1(nonce+created+password))) scheme.
    """
    return hashlib.sha1(password.encode("utf-8")).hexdigest()


def _first_text(el: etree._Element, xpath: str) -> str:
    """First non-empty text produced by ``xpath`` under ``el``, else ``""``."""
    for hit in el.xpath(xpath, namespaces=_NSMAP):
        text = hit if isinstance(hit, str) else (hit.text or "")
        text = text.strip()
        if text:
            return text
    return ""


def _looks_like_login_page(response: requests.Response) -> bool:
    """Heuristic: did OC bounce us back to the HTML login form?

    OC3 answers 200 for both a successful export and a redirect-to-login, so
    we sniff the payload instead of trusting status codes.
    """
    if "login" in response.url:
        return True
    head = response.content[:2048].lower()
    return b"j_username" in head or b"j_spring_security_check" in head


# --------------------------------------------------------------------------- #
# The client                                                                  #
# --------------------------------------------------------------------------- #


class OC3Client:
    """Read (form-session ODM export) + write (SOAP importData) for OC 3.17."""

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        ws_base_url: str | None = None,
        verify_tls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        # Constructor args win over environment; env is the operational default
        # so the CLI and seed.py need zero flags on a configured box.
        self.base_url = (base_url or os.environ.get("OC_BASE_URL", "")).rstrip("/")
        self.username = username or os.environ.get("OC_USER", "")
        self.password = password or os.environ.get("OC_PASS", "")
        if not (self.base_url and self.username and self.password):
            raise OC3Error(
                "Missing OC connection settings: provide base_url/username/"
                "password or set OC_BASE_URL, OC_USER, OC_PASS."
            )
        # The SOAP WAR is a sibling webapp; '<base>-ws' is the stock layout
        # (OpenClinica -> OpenClinica-ws). Override for non-standard deploys.
        self.ws_base_url = (
            ws_base_url or os.environ.get("OC_WS_BASE_URL") or f"{self.base_url}-ws"
        ).rstrip("/")
        self.verify_tls = verify_tls
        self.timeout = timeout
        # One requests.Session carries JSESSIONID across login + export GETs.
        self._session = requests.Session()
        self._session.verify = verify_tls
        self._logged_in = False

    # ------------------------------------------------------------------ READ

    def login(self) -> None:
        """Establish the OC3 web session (Spring Security form login).

        Idempotent; :meth:`fetch_odm` calls it lazily and re-calls it once if
        the session has expired mid-run.
        """
        # Step 1: prime the session cookie by loading the login page.
        self._session.get(f"{self.base_url}/pages/login/login", timeout=self.timeout)
        # Step 2: post the same form the browser posts.
        resp = self._session.post(
            f"{self.base_url}/j_spring_security_check",
            data={"j_username": self.username, "j_password": self.password},
            timeout=self.timeout,
            allow_redirects=True,  # follow through to MainMenu or back to login
        )
        # Step 3: success lands on MainMenu; failure re-renders the login form.
        if _looks_like_login_page(resp):
            raise OC3AuthError(
                f"OC3 form login rejected for user {self.username!r} at "
                f"{self.base_url} (redirected back to login page)."
            )
        self._logged_in = True

    def fetch_odm(
        self,
        study_oid: str,
        subject_key: str = "*",
        *,
        include_dns: bool = True,
        include_audits: bool = True,
    ) -> etree._Element:
        """GET the ODM export for ``study_oid`` and return the parsed root.

        ``subject_key='*'`` exports every subject; the trailing ``/*/*`` path
        segments wildcard the study event and form version (OC 3.x "session
        REST" ODM view URL shape).
        """
        if not self._logged_in:
            self.login()
        url = (
            f"{self.base_url}/rest/clinicaldata/xml/view/"
            f"{study_oid}/{subject_key}/*/*"
        )
        params = {
            "includeDNs": "y" if include_dns else "n",
            "includeAudits": "y" if include_audits else "n",
            "showArchived": "n",
        }
        resp = self._session.get(url, params=params, timeout=self.timeout)
        if _looks_like_login_page(resp):
            # Session expired (Tomcat timeout) — re-login once and retry.
            self.login()
            resp = self._session.get(url, params=params, timeout=self.timeout)
            if _looks_like_login_page(resp):
                raise OC3AuthError("ODM export still redirects to login after re-auth.")
        if resp.status_code != 200:
            raise OC3ParseError(
                f"ODM export returned HTTP {resp.status_code} for {url}"
            )
        try:
            root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError as exc:
            raise OC3ParseError(f"ODM export is not well-formed XML: {exc}") from exc
        if etree.QName(root).localname != "ODM":
            raise OC3ParseError(
                f"Expected <ODM> root, got <{etree.QName(root).localname}>."
            )
        return root

    def list_open_queries(self, study_oid: str) -> list[OpenQuery]:
        """Return every OPEN ("New"/"Updated") discrepancy note with context.

        Walks the ODM export's ClinicalData tree. Item-level notes hang off
        ``ItemData`` as ``<OpenClinica:DiscrepancyNotes>`` children (the
        normal case); we also sweep FormData/StudyEventData-level notes so
        nothing open is silently dropped.
        """
        root = self.fetch_odm(study_oid, "*", include_dns=True)
        queries: list[OpenQuery] = []
        # Every DiscrepancyNote element anywhere under ClinicalData.
        for dn in root.xpath(
            ".//odm:ClinicalData//oc:DiscrepancyNote", namespaces=_NSMAP
        ):
            status = dn.get("Status", "")
            if status not in OPEN_DN_STATUSES:
                continue  # Closed / Resolution Proposed / N-A: not actionable
            # Reconstruct context from the nearest ODM ancestors. Any level
            # can be absent for event-level notes, hence the "" defaults.
            ctx: dict[str, str] = {
                "item_oid": "",
                "subject_key": "",
                "event_oid": "",
                "form_oid": "",
            }
            for anc in dn.iterancestors():
                local = etree.QName(anc).localname
                if local == "ItemData" and not ctx["item_oid"]:
                    ctx["item_oid"] = anc.get("ItemOID", "")
                elif local == "FormData" and not ctx["form_oid"]:
                    ctx["form_oid"] = anc.get("FormOID", "")
                elif local == "StudyEventData" and not ctx["event_oid"]:
                    ctx["event_oid"] = anc.get("StudyEventOID", "")
                elif local == "SubjectData" and not ctx["subject_key"]:
                    ctx["subject_key"] = anc.get("SubjectKey", "")
            # Human text: prefer the newest child note's DetailedNote, then its
            # Description, then any attribute-style Name OC may have stamped.
            description = (
                _first_text(dn, "oc:ChildNote[last()]/oc:DetailedNote")
                or _first_text(dn, "oc:ChildNote[last()]/oc:Description")
                or _first_text(dn, "oc:ChildNote/oc:DetailedNote")
                or dn.get("Name", "")
            )
            dn_id = (
                dn.get("OID")
                or dn.get("ID")
                or (
                    # Synthesized stable-ish fallback if the export omits an OID.
                    f"DN@{ctx['subject_key']}/{ctx['item_oid']}/{status}"
                )
            )
            queries.append(
                OpenQuery(
                    id=dn_id,
                    item_oid=ctx["item_oid"],
                    subject_key=ctx["subject_key"],
                    event_oid=ctx["event_oid"],
                    form_oid=ctx["form_oid"],
                    description=description,
                    status=status,
                )
            )
        return queries

    def list_item_values(
        self, study_oid: str, subject_key: str = "*"
    ) -> list[ItemValue]:
        """All captured item values with full write-back context.

        This is what lets the demo agent read *sibling* source fields (nurse
        notes, BMI, lab-report transcriptions) to derive corrections instead
        of inventing them.
        """
        root = self.fetch_odm(study_oid, subject_key, include_dns=False)
        values: list[ItemValue] = []
        for item in root.xpath(
            ".//odm:ClinicalData/odm:SubjectData/odm:StudyEventData"
            "/odm:FormData/odm:ItemGroupData/odm:ItemData",
            namespaces=_NSMAP,
        ):
            group = item.getparent()
            form = group.getparent()
            event = form.getparent()
            subject = event.getparent()
            values.append(
                ItemValue(
                    item_oid=item.get("ItemOID", ""),
                    value=item.get("Value", ""),
                    subject_key=subject.get("SubjectKey", ""),
                    event_oid=event.get("StudyEventOID", ""),
                    form_oid=form.get("FormOID", ""),
                    item_group_oid=group.get("ItemGroupOID", ""),
                    item_group_repeat_key=group.get("ItemGroupRepeatKey", "1"),
                )
            )
        return values

    # ----------------------------------------------------------------- WRITE

    def write_item_correction(
        self,
        *,
        study_oid: str,
        subject_key: str,
        event_oid: str,
        form_oid: str,
        item_group_oid: str,
        item_oid: str,
        value: str,
        item_group_repeat_key: str = "1",
        metadata_version_oid: str = "v1.0.0",
    ) -> str:
        """Write one corrected item value via the SOAP Data WS.

        Builds a single-item ODM 1.3 payload with ``UpsertOn`` +
        ``TransactionType="Update"`` and submits it through ``importData``.
        Returns the server's success message text; raises :class:`OC3WriteError`
        on any non-Success result.
        """
        target = _WriteTarget(
            study_oid=study_oid,
            subject_key=subject_key,
            event_oid=event_oid,
            form_oid=form_oid,
            item_group_oid=item_group_oid,
            item_oid=item_oid,
            value=str(value),
            item_group_repeat_key=item_group_repeat_key,
            metadata_version_oid=metadata_version_oid,
        )
        odm = self._build_correction_odm(target)
        return self.import_data(odm)

    def import_data(self, odm: etree._Element | str | bytes) -> str:
        """POST an arbitrary ODM ClinicalData document to importData.

        Accepts a prebuilt ``<ODM>`` element (or raw XML) so callers like
        ``seed.py`` can push multi-item / multi-subject payloads through the
        exact same SOAP + WS-Security plumbing as single corrections.
        """
        if isinstance(odm, (str, bytes)):
            try:
                odm = etree.fromstring(
                    odm.encode("utf-8") if isinstance(odm, str) else odm
                )
            except etree.XMLSyntaxError as exc:
                raise OC3ParseError(f"import_data got malformed XML: {exc}") from exc
        envelope = self._build_soap_envelope(odm)
        return self._post_import(envelope)

    # ------------------------------------------------------- WRITE internals

    def _build_correction_odm(self, t: _WriteTarget) -> etree._Element:
        """Minimal ODM 1.3 tree: one subject, one event, one form, one item."""
        odm = etree.Element(
            f"{{{NS_ODM}}}ODM",
            nsmap={None: NS_ODM, "OpenClinica": NS_OC},
        )
        clinical = etree.SubElement(
            odm,
            f"{{{NS_ODM}}}ClinicalData",
            StudyOID=t.study_oid,
            MetaDataVersionOID=t.metadata_version_oid,
        )
        # UpsertOn (OpenClinica extension): allow the import to land whether
        # the target event/CRF is not started, in progress, or complete.
        etree.SubElement(
            clinical,
            f"{{{NS_OC}}}UpsertOn",
            NotStarted="true",
            DataEntryStarted="true",
            DataEntryComplete="true",
        )
        subject = etree.SubElement(
            clinical, f"{{{NS_ODM}}}SubjectData", SubjectKey=t.subject_key
        )
        event = etree.SubElement(
            subject, f"{{{NS_ODM}}}StudyEventData", StudyEventOID=t.event_oid
        )
        form = etree.SubElement(event, f"{{{NS_ODM}}}FormData", FormOID=t.form_oid)
        # TransactionType="Update": overwrite the existing value instead of
        # OC rejecting the row as an already-present insert.
        group = etree.SubElement(
            form,
            f"{{{NS_ODM}}}ItemGroupData",
            ItemGroupOID=t.item_group_oid,
            ItemGroupRepeatKey=t.item_group_repeat_key,
            TransactionType="Update",
        )
        etree.SubElement(
            group, f"{{{NS_ODM}}}ItemData", ItemOID=t.item_oid, Value=t.value
        )
        for extra_oid, extra_value in t.extra_items.items():
            etree.SubElement(
                group, f"{{{NS_ODM}}}ItemData", ItemOID=extra_oid, Value=extra_value
            )
        return odm

    def _build_soap_envelope(self, odm: etree._Element) -> bytes:
        """Wrap an ODM tree in a SOAP 1.1 envelope with the OC3 wsse header."""
        env = etree.Element(
            f"{{{NS_SOAP}}}Envelope",
            nsmap={"soapenv": NS_SOAP, "v1": NS_OCWS_DATA, "wsse": NS_WSSE},
        )
        header = etree.SubElement(env, f"{{{NS_SOAP}}}Header")
        security = etree.SubElement(
            header,
            f"{{{NS_WSSE}}}Security",
            {f"{{{NS_SOAP}}}mustUnderstand": "1"},
        )
        token = etree.SubElement(security, f"{{{NS_WSSE}}}UsernameToken")
        etree.SubElement(token, f"{{{NS_WSSE}}}Username").text = self.username
        password_el = etree.SubElement(
            token, f"{{{NS_WSSE}}}Password", Type=WSSE_PASSWORD_TEXT_TYPE
        )
        # THE OC3 QUIRK: SHA1 hex digest of the password inside a
        # PasswordText-typed field. See _ws_password's docstring.
        password_el.text = _ws_password(self.password)
        body = etree.SubElement(env, f"{{{NS_SOAP}}}Body")
        request = etree.SubElement(body, f"{{{NS_OCWS_DATA}}}importRequest")
        # The ODM rides inline (as XML, not an escaped string) inside
        # importRequest — that is the shape OpenClinica-ws expects.
        request.append(odm)
        return etree.tostring(
            env, xml_declaration=True, encoding="UTF-8", pretty_print=False
        )

    def _post_import(self, envelope: bytes) -> str:
        """POST the SOAP envelope; parse Success/error out of the response."""
        url = f"{self.ws_base_url}/ws/dataImport/v1"
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            # OC-ws routes on the body element, not SOAPAction, but the header
            # must be present (empty) or some SOAP stacks 500.
            "SOAPAction": '""',
        }
        resp = requests.post(
            url,
            data=envelope,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_tls,
        )
        try:
            root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError as exc:
            raise OC3WriteError(
                f"importData returned non-XML (HTTP {resp.status_code}): "
                f"{resp.text[:300]!r}"
            ) from exc
        # A SOAP Fault (e.g. wsse auth failure) has no <result> at all.
        fault = root.find(f".//{{{NS_SOAP}}}Fault")
        if fault is not None:
            fault_text = "".join(fault.itertext()).strip()
            raise OC3WriteError(
                f"SOAP Fault from importData (check WS user type and the SHA1 "
                f"password digest): {_xml_escape(fault_text[:500])}"
            )
        # Result / error elements live in the data/v1 response namespace, but
        # be namespace-tolerant: match on localname.
        results = [
            el.text.strip()
            for el in root.iter()
            if etree.QName(el).localname == "result" and el.text
        ]
        errors = [
            "".join(el.itertext()).strip()
            for el in root.iter()
            if etree.QName(el).localname == "error"
        ]
        if results and results[0] == "Success":
            return results[0]
        raise OC3WriteError(
            f"importData result={results[0] if results else '<missing>'} "
            f"(HTTP {resp.status_code}); errors: {errors or ['<none reported>']}",
            errors=errors,
        )


# --------------------------------------------------------------------------- #
# CLI — the spike's manual acceptance harness                                 #
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oc3_client.py",
        description=(
            "OC3 spike client. Reads use the form-session ODM export; writes "
            "use the OpenClinica-ws SOAP Data service. Connection settings "
            "come from OC_BASE_URL / OC_USER / OC_PASS (and optionally "
            "OC_WS_BASE_URL) unless overridden with flags."
        ),
    )
    parser.add_argument("--base-url", help="OC base URL (default: $OC_BASE_URL)")
    parser.add_argument("--user", help="OC username (default: $OC_USER)")
    parser.add_argument("--password", help="OC password (default: $OC_PASS)")
    parser.add_argument(
        "--ws-base-url",
        help="OpenClinica-ws base URL (default: $OC_WS_BASE_URL or '<base>-ws')",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification (lab boxes with self-signed certs only)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser(
        "list-queries",
        help="List OPEN discrepancy notes with item context "
        "(e.g. list-queries S_BJTDEMO)",
    )
    p_list.add_argument("study_oid", help="Study OID, e.g. S_BJTDEMO")

    p_write = sub.add_parser(
        "write",
        help="Write one corrected item value via SOAP importData "
        "(e.g. write I_LABS_CREAT 1.52 --subject SS_002 --study S_BJTDEMO "
        "--event SE_VISIT1 --form F_LABS --item-group IG_LABS)",
    )
    p_write.add_argument("item_oid", help="Item OID, e.g. I_VITALS_SBP")
    p_write.add_argument("value", help="Corrected value, e.g. 120")
    p_write.add_argument("--subject", required=True, help="SubjectKey, e.g. SS_001")
    p_write.add_argument("--study", required=True, help="Study OID, e.g. S_BJTDEMO")
    p_write.add_argument("--event", required=True, help="Event OID, e.g. SE_VISIT1")
    p_write.add_argument("--form", required=True, help="Form OID, e.g. F_VITALS")
    p_write.add_argument(
        "--item-group", required=True, help="ItemGroup OID, e.g. IG_VITALS"
    )
    p_write.add_argument(
        "--repeat-key", default="1", help="ItemGroupRepeatKey (default 1)"
    )
    p_write.add_argument(
        "--metadata-version",
        default="v1.0.0",
        help="MetaDataVersionOID of the target study (default v1.0.0)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        client = OC3Client(
            base_url=args.base_url,
            username=args.user,
            password=args.password,
            ws_base_url=args.ws_base_url,
            verify_tls=not args.insecure,
        )
        if args.command == "list-queries":
            queries = client.list_open_queries(args.study_oid)
            if not queries:
                print(f"No OPEN discrepancy notes in {args.study_oid}.")
                return 0
            print(f"{len(queries)} OPEN discrepancy note(s) in {args.study_oid}:\n")
            for q in queries:
                print(
                    f"  [{q.status:>7}] {q.id}  subject={q.subject_key} "
                    f"event={q.event_oid} form={q.form_oid} item={q.item_oid}"
                )
                print(f"           {q.description or '(no note text)'}")
            return 0
        if args.command == "write":
            message = client.write_item_correction(
                study_oid=args.study,
                subject_key=args.subject,
                event_oid=args.event,
                form_oid=args.form,
                item_group_oid=args.item_group,
                item_oid=args.item_oid,
                value=args.value,
                item_group_repeat_key=args.repeat_key,
                metadata_version_oid=args.metadata_version,
            )
            print(
                f"importData: {message} — wrote {args.item_oid}={args.value!r} "
                f"for subject {args.subject}. Verify in the OC UI "
                f"(spike acceptance) and via 'list-queries {args.study}'."
            )
            return 0
        raise AssertionError(f"unhandled command {args.command!r}")
    except OC3Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if isinstance(exc, OC3WriteError) and exc.errors:
            for err in exc.errors:
                print(f"  server error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
