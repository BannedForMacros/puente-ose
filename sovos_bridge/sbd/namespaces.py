"""Namespaces del SBD de Sovos (tomados de los XSD oficiales, schemas/sovos/)."""

SBD = "http://www.unece.org/cefact/namespaces/StandardBusinessDocumentHeader"
SVS = "http://www.sovos.com/namespaces/sovosDocument"
SCI = "http://www.sovos.com/namespaces/sovosCanonicalInvoice"

INV = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CRN = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
DBN = "urn:oasis:names:specification:ubl:schema:xsd:DebitNote-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

# Raíz UBL -> (namespace, prefijo usado en la documentación de Sovos)
ROOTS = {
    "Invoice": (INV, "inv"),
    "CreditNote": (CRN, "crn"),
    "DebitNote": (DBN, "dbn"),
}
